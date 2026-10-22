"""Audio chunking, Gemini thematic segmentation, merge and repair."""
import base64
import concurrent.futures
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from k2y import config

_sleep = time.sleep

FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
]

CHUNK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary_en": {"type": "STRING"},
        "activity": {"type": "STRING"},
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "at": {"type": "STRING"},
                    "topic": {"type": "STRING"},
                },
                "required": ["at", "topic"],
            },
        },
    },
    "required": ["summary_en", "activity", "segments"],
}

CHUNK_PROMPT = """Audio: a {mins}-minute piece of a Kick livestream by a Tunisian streamer speaking Tunisian darija
(mixed with French and English). Describe it:
- summary_en: 2-3 sentences, what is said and done.
- activity: one of intro, just chatting, coding/building, reacting to a video, gaming, giveaway, break, outro.
- segments: the topics in order. "at" = MM:SS from the start of THIS audio piece (first one at 00:00);
  add a new segment only when the subject really changes. "topic" = 3-8 words in English."""

MERGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "parts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start_s": {"type": "INTEGER"},
                    "title": {"type": "STRING"},
                    "description": {"type": "STRING"},
                    "summary_en": {"type": "STRING"},
                    "key_moments": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "at_s": {"type": "INTEGER"},
                                "what": {"type": "STRING"},
                            },
                            "required": ["at_s", "what"],
                        },
                    },
                },
                "required": ["start_s", "title", "description", "summary_en", "key_moments"],
            },
        },
    },
    "required": ["parts"],
}

MERGE_PROMPT = """Below is a timeline of a {dur} livestream by a Tunisian streamer (Tunisian darija),
described in chunks with topic changes at absolute times (seconds).
Group it into thematic parts, the way an editor would cut it into separate YouTube videos.
Rules:
- Cut where the subject changes, not at chunk edges; chunk boundaries are arbitrary.
- Parts must be 15 to 60 minutes long.
- Titles must be short, punchy, in French or Latin-script darija like "Stream khedma fel gueyla".
- Descriptions must be 1-2 sentences in French.
- Every start_s and key_moments at_s must be timeline values from the at_s values listed below. First part starts at 0.
- key_moments: 2-3 key moments per part, with at_s from the timeline and "what" in French.
Timeline:
{timeline}"""


class DailyQuota(Exception):
    """Raised when Gemini returns 429 and error details contain a quotaId with 'PerDay'."""


class SegmentError(Exception):
    """Raised on segmentation or pipeline errors."""


def _get_api_key() -> str:
    try:
        return config.read_secret("gemini.key")
    except SystemExit:
        return ""


def _is_daily_quota_error(body: bytes | str) -> bool:
    try:
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = str(body)
        data = json.loads(body_str)
        details = data.get("error", {}).get("details", [])
        for item in details:
            if isinstance(item, dict):
                for v in item.get("violations", []):
                    if isinstance(v, dict) and "PerDay" in v.get("quotaId", ""):
                        return True
                if "PerDay" in item.get("quotaId", ""):
                    return True
        return "PerDay" in body_str
    except Exception:
        if isinstance(body, bytes):
            return b"PerDay" in body
        return "PerDay" in str(body)


def _parse_time_to_seconds(val: str | int | float) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    val = str(val).strip()
    parts = val.split(":")
    try:
        if len(parts) == 1:
            return int(float(parts[0]))
        elif len(parts) == 2:
            return int(float(parts[0])) * 60 + int(float(parts[1]))
        elif len(parts) == 3:
            return int(float(parts[0])) * 3600 + int(float(parts[1])) * 60 + int(float(parts[2]))
    except (ValueError, TypeError):
        pass
    return 0


def _format_time(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _snap(val: int, allowed: list[int]) -> int:
    if not allowed:
        return val
    return min(allowed, key=lambda a: abs(a - val))


def generate(parts: list, schema: dict, models: list[str]) -> dict:
    if not models:
        raise SegmentError("No models provided for generate")

    body_bytes = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }).encode("utf-8")

    api_key = _get_api_key()
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    per_day_models: set[str] = set()
    skipped_models: set[str] = set()
    last_error: Exception | None = None

    for attempt in range(8):
        candidates = [m for m in models if m not in skipped_models and m not in per_day_models]
        if not candidates:
            if set(models) <= per_day_models:
                raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")
            raise SegmentError("All candidate models exhausted or unavailable")

        model = candidates[attempt % len(candidates)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
            text = resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            return json.loads(text.strip())
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 404:
                skipped_models.add(model)
                continue
            elif e.code == 429:
                err_body = e.read()
                if _is_daily_quota_error(err_body):
                    per_day_models.add(model)
                    if set(models) <= per_day_models:
                        raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")
                    if attempt < 7:
                        _sleep(min(5 * (2 ** attempt), 60))
                    continue
                else:
                    if attempt < 7:
                        _sleep(min(5 * (2 ** attempt), 60))
                    continue
            elif e.code in (500, 503):
                if attempt < 7:
                    _sleep(min(5 * (2 ** attempt), 60))
                continue
            else:
                raise SegmentError(f"Gemini API HTTP error {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            last_error = e
            if attempt < 7:
                _sleep(min(5 * (2 ** attempt), 60))
            continue
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise SegmentError(f"Malformed Gemini response: {e}")

    if set(models) <= per_day_models:
        raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")
    raise SegmentError(f"Gemini generation failed after 8 attempts: {last_error}")


def chunk_audio(src: Path, workdir: Path, chunk_s: int = 600) -> list[Path]:
    src = Path(src)
    workdir = Path(workdir)
    chunks_dir = workdir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    for old_chunk in chunks_dir.glob("c*.ogg"):
        try:
            old_chunk.unlink()
        except OSError:
            pass

    audio_file = workdir / "audio.ogg"
    extract_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "libopus",
        "-b:a", "24k",
        str(audio_file),
    ]
    try:
        subprocess.run(extract_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise SegmentError(f"ffmpeg audio extraction failed (code {e.returncode}): {e.stderr}")

    segment_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(audio_file),
        "-f", "segment",
        "-segment_time", str(chunk_s),
        "-c", "copy",
        str(chunks_dir / "c%03d.ogg"),
    ]
    try:
        subprocess.run(segment_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise SegmentError(f"ffmpeg audio segmenting failed (code {e.returncode}): {e.stderr}")
    finally:
        if audio_file.exists():
            try:
                audio_file.unlink()
            except OSError:
                pass

    return sorted(chunks_dir.glob("c*.ogg"))


def describe_chunk(i: int, path: Path, chunk_s: int, models: list[str]) -> dict:
    path = Path(path)
    audio_data = base64.b64encode(path.read_bytes()).decode("ascii")
    mins = max(1, chunk_s // 60)
    prompt_text = CHUNK_PROMPT.format(mins=mins)
    parts = [
        {"inline_data": {"mime_type": "audio/ogg", "data": audio_data}},
        {"text": prompt_text},
    ]

    data = generate(parts, CHUNK_SCHEMA, models)

    summary_en = data.get("summary_en", "")
    activity = data.get("activity", "")
    raw_segments = data.get("segments", [])

    chunk_start = i * chunk_s
    segs = []
    for s in raw_segments:
        at_val = s.get("at", 0)
        topic = s.get("topic", "")
        rel_s = _parse_time_to_seconds(at_val)
        clamped_rel_s = max(0, min(rel_s, max(0, chunk_s - 1)))
        at_s = chunk_start + clamped_rel_s
        segs.append({"at_s": at_s, "topic": topic})

    if not any(s["at_s"] == chunk_start for s in segs):
        first_topic = segs[0]["topic"] if segs else (summary_en or activity or "Start")
        segs.insert(0, {"at_s": chunk_start, "topic": first_topic})

    segs.sort(key=lambda s: s["at_s"])

    return {
        "start_s": chunk_start,
        "summary_en": summary_en,
        "activity": activity,
        "segments": segs,
    }


def merge(chunks: list[dict], duration_s: int, models: list[str]) -> list[dict]:
    timeline_lines = []
    for c in chunks:
        start_s = c.get("start_s", 0)
        activity = c.get("activity", "")
        summary_en = c.get("summary_en", "")
        timeline_lines.append(f"[{_format_time(start_s)}] ({activity}) {summary_en}")
        for s in c.get("segments", []):
            at_s = s.get("at_s", 0)
            topic = s.get("topic", "")
            timeline_lines.append(f"   at_s={at_s} ({_format_time(at_s)}): {topic}")
    timeline = "\n".join(timeline_lines)

    prompt = MERGE_PROMPT.format(dur=_format_time(duration_s), timeline=timeline)
    result = generate([{"text": prompt}], MERGE_SCHEMA, models)
    if isinstance(result, dict) and "parts" in result:
        return result["parts"]
    elif isinstance(result, list):
        return result
    raise SegmentError(f"Unexpected merge response format: {type(result)}")


def repair(parts: list[dict], duration_s: int, allowed_s: list[int], min_part_s: int) -> list[dict]:
    if duration_s < 20 * 60:
        first_title = ""
        first_desc = ""
        all_km = []
        if parts:
            for p in parts:
                if not first_title and p.get("title", "").strip():
                    first_title = p.get("title", "").strip()
                if not first_desc and p.get("description", "").strip():
                    first_desc = p.get("description", "").strip()
                for km in p.get("key_moments", []):
                    at_s = _snap(km.get("at_s", 0), allowed_s) if allowed_s else km.get("at_s", 0)
                    at_s = max(0, min(at_s, max(0, duration_s - 1))) if duration_s > 0 else 0
                    all_km.append({"at_s": at_s, "what": km.get("what", "")})
        all_km.sort(key=lambda m: m["at_s"])
        seen_at = set()
        unique_km = []
        for km in all_km:
            if km["at_s"] not in seen_at:
                seen_at.add(km["at_s"])
                unique_km.append(km)
        return [{
            "start_s": 0,
            "end_s": duration_s,
            "title": first_title if first_title else "Partie 1",
            "description": first_desc,
            "key_moments": unique_km[:3],
        }]

    if not parts:
        return [{
            "start_s": 0,
            "end_s": duration_s,
            "title": "Partie 1",
            "description": "",
            "key_moments": [],
        }]

    cleaned = []
    for p in parts:
        start_s = _snap(p.get("start_s", 0), allowed_s) if allowed_s else p.get("start_s", 0)
        kms = []
        for km in p.get("key_moments", []):
            at_s = _snap(km.get("at_s", 0), allowed_s) if allowed_s else km.get("at_s", 0)
            kms.append({"at_s": at_s, "what": km.get("what", "")})
        cleaned.append({
            "start_s": start_s,
            "title": p.get("title", ""),
            "description": p.get("description", ""),
            "key_moments": kms,
        })

    cleaned.sort(key=lambda p: p["start_s"])

    seen_start = set()
    deduped = []
    for p in cleaned:
        if p["start_s"] not in seen_start:
            seen_start.add(p["start_s"])
            deduped.append(p)
    cleaned = deduped

    cleaned[0]["start_s"] = 0

    for p in cleaned:
        p["start_s"] = max(0, min(p["start_s"], max(0, duration_s - 1)))

    seen_start = set()
    clamped = []
    for p in cleaned:
        if p["start_s"] not in seen_start:
            seen_start.add(p["start_s"])
            clamped.append(p)
    cleaned = clamped

    for i in range(len(cleaned)):
        if i + 1 < len(cleaned):
            cleaned[i]["end_s"] = cleaned[i + 1]["start_s"]
        else:
            cleaned[i]["end_s"] = duration_s

    while len(cleaned) > 1:
        short_idx = None
        for i, p in enumerate(cleaned):
            if (p["end_s"] - p["start_s"]) < min_part_s:
                short_idx = i
                break
        if short_idx is None:
            break

        if short_idx == 0:
            neighbor_idx = 1
        elif short_idx == len(cleaned) - 1:
            neighbor_idx = short_idx - 1
        else:
            left_len = cleaned[short_idx - 1]["end_s"] - cleaned[short_idx - 1]["start_s"]
            right_len = cleaned[short_idx + 1]["end_s"] - cleaned[short_idx + 1]["start_s"]
            if left_len <= right_len:
                neighbor_idx = short_idx - 1
            else:
                neighbor_idx = short_idx + 1

        left_idx = min(short_idx, neighbor_idx)
        right_idx = max(short_idx, neighbor_idx)
        p_left = cleaned[left_idx]
        p_right = cleaned[right_idx]

        len_left = p_left["end_s"] - p_left["start_s"]
        len_right = p_right["end_s"] - p_right["start_s"]

        if len_left >= len_right:
            title = p_left.get("title") or p_right.get("title") or ""
            desc = p_left.get("description") or p_right.get("description") or ""
        else:
            title = p_right.get("title") or p_left.get("title") or ""
            desc = p_right.get("description") or p_left.get("description") or ""

        combined_km = p_left.get("key_moments", []) + p_right.get("key_moments", [])
        combined_km.sort(key=lambda m: m["at_s"])
        seen_at = set()
        unique_km = []
        for km in combined_km:
            if km["at_s"] not in seen_at:
                seen_at.add(km["at_s"])
                unique_km.append({"at_s": km["at_s"], "what": km.get("what", "")})
        merged_km = unique_km[:3]

        merged_part = {
            "start_s": p_left["start_s"],
            "end_s": p_right["end_s"],
            "title": title,
            "description": desc,
            "key_moments": merged_km,
        }
        cleaned[left_idx:right_idx + 1] = [merged_part]

    result = []
    for idx, p in enumerate(cleaned, start=1):
        title = p.get("title", "").strip()
        if not title:
            title = f"Partie {idx}"
        desc = p.get("description", "")
        kms = p.get("key_moments", [])
        kms.sort(key=lambda m: m["at_s"])
        result.append({
            "start_s": p["start_s"],
            "end_s": p["end_s"],
            "title": title,
            "description": desc,
            "key_moments": [{"at_s": m["at_s"], "what": m.get("what", "")} for m in kms[:3]],
        })
    return result


def plan(src: Path, workdir: Path, duration_s: int, min_part_minutes: int, model: str) -> list[dict]:
    src = Path(src)
    workdir = Path(workdir)

    raw_models = [model] + FALLBACK_MODELS
    models = list(dict.fromkeys(raw_models))

    chunk_paths = []
    try:
        chunk_paths = chunk_audio(src, workdir, chunk_s=600)
        if not chunk_paths:
            parts = [{
                "start_s": 0,
                "end_s": duration_s,
                "title": "Partie 1",
                "description": "",
                "key_moments": [],
            }]
            return repair(parts, duration_s, [0], min_part_s=min_part_minutes * 60)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(describe_chunk, i, cp, 600, models)
                for i, cp in enumerate(chunk_paths)
            ]
            chunks = [f.result() for f in futures]

        allowed_s = []
        for c in chunks:
            for s in c.get("segments", []):
                allowed_s.append(s["at_s"])
        if 0 not in allowed_s:
            allowed_s.append(0)
        allowed_s = sorted(set(allowed_s))

        parts = None
        for attempt in range(2):
            try:
                parts = merge(chunks, duration_s, models)
                break
            except Exception:
                pass

        if parts is None:
            first_topic = "Partie 1"
            if chunks and chunks[0].get("segments"):
                first_topic = chunks[0]["segments"][0].get("topic", "Partie 1")
            parts = [{
                "start_s": 0,
                "end_s": duration_s,
                "title": first_topic,
                "description": chunks[0].get("summary_en", "") if chunks else "",
                "key_moments": [],
            }]

        return repair(parts, duration_s, allowed_s, min_part_s=min_part_minutes * 60)
    finally:
        for cp in chunk_paths:
            try:
                cp.unlink(missing_ok=True)
            except OSError:
                pass

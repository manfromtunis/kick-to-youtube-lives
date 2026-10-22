"""Audio chunking, Gemini thematic segmentation, merge and repair."""
from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from k2y import config
from .media import FFMPEG

logger = logging.getLogger("k2y")
logger.addHandler(logging.NullHandler())

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

MERGE_PROMPT = """You are a video editor assembling thematic YouTube videos from a Kick livestream.
The stream was {dur} long. Here is the chronological timeline of what happened:
{timeline}

Instructions:
1. Divide the stream into thematic parts (cut where the activity or subject changes).
2. Each part must be between 15 and 60 minutes long (unless the whole stream is shorter).
3. Do not cut exactly at chunk boundaries (00:00, 10:00, etc.) unless the topic really changes there.
4. "start_s" for each part MUST be chosen from one of the "at_s" timestamps in the timeline. The first part MUST start at 0.
5. "title": short, catchy title in French or Latin-script Tunisian darija (max 60 chars).
6. "description": 1-2 paragraphs in French summarizing this part.
7. "summary_en": 1 sentence in English summarizing the part.
8. "key_moments": 1 to 3 highlight moments in this part. Each "at_s" MUST be chosen from the timeline "at_s" timestamps that fall within this part. "what": short description in French."""


class SegmentError(Exception):
    pass


class DailyQuota(Exception):
    pass


def _get_api_key() -> str:
    return config.read_secret("gemini.key")


def _is_daily_quota_error(error_body: bytes | str) -> bool:
    if isinstance(error_body, bytes):
        error_body = error_body.decode("utf-8", errors="replace")
    try:
        data = json.loads(error_body)
        err = data.get("error", {})
        details = err.get("details", [])
        for d in details:
            if "quotaId" in d and "PerDay" in d.get("quotaId", ""):
                return True
            for vi in d.get("violations", []):
                if "PerDay" in vi.get("quotaId", ""):
                    return True
    except Exception:
        pass
    return "PerDay" in error_body


def _parse_time_to_seconds(at_val: str | int) -> int:
    if isinstance(at_val, int):
        return max(0, at_val)
    parts = str(at_val).strip().split(":")
    try:
        if len(parts) == 1:
            return max(0, int(parts[0]))
        elif len(parts) == 2:
            return max(0, int(parts[0]) * 60 + int(parts[1]))
        elif len(parts) == 3:
            return max(0, int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    except ValueError:
        return 0
    return 0


def _format_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _snap(val: int, allowed: list[int]) -> int:
    if not allowed:
        return val
    return min(allowed, key=lambda a: abs(a - val))


def generate(parts: list[dict], schema: dict, models: list[str]) -> dict:
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
    malformed_json_retries: int = 0
    last_error: Exception | None = None

    for attempt in range(8):
        active = set(models) - skipped_models
        if not active:
            raise SegmentError("All candidate models returned 404")
        if active <= per_day_models:
            raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")

        candidates = [m for m in models if m in active and m not in per_day_models]
        if not candidates:
            if active and active <= per_day_models:
                raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")
            raise SegmentError("All candidate models exhausted or unavailable")

        model = candidates[attempt % len(candidates)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))

            candidates_data = resp_data.get("candidates")
            candidate = (
                candidates_data[0]
                if (isinstance(candidates_data, list) and candidates_data)
                else {}
            )
            finish_reason = (
                candidate.get("finishReason")
                or resp_data.get("promptFeedback", {}).get("blockReason")
                or "EMPTY"
            )
            content = candidate.get("content") or {}
            parts_list = content.get("parts") or []

            text_val: str | None = None
            if isinstance(parts_list, list):
                for p in parts_list:
                    if isinstance(p, dict) and "text" in p and not p.get("thought", False):
                        text_val = p["text"]
                        break

            if text_val is None:
                logger.warning(f"{model}: no candidate/text response (finishReason={finish_reason})")
                last_error = SegmentError(f"No text part for {model} (finishReason={finish_reason})")
                continue

            text = text_val.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            try:
                return json.loads(text.strip())
            except json.JSONDecodeError as e:
                malformed_json_retries += 1
                logger.warning(f"{model}: malformed JSON response, retrying on another model: {e}")
                last_error = SegmentError(f"Malformed Gemini response: {e}")
                if malformed_json_retries > 1:
                    raise last_error
                continue

        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 404:
                skipped_models.add(model)
                logger.warning(f"{model}: HTTP {e.code}")
                active = set(models) - skipped_models
                if not active:
                    raise SegmentError("All candidate models returned 404")
                if active <= per_day_models:
                    raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")
                continue
            elif e.code == 429:
                err_body = e.read()
                if _is_daily_quota_error(err_body):
                    per_day_models.add(model)
                    active = set(models) - skipped_models
                    if active and active <= per_day_models:
                        raise DailyQuota(f"Daily quota exceeded on all models: {list(per_day_models)}")
                logger.warning(f"{model}: HTTP {e.code}")
            elif e.code not in (500, 503):
                raise SegmentError(f"Gemini API HTTP error {e.code}: {e.reason}")
            else:
                logger.warning(f"{model}: HTTP {e.code}")
        except urllib.error.URLError as e:
            last_error = e
            logger.warning(f"{model}: URLError")
        except (KeyError, IndexError) as e:
            raise SegmentError(f"Malformed Gemini response: {e}")

        if attempt < 7:
            _sleep(min(5 * (2 ** attempt), 60))

    raise SegmentError(f"Gemini generation failed after 8 attempts: {last_error}")


def chunk_audio(src: Path, workdir: Path, chunk_s: int = 600) -> list[Path]:
    src = Path(src)
    workdir = Path(workdir)
    chunks_dir = workdir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    for old_chunk in chunks_dir.glob("c*.ogg"):
        old_chunk.unlink(missing_ok=True)

    audio_file = workdir / "audio.ogg"
    extract_cmd = [
        *FFMPEG,
        "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "libopus",
        "-b:a", "24k",
        str(audio_file),
    ]
    try:
        subprocess.run(extract_cmd, capture_output=True, text=True, check=True, timeout=3600)
    except subprocess.TimeoutExpired:
        audio_file.unlink(missing_ok=True)
        raise SegmentError("ffmpeg audio extraction timed out after 1 hour")
    except subprocess.CalledProcessError as e:
        audio_file.unlink(missing_ok=True)
        raise SegmentError(f"ffmpeg audio extraction failed (code {e.returncode}): {e.stderr}")

    segment_cmd = [
        *FFMPEG,
        "-i", str(audio_file),
        "-f", "segment",
        "-segment_time", str(chunk_s),
        "-c", "copy",
        str(chunks_dir / "c%03d.ogg"),
    ]
    try:
        subprocess.run(segment_cmd, capture_output=True, text=True, check=True, timeout=3600)
    except subprocess.TimeoutExpired:
        for c in chunks_dir.glob("c*.ogg"):
            c.unlink(missing_ok=True)
        raise SegmentError("ffmpeg audio segmenting timed out after 1 hour")
    except subprocess.CalledProcessError as e:
        raise SegmentError(f"ffmpeg audio segmenting failed (code {e.returncode}): {e.stderr}")
    finally:
        audio_file.unlink(missing_ok=True)

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


def _dedupe(items: list, key: object) -> list:
    seen = set()
    out = []
    for item in items:
        k = key(item) if callable(key) else item[key]
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def _top_moments(kms: list[dict]) -> list[dict]:
    sorted_kms = sorted(kms, key=lambda m: m["at_s"])
    return _dedupe(sorted_kms, key=lambda m: m["at_s"])[:3]


def repair(parts: list[dict], duration_s: int, allowed_s: list[int], min_part_s: int) -> list[dict]:
    if duration_s < 20 * 60:
        first_title = ""
        first_desc = ""
        all_km = []
        for p in parts:
            if not first_title and p.get("title", "").strip():
                first_title = p.get("title", "").strip()
            if not first_desc and p.get("description", "").strip():
                first_desc = p.get("description", "").strip()
            for km in p.get("key_moments", []):
                at_s = _snap(km.get("at_s", 0), allowed_s)
                if 0 <= at_s < duration_s:
                    all_km.append({"at_s": at_s, "what": km.get("what", "")})
        return [{
            "start_s": 0,
            "end_s": duration_s,
            "title": first_title if first_title else "Partie 1",
            "description": first_desc,
            "key_moments": _top_moments(all_km),
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
        start_s = _snap(p.get("start_s", 0), allowed_s)
        kms = []
        for km in p.get("key_moments", []):
            at_s = _snap(km.get("at_s", 0), allowed_s)
            kms.append({"at_s": at_s, "what": km.get("what", "")})
        cleaned.append({
            "start_s": start_s,
            "title": p.get("title", ""),
            "description": p.get("description", ""),
            "key_moments": kms,
        })

    cleaned.sort(key=lambda p: p["start_s"])
    cleaned[0]["start_s"] = 0

    for p in cleaned:
        p["start_s"] = max(0, min(p["start_s"], max(0, duration_s - 1)))

    cleaned = _dedupe(cleaned, key=lambda p: p["start_s"])

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

        merged_part = {
            "start_s": p_left["start_s"],
            "end_s": p_right["end_s"],
            "title": title,
            "description": desc,
            "key_moments": combined_km,
        }
        cleaned[left_idx:right_idx + 1] = [merged_part]

    result = []
    for idx, p in enumerate(cleaned, start=1):
        title = p.get("title", "").strip()
        if not title:
            title = f"Partie {idx}"
        desc = p.get("description", "")
        p_start = p["start_s"]
        p_end = p["end_s"]
        kms = [
            m for m in p.get("key_moments", [])
            if p_start <= m["at_s"] < p_end
        ]
        result.append({
            "start_s": p_start,
            "end_s": p_end,
            "title": title,
            "description": desc,
            "key_moments": _top_moments(kms),
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

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            future_to_idx = {
                executor.submit(describe_chunk, i, cp, 600, models): i
                for i, cp in enumerate(chunk_paths)
            }
            chunks_by_idx = {}
            for future in concurrent.futures.as_completed(future_to_idx):
                try:
                    chunks_by_idx[future_to_idx[future]] = future.result()
                except Exception:
                    for f in future_to_idx:
                        f.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
            chunks = [chunks_by_idx[i] for i in range(len(chunk_paths))]
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        allowed_s = []
        for c in chunks:
            for s in c.get("segments", []):
                allowed_s.append(s["at_s"])
        if 0 not in allowed_s:
            allowed_s.append(0)
        allowed_s = sorted(set(allowed_s))

        parts = None
        last_merge_err = None
        for attempt in range(2):
            try:
                parts = merge(chunks, duration_s, models)
                break
            except (DailyQuota, config.MissingSecret):
                raise
            except Exception as e:
                last_merge_err = e

        if parts is None:
            logger.warning(f"Merge failed twice, falling back to single part: {last_merge_err}")
            first_topic = "Partie 1"
            if chunks and chunks[0].get("segments"):
                first_topic = chunks[0]["segments"][0].get("topic", "Partie 1")
            parts = [{
                "start_s": 0,
                "end_s": duration_s,
                "title": first_topic,
                "description": "",
                "key_moments": [],
            }]

        return repair(parts, duration_s, allowed_s, min_part_s=min_part_minutes * 60)
    finally:
        for cp in chunk_paths:
            try:
                cp.unlink(missing_ok=True)
            except OSError:
                pass

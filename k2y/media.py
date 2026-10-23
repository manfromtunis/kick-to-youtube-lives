"""Media manipulation: cutting parts, generating thumbnails, chapters and descriptions."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import subprocess
import textwrap
from typing import Any

from . import config
from .kick import FFMPEG, probe_duration

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
]
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _escape_font_path(p: Path) -> str:
    """Convert path to forward slashes, escaping colons and single quotes for ffmpeg filter syntax."""
    s = str(p).replace("\\", "/")
    return s.replace(":", r"\:").replace("'", r"\'")


def _find_font() -> Path | None:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def hms(seconds: int) -> str:
    """Format seconds into YouTube chapter timestamp ("M:SS" or "H:MM:SS")."""
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def chapters(items: list[tuple[int, str]], end_s: int | None = None) -> str:
    """Format YouTube chapter lines "<hms> <title>".

    Returns "" unless:
      - sorted by time
      - at least 3 items
      - the first item is at 0
      - every gap between consecutive items (and to end_s if given) is >= 10s
    """
    sorted_items = sorted(items, key=lambda x: x[0])
    if len(sorted_items) < 3:
        return ""
    if sorted_items[0][0] != 0:
        return ""
    for i in range(len(sorted_items) - 1):
        if sorted_items[i + 1][0] - sorted_items[i][0] < 10:
            return ""
    if end_s is not None and end_s - sorted_items[-1][0] < 10:
        return ""
    return "\n".join(f"{hms(t)} {title.strip()}" for t, title in sorted_items)


def cut(src: Path, start_s: int, end_s: int, dest: Path) -> Path:
    """Cut a segment using ffmpeg copy and input seeking."""
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *FFMPEG,
        "-ss", str(start_s),
        "-to", str(end_s),
        "-i", str(src),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(dest),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg cut timed out after 2 hours for {dest}")
    if res.returncode != 0:
        dest.unlink(missing_ok=True)
        tail = "\n".join(res.stderr.strip().splitlines()[-10:])
        raise RuntimeError(f"ffmpeg cut failed (code {res.returncode}): {tail}")
    return dest


def wrap_title(title: str) -> str:
    """Wrap long titles onto at most 2 lines (~24 chars per line), truncating with '…'."""
    title = title.strip()
    if not title:
        return ""
    lines = textwrap.wrap(title, width=24)
    if len(lines) <= 2:
        return "\n".join(lines)
    first_line = lines[0]
    remaining = " ".join(lines[1:])
    short = textwrap.shorten(remaining, width=24, placeholder="…")
    second_line = short if short and short != "…" else remaining[:23].rstrip() + "…"
    return f"{first_line}\n{second_line}"


def sanitize_hook(text: str | None, fallback_title: str = "") -> str:
    """Sanitise hook text: strip, upper-case, remove <> and newlines, cut at 22 chars on word boundary.

    Fallback (empty text or None): first 3 words of fallback_title, upper-cased.
    """
    cleaned = (text or "").strip()
    cleaned = cleaned.replace("<", "").replace(">", "")
    cleaned = re.sub(r"[\r\n]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        fb = (fallback_title or "").strip()
        fb = fb.replace("<", "").replace(">", "")
        fb = re.sub(r"[\r\n]+", " ", fb).strip()
        # Kick titles chain ideas with ; | , : and the first idea makes the best hook.
        fb = re.split(r"[;|,:]", fb)[0]
        words = fb.split()[:3]
        cleaned = " ".join(words)

    cleaned = cleaned.upper()
    if len(cleaned) > 22:
        cut_text = cleaned[:22]
        if cleaned[22] == " ":
            cleaned = cut_text
        elif " " in cut_text:
            cleaned = cut_text.rsplit(" ", 1)[0]
        else:
            cleaned = cut_text
    return cleaned.strip()


def hook_lines(text: str) -> tuple[str, str]:
    """Split hook into at most two lines: line 1 = first word (or first two if short), line 2 = rest."""
    words = text.split()
    if not words:
        return ("", "")
    if len(words) == 1:
        return (words[0], "")
    if len(words) == 2:
        return (words[0], words[1])
    if len(words[0]) <= 3 or (len(words[0]) + len(words[1]) <= 7):
        return (f"{words[0]} {words[1]}", " ".join(words[2:]))
    return (words[0], " ".join(words[1:]))


def _estimate_text_width(text: str, fontsize: int, is_condensed: bool = False) -> float:
    if not text:
        return 0.0
    factor = 0.55 if is_condensed else 0.80
    w = 0.0
    for c in text:
        if c in "MW":
            w += 1.15 * fontsize * (factor / 0.80)
        elif c in "I 1!':.,":
            w += 0.40 * fontsize * (factor / 0.80)
        elif c in "JLFT":
            w += 0.70 * fontsize * (factor / 0.80)
        else:
            w += factor * fontsize
    return w + 20.0  # black border 10 on each side


def _fit_fontsize(text: str, max_fontsize: int, max_w: int, font_file: Path | None) -> int:
    if not text:
        return max_fontsize
    is_condensed = font_file is not None and "impact" in font_file.name.lower()
    est = _estimate_text_width(text, max_fontsize, is_condensed=is_condensed)
    if est <= max_w:
        return max_fontsize
    fitted = int(max_fontsize * (max_w / est))
    return max(20, min(max_fontsize, fitted))


def thumbnail(
    src: Path,
    at_s: int,
    hook: str,
    dest: Path,
    face: Path | None = None,
) -> Path:
    """Generate 1280x720 thumbnail with background, reaction face, and hook text under 2MB."""
    src = Path(src).resolve()
    dest = Path(dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    dur = probe_duration(src)
    max_at = max(0, int(dur - 1))
    clamped_at_s = max(0, min(int(at_s), max_at))

    sanitized = sanitize_hook(hook)
    line1, line2 = hook_lines(sanitized)

    safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", dest.stem) or "thumb"
    tf1_name = f"{safe_stem}_1.txt"
    tf2_name = f"{safe_stem}_2.txt"
    tf1_path = dest.parent / tf1_name
    tf2_path = dest.parent / tf2_name

    tf1_path.write_text(line1, encoding="utf-8")
    tf2_path.write_text(line2, encoding="utf-8")

    face_path = Path(face).resolve() if face and Path(face).exists() else None

    try:
        font_file = _find_font()
        font_arg = f"fontfile='{_escape_font_path(font_file)}':" if font_file else ""

        # Layout rules: text fits in left 60% starting at x=50
        max_w = (1280 - 100) if face_path is None else (int(1280 * 0.60) - 50)
        fs1 = _fit_fontsize(line1, 160, max_w, font_file)
        fs2 = _fit_fontsize(line2, 110, max_w, font_file)

        dt1 = (
            f"drawtext={font_arg}textfile={tf1_name}:"
            f"fontsize={fs1}:fontcolor=#FFD400:borderw=10:bordercolor=black:x=50:y=210:expansion=none"
        )
        dt2 = (
            f"drawtext={font_arg}textfile={tf2_name}:"
            f"fontsize={fs2}:fontcolor=white:borderw=10:bordercolor=black:x=50:y=400:expansion=none"
        )

        if face_path is not None:
            filter_complex = (
                "[0]scale=1280:720,boxblur=6,eq=brightness=-0.25[bg];"
                # Phone photos are landscape with the face centred: keep a centred 4:5 portrait.
                "[1]crop='min(iw,ih*4/5)':ih,scale=-1:760,format=rgba,split=3[p][pa][pw];"
                # White rim from the dilated alpha: separates a cut-out PNG from the dark
                # background, and stays hidden under an opaque JPG.
                "[pa]alphaextract," + ",".join(["dilation"] * 8) + "[rim];"
                "[pw]drawbox=c=white:t=fill,format=rgb24[white];"
                "[white][rim]alphamerge[outline];"
                "[outline][p]overlay=format=rgb,format=rgba,"
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*min(1, X/(W*0.35))'[face];"
                f"[bg][face]overlay=x=W-w+40:y=-20,{dt1},{dt2}"
            )
            cmd = [
                *FFMPEG,
                "-ss", str(clamped_at_s),
                "-i", str(src),
                "-i", str(face_path),
                "-filter_complex", filter_complex,
                "-frames:v", "1",
                "-q:v", "3",
                str(dest),
            ]
        else:
            vf = (
                f"scale=1280:720,boxblur=6,eq=brightness=-0.25,{dt1},{dt2}"
            )
            cmd = [
                *FFMPEG,
                "-ss", str(clamped_at_s),
                "-i", str(src),
                "-vf", vf,
                "-frames:v", "1",
                "-q:v", "3",
                str(dest),
            ]

        try:
            res = subprocess.run(cmd, cwd=dest.parent, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg thumbnail timed out after 120 seconds for {dest}")

        if res.returncode != 0:
            dest.unlink(missing_ok=True)
            tail = "\n".join(res.stderr.strip().splitlines()[-10:])
            raise RuntimeError(f"ffmpeg thumbnail failed (code {res.returncode}): {tail}")

        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Thumbnail generation failed: destination file {dest} is missing or empty")

        # Re-encode loop under 2MB
        q = 3
        while dest.stat().st_size > MAX_IMAGE_BYTES and q < 31:
            q += 2
            tmp_dest = dest.with_suffix(".tmp.jpg")
            cmd_reencode = [
                *FFMPEG,
                "-i", str(dest),
                "-frames:v", "1",
                "-q:v", str(q),
                str(tmp_dest),
            ]
            try:
                re_res = subprocess.run(cmd_reencode, cwd=dest.parent, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                tmp_dest.unlink(missing_ok=True)
                dest.unlink(missing_ok=True)
                raise RuntimeError("ffmpeg re-encode timed out after 120 seconds")

            if re_res.returncode == 0 and tmp_dest.exists():
                tmp_dest.replace(dest)
            else:
                break

        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Thumbnail generation failed: destination file {dest} is missing or empty")

        return dest
    finally:
        tf1_path.unlink(missing_ok=True)
        tf2_path.unlink(missing_ok=True)


def _fit(text: str, suffix: str) -> str:
    return f"{text[:100 - len(suffix)]}{suffix}"


def full_title(session_title: str, started_at: str) -> str:
    """Kick session_title + ' | Stream du <DD/MM/YYYY>', max 100 chars."""
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        dt_str = dt.strftime("%d/%m/%Y")
    except Exception:
        dt_str = started_at[:10]
    return _fit(session_title, f" | Stream du {dt_str}")


def part_title(gemini_title: str, index: int, total: int) -> str:
    """Gemini title + ' | Partie <n>/<total>', max 100 chars."""
    return _fit(gemini_title, f" | Partie {index}/{total}")


def sanitize_tags(tags: list[str] | None) -> list[str]:
    """Sanitise tags for YouTube snippet: lowercase, strip, remove <> and commas, drop >100 chars, <= 480 chars total budget."""
    if not tags:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    total_budget = 0
    for t in tags:
        t_clean = str(t).strip().lower().replace("<", "").replace(">", "").replace(",", "")
        if not t_clean or t_clean in seen:
            continue
        if len(t_clean) > 100:
            continue
        cost = len(t_clean) + 2 if " " in t_clean else len(t_clean)
        needed = cost + (1 if cleaned else 0)
        if total_budget + needed > 480:
            break
        seen.add(t_clean)
        cleaned.append(t_clean)
        total_budget += needed
    return cleaned


def sanitize_hashtags(hashtags: list[str] | None) -> list[str]:
    """Sanitise hashtags: strip, remove <>, no spaces, prefix #, deduplicate."""
    if not hashtags:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for h in hashtags:
        h_clean = str(h).strip().replace("<", "").replace(">", "")
        h_clean = "".join(h_clean.split())
        h_val = h_clean.lstrip("#")
        if not h_val:
            continue
        h_clean = f"#{h_val}"
        lower_h = h_clean.lower()
        if lower_h in seen:
            continue
        seen.add(lower_h)
        cleaned.append(h_clean)
    return cleaned


def format_link(label: str, url: str) -> str:
    lbl = str(label).strip()
    u = str(url).strip()
    if lbl.endswith(":"):
        return f"{lbl} {u}"
    return f"{lbl} : {u}"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}"
    return f"{m}min"


def _build_links_block(channel: str, links: list | tuple | None) -> str:
    lines = []
    if channel:
        lines.append(f"🔴 En live sur Kick : https://kick.com/{channel.strip()}")
    active_links = links if links is not None else config.DEFAULT_LINKS
    for item in active_links:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            lines.append(format_link(item[0], item[1]))
    return "\n".join(lines)


def _resolve_part_yt_id(part_source: Any, idx_1based: int, part_dict: Any = None) -> str | None:
    if part_dict is not None:
        try:
            if hasattr(part_dict, "get") and part_dict.get("youtube_id"):
                return str(part_dict["youtube_id"])
            if hasattr(part_dict, "keys") and "youtube_id" in part_dict.keys() and part_dict["youtube_id"]:
                return str(part_dict["youtube_id"])
        except Exception:
            pass
    if not part_source:
        return None
    if isinstance(part_source, dict):
        return part_source.get(idx_1based) or part_source.get(str(idx_1based))
    if isinstance(part_source, (list, tuple)):
        if idx_1based - 1 < len(part_source):
            item = part_source[idx_1based - 1]
            if isinstance(item, str):
                return item
        for item in part_source:
            if isinstance(item, (tuple, list)) and len(item) >= 3 and item[0] == idx_1based:
                return str(item[2])
            if hasattr(item, "__getitem__"):
                item_idx = None
                try:
                    if hasattr(item, "get"):
                        item_idx = item.get("idx")
                    elif hasattr(item, "keys") and "idx" in item.keys():
                        item_idx = item["idx"]
                except Exception:
                    pass
                if item_idx == idx_1based:
                    try:
                        if hasattr(item, "get") and item.get("youtube_id"):
                            return str(item["youtube_id"])
                        if hasattr(item, "keys") and "youtube_id" in item.keys() and item["youtube_id"]:
                            return str(item["youtube_id"])
                    except Exception:
                        pass
    return None


def _assemble_and_trim_description(
    hook: str,
    chapters_block: str,
    nav_header: str,
    nav_lines: list[str],
    links_block: str,
    hashtags_block: str,
) -> str:
    hook = hook.replace("<", "").replace(">", "").strip()
    if len(hook) > 200:
        hook = hook[:200].rstrip()

    def _build(h: str, n_lines: list[str]) -> str:
        blocks = []
        if h:
            blocks.append(h)
        if chapters_block:
            blocks.append(chapters_block)
        if nav_header and n_lines:
            blocks.append(f"{nav_header}\n" + "\n".join(n_lines))
        if links_block:
            blocks.append(links_block)
        if hashtags_block:
            blocks.append(hashtags_block)
        return "\n\n".join(blocks)

    curr_nav = [ln.replace("<", "").replace(">", "").strip() for ln in nav_lines if ln.strip()]
    text = _build(hook, curr_nav)
    if len(text.encode("utf-8")) <= 5000:
        return text

    # Drop navigation lines from the end first
    while curr_nav:
        curr_nav.pop()
        text = _build(hook, curr_nav)
        if len(text.encode("utf-8")) <= 5000:
            return text

    # If all navigation lines are dropped, shorten the hook
    while hook:
        text = _build(hook, [])
        encoded_len = len(text.encode("utf-8"))
        if encoded_len <= 5000:
            return text
        excess = encoded_len - 5000
        hook = hook[:-max(1, excess)].rstrip()

    res = _build("", [])
    if len(res.encode("utf-8")) <= 5000:
        return res
    return _truncate_utf8(res, 5000)


def full_description(
    parts: list[dict],
    channel: str = "randomguy42",
    *,
    links: list | tuple | None = None,
    vod_hook: str | None = None,
    hashtags: list[str] | None = None,
    part_videos: list[Any] | None = None,
) -> str:
    """Format full VOD description following the v2 layout."""
    hook = str(vod_hook or "").strip()

    items = []
    for p in parts:
        start_s = int(p.get("start_s", 0))
        t = str(p.get("title", "")).strip()
        items.append((start_s, t))

    chaps_text = chapters(items)
    chaps_block = f"⏱️ Chapitres\n{chaps_text}" if chaps_text else ""

    nav_header = ""
    nav_lines: list[str] = []
    if len(parts) > 1:
        nav_header = f"📺 Ce live découpé en {len(parts)} vidéos"
        for i, p in enumerate(parts, 1):
            title = re.sub(r"\s*\|\s*Partie\s+\d+/\d+\s*$", "", str(p.get("title", f"Partie {i}"))).strip()
            yt_id = _resolve_part_yt_id(part_videos, i, p)
            if yt_id:
                nav_lines.append(f"{i}/{len(parts)} {title} https://youtu.be/{yt_id}")
        if not nav_lines:
            nav_header = ""

    links_block = _build_links_block(channel, links)

    sanitized_ht = sanitize_hashtags(hashtags)
    hashtags_block = " ".join(sanitized_ht) if sanitized_ht else ""

    return _assemble_and_trim_description(
        hook, chaps_block, nav_header, nav_lines, links_block, hashtags_block
    )


def part_description(
    part: dict,
    full_video_id: str | None = None,
    channel: str = "randomguy42",
    *,
    part_index: int | None = None,
    total_parts: int | None = None,
    all_parts: list[dict] | None = None,
    other_parts: list[Any] | None = None,
    full_duration_s: int | None = None,
    links: list | tuple | None = None,
    hashtags: list[str] | None = None,
) -> str:
    """Format part video description following the v2 layout."""
    hook = str(part.get("hook") or part.get("description", "")).strip()

    part_start_s = int(part.get("start_s", 0))
    part_end_s = int(part.get("end_s", 0))
    part_title_str = str(part.get("title", "")).strip()

    raw_kms = part.get("key_moments") or []
    rebased_kms = []
    has_zero = False
    for km in raw_kms:
        at_s = int(km.get("at_s", 0))
        if part_end_s > part_start_s and not (part_start_s <= at_s < part_end_s):
            continue
        rel_s = at_s - part_start_s
        if rel_s == 0:
            has_zero = True
        what = str(km.get("what", "")).strip()
        rebased_kms.append((rel_s, what))

    if not has_zero:
        rebased_kms.insert(0, (0, part_title_str))

    rebased_kms.sort(key=lambda x: x[0])
    part_duration = part_end_s - part_start_s if part_end_s > part_start_s else None
    chaps_text = chapters(rebased_kms, end_s=part_duration)
    chaps_block = f"⏱️ Chapitres\n{chaps_text}" if chaps_text else ""

    part_items = []
    if all_parts:
        for p in all_parts:
            p_idx = None
            try:
                if hasattr(p, "get"):
                    p_idx = p.get("idx")
                elif hasattr(p, "keys") and "idx" in p.keys():
                    p_idx = p["idx"]
            except Exception:
                pass
            if p_idx == 0:
                continue
            part_items.append(p)

    tot = total_parts or (len(part_items) if all_parts is not None else (len(other_parts) + 1 if other_parts is not None else 1))
    idx = part_index
    if idx is None:
        if part_items and part in part_items:
            idx = part_items.index(part) + 1
        else:
            p_idx = None
            try:
                if hasattr(part, "get"):
                    p_idx = part.get("idx")
                elif hasattr(part, "keys") and "idx" in part.keys():
                    p_idx = part["idx"]
            except Exception:
                pass
            idx = int(p_idx) if p_idx is not None else 1

    other_list: list[tuple[int, str, str]] = []
    if part_items:
        for loop_j, p in enumerate(part_items, 1):
            p_idx = None
            try:
                if hasattr(p, "get"):
                    p_idx = p.get("idx")
                elif hasattr(p, "keys") and "idx" in p.keys():
                    p_idx = p["idx"]
            except Exception:
                pass
            j = int(p_idx) if p_idx is not None else loop_j
            if j != idx:
                yt_id = _resolve_part_yt_id(p, j, p)
                p_title = None
                try:
                    if hasattr(p, "get"):
                        p_title = p.get("title")
                    elif hasattr(p, "keys") and "title" in p.keys():
                        p_title = p["title"]
                except Exception:
                    pass
                p_title = p_title or f"Partie {j}"
                title = re.sub(r"\s*\|\s*Partie\s+\d+/\d+\s*$", "", str(p_title)).strip()
                if yt_id:
                    other_list.append((j, title, yt_id))
    elif other_parts:
        for item in other_parts:
            if isinstance(item, (tuple, list)) and len(item) >= 3:
                other_list.append((int(item[0]), str(item[1]), str(item[2])))
            elif hasattr(item, "__getitem__"):
                j = int(item.get("idx", 0)) if hasattr(item, "get") else int(item["idx"])
                t = str(item.get("title", f"Partie {j}")) if hasattr(item, "get") else str(item["title"])
                t = re.sub(r"\s*\|\s*Partie\s+\d+/\d+\s*$", "", t).strip()
                y = str(item.get("youtube_id", "")) if hasattr(item, "get") else str(item["youtube_id"])
                if y:
                    other_list.append((j, t, y))

    nav_header = ""
    nav_lines: list[str] = []
    if tot > 1:
        nav_header = "📺 Les autres parties de ce live"
        for other_idx, other_title, other_id in other_list:
            nav_lines.append(f"{other_idx}/{tot} {other_title} https://youtu.be/{other_id}")
        if full_video_id:
            dur_label = f"({format_duration(full_duration_s)}) " if full_duration_s else ""
            nav_lines.append(f"▶️ Le stream complet {dur_label}: https://youtu.be/{full_video_id}")
        if not nav_lines:
            nav_header = ""

    links_block = _build_links_block(channel, links)

    ht_val = part.get("hashtags") if part.get("hashtags") is not None else hashtags
    sanitized_ht = sanitize_hashtags(ht_val)
    hashtags_block = " ".join(sanitized_ht) if sanitized_ht else ""

    return _assemble_and_trim_description(
        hook, chaps_block, nav_header, nav_lines, links_block, hashtags_block
    )

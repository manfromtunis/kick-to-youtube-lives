"""Media manipulation: cutting parts, generating thumbnails, chapters and descriptions."""
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import textwrap

FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MAX_IMAGE_BYTES = 2 * 1024 * 1024


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
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(start_s),
        "-to", str(end_s),
        "-i", str(src),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-10:])
        raise RuntimeError(f"ffmpeg cut failed (code {res.returncode}): {tail}")
    return dest


def wrap_title(title: str, max_lines: int = 2, width: int = 24) -> str:
    """Wrap long titles onto at most 2 lines (~24 chars per line), truncating with '…'."""
    title = title.strip()
    if not title:
        return ""
    lines = textwrap.wrap(title, width=width)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    first_line = lines[0]
    remaining = " ".join(lines[1:])
    short = textwrap.shorten(remaining, width=width, placeholder="…")
    if short and short != "…":
        second_line = short
    else:
        second_line = remaining[:width - 1].rstrip() + "…"
    return f"{first_line}\n{second_line}"


def thumbnail(src: Path, at_s: int, title: str, dest: Path) -> Path:
    """Grab frame at at_s, scale/pad to 1280x720, overlay title with drawtext, output JPEG < 2MB."""
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    wrapped = wrap_title(title, max_lines=2, width=24)

    # Use a temporary file for drawtext to handle apostrophes, colons, accents, etc.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
        tf.write(wrapped)
        tf_path = Path(tf.name)

    try:
        font_file = FONT_PATH if FONT_PATH.exists() else "Sans"
        vf = (
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
            f"drawtext=fontfile='{font_file}':textfile='{tf_path}':"
            "fontcolor=white:borderw=6:bordercolor=black:fontsize=72:text_align=center:"
            "x=(w-text_w)/2:y=h*2/3+(h/3-text_h)/2:expansion=none"
        )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", str(at_s),
            "-i", str(src),
            "-vf", vf,
            "-frames:v", "1",
            "-q:v", "3",
            str(dest),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            tail = "\n".join(res.stderr.strip().splitlines()[-10:])
            raise RuntimeError(f"ffmpeg thumbnail failed (code {res.returncode}): {tail}")

        # If file is over 2MB, re-encode with higher q until under 2MB
        q = 3
        while dest.stat().st_size > MAX_IMAGE_BYTES and q < 31:
            q += 2
            tmp_dest = dest.with_suffix(".tmp.jpg")
            cmd_reencode = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(dest),
                "-frames:v", "1",
                "-q:v", str(q),
                str(tmp_dest),
            ]
            re_res = subprocess.run(cmd_reencode, capture_output=True, text=True)
            if re_res.returncode != 0:
                tail = "\n".join(re_res.stderr.strip().splitlines()[-10:])
                raise RuntimeError(f"ffmpeg re-encode failed (code {re_res.returncode}): {tail}")
            tmp_dest.replace(dest)

        return dest
    finally:
        tf_path.unlink(missing_ok=True)


def full_description(vod_title: str, parts: list[dict], kick_channel: str) -> str:
    """YouTube description for full VOD: chapters, blank line, Kick channel link."""
    chaps = chapters([(p["start_s"], p["title"]) for p in parts])
    kick_link = f"Live sur https://kick.com/{kick_channel}"
    body = f"{chaps}\n\n{kick_link}" if chaps else kick_link
    return body[:5000]


def part_description(part: dict, full_video_id: str | None, kick_channel: str) -> str:
    """YouTube description for a part: description, chapters from key moments, full stream link, Kick link."""
    desc = part.get("description", "").strip()
    start_s = part.get("start_s", 0)
    moments = [(m["at_s"] - start_s, m["what"]) for m in part.get("key_moments", [])]
    if not any(t == 0 for t, _ in moments):
        moments.insert(0, (0, part.get("title", "")))
    chaps = chapters(moments)

    links = []
    if full_video_id:
        links.append(f"Stream complet: https://youtu.be/{full_video_id}")
    links.append(f"Live sur https://kick.com/{kick_channel}")
    links_str = "\n".join(links)

    sections = [s for s in [desc, chaps, links_str] if s]
    return "\n\n".join(sections)[:5000]


def full_title(session_title: str, started_at: str) -> str:
    """Format full VOD title: f"{session_title} | Stream du {DD/MM/YYYY}", max 100 chars."""
    try:
        dt = datetime.fromisoformat(started_at.strip().replace("Z", "+00:00"))
        date_str = dt.strftime("%d/%m/%Y")
    except Exception:
        raw = started_at.strip()[:10]
        parts = raw.split("-")
        if len(parts) == 3:
            date_str = f"{parts[2]}/{parts[1]}/{parts[0]}"
        else:
            date_str = raw

    suffix = f" | Stream du {date_str}"
    max_prefix = 100 - len(suffix)
    if len(session_title) > max_prefix:
        session_title = session_title[:max_prefix]
    return f"{session_title}{suffix}"


def part_title(title: str, n: int, total: int) -> str:
    """Format part title: f"{title} | Partie {n}/{total}", max 100 chars."""
    suffix = f" | Partie {n}/{total}"
    max_prefix = 100 - len(suffix)
    if len(title) > max_prefix:
        title = title[:max_prefix]
    return f"{title}{suffix}"

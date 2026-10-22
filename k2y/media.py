"""Media manipulation: cutting parts, generating thumbnails, chapters and descriptions."""
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import textwrap

FONT_CANDIDATES = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
]
MAX_IMAGE_BYTES = 2 * 1024 * 1024
FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]



def _filter_path(p: Path) -> str:
    """Convert path to forward slashes, escaping drive colon and single quotes for ffmpeg."""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


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
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
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


def thumbnail(src: Path, at_s: int, title: str, dest: Path) -> Path:
    """Grab frame at at_s, scale/pad to 1280x720, overlay title with drawtext, output JPEG < 2MB."""
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    wrapped = wrap_title(title)

    # Use a temporary file for drawtext to handle apostrophes, colons, accents, etc.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
        tf.write(wrapped)
        tf_path = Path(tf.name)

    try:
        font_file = _find_font()
        font_arg = f"fontfile='{_filter_path(font_file)}':" if font_file else ""
        vf = (
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
            f"drawtext={font_arg}textfile='{_filter_path(tf_path)}':"
            "fontcolor=white:borderw=6:bordercolor=black:fontsize=72:text_align=center:"
            "x=(w-text_w)/2:y=h*2/3+(h/3-text_h)/2:expansion=none"
        )
        cmd = [
            *FFMPEG,
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
                *FFMPEG,
                "-i", str(dest),
                "-frames:v", "1",
                "-q:v", str(q),
                str(tmp_dest),
            ]
            re_res = subprocess.run(cmd_reencode, capture_output=True, text=True)
            if re_res.returncode == 0 and tmp_dest.exists():
                tmp_dest.replace(dest)
            else:
                break

        return dest
    finally:
        tf_path.unlink(missing_ok=True)


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


def full_description(parts: list[dict], channel: str) -> str:
    """Format full VOD description."""
    items = []
    for p in parts:
        start_s = int(p.get("start_s", 0))
        t = str(p.get("title", "")).strip()
        items.append((start_s, t))

    chaps = chapters(items)
    link_line = f"Live sur https://kick.com/{channel}"

    if chaps:
        desc = f"{chaps}\n\n{link_line}"
    else:
        desc = link_line

    return desc[:5000]


def part_description(part: dict, full_video_id: str | None, channel: str) -> str:
    """Format part video description."""
    desc_text = str(part.get("description", "")).strip()
    part_start_s = int(part.get("start_s", 0))
    part_end_s = int(part.get("end_s", 0))
    part_title_str = str(part.get("title", "")).strip()

    raw_kms = part.get("key_moments") or []
    rebased_kms = []
    has_zero = False
    for km in raw_kms:
        at_s = int(km.get("at_s", 0))
        rel_s = at_s - part_start_s
        if rel_s == 0:
            has_zero = True
        what = str(km.get("what", "")).strip()
        rebased_kms.append((rel_s, what))

    if not has_zero:
        rebased_kms.insert(0, (0, part_title_str))

    rebased_kms.sort(key=lambda x: x[0])
    part_duration = part_end_s - part_start_s if part_end_s > part_start_s else None
    chaps = chapters(rebased_kms, end_s=part_duration)

    links = []
    if full_video_id:
        links.append(f"Stream complet: https://youtu.be/{full_video_id}")
    links.append(f"Live sur https://kick.com/{channel}")
    links_str = "\n".join(links)

    sections = []
    if desc_text:
        sections.append(desc_text)
    if chaps:
        sections.append(chaps)
    sections.append(links_str)

    return "\n\n".join(sections)[:5000]

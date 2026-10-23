"""Media manipulation: cutting parts, generating thumbnails, chapters and descriptions."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import subprocess
import textwrap

from .kick import FFMPEG, probe_duration

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
]
MAX_IMAGE_BYTES = 2 * 1024 * 1024


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
        if font_file:
            font_esc = (
                _escape_font_path(font_file)
                .replace(",", r"\,")
                .replace("[", r"\[")
                .replace("]", r"\]")
                .replace(" ", r"\ ")
            )
            font_arg = f"fontfile={font_esc}:"
        else:
            font_arg = ""

        # Layout rules: text fits in left 60% starting at x=50
        max_w = (1280 - 100) if face_path is None else (int(1280 * 0.60) - 50)
        fs1 = _fit_fontsize(line1, 160, max_w, font_file)
        fs2 = _fit_fontsize(line2, 110, max_w, font_file)

        dt1 = (
            f"drawtext={font_arg}textfile={tf1_name}:"
            f"fontsize={fs1}:fontcolor=#FFD400:borderw=10:bordercolor=black:x=50:y=210"
        )
        dt2 = (
            f"drawtext={font_arg}textfile={tf2_name}:"
            f"fontsize={fs2}:fontcolor=white:borderw=10:bordercolor=black:x=50:y=400"
        )

        if face_path is not None:
            filter_complex = (
                "[0]scale=1280:720,boxblur=6,eq=brightness=-0.25[bg];"
                # Phone photos are landscape with the face centred: keep a centred 4:5 portrait.
                "[1]crop='min(iw,ih*4/5)':ih,scale=-1:760,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='min(255, 255*X/(W*0.35))'[face];"
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

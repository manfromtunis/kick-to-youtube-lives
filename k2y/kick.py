"""Kick VOD discovery, HLS rendition resolution, and video downloading."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .media import FFMPEG

logger = logging.getLogger("k2y")
logger.addHandler(logging.NullHandler())

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"


class KickError(Exception):
    """Raised when Kick API or video operations fail."""


def list_vods(channel: str) -> list[dict]:
    """Fetch VOD list for a channel from the unofficial Kick API."""
    url = f"https://kick.com/api/v2/channels/{channel}/videos"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise KickError(f"Kick API HTTP error {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise KickError(f"Kick API network error: {e.reason}") from e
    except Exception as e:
        raise KickError(f"Failed to fetch Kick VODs: {e}") from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise KickError(f"Invalid JSON from Kick API: {e}") from e

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "videos" in data and isinstance(data["videos"], list):
        items = data["videos"]
    elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        items = data["data"]
    else:
        raise KickError("Unexpected JSON format from Kick API: expected list of videos")

    required_keys = ("id", "session_title", "start_time", "duration", "source", "is_live")
    result = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            logger.warning(f"skipping malformed Kick item {idx}: not a dict")
            continue

        item_id = item.get("id", idx)
        missing_keys = [k for k in required_keys if k not in item]
        if missing_keys:
            logger.warning(f"skipping malformed Kick item {item_id}: missing required key '{missing_keys[0]}'")
            continue

        duration = item.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            logger.warning(f"skipping malformed Kick item {item_id}: duration is not a positive number ({duration})")
            continue

        categories = item.get("categories")
        category = (
            categories[0].get("name")
            if isinstance(categories, list) and len(categories) > 0 and isinstance(categories[0], dict)
            else None
        )

        result.append(
            {
                "id": item["id"],
                "title": item["session_title"],
                "started_at": item["start_time"],
                "duration_s": int(duration // 1000),
                "source_url": item["source"],
                "category": category,
                "is_live": item["is_live"],
            }
        )

    return result


def best_rendition(master_url: str, master_text: str) -> str:
    """Parse an HLS master playlist and return the absolute URL of the highest-bandwidth stream."""
    lines = [line.strip() for line in master_text.splitlines()]
    variants = []
    current_bw: int | None = None

    for line in lines:
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            current_bw = int(m.group(1)) if m else 0
        elif not line.startswith("#"):
            if current_bw is not None:
                variants.append((current_bw, line))
                current_bw = None

    if not variants:
        raise KickError("No stream variants found in master playlist")

    best_uri = max(variants, key=lambda v: v[0])[1]
    return urllib.parse.urljoin(master_url, best_uri)


def probe_duration(path: Path) -> float:
    """Return container duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise KickError(f"ffprobe timed out after 120 seconds for {path}")
    if proc.returncode != 0:
        raise KickError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as e:
        raise KickError(f"Failed to parse duration from ffprobe output: {proc.stdout.strip()}") from e


def download(vod: dict, dest: Path) -> Path:
    """Download the highest-rendition HLS stream for a VOD and verify its duration."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_file = Path(f"{dest}.part")

    master_url = vod.get("source_url")
    if not master_url:
        raise KickError("VOD missing source_url")

    try:
        req = urllib.request.Request(master_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            master_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise KickError(f"HTTP error fetching master playlist: {e.code}") from e
    except urllib.error.URLError as e:
        raise KickError(f"Network error fetching master playlist: {e.reason}") from e
    except Exception as e:
        raise KickError(f"Failed to fetch master playlist: {e}") from e

    rendition = best_rendition(master_url, master_text)

    cmd = [
        *FFMPEG,
        "-i", rendition,
        "-c", "copy",
        "-f", "mp4",
        str(part_file),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=28800)
    except subprocess.TimeoutExpired:
        part_file.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise KickError("ffmpeg download timed out after 8 hours")

    if proc.returncode != 0:
        part_file.unlink(missing_ok=True)
        stderr_tail = proc.stderr.strip().splitlines()
        tail = "\n".join(stderr_tail[-5:]) if stderr_tail else "unknown error"
        raise KickError(f"ffmpeg download failed: {tail}")

    expected_duration = vod.get("duration_s")
    if expected_duration is None or expected_duration <= 0:
        part_file.unlink(missing_ok=True)
        raise KickError("VOD missing or invalid duration_s for duration check")

    try:
        actual_duration = probe_duration(part_file)
    except Exception as e:
        part_file.unlink(missing_ok=True)
        raise KickError(f"Failed to probe duration of downloaded file: {e}") from e

    diff = abs(actual_duration - expected_duration)
    if (diff / expected_duration) > 0.01:
        part_file.unlink(missing_ok=True)
        raise KickError(
            f"Duration mismatch: probed {actual_duration:.1f}s vs expected {expected_duration}s (>1% difference)"
        )

    part_file.replace(dest)
    return dest

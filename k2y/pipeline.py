"""Pipeline state machine and execution loop for k2y."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Callable

from . import config, db, kick, media, segment, youtube
from .kick import probe_duration

logger = logging.getLogger("k2y")
_logging_configured = False


class StopRun(Exception):
    """Raised to stop the entire pipeline run immediately without counting attempts."""


def setup_logging(work_dir: Path) -> None:
    """Configure stdlib logging to stderr (if TTY) and a RotatingFileHandler at work_dir/k2y.log.

    5 MB max size, 3 backups, format '%(asctime)s %(levelname)s %(message)s'.
    Idempotent (configures once).
    """
    global _logging_configured
    if _logging_configured:
        return

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    log_file = work_dir / "k2y.log"

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if sys.stderr and hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    _logging_configured = True


_ensure_since = config.ensure_since


def _thumb_moment_for_part(part: dict) -> int:
    km = part.get("key_moments") or []
    if km and "at_s" in km[0]:
        return int(km[0]["at_s"])
    return int(part.get("start_s", 0)) + 60


def expected_upload_count(plan: list) -> int:
    """Compute expected number of upload rows from a plan."""
    return len(plan) + 1 if len(plan) >= 2 else 1


def step_download(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    dest = cfg.vod_dir(vod_id) / "source.mp4"
    kick.download(dict(vod), dest)
    dur = int(kick.probe_duration(dest))
    db.set_duration(conn, vod_id, dur)
    return "downloaded"


def step_plan(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    src = cfg.vod_dir(vod_id) / "source.mp4"
    if not src.exists():
        return "new"
    if vod["plan_json"]:
        return "planned"
    plan = segment.plan(
        src,
        cfg.vod_dir(vod_id),
        int(vod["duration_s"]),
        int(cfg.min_part_minutes),
        str(cfg.model),
    )
    db.delete_uploads(conn, vod_id)
    db.save_plan(conn, vod_id, plan)
    return "planned"


def step_cut(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    src = cfg.vod_dir(vod_id) / "source.mp4"
    if not src.exists():
        return "new"
    plan_raw = vod["plan_json"]
    if not plan_raw:
        return "downloaded"
    plan = json.loads(plan_raw)

    vod_dir = cfg.vod_dir(vod_id)
    vod_dir.mkdir(parents=True, exist_ok=True)

    # Full VOD (idx 0)
    full_thumb_at = _thumb_moment_for_part(plan[0]) if plan else 60
    full_title = media.full_title(vod["title"], vod["started_at"])
    thumb_0 = vod_dir / "thumb_0.jpg"
    media.thumbnail(src, full_thumb_at, full_title, thumb_0)
    db.upsert_upload(
        conn,
        vod_id=vod_id,
        idx=0,
        title=full_title,
        start_s=0,
        end_s=int(vod["duration_s"]),
        file=src,
        thumb=thumb_0,
    )

    # Parts (idx 1..n) if 2 or more parts
    if len(plan) >= 2:
        for i, part in enumerate(plan, 1):
            part_file = vod_dir / f"part_{i}.mp4"
            media.cut(src, int(part["start_s"]), int(part["end_s"]), part_file)
            p_title = media.part_title(part.get("title", ""), i, len(plan))
            p_thumb = vod_dir / f"thumb_{i}.jpg"
            p_moment = _thumb_moment_for_part(part)
            media.thumbnail(src, p_moment, p_title, p_thumb)
            db.upsert_upload(
                conn,
                vod_id=vod_id,
                idx=i,
                title=p_title,
                start_s=int(part["start_s"]),
                end_s=int(part["end_s"]),
                file=part_file,
                thumb=p_thumb,
            )

    return "cut"


def step_cut_ready(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    src = cfg.vod_dir(vod_id) / "source.mp4"
    plan = json.loads(vod["plan_json"]) if vod["plan_json"] else []
    expected = expected_upload_count(plan)
    ups = db.uploads(conn, vod_id)
    indices = {u["idx"] for u in ups}

    needs_cutting = False
    if indices != set(range(expected)):
        needs_cutting = True
    else:
        for u in ups:
            if u["file"] and not Path(u["file"]).exists():
                needs_cutting = True
                break
            if u["thumb"] and not Path(u["thumb"]).exists():
                needs_cutting = True
                break

    if needs_cutting:
        if not src.exists():
            return "new"
        return "planned"

    return "uploading"


def step_upload(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    src = cfg.vod_dir(vod_id) / "source.mp4"
    plan = json.loads(vod["plan_json"]) if vod["plan_json"] else []
    expected = expected_upload_count(plan)
    ups = db.uploads(conn, vod_id)
    indices = {u["idx"] for u in ups}

    needs_cutting = False
    if indices != set(range(expected)):
        needs_cutting = True
    else:
        for u in ups:
            if not u["youtube_id"] and u["file"] and not Path(u["file"]).exists():
                needs_cutting = True
                break
            if not u["thumb_done"] and u["thumb"] and not Path(u["thumb"]).exists():
                needs_cutting = True
                break

    if needs_cutting:
        if not src.exists():
            return "new"
        return "planned"

    non_game_categories = {
        "Just Chatting",
        "IRL",
        "Cooking",
        "Music",
        "Art",
        "Talk Shows & Podcasts",
        "Science & Technology",
        "Software & Game Development",
        None,
        "",
    }
    cat = vod["category"]
    category_id = "22" if cat in non_game_categories else "20"

    row_0 = next((u for u in ups if u["idx"] == 0), None)
    full_youtube_id = row_0["youtube_id"] if row_0 and row_0["youtube_id"] else None

    for row in ups:
        video_id = row["youtube_id"]
        if not video_id:
            if row["idx"] == 0:
                desc = media.full_description(plan, cfg.channel)
            else:
                part_idx = row["idx"] - 1
                part_dict = plan[part_idx] if part_idx < len(plan) else {}
                desc = media.part_description(part_dict, full_youtube_id, cfg.channel)

            meta = {
                "title": row["title"],
                "description": desc,
                "category_id": category_id,
            }

            def on_session(session_url: str) -> None:
                db.update_upload(conn, vod_id, row["idx"], upload_session=session_url)

            video_id = youtube.upload(
                Path(row["file"]),
                meta,
                row["upload_session"],
                on_session,
            )
            db.update_upload(conn, vod_id, row["idx"], youtube_id=video_id)
            if row["idx"] == 0:
                full_youtube_id = video_id

        if not row["thumb_done"] and row["thumb"]:
            youtube.set_thumbnail(video_id, Path(row["thumb"]))
            db.update_upload(conn, vod_id, row["idx"], thumb_done=1)

    vod_dir = cfg.vod_dir(vod_id)
    if vod_dir.exists():
        try:
            shutil.rmtree(vod_dir)
        except Exception as e:
            logger.warning(f"Failed to remove VOD directory {vod_dir}: {e}")
    return "done"


STEPS: dict[str, Callable] = {
    "new": step_download,
    "downloaded": step_plan,
    "planned": step_cut,
    "cut": step_cut_ready,
    "uploading": step_upload,
}


def advance(conn, cfg: config.Config, vod_id: int) -> None:
    while True:
        vod = db.get_vod(conn, vod_id)
        if vod is None:
            break
        state = vod["state"]
        step_fn = STEPS.get(state)
        if step_fn is None:
            break
        try:
            next_state = step_fn(conn, cfg, vod)
        except StopRun:
            raise
        except Exception as e:
            if isinstance(e, (config.MissingSecret, youtube.AuthRequired)):
                logger.error(f"{e} - run `py -m k2y auth`")
                raise StopRun(str(e)) from e

            if isinstance(e, (youtube.QuotaExceeded, segment.DailyQuota)):
                raise StopRun(str(e)) from e

            err_msg = f"{type(e).__name__}: {e}"
            logger.error(f"vod {vod_id}: step {state} error: {err_msg}")
            db.record_error(conn, vod_id, err_msg)
            break

        if next_state:
            db.set_state(conn, vod_id, next_state)
            logger.info(f"vod {vod_id}: {state} -> {next_state}")


def _is_pid_running(pid: int) -> bool | None:
    """Check if process with pid is running.

    Returns True if alive, False if dead, None if cannot be determined.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        h_process = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not h_process:
            err = kernel32.GetLastError()
            if err == 5:  # ERROR_ACCESS_DENIED
                return True
            if err == 87:  # ERROR_INVALID_PARAMETER
                return False
            return None
        try:
            exit_code = wintypes.DWORD()
            success = kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
            if success:
                return bool(exit_code.value == still_active)
            return None
        finally:
            kernel32.CloseHandle(h_process)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None


def _acquire_lock(lock_path: Path) -> bool:
    """Acquire run lock via os.open(lock, O_CREAT|O_EXCL|O_WRONLY).

    Writes '<pid> <unix time>'.
    A lock whose PID is alive is NEVER stale, whatever its age.
    Age-based staleness (8h) applies only when the PID cannot be checked.
    An empty/unparseable lock file is stale only if older than 60 s.
    Steal a stale lock atomically: os.replace a freshly written temp lock over it,
    then re-read and verify it holds our PID; if not, give up.
    """
    now = int(time.time())
    pid = os.getpid()
    payload = f"{pid} {now}\n".encode("utf-8")

    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            stale = False
            try:
                raw = lock_path.read_text(encoding="utf-8").strip()
                parts = raw.split()
                if len(parts) >= 2:
                    lock_pid = int(parts[0])
                    lock_time = int(parts[1])
                    pid_alive = _is_pid_running(lock_pid)
                    if pid_alive is True:
                        stale = False
                    elif pid_alive is False:
                        stale = True
                    else:
                        stale = (now - lock_time > 8 * 3600)
                else:
                    mtime = lock_path.stat().st_mtime
                    stale = (now - mtime > 60)
            except Exception:
                try:
                    mtime = lock_path.stat().st_mtime
                    stale = (now - mtime > 60)
                except OSError:
                    stale = False

            if stale:
                temp_lock = lock_path.parent / f"k2y.lock.{pid}.{now}.tmp"
                try:
                    temp_lock.write_text(f"{pid} {now}\n", encoding="utf-8")
                    os.replace(temp_lock, lock_path)
                    content = lock_path.read_text(encoding="utf-8").strip().split()
                    if content and content[0] == str(pid):
                        return True
                except Exception:
                    pass
                finally:
                    try:
                        temp_lock.unlink(missing_ok=True)
                    except OSError:
                        pass
                return False
            return False
    return False


def _release_lock(lock_path: Path) -> None:
    try:
        if lock_path.exists():
            text = lock_path.read_text(encoding="utf-8").strip().split()
            if not text or text[0] == str(os.getpid()):
                lock_path.unlink()
    except OSError:
        pass


acquire_lock = _acquire_lock
release_lock = _release_lock


def run(cfg: config.Config | None = None) -> int:
    if cfg is None:
        cfg = config.load()

    setup_logging(cfg.work_dir)

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.work_dir / "k2y.lock"

    if not acquire_lock(lock_path):
        logger.info("another run in progress")
        return 0

    try:
        cfg = config.ensure_since(cfg)
        conn = db.connect(cfg.db_path)

        try:
            vods = kick.list_vods(cfg.channel)
            for v in vods:
                if not v.get("is_live"):
                    v_date = str(v.get("started_at", ""))[:10]
                    since_date = str(cfg.since)[:10] if cfg.since else ""
                    if not since_date or v_date >= since_date:
                        db.upsert_vod(conn, v)
        except kick.KickError as e:
            logger.warning(f"Kick discovery error: {e}")

        try:
            for row in db.pending_vods(conn):
                advance(conn, cfg, row["id"])
        except StopRun as e:
            logger.info(f"Run stopped: {e}")

        return 0
    finally:
        release_lock(lock_path)

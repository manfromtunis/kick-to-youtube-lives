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

from . import config, db, kick, media, youtube

logger = logging.getLogger("k2y")
_logging_configured = False


class StopRun(Exception):
    """Raised to stop the entire pipeline run immediately without counting attempts."""


def setup_logging(work_dir: Path) -> None:
    """Configure stdlib logging to stderr and a RotatingFileHandler at work_dir/k2y.log.

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

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(stderr_handler)
    root_logger.addHandler(file_handler)

    _logging_configured = True


_ensure_since = config.ensure_since


def _thumb_moment_for_part(part: dict) -> int:
    km = part.get("key_moments") or []
    if km and "at_s" in km[0]:
        return int(km[0]["at_s"])
    return int(part.get("start_s", 0)) + 60


def step_download(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    dest = cfg.vod_dir(vod_id) / "source.mp4"
    kick.download(dict(vod), dest)
    return "downloaded"


def step_plan(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    src = cfg.vod_dir(vod_id) / "source.mp4"
    if not src.exists():
        return "new"
    from k2y import segment
    plan = segment.plan(
        src,
        cfg.vod_dir(vod_id),
        int(vod["duration_s"]),
        int(cfg.min_part_minutes),
        str(cfg.model),
    )
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
    ups = db.uploads(conn, vod_id)
    for u in ups:
        if u["file"] and not Path(u["file"]).exists():
            return "new"
        if u["thumb"] and not Path(u["thumb"]).exists():
            return "new"
    return "uploading"


def step_upload(conn, cfg: config.Config, vod) -> str:
    vod_id = vod["id"]
    ups = db.uploads(conn, vod_id)
    if not ups:
        return "planned"

    for u in ups:
        if not u["youtube_id"] and u["file"] and not Path(u["file"]).exists():
            return "new"
        if not u["thumb_done"] and u["thumb"] and not Path(u["thumb"]).exists():
            return "new"

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

    plan = json.loads(vod["plan_json"]) if vod["plan_json"] else []

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

    shutil.rmtree(cfg.vod_dir(vod_id), ignore_errors=True)
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
            if isinstance(e, config.MissingSecret):
                logger.error(str(e))
                raise StopRun(str(e)) from e

            daily_quota_cls = None
            if "k2y.segment" in sys.modules:
                daily_quota_cls = getattr(sys.modules["k2y.segment"], "DailyQuota", None)
            is_daily_quota = (
                (daily_quota_cls is not None and isinstance(e, daily_quota_cls))
                or (type(e).__name__ == "DailyQuota")
            )
            if isinstance(e, youtube.QuotaExceeded) or is_daily_quota:
                raise StopRun(str(e)) from e

            err_msg = f"{type(e).__name__}: {e}"
            logger.error(f"vod {vod_id}: step {state} error: {err_msg}")
            db.record_error(conn, vod_id, err_msg)
            break

        if next_state:
            db.set_state(conn, vod_id, next_state)
            logger.info(f"vod {vod_id}: {state} -> {next_state}")


def _is_pid_running(pid: int) -> bool:
    """Check if process with pid is running.

    On POSIX, uses os.kill(pid, 0).
    On Windows, uses ctypes OpenProcess and GetExitCodeProcess.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows, query process status via OpenProcess and GetExitCodeProcess.
        import ctypes
        from ctypes import wintypes
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        h_process = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not h_process:
            return False
        try:
            exit_code = wintypes.DWORD()
            success = kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
            return bool(success and exit_code.value == still_active)
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
            return False


def _acquire_lock(lock_path: Path) -> bool:
    """Acquire run lock via os.open(lock, O_CREAT|O_EXCL|O_WRONLY).

    Writes '<pid> <unix time>'. If the lock exists and is older than 8 hours
    or its PID is not running, replace it.
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
            # Check if stale
            stale = False
            try:
                raw = lock_path.read_text(encoding="utf-8").strip()
                parts = raw.split()
                if len(parts) >= 2:
                    lock_pid = int(parts[0])
                    lock_time = int(parts[1])
                    if (now - lock_time > 8 * 3600) or not _is_pid_running(lock_pid):
                        stale = True
                else:
                    stale = True
            except Exception:
                stale = True

            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            return False
    return False


def run(cfg: config.Config | None = None) -> int:
    if cfg is None:
        cfg = config.load()

    setup_logging(cfg.work_dir)

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.work_dir / "k2y.lock"

    if not _acquire_lock(lock_path):
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
        try:
            if lock_path.exists():
                text = lock_path.read_text(encoding="utf-8").strip().split()
                if not text or text[0] == str(os.getpid()):
                    lock_path.unlink()
        except OSError:
            pass

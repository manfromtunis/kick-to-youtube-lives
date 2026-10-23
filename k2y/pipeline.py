"""Pipeline state machine and execution loop for k2y."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

from . import config, db, kick, media, segment, youtube

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
    res = kick.download(dict(vod), dest)
    if isinstance(res, tuple) and len(res) >= 2 and isinstance(res[1], (int, float)):
        dur = int(res[1])
    elif isinstance(res, (int, float)):
        dur = int(res)
    else:
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

    expected = expected_upload_count(plan)
    existing_uploads = db.uploads(conn, vod_id)
    for r in existing_uploads:
        if r["idx"] >= expected:
            if r["youtube_id"] or r["upload_session"]:
                id_val = r["youtube_id"] or r["upload_session"]
                raise RuntimeError(
                    f"stale upload row idx {r['idx']} already on YouTube as {id_val}; resolve manually"
                )
            with conn:
                conn.execute(
                    "DELETE FROM uploads WHERE vod_id = ? AND idx = ?",
                    (vod_id, r["idx"]),
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

STATE_ORDER: dict[str, int] = {
    "new": 0,
    "downloaded": 1,
    "planned": 2,
    "cut": 3,
    "uploading": 4,
    "done": 5,
}


def advance(conn, cfg: config.Config, vod_id: int) -> None:
    transitions = 0
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
            if isinstance(e, youtube.AuthRequired):
                logger.error(str(e))
                raise StopRun(str(e)) from e

            if isinstance(e, config.MissingSecret):
                err_text = str(e)
                if "youtube_token.json" in err_text or "client_secret.json" in err_text:
                    logger.error(f"{err_text} - run `py -m k2y auth`")
                else:
                    logger.error(err_text)
                raise StopRun(err_text) from e

            if hasattr(youtube, "ThumbnailsForbidden") and isinstance(e, youtube.ThumbnailsForbidden):
                logger.error(str(e))
                raise StopRun(str(e)) from e

            if isinstance(e, (youtube.QuotaExceeded, segment.DailyQuota)):
                raise StopRun(str(e)) from e

            err_msg = f"{type(e).__name__}: {e}"
            logger.error(f"vod {vod_id}: step {state} error: {err_msg}")
            db.record_error(conn, vod_id, err_msg)
            break

        if next_state:
            is_forward = STATE_ORDER.get(next_state, -1) > STATE_ORDER.get(state, -1)
            db.set_state(conn, vod_id, next_state, reset_attempts=is_forward)
            logger.info(f"vod {vod_id}: {state} -> {next_state}")
            transitions += 1
            if transitions >= 20:
                err_msg = "too many state transitions"
                logger.error(f"vod {vod_id}: {err_msg}")
                db.record_error(conn, vod_id, err_msg)
                break


_OPEN_LOCKS: dict[Path, list[Any]] = {}


def acquire_lock(lock_path: Path) -> bool:
    """Acquire run lock using OS lock on work_dir/k2y.lock.

    Windows: msvcrt.locking(fd, LK_NBLCK, 1) (seek 0 first)
    POSIX: fcntl.flock(fd, LOCK_EX | LOCK_NB)
    Returns True if acquired, False if already held.
    """
    if not isinstance(lock_path, Path):
        lock_path = Path(lock_path)
    try:
        lock_path = lock_path.resolve()
    except (NotImplementedError, OSError):
        pass

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    fd = f.fileno()
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        f.close()
        return False

    _OPEN_LOCKS.setdefault(lock_path, []).append(f)
    return True


def release_lock(lock_path: Path | Any) -> None:
    """Release run lock (unlock + close; never delete the file)."""
    if hasattr(lock_path, "fileno"):
        f = lock_path
        for p, handles in list(_OPEN_LOCKS.items()):
            if f in handles:
                handles.remove(f)
                if not handles:
                    del _OPEN_LOCKS[p]
    else:
        if not isinstance(lock_path, Path):
            lock_path = Path(lock_path)
        try:
            path = lock_path.resolve()
        except (NotImplementedError, OSError):
            path = lock_path

        handles = _OPEN_LOCKS.get(path)
        if not handles:
            return
        f = handles.pop()
        if not handles:
            del _OPEN_LOCKS[path]

    fd = f.fileno()
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except (BlockingIOError, OSError):
        pass
    finally:
        f.close()


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
                        try:
                            db.upsert_vod(conn, v)
                        except Exception as e:
                            v_id = v.get("id") if isinstance(v, dict) else "?"
                            logger.warning(f"skipping Kick VOD {v_id}: {e}")
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

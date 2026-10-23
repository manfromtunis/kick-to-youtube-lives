"""Command-line interface for k2y."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

from . import config, db, kick, pipeline, segment, youtube

REPO_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("k2y")


def _print_err(*args: Any, **kwargs: Any) -> None:
    if sys.stderr is not None:
        try:
            print(*args, file=sys.stderr, **kwargs)
        except Exception:
            pass


def _determine_work_dir() -> Path:
    cfg_file = config.CONFIG_DIR / "config.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and "work_dir" in data:
                return Path(data["work_dir"]).expanduser()
        except Exception:
            try:
                m = re.search(r'"work_dir"\s*:\s*"([^"]+)"', cfg_file.read_text(encoding="utf-8-sig"))
                if m:
                    return Path(m.group(1)).expanduser()
            except Exception:
                pass
    return Path.home() / ".local/share/k2y"


def _vod_or_err(cfg: config.Config, vod_id: int):
    conn = db.connect(cfg.db_path)
    vod = db.get_vod(conn, vod_id)
    if not vod:
        _print_err(f"VOD {vod_id} not found in database")
        return conn, None
    return conn, vod


def _is_windows() -> bool:
    return os.name == "nt"


def _with_lock(cfg: config.Config, action):
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.work_dir / "k2y.lock"
    if not pipeline.acquire_lock(lock_path):
        _print_err("a run is in progress, try again later")
        return 1
    try:
        return action()
    finally:
        pipeline.release_lock(lock_path)


def _plan_loads(plan_raw: str | None) -> bool:
    if not plan_raw:
        return False
    try:
        pipeline.load_plan_data(plan_raw)
        return True
    except ValueError:  # includes json.JSONDecodeError
        return False


def main(argv: list[str] | None = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="k2y",
        description="Kick to YouTube live VOD pipeline",
    )
    subparsers = parser.add_subparsers(dest="command")

    # run
    subparsers.add_parser("run", help="Run the pipeline")

    # status
    p_status = subparsers.add_parser("status", help="Show pipeline status")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")

    # plan
    p_plan = subparsers.add_parser("plan", help="Plan segmentation without uploading")
    p_plan.add_argument("vod_id", type=int, help="Kick VOD ID")

    # retry
    p_retry = subparsers.add_parser("retry", help="Retry a failed or errored VOD")
    p_retry.add_argument("vod_id", type=int, help="Kick VOD ID")
    p_retry.add_argument(
        "--restart-upload",
        action="store_true",
        help="Clear unfinished upload sessions on retry",
    )

    # set-upload
    p_set_upload = subparsers.add_parser(
        "set-upload",
        help="Record that an upload part is already on YouTube",
    )
    p_set_upload.add_argument("vod_id", type=int, help="Kick VOD ID")
    p_set_upload.add_argument("idx", type=int, help="Upload part index")
    p_set_upload.add_argument("youtube_id", type=str, help="YouTube video ID")

    # skip
    p_skip = subparsers.add_parser("skip", help="Skip a VOD")
    p_skip.add_argument("vod_id", type=int, help="Kick VOD ID")

    # backfill
    p_backfill = subparsers.add_parser("backfill", help="Backfill a VOD before since date")
    p_backfill.add_argument("vod_id", type=int, help="Kick VOD ID")

    # auth
    p_auth = subparsers.add_parser("auth", help="Authorize YouTube OAuth")
    p_auth.add_argument("--paste", action="store_true", help="Authorize using pasted redirect URL (headless)")

    # install-timer
    subparsers.add_parser(
        "install-timer",
        help="Install Task Scheduler task on Windows or print crontab line",
    )

    # refresh-metadata
    p_refresh = subparsers.add_parser(
        "refresh-metadata",
        help="Rebuild descriptions and update YouTube metadata for published VODs",
    )
    p_refresh.add_argument("vod_id", type=int, nargs="?", default=None, help="Kick VOD ID")
    p_refresh.add_argument("--all", action="store_true", help="Refresh all VODs in done state")
    p_refresh.add_argument("--force", action="store_true", help="Re-send videos already at the current layout")

    args = parser.parse_args(argv)
    if not args.command:
        if sys.stderr is not None:
            parser.print_help(file=sys.stderr)
        return 2

    try:
        try:
            cfg = config.load()
            pipeline.setup_logging(cfg.work_dir)
        except config.MissingSecret as e:
            pipeline.setup_logging(_determine_work_dir())
            logger.exception(f"Missing secret: {e}")
            _print_err(e)
            return 1
        except Exception as e:
            pipeline.setup_logging(_determine_work_dir())
            logger.exception(f"Failed to load configuration: {e}")
            _print_err(f"Error: {e}")
            return 1

        if args.command == "run":
            return pipeline.run(cfg)

        elif args.command == "status":
            conn = db.connect(cfg.db_path)
            rows = []
            for v in db.all_vods(conn):
                ups = db.uploads(conn, v["id"])
                done = sum(1 for u in ups if u["youtube_id"])
                rows.append({
                    "id": v["id"],
                    "date": str(v["started_at"])[:10],
                    "title": str(v["title"])[:40],
                    "state": v["state"],
                    "uploads": f"{done}/{len(ups)}",
                    "attempts": v["attempts"],
                    "error": str(v["error"] or "")[:60],
                })
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print(f"{'ID':<12} {'Date':<10} {'Title':<40} {'State':<12} {'Uploads':<10} {'Attempts':<8} {'Error':<60}")
                for r in rows:
                    print(f"{r['id']:<12} {r['date']:<10} {r['title']:<40} {r['state']:<12} {r['uploads']:<10} {r['attempts']:<8} {r['error']:<60}")
            return 0

        elif args.command == "plan":
            def do_plan():
                conn, vod = _vod_or_err(cfg, args.vod_id)
                if not vod:
                    return 1

                if vod["state"] in ("done", "skipped"):
                    _print_err(f"Cannot re-plan VOD {args.vod_id}: state is {vod['state']}")
                    return 1

                ups = db.uploads(conn, args.vod_id)
                if any(u["youtube_id"] or u["upload_session"] for u in ups):
                    _print_err(f"Cannot re-plan VOD {args.vod_id}: upload in progress or completed")
                    return 1

                source_file = cfg.vod_dir(args.vod_id) / "source.mp4"
                if not source_file.exists():
                    pipeline.step_download(conn, cfg, vod)
                    db.set_state(conn, args.vod_id, "downloaded")
                    vod = db.get_vod(conn, args.vod_id)

                vod_for_plan = dict(vod, plan_json=None)
                pipeline.step_plan(conn, cfg, vod_for_plan)
                db.set_state(conn, args.vod_id, "planned")
                vod = db.get_vod(conn, args.vod_id)
                parts, _ = pipeline.load_plan(vod["plan_json"]) if vod and vod["plan_json"] else ([], "")

                print(f"{'Start':<8} {'End':<8} {'Min':<6} {'Title'}")
                for p in parts:
                    mins = round((p["end_s"] - p["start_s"]) / 60, 1)
                    print(f"{p['start_s']:<8} {p['end_s']:<8} {mins:<6.1f} {p['title']}")

                return 0

            return _with_lock(cfg, do_plan)

        elif args.command == "retry":
            def do_retry():
                conn, vod = _vod_or_err(cfg, args.vod_id)
                if not vod:
                    return 1

                if args.restart_upload:
                    cleared = db.clear_upload_sessions(conn, args.vod_id)
                    print(f"Cleared {cleared} upload session(s) for VOD {args.vod_id}")

                if vod["state"] == "failed":
                    source_file = cfg.vod_dir(args.vod_id) / "source.mp4"
                    if not source_file.exists():
                        target = "new"
                    elif not _plan_loads(vod["plan_json"]):
                        target = "downloaded"  # no plan or a corrupt one: plan again
                    else:
                        plan = pipeline.load_plan_data(vod["plan_json"])["parts"]
                        expected = pipeline.expected_upload_count(plan)
                        ups = db.uploads(conn, args.vod_id)
                        indices = {u["idx"] for u in ups}
                        all_files_exist = (
                            indices == set(range(expected))
                            and all(
                                u["file"] and Path(u["file"]).exists()
                                and (not u["thumb"] or Path(u["thumb"]).exists())
                                for u in ups
                            )
                        )
                        if all_files_exist:
                            target = "cut"
                        else:
                            target = "planned"
                else:
                    target = vod["state"]

                db.reset(conn, args.vod_id, target)
                return 0

            return _with_lock(cfg, do_retry)

        elif args.command == "set-upload":
            def do_set_upload():
                conn = db.connect(cfg.db_path)
                row = db.get_upload(conn, args.vod_id, args.idx)
                if not row:
                    _print_err(f"Upload row {args.idx} for VOD {args.vod_id} does not exist")
                    return 1
                db.set_upload_youtube_id(conn, args.vod_id, args.idx, args.youtube_id)
                print(f"Recorded VOD {args.vod_id} part {args.idx} as {args.youtube_id}")
                return 0

            return _with_lock(cfg, do_set_upload)

        elif args.command == "skip":
            def do_skip():
                conn, vod = _vod_or_err(cfg, args.vod_id)
                if not vod:
                    return 1
                vod_dir = cfg.vod_dir(args.vod_id)
                if vod_dir.exists():
                    try:
                        shutil.rmtree(vod_dir)
                    except Exception as e:
                        logger.warning(f"Failed to remove VOD directory {vod_dir}: {e}")
                db.set_state(conn, args.vod_id, "skipped")
                return 0

            return _with_lock(cfg, do_skip)

        elif args.command == "backfill":
            def do_backfill():
                conn = db.connect(cfg.db_path)
                vods = kick.list_vods(cfg.channel)
                match = next((v for v in vods if v["id"] == args.vod_id), None)
                if not match:
                    _print_err(f"VOD {args.vod_id} not found in channel {cfg.channel}")
                    return 1
                inserted = db.upsert_vod(conn, match)
                if inserted:
                    print(f"Backfilled VOD {args.vod_id}: {match['title']}")
                else:
                    row = db.get_vod(conn, args.vod_id)
                    st = row["state"] if row else "unknown"
                    print(f"VOD {args.vod_id} already known (state {st})")
                return 0

            return _with_lock(cfg, do_backfill)

        elif args.command == "auth":
            def do_auth():
                youtube.auth(paste=args.paste)
                return 0

            return _with_lock(cfg, do_auth)

        elif args.command == "refresh-metadata":
            if not args.all and args.vod_id is None:
                _print_err("Error: must specify vod_id or --all")
                return 2
            if args.all and args.vod_id is not None:
                _print_err("Error: cannot specify both vod_id and --all")
                return 2

            def do_refresh():
                conn = None
                try:
                    if args.all:
                        conn = db.connect(cfg.db_path)
                        vods = [v for v in db.all_vods(conn) if v["state"] == "done"]
                        if not vods:
                            print("No VODs in done state to refresh.")
                            return 0
                    else:
                        conn, vod = _vod_or_err(cfg, args.vod_id)
                        if not vod:
                            return 1
                        if vod["state"] != "done":
                            _print_err(f"Cannot refresh metadata for VOD {args.vod_id}: state is {vod['state']} (must be 'done')")
                            return 1
                        vods = [vod]

                    had_error = False
                    for v in vods:
                        try:
                            print(f"Refreshing metadata for VOD {v['id']} ({v['title']})...")
                            refused = pipeline.refresh_metadata(conn, cfg, v["id"], force=args.force)
                            if refused:
                                had_error = True
                                _print_err(f"VOD {v['id']}: YouTube refused {refused} video(s), see k2y.log")
                            else:
                                print(f"Successfully refreshed metadata for VOD {v['id']}")
                        except pipeline.StopRun as sr:
                            _print_err(f"Stopped: {sr}")
                            return 1
                        except (youtube.QuotaExceeded, youtube.AuthRequired, segment.DailyQuota) as e:
                            _print_err(f"Error on VOD {v['id']}: {e}")
                            return 1
                        except Exception as e:
                            logger.exception(f"Error refreshing metadata for VOD {v['id']}: {e}")
                            _print_err(f"Error refreshing metadata for VOD {v['id']}: {e}")
                            if args.all:
                                had_error = True
                                continue
                            return 1
                    return 1 if had_error else 0
                finally:
                    if args.all and conn is not None:
                        conn.close()

            return _with_lock(cfg, do_refresh)

        elif args.command == "install-timer":
            if _is_windows():
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                python_exe = str(pythonw) if os.path.exists(pythonw) else sys.executable
                task_script = str(REPO_DIR / "k2y-task.pyw").replace("/", "\\")
                tr_val = f'"{python_exe}" "{task_script}"'
                subprocess.run(
                    [
                        "schtasks",
                        "/Create",
                        "/F",
                        "/SC",
                        "MINUTE",
                        "/MO",
                        "10",
                        "/TN",
                        "k2y",
                        "/TR",
                        tr_val,
                    ],
                    check=True,
                )
                print("Task 'k2y' scheduled successfully.")
                print("To check: schtasks /Query /TN k2y")
                print("To remove: schtasks /Delete /TN k2y /F")
                print("Battery note: Windows Task Scheduler runs on AC power only by default; see README to allow running on battery.")
            else:
                task_log = cfg.work_dir / "k2y-task.log"
                repo_q = shlex.quote(str(REPO_DIR))
                py_q = shlex.quote(sys.executable)
                log_q = shlex.quote(str(task_log))
                print(f"*/10 * * * * cd {repo_q} && {py_q} -m k2y run >> {log_q} 2>&1")
            return 0

    except config.MissingSecret as e:
        logger.exception(f"Missing secret: {e}")
        _print_err(e)
        return 1
    except Exception as e:
        logger.exception(f"Error: {e}")
        _print_err(f"Error: {e}")
        return 1

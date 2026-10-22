"""Command-line interface for k2y."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from . import config, db, kick, pipeline, youtube

REPO_DIR = Path(__file__).resolve().parent.parent


def _is_windows() -> bool:
    return os.name == "nt"


def main(argv: list[str] | None = None) -> int:
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

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(file=sys.stderr)
        return 2

    cfg = config.load()

    try:
        if args.command == "run":
            return pipeline.run(cfg)

        elif args.command == "status":
            conn = db.connect(cfg.db_path)
            cur = conn.execute("SELECT * FROM vods ORDER BY started_at ASC")
            vods = cur.fetchall()
            if args.json:
                out = []
                for v in vods:
                    ups = db.uploads(conn, v["id"])
                    total = len(ups)
                    done = sum(1 for u in ups if u["youtube_id"])
                    out.append({
                        "id": v["id"],
                        "date": str(v["started_at"])[:10],
                        "title": str(v["title"])[:40],
                        "state": v["state"],
                        "uploads": f"{done}/{total}",
                        "attempts": v["attempts"],
                        "error": str(v["error"] or "")[:60],
                    })
                print(json.dumps(out, indent=2))
            else:
                print(f"{'ID':<12} {'Date':<10} {'Title':<40} {'State':<12} {'Uploads':<10} {'Attempts':<8} {'Error':<60}")
                for v in vods:
                    ups = db.uploads(conn, v["id"])
                    total = len(ups)
                    done = sum(1 for u in ups if u["youtube_id"])
                    date_str = str(v["started_at"])[:10]
                    title_str = str(v["title"])[:40]
                    err_str = str(v["error"] or "")[:60]
                    print(f"{v['id']:<12} {date_str:<10} {title_str:<40} {v['state']:<12} {f'{done}/{total}':<10} {v['attempts']:<8} {err_str:<60}")
            return 0

        elif args.command == "plan":
            conn = db.connect(cfg.db_path)
            vod = db.get_vod(conn, args.vod_id)
            if not vod:
                print(f"VOD {args.vod_id} not found in database", file=sys.stderr)
                return 1

            source_file = cfg.vod_dir(args.vod_id) / "source.mp4"
            if not source_file.exists():
                kick.download(dict(vod), source_file)
                db.set_state(conn, args.vod_id, "downloaded")

            if "k2y.segment" in sys.modules:
                segment = sys.modules["k2y.segment"]
            else:
                from k2y import segment
            parts = segment.plan(
                source_file,
                cfg.vod_dir(args.vod_id),
                int(vod["duration_s"]),
                int(cfg.min_part_minutes),
                str(cfg.model),
            )

            print(f"{'Start':<8} {'End':<8} {'Min':<6} {'Title'}")
            for p in parts:
                mins = round((p["end_s"] - p["start_s"]) / 60, 1)
                print(f"{p['start_s']:<8} {p['end_s']:<8} {mins:<6.1f} {p['title']}")

            db.save_plan(conn, args.vod_id, parts)
            db.set_state(conn, args.vod_id, "planned")
            with conn:
                conn.execute(
                    "DELETE FROM uploads WHERE vod_id = ? AND youtube_id IS NULL",
                    (args.vod_id,),
                )
            return 0

        elif args.command == "retry":
            conn = db.connect(cfg.db_path)
            vod = db.get_vod(conn, args.vod_id)
            if not vod:
                print(f"VOD {args.vod_id} not found in database", file=sys.stderr)
                return 1

            if vod["state"] == "failed":
                source_file = cfg.vod_dir(args.vod_id) / "source.mp4"
                if not source_file.exists():
                    target = "new"
                elif not vod["plan_json"]:
                    target = "downloaded"
                elif len(db.uploads(conn, args.vod_id)) == 0:
                    target = "planned"
                else:
                    target = "cut"
            else:
                target = vod["state"]

            db.reset(conn, args.vod_id, target)
            return 0

        elif args.command == "skip":
            conn = db.connect(cfg.db_path)
            vod = db.get_vod(conn, args.vod_id)
            if not vod:
                print(f"VOD {args.vod_id} not found in database", file=sys.stderr)
                return 1
            db.set_state(conn, args.vod_id, "skipped")
            return 0

        elif args.command == "backfill":
            conn = db.connect(cfg.db_path)
            vods = kick.list_vods(cfg.channel)
            match = next((v for v in vods if v["id"] == args.vod_id), None)
            if not match:
                print(f"VOD {args.vod_id} not found in channel {cfg.channel}", file=sys.stderr)
                return 1
            db.upsert_vod(conn, match)
            print(f"Backfilled VOD {args.vod_id}: {match['title']}")
            return 0

        elif args.command == "auth":
            youtube.auth(paste=bool(getattr(args, "paste", False)))
            return 0

        elif args.command == "install-timer":
            repo_dir = REPO_DIR
            python_exe = sys.executable
            if _is_windows():
                cfg.work_dir.mkdir(parents=True, exist_ok=True)
                cmd_path = cfg.work_dir / "k2y-run.cmd"
                log_path = cfg.work_dir / "k2y.log"
                cmd_content = f'cd /d "{repo_dir}"\n"{python_exe}" -m k2y run >> "{log_path}" 2>&1\n'
                cmd_path.write_text(cmd_content, encoding="utf-8")
                subprocess.run(
                    ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "10", "/TN", "k2y", "/TR", str(cmd_path)],
                    check=True,
                )
                print("Task 'k2y' scheduled successfully.")
                print("To check: schtasks /Query /TN k2y")
                print("To remove: schtasks /Delete /TN k2y /F")
            else:
                log_path = cfg.work_dir / "k2y.log"
                print(f"*/10 * * * * cd {repo_dir} && {python_exe} -m k2y run >> {log_path} 2>&1")
            return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

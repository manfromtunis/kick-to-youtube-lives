"""Windows Task Scheduler launcher for k2y without a console window."""
import os
from pathlib import Path
import sys
import traceback

repo_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_dir))
os.chdir(repo_dir)

try:
    from k2y import cli
    cli.main(["run"])
except Exception:
    try:
        from k2y import config
        cfg = config.load()
        work_dir = cfg.work_dir
    except Exception:
        work_dir = Path.home() / ".local/share/k2y"

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        log_file = work_dir / "k2y.log"
        with open(log_file, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
    except Exception:
        pass

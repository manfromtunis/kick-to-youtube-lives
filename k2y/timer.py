"""Install the recurring k2y timer (Windows Task Scheduler or a crontab line)."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys


def is_windows() -> bool:
    return os.name == "nt"


def install_timer(
    repo_dir: Path,
    work_dir: Path,
    *,
    run=None,
) -> list[str]:
    """Install the 10-minute timer and return the lines to show the user.

    Windows creates the ``k2y`` Task Scheduler task. Linux and macOS only build the
    equivalent crontab line, because installing it would modify the user's crontab
    behind their back.
    """
    if run is None:
        run = subprocess.run

    if is_windows():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        python_exe = str(pythonw) if os.path.exists(pythonw) else sys.executable
        task_script = str(repo_dir / "k2y-task.pyw").replace("/", "\\")
        tr_val = f'"{python_exe}" "{task_script}"'
        run(
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
        return [
            "Task 'k2y' scheduled successfully.",
            "To check: schtasks /Query /TN k2y",
            "To remove: schtasks /Delete /TN k2y /F",
            "Battery note: Windows Task Scheduler runs on AC power only by default; "
            "see docs/SETUP.md to allow running on battery.",
        ]

    task_log = work_dir / "k2y-task.log"
    repo_q = shlex.quote(str(repo_dir))
    py_q = shlex.quote(sys.executable)
    log_q = shlex.quote(str(task_log))
    return [f"*/10 * * * * cd {repo_q} && {py_q} -m k2y run >> {log_q} 2>&1"]

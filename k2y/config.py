"""Paths, user config and secrets. Every other module reads settings from here only."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("K2Y_CONFIG_DIR", Path.home() / ".config/k2y"))


@dataclass(frozen=True)
class Config:
    channel: str = "randomguy42"
    work_dir: Path = Path.home() / ".local/share/k2y"
    model: str = "gemini-3.8-flash"
    min_part_minutes: int = 15
    since: str | None = None  # ISO date; None = set on first `k2y run`

    @property
    def db_path(self) -> Path:
        return self.work_dir / "k2y.db"

    def vod_dir(self, vod_id: int) -> Path:
        return self.work_dir / str(vod_id)


def load() -> Config:
    p = CONFIG_DIR / "config.json"
    raw = json.loads(p.read_text()) if p.exists() else {}
    if "work_dir" in raw:
        raw["work_dir"] = Path(raw["work_dir"]).expanduser()
    return Config(**raw)


def secret_path(name: str) -> Path:
    """name: 'gemini.key' | 'client_secret.json' | 'youtube_token.json'."""
    return CONFIG_DIR / name


def read_secret(name: str) -> str:
    p = secret_path(name)
    if not p.exists():
        raise SystemExit(f"missing secret {p} (see docs/superpowers/specs/2026-10-22-vod-pipeline-design.md section 5)")
    return p.read_text().strip()


def write_secret(name: str, text: str) -> None:
    p = secret_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)

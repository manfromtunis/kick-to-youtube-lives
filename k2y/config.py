from datetime import datetime, timezone
"""Paths, user config and secrets. Every other module reads settings from here only."""
import dataclasses
from dataclasses import dataclass
import json
import os
from pathlib import Path

class MissingSecret(RuntimeError):
    pass


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


def ensure_since(cfg: Config) -> Config:
    if cfg.since is not None:
        return cfg
    p = CONFIG_DIR / "config.json"
    raw = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text())
        except Exception:
            raw = {}
    stored_since = raw.get("since")
    if stored_since:
        return dataclasses.replace(cfg, since=str(stored_since))
    today = datetime.now(timezone.utc).date().isoformat()
    raw["since"] = today
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2))
    return dataclasses.replace(cfg, since=today)


def load() -> Config:
    p = CONFIG_DIR / "config.json"
    raw = json.loads(p.read_text()) if p.exists() else {}
    if "work_dir" in raw:
        raw["work_dir"] = Path(raw["work_dir"]).expanduser()
    valid_fields = {f.name for f in dataclasses.fields(Config)}
    filtered = {k: v for k, v in raw.items() if k in valid_fields}
    return Config(**filtered)


def secret_path(name: str) -> Path:
    """name: 'gemini.key' | 'client_secret.json' | 'youtube_token.json'."""
    return CONFIG_DIR / name


def read_secret(name: str) -> str:
    p = secret_path(name)
    if not p.exists():
        raise MissingSecret(f"missing secret {p} (see docs/superpowers/specs/2026-10-22-vod-pipeline-design.md section 5)")
    return p.read_text().strip()


def write_secret(name: str, text: str) -> None:
    p = secret_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)

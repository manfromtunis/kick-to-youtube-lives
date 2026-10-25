"""Paths, user config and secrets. Every other module reads settings from here only."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path


class MissingSecret(RuntimeError):
    pass


CONFIG_DIR = Path(os.environ.get("K2Y_CONFIG_DIR", Path.home() / ".config/k2y"))

DEFAULT_LINKS: tuple[tuple[str, str], ...] = (
    ("📸 Instagram", "https://www.instagram.com/ghmelek/"),
    ("🛠️ AuraStack", "https://aurastack.link/"),
    ("💸 Clip2Earn", "https://www.clip2earn.money/"),
    ("📱 Bakchich", "https://www.instagram.com/bakchich.app/"),
)


@dataclass(frozen=True)
class Config:
    channel: str = "randomguy42"
    work_dir: Path = Path.home() / ".local/share/k2y"
    model: str = "gemini-3.8-flash"
    min_part_minutes: int = 15
    since: str | None = None  # ISO date; None = set on first `k2y run`
    links: tuple | list = DEFAULT_LINKS

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
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise ValueError(f"Invalid JSON in config file {p}: {e}") from e
        if not isinstance(raw, dict):
            raise ValueError(f"Config file {p} must contain a JSON object, got {type(raw).__name__}")
    stored_since = raw.get("since")
    if stored_since:
        return dataclasses.replace(cfg, since=str(stored_since))
    today = datetime.now(timezone.utc).date().isoformat()
    raw["since"] = today
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return dataclasses.replace(cfg, since=today)


def load() -> Config:
    p = CONFIG_DIR / "config.json"
    raw = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    if "work_dir" in raw:
        raw["work_dir"] = Path(raw["work_dir"]).expanduser()
    valid_fields = {f.name for f in dataclasses.fields(Config)}
    filtered = {k: v for k, v in raw.items() if k in valid_fields}
    return Config(**filtered)


def secret_path(name: str, config_dir: Path | None = None) -> Path:
    """name: 'gemini.key' | 'client_secret.json' | 'youtube_token.json'."""
    return (config_dir or CONFIG_DIR) / name


def read_secret(name: str, config_dir: Path | None = None) -> str:
    p = secret_path(name, config_dir)
    if not p.exists():
        raise MissingSecret(f"missing secret {p} (see docs/SETUP.md)")
    return p.read_text(encoding="utf-8-sig").strip()


def write_secret(name: str, text: str, config_dir: Path | None = None) -> None:
    p = secret_path(name, config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp_file = p.parent / f"{name}.{os.getpid()}.tmp"
    fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp_file, p)
    except Exception:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise

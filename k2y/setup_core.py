"""Shared setup logic used by CLI and web wizards."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import config


class SetupError(RuntimeError):
    pass


def normalize_channel(raw: str) -> str:
    value = str(raw).strip()
    if not value:
        return ""
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(www\.)?kick\.com/", "", value, flags=re.IGNORECASE)
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = value.strip("/").lstrip("@").lower()
    value = value.split("/", 1)[0]
    if re.fullmatch(r"[a-z0-9_-]+", value):
        return value
    return ""


def read_client_secret_text(text: str) -> tuple[bool, str]:
    try:
        data = json.loads(text)
    except Exception as e:
        return False, f"That file is not valid JSON: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("installed"), dict):
        return False, "This is not a Desktop app OAuth client (missing 'installed').", 
    installed = data["installed"]
    if not installed.get("client_id") or not installed.get("client_secret"):
        return False, "The JSON is missing client_id or client_secret."
    return True, text


def validate_gemini_key(text: str) -> tuple[bool, str]:
    candidate = text.strip()
    if not candidate:
        return False, "The key cannot be empty."
    if len(candidate) < 20 or re.search(r"\s", candidate):
        return False, "That does not look like an API key. Copy the whole key, without spaces."
    return True, candidate


def load_existing_config(config_dir: Path) -> dict:
    path = Path(config_dir) / "config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_config(config_dir: Path, existing: dict, values: dict) -> Path:
    data = dict(existing)
    data.update(values)
    path = Path(config_dir) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_secret(name: str, text: str, config_dir: Path) -> Path:
    return config.write_secret(name, text, Path(config_dir))


def ensure_config_dir(config_dir: Path) -> Path:
    p = Path(config_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def faces_dir(config_dir: Path) -> Path:
    p = Path(config_dir) / "faces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_faces(config_dir: Path, files: list[tuple[str, bytes]]) -> list[Path]:
    out_dir = faces_dir(config_dir)
    saved = []
    for name, data in files:
        safe = os.path.basename(name)
        ext = Path(safe).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        dest = out_dir / safe
        if dest.exists():
            stem = Path(safe).stem
            for i in range(1, 100):
                cand = out_dir / f"{stem}_{i}{ext}"
                if not cand.exists():
                    dest = cand
                    break
        dest.write_bytes(data)
        try:
            os.chmod(dest, 0o644)
        except Exception:
            pass
        saved.append(dest)
    return saved


def token_present(config_dir: Path) -> bool:
    path = config.secret_path("youtube_token.json", Path(config_dir))
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("refresh_token"))

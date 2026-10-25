"""Local web wizard for k2y setup."""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
import shutil
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, kick, setup_core, timer, youtube


STATIC_DIR = Path(__file__).parent / "webwizard_static"


class WizardState:
    def __init__(self, config_dir: Path, repo_dir: Path):
        self.config_dir = setup_core.ensure_config_dir(config_dir)
        self.repo_dir = repo_dir
        self.lock = threading.Lock()
        self.faces: list[Path] = []
        self.auth_running = False
        self.auth_error: str | None = None
        self.timer_lines: list[str] = []

    def checks(self) -> dict:
        return {
            "python": True,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "config": (self.config_dir / "config.json").exists(),
            "client_secret": (self.config_dir / "client_secret.json").exists(),
            "gemini": (self.config_dir / "gemini.key").exists(),
            "token": setup_core.token_present(self.config_dir),
        }

    def snapshot(self) -> dict:
        existing = setup_core.load_existing_config(self.config_dir)
        faces_dir = self.config_dir / "faces"
        faces = sorted(p.name for p in faces_dir.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")) if faces_dir.exists() else []
        return {
            "config_dir": str(self.config_dir),
            "checks": self.checks(),
            "existing": existing,
            "faces": faces,
            "auth_running": self.auth_running,
            "auth_error": self.auth_error,
            "timer_lines": self.timer_lines,
        }

    def start_auth(self) -> None:
        if self.auth_running:
            return
        self.auth_running = True
        self.auth_error = None

        def worker() -> None:
            try:
                youtube.auth(paste=False)
            except Exception as e:
                self.auth_error = str(e)
            finally:
                self.auth_running = False

        threading.Thread(target=worker, daemon=True).start()


STATE: WizardState | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "k2y-webwizard/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send_bytes(self, status: int, ctype: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send_bytes(status, "application/json", json.dumps(obj).encode("utf-8"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        return json.loads(data.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/faces/"):
            name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            target = (STATE.config_dir / "faces" / name).resolve()
            faces_dir = (STATE.config_dir / "faces").resolve()
            if target.parent != faces_dir or not target.is_file():
                self._send_json({"error": "not found"}, status=404)
                return
            mime, _ = mimetypes.guess_type(str(target))
            self._send_bytes(200, mime or "application/octet-stream", target.read_bytes())
            return
        if parsed.path == "/api/status":
            self._send_json(STATE.snapshot())
            return
        if parsed.path == "/api/check-channel":
            qs = urllib.parse.parse_qs(parsed.query)
            slug = setup_core.normalize_channel(qs.get("slug", [""])[0])
            if not slug:
                self._send_json({"ok": False, "error": "Invalid channel slug."}, status=400)
                return
            try:
                count = len(kick.list_vods(slug))
                self._send_json({"ok": True, "slug": slug, "count": count})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        self._serve_static(parsed.path)

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self._send_json({"error": "not found"}, status=404)
            return
        mime, _ = mimetypes.guess_type(str(target))
        self._send_bytes(200, mime or "application/octet-stream", target.read_bytes())

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self._read_json()
        except Exception:
            self._send_json({"ok": False, "error": "Invalid request body."}, status=400)
            return

        if parsed.path == "/api/client-secret":
            ok, msg = setup_core.read_client_secret_text(body.get("content", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, status=400)
                return
            setup_core.write_secret("client_secret.json", msg, STATE.config_dir)
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/gemini":
            ok, key = setup_core.validate_gemini_key(body.get("content", ""))
            if not ok:
                self._send_json({"ok": False, "error": key}, status=400)
                return
            setup_core.write_secret("gemini.key", key, STATE.config_dir)
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/faces":
            files: list[tuple[str, bytes]] = []
            for item in body.get("files", []):
                try:
                    raw = base64.b64decode(item.get("data", ""), validate=True)
                except Exception:
                    continue
                files.append((str(item.get("name", "face")), raw))
            saved = setup_core.save_faces(STATE.config_dir, files)
            self._send_json({"ok": True, "saved": [p.name for p in saved]})
            return

        if parsed.path == "/api/config":
            channel = setup_core.normalize_channel(body.get("channel", ""))
            if not channel:
                self._send_json({"ok": False, "error": "Enter a valid Kick channel slug."}, status=400)
                return
            try:
                min_part = int(body.get("min_part_minutes", 15))
            except Exception:
                self._send_json({"ok": False, "error": "Min part length must be a number."}, status=400)
                return
            if min_part < 1:
                self._send_json({"ok": False, "error": "Min part length must be at least 1."}, status=400)
                return
            since = str(body.get("since", "")).strip()
            work_dir = str(body.get("work_dir", "")).strip()
            if work_dir:
                work_dir = str(Path(work_dir).expanduser())
            if not work_dir:
                self._send_json({"ok": False, "error": "Enter a work folder."}, status=400)
                return
            links = []
            for pair in body.get("links", []):
                label = str(pair.get("label", "")).strip()
                url = str(pair.get("url", "")).strip()
                if label and url:
                    links.append([label, url])
            values = {
                "channel": channel,
                "since": since or None,
                "work_dir": work_dir,
                "min_part_minutes": min_part,
                "links": links,
            }
            existing = setup_core.load_existing_config(STATE.config_dir)
            path = setup_core.write_config(STATE.config_dir, existing, values)
            self._send_json({"ok": True, "path": str(path)})
            return

        if parsed.path == "/api/auth":
            if not (STATE.config_dir / "client_secret.json").exists():
                self._send_json(
                    {"ok": False, "error": "Upload client_secret.json first."}, status=400
                )
                return
            STATE.start_auth()
            self._send_json({"ok": True, "started": True})
            return

        if parsed.path == "/api/timer":
            work_dir_raw = body.get("work_dir") or setup_core.load_existing_config(
                STATE.config_dir
            ).get("work_dir")
            if not work_dir_raw:
                self._send_json({"ok": False, "error": "Save a work folder first."}, status=400)
                return
            try:
                STATE.timer_lines = timer.install_timer(
                    STATE.repo_dir, Path(work_dir_raw).expanduser()
                )
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
                return
            self._send_json({"ok": True, "lines": STATE.timer_lines})
            return

        self._send_json({"ok": False, "error": "not found"}, status=404)


def run_webwizard(
    config_dir: Path | None = None,
    repo_dir: Path | None = None,
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    global STATE
    cfg_dir = Path(config_dir).expanduser() if config_dir else config.CONFIG_DIR
    config.CONFIG_DIR = cfg_dir
    repo = Path(repo_dir) if repo_dir else Path(__file__).resolve().parent.parent
    STATE = WizardState(cfg_dir, repo)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"k2y web wizard running at {url}")
    print("Local only. Press Ctrl+C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

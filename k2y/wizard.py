"""Guided first-time setup: credentials, config, YouTube authorization and timer."""
from __future__ import annotations

from datetime import date
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import config, kick, timer, youtube

REPO_DIR = Path(__file__).resolve().parent.parent


class SetupError(RuntimeError):
    """A setup step cannot continue without the user fixing something."""


def _normalize_channel(raw: str) -> str:
    value = raw.strip()
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


def _read_client_secret(path: Path) -> tuple[bool, str, str]:
    if not path.is_file():
        return False, f"File not found: {path}", ""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return False, f"Cannot read {path}: {e}", ""
    try:
        data = json.loads(text)
    except Exception as e:
        return False, f"That file is not valid JSON: {e}", ""
    if not isinstance(data, dict) or not isinstance(data.get("installed"), dict):
        return (
            False,
            "This is not a Desktop app OAuth client. In Google Cloud, create an OAuth "
            "client of type 'Desktop app' and download its JSON.",
            "",
        )
    installed = data["installed"]
    if not installed.get("client_id") or not installed.get("client_secret"):
        return False, "The JSON is missing client_id or client_secret.", ""
    return True, "", text


def _clean_path(raw: str) -> Path:
    value = raw.strip().strip('"').strip("'")
    return Path(value).expanduser()


class Wizard:
    def __init__(
        self,
        *,
        config_dir: Path | None = None,
        repo_dir: Path | None = None,
        ask=input,
        secret_ask=getpass.getpass,
        out=print,
        auth_fn=None,
        install_timer_fn=None,
        check_channel_fn=None,
        which=shutil.which,
        run=subprocess.run,
    ):
        self.config_dir = Path(config_dir).expanduser() if config_dir else config.CONFIG_DIR
        self.repo_dir = Path(repo_dir) if repo_dir else REPO_DIR
        self._ask = ask
        self._secret_ask = secret_ask
        self._out = out
        self.auth_fn = auth_fn or youtube.auth
        self.install_timer_fn = install_timer_fn or timer.install_timer
        self.check_channel_fn = check_channel_fn or kick.list_vods
        self.which = which
        self._run_command = run

    # ---------- small helpers ----------

    def _say(self, text: str = "") -> None:
        self._out(text)

    def _section(self, number: int, title: str) -> None:
        line = f"Step {number} — {title}"
        self._say("")
        self._say(line)
        self._say("-" * len(line))

    def _prompt(self, label: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = self._ask(f"{label}{suffix}: ")
        if raw is None:
            raise EOFError
        raw = raw.strip()
        if raw:
            return raw
        return default if default is not None else ""

    def _yes(self, label: str, *, default: bool = True) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        raw = self._ask(f"{label} {hint} ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes", "o", "oui", "true")

    # ---------- steps ----------

    def run(self, *, do_auth: bool = True, do_timer: bool = True) -> int:
        self._say("k2y setup")
        self._say("=========")
        self._say("This wizard saves your credentials, writes the configuration, authorizes")
        self._say("YouTube and installs the 10-minute timer. It uploads nothing.")
        self._say("Press Ctrl+C to stop at any time.")

        if sys.version_info < (3, 12):
            found = ".".join(str(part) for part in sys.version_info[:3])
            raise SetupError(
                f"Python 3.12 or newer is required (found {found}). "
                "Install it, then run setup again."
            )

        self._check_tools()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        existing = self._load_existing()

        self._section(1, "Credentials")
        self._configure_client_secret()
        self._configure_gemini_key()

        self._section(2, "Kick channel and settings")
        values = self._configure_values(existing)
        self._write_config(existing, values)

        authorized = False
        if do_auth:
            self._section(3, "YouTube authorization")
            authorized = self._authorize()

        timer_installed = False
        if do_timer:
            self._section(4, "Schedule")
            timer_installed = self._install_timer(values["work_dir"])

        self._summary(values, authorized=authorized, timer_installed=timer_installed)
        return 0

    def _check_tools(self) -> None:
        missing = [name for name in ("ffmpeg", "ffprobe") if not self.which(name)]
        if not missing:
            self._say("")
            self._say("Python and ffmpeg are ready.")
            return

        tools = " and ".join(missing)
        if sys.platform == "win32" and self.which("winget"):
            self._say("")
            self._say(f"{tools} is missing.")
            if self._yes("Install FFmpeg with winget now?", default=True):
                try:
                    self._run_command(
                        [
                            "winget",
                            "install",
                            "--id",
                            "Gyan.FFmpeg",
                            "-e",
                            "--accept-source-agreements",
                            "--accept-package-agreements",
                        ],
                        check=True,
                    )
                except Exception as e:
                    raise SetupError(f"winget could not install FFmpeg: {e}") from e
                still_missing = [name for name in ("ffmpeg", "ffprobe") if not self.which(name)]
                if not still_missing:
                    self._say("FFmpeg installed.")
                    return
                raise SetupError(
                    "FFmpeg was installed but this window does not see it yet. Close this "
                    "window, open a new terminal in the repository folder, and run "
                    "`py -m k2y setup` again."
                )

        hint = {
            "win32": "winget install --id Gyan.FFmpeg -e",
            "darwin": "brew install ffmpeg",
        }.get(sys.platform, "sudo apt install ffmpeg")
        raise SetupError(
            f"{tools} is missing. Install it ({hint}), reopen the terminal, then run setup again."
        )

    def _load_existing(self) -> dict:
        path = self.config_dir / "config.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            self._say(f"Warning: {path} is not valid JSON; it will be rewritten.")
            return {}
        if not isinstance(data, dict):
            self._say(f"Warning: {path} does not contain a JSON object; it will be rewritten.")
            return {}
        return data

    def _configure_client_secret(self) -> None:
        target = self.config_dir / "client_secret.json"
        replace = True
        if target.exists():
            self._say(f"You already have {target}.")
            replace = self._yes("Replace it?", default=False)
        if not replace:
            return

        self._say("")
        self._say("You need the JSON file of a Google OAuth client of type 'Desktop app'.")
        self._say("Google Cloud → APIs & Services → Credentials → Create credentials")
        self._say("→ OAuth client ID → Desktop app → Download JSON.")
        self._say("")
        while True:
            raw = self._prompt("Path to that JSON file")
            if not raw:
                raise SetupError("No client secret file provided.")
            path = _clean_path(raw)
            ok, message, text = _read_client_secret(path)
            if ok:
                config.write_secret("client_secret.json", text, self.config_dir)
                self._say(f"Saved {target}")
                return
            self._say(f"  {message}")

    def _configure_gemini_key(self) -> None:
        target = self.config_dir / "gemini.key"
        replace = True
        if target.exists():
            self._say(f"You already have {target}.")
            replace = self._yes("Replace it?", default=False)
        if not replace:
            return

        self._say("")
        self._say("Create a Gemini API key at https://aistudio.google.com/apikey and enable")
        self._say("billing on its project. Paste the key below (it stays hidden), or paste")
        self._say("the path of a file that contains it.")
        while True:
            raw = self._secret_ask("Gemini API key or key file: ")
            if raw is None:
                raise EOFError
            candidate = raw.strip()
            if not candidate:
                self._say("  The key cannot be empty.")
                continue
            path = _clean_path(candidate)
            if path.is_file():
                try:
                    candidate = path.read_text(encoding="utf-8-sig").strip()
                except OSError as e:
                    self._say(f"  Cannot read {path}: {e}")
                    continue
            if len(candidate) < 20 or re.search(r"\s", candidate):
                self._say("  That does not look like an API key. Copy the whole key, without spaces.")
                continue
            config.write_secret("gemini.key", candidate, self.config_dir)
            self._say(f"Saved {target}")
            return

    def _configure_values(self, existing: dict) -> dict:
        channel = self._ask_channel(existing)
        since = self._ask_since(existing)
        work_dir = self._ask_work_dir(existing)
        min_part_minutes = self._ask_min_part_minutes(existing)
        links = self._ask_links(existing)
        return {
            "channel": channel,
            "since": since,
            "work_dir": work_dir,
            "min_part_minutes": min_part_minutes,
            "links": links,
        }

    def _ask_channel(self, existing: dict) -> str:
        default = str(existing.get("channel") or "")
        while True:
            raw = self._prompt("Kick channel slug (from kick.com/<slug>)", default or None)
            slug = _normalize_channel(raw)
            if not slug:
                self._say("  Use only letters, numbers, '-' and '_' (for example: randomguy42).")
                default = ""
                continue
            try:
                vods = self.check_channel_fn(slug)
            except Exception as e:
                self._say(f"  Could not read kick.com/{slug}: {e}")
                if self._yes("  Use this channel anyway?", default=False):
                    return slug
                default = ""
                continue
            self._say(f"  Found {len(vods)} finished VOD(s) on kick.com/{slug}.")
            return slug

    def _ask_since(self, existing: dict) -> str:
        default = str(existing.get("since") or date.today().isoformat())
        while True:
            raw = self._prompt("Only process VODs from this date (YYYY-MM-DD)", default)
            try:
                return date.fromisoformat(raw).isoformat()
            except ValueError:
                self._say("  Use the format YYYY-MM-DD, for example 2026-10-24.")

    def _ask_work_dir(self, existing: dict) -> str:
        default = str(existing.get("work_dir") or (Path.home() / ".local/share/k2y"))
        while True:
            raw = self._prompt("Work folder (downloads, database, logs)", default)
            path = _clean_path(raw)
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".k2y-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as e:
                self._say(f"  Cannot write to {path}: {e}")
                default = ""
                continue
            return str(path)

    def _ask_min_part_minutes(self, existing: dict) -> int:
        default = str(existing.get("min_part_minutes") or 15)
        while True:
            raw = self._prompt("Minimum length of a thematic part, in minutes", default)
            try:
                value = int(raw)
                if value >= 1:
                    return value
            except ValueError:
                pass
            self._say("  Enter a whole number of minutes, at least 1.")

    def _ask_links(self, existing: dict) -> list[list[str]]:
        current = existing.get("links")
        if isinstance(current, list) and current:
            self._say("")
            self._say("Current links in descriptions:")
            valid = True
            for item in current:
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and all(isinstance(part, str) and part for part in item)
                ):
                    self._say(f"  {item[0]} — {item[1]}")
                else:
                    valid = False
            if valid and self._yes("Keep these links?", default=True):
                return [[str(item[0]), str(item[1])] for item in current]

        self._say("")
        self._say("Add links at the bottom of every description. Leave the label empty to finish.")
        links: list[list[str]] = []
        while True:
            label = self._prompt("Link label (blank to finish)")
            if not label:
                return links
            while True:
                url = self._prompt(f"URL for {label}")
                if url.startswith(("http://", "https://")) and not re.search(r"\s", url):
                    break
                self._say("  Enter a full URL starting with http:// or https://.")
            links.append([label, url])

    def _write_config(self, existing: dict, values: dict) -> Path:
        data = dict(existing)
        data.update(values)
        path = self.config_dir / "config.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self._say("")
        self._say(f"Configuration saved to {path}")
        return path

    def _authorize(self) -> bool:
        token = config.secret_path("youtube_token.json", self.config_dir)
        if token.exists() and self._has_refresh_token(token):
            self._say(f"YouTube is already authorized ({token}).")
            if not self._yes("Authorize again?", default=False):
                return True

        self._say("A browser will open. Choose the Google account that owns the YouTube")
        self._say("channel. For a Brand Account, pick that channel in the account chooser.")
        headless = self._yes("Is this a headless machine (no browser here)?", default=False)
        try:
            self.auth_fn(paste=headless)
        except Exception as e:
            raise SetupError(
                f"YouTube authorization failed: {e}\n"
                "Your configuration is saved. Fix the problem and run `py -m k2y auth`."
            ) from e
        self._say("YouTube authorization saved.")
        return True

    def _has_refresh_token(self, path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return False
        return isinstance(data, dict) and bool(data.get("refresh_token"))

    def _install_timer(self, work_dir: str) -> bool:
        if not self._yes("Install the 10-minute timer now?", default=True):
            self._say("Timer skipped. You can install it later with `py -m k2y install-timer`.")
            return False
        try:
            lines = self.install_timer_fn(self.repo_dir, Path(work_dir))
        except Exception as e:
            raise SetupError(f"Could not install the timer: {e}") from e
        for line in lines:
            self._say(line)
        if sys.platform != "win32":
            self._say("")
            self._say("This machine uses cron: add the line above with `crontab -e`.")
            self._say("Nothing was modified automatically.")
        return True

    def _summary(self, values: dict, *, authorized: bool, timer_installed: bool) -> None:
        self._say("")
        self._say("Setup complete.")
        self._say("")
        self._say(f"  Kick channel : {values['channel']}")
        self._say(f"  Configuration: {self.config_dir / 'config.json'}")
        self._say(f"  Work folder  : {values['work_dir']}")
        if authorized:
            self._say("  YouTube      : authorized (uploads are private)")
        else:
            self._say("  YouTube      : not authorized yet — run `py -m k2y auth`")
        if timer_installed:
            self._say("  Timer        : installed")
        else:
            self._say("  Timer        : not installed — run `py -m k2y install-timer`")
        self._say("")
        self._say("Next:")
        self._say("  1. py -m k2y status")
        self._say("  2. Test on one stream: py -m k2y backfill <vod_id>, then py -m k2y run")
        self._say("  3. Publish the results manually in YouTube Studio when they are ready.")
        self._say("")
        self._say("Full guide: docs/SETUP.md")


def run_setup(
    *,
    config_dir: Path | None = None,
    repo_dir: Path | None = None,
    do_auth: bool = True,
    do_timer: bool = True,
    **kwargs,
) -> int:
    """Run the wizard and translate cancellations and known failures into exit codes."""
    wizard = Wizard(config_dir=config_dir, repo_dir=repo_dir, **kwargs)
    original_config_dir = config.CONFIG_DIR
    config.CONFIG_DIR = wizard.config_dir
    try:
        return wizard.run(do_auth=do_auth, do_timer=do_timer)
    except (KeyboardInterrupt, EOFError):
        wizard._say("")
        wizard._say("Setup cancelled.")
        return 130
    except SetupError as e:
        wizard._say("")
        wizard._say(f"Setup failed: {e}")
        return 1
    except Exception as e:
        wizard._say("")
        wizard._say(f"Setup failed unexpectedly: {e}")
        return 1
    finally:
        config.CONFIG_DIR = original_config_dir

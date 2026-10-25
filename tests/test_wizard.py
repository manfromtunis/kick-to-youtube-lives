"""Unit tests for k2y.wizard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from k2y import config, wizard


class Answers:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        value = self.answers.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def assert_done(self) -> None:
        if self.answers:
            raise AssertionError(f"unused answers: {self.answers!r}")


CLIENT_JSON = {
    "installed": {
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


class TestWizard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        self.repo_dir = self.root / "repo"
        self.work_dir = self.root / "work"
        self.repo_dir.mkdir()
        self.lines: list[str] = []
        self.auth_calls: list[bool] = []
        self.timer_calls: list[tuple[Path, Path]] = []
        self.orig_config_dir = config.CONFIG_DIR

    def tearDown(self):
        config.CONFIG_DIR = self.orig_config_dir
        self.tmp.cleanup()

    def make(self, ask, *, secret_ask=None, **kwargs):
        return wizard.Wizard(
            config_dir=kwargs.pop("config_dir", self.config_dir),
            repo_dir=self.repo_dir,
            ask=ask,
            secret_ask=secret_ask or (lambda prompt: "AIza" + "x" * 32),
            out=self.lines.append,
            which=kwargs.pop("which", lambda name: f"/usr/bin/{name}"),
            auth_fn=kwargs.pop("auth_fn", lambda **kw: self.auth_calls.append(kw.get("paste", False))),
            install_timer_fn=kwargs.pop(
                "install_timer_fn",
                lambda repo, work: self.timer_calls.append((repo, work)) or ["timer line"],
            ),
            check_channel_fn=kwargs.pop("check_channel_fn", lambda slug: [{"id": 1}]),
            run=kwargs.pop("run", lambda *a, **k: None),
            **kwargs,
        )

    def write_client(self, data=None) -> Path:
        path = self.root / "client_secret.json"
        path.write_text(json.dumps(data if data is not None else CLIENT_JSON), encoding="utf-8")
        return path

    def run_setup(self, ask, *, secret_ask=None, **kwargs):
        return wizard.run_setup(
            config_dir=kwargs.pop("config_dir", self.config_dir),
            repo_dir=self.repo_dir,
            ask=ask,
            secret_ask=secret_ask or (lambda prompt: "AIza" + "x" * 32),
            out=self.lines.append,
            which=kwargs.pop("which", lambda name: f"/usr/bin/{name}"),
            auth_fn=kwargs.pop("auth_fn", lambda **kw: self.auth_calls.append(kw.get("paste", False))),
            install_timer_fn=kwargs.pop(
                "install_timer_fn",
                lambda repo, work: self.timer_calls.append((repo, work)) or ["timer line"],
            ),
            check_channel_fn=kwargs.pop("check_channel_fn", lambda slug: [{"id": 1}]),
            run=kwargs.pop("run", lambda *a, **k: None),
            **kwargs,
        )

    def test_normalize_channel(self):
        self.assertEqual(wizard._normalize_channel("NewStreamer"), "newstreamer")
        self.assertEqual(wizard._normalize_channel("https://kick.com/NewStreamer/"), "newstreamer")
        self.assertEqual(wizard._normalize_channel("kick.com/NewStreamer?tab=videos"), "newstreamer")
        self.assertEqual(wizard._normalize_channel("@new-streamer_1"), "new-streamer_1")
        self.assertEqual(wizard._normalize_channel("kick.com/someone/videos"), "someone")
        self.assertEqual(wizard._normalize_channel("bad slug!"), "")
        self.assertEqual(wizard._normalize_channel(""), "")

    def test_client_secret_rejects_wrong_shape_then_accepts_desktop_client(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")
        good = self.write_client()
        ask = Answers([str(bad), str(good)])
        w = self.make(ask)

        w._configure_client_secret()

        ask.assert_done()
        saved = (self.config_dir / "client_secret.json").read_text(encoding="utf-8")
        self.assertIn("client_id", saved)
        self.assertTrue(any("Desktop app" in line for line in self.lines))

    def test_client_secret_missing_file_reports_and_retries(self):
        good = self.write_client()
        ask = Answers([str(self.root / "nope.json"), str(good)])
        self.make(ask)._configure_client_secret()
        ask.assert_done()
        self.assertTrue(any("File not found" in line for line in self.lines))

    def test_gemini_key_from_file(self):
        key_file = self.root / "key.txt"
        key_file.write_text("AIza" + "y" * 34 + "\n", encoding="utf-8")
        secret = Answers([str(key_file)])
        self.make(Answers([]), secret_ask=secret)._configure_gemini_key()
        saved = (self.config_dir / "gemini.key").read_text(encoding="utf-8")
        self.assertEqual(saved, "AIza" + "y" * 34)
        self.assertEqual(os.stat(self.config_dir / "gemini.key").st_mode & 0o777, 0o600)

    def test_gemini_key_hidden_input(self):
        secret = Answers(["AIza" + "z" * 34])
        self.make(Answers([]), secret_ask=secret)._configure_gemini_key()
        self.assertEqual(
            (self.config_dir / "gemini.key").read_text(encoding="utf-8"),
            "AIza" + "z" * 34,
        )

    def test_links_flow_retries_invalid_url(self):
        ask = Answers(["Twitter", "not-a-url", "https://x.com/me", "Site", "https://example.com", ""])
        links = self.make(ask)._ask_links({})
        ask.assert_done()
        self.assertEqual(links, [["Twitter", "https://x.com/me"], ["Site", "https://example.com"]])

    def test_existing_links_can_be_kept(self):
        ask = Answers(["y"])
        existing = {"links": [["Twitter", "https://x.com/me"]]}
        self.assertEqual(self.make(ask)._ask_links(existing), [["Twitter", "https://x.com/me"]])
        ask.assert_done()

    def test_channel_check_failure_can_be_overridden(self):
        def boom(slug):
            raise RuntimeError("Kick is down")

        ask = Answers(["newstreamer", "y"])
        w = self.make(ask, check_channel_fn=boom)
        self.assertEqual(w._ask_channel({}), "newstreamer")
        ask.assert_done()
        self.assertTrue(any("Kick is down" in line for line in self.lines))

    def test_config_preserves_unknown_keys_and_updates_values(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "config.json").write_text(
            json.dumps({"channel": "old", "extra": {"keep": True}}), encoding="utf-8"
        )
        ask = Answers(["NewStreamer", "2026-01-05", str(self.work_dir), "20", ""])
        w = self.make(ask)
        values = w._configure_values(w._load_existing())
        w._write_config(w._load_existing(), values)

        data = json.loads((self.config_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["channel"], "newstreamer")
        self.assertEqual(data["since"], "2026-01-05")
        self.assertEqual(data["work_dir"], str(self.work_dir))
        self.assertEqual(data["min_part_minutes"], 20)
        self.assertEqual(data["links"], [])
        self.assertEqual(data["extra"], {"keep": True})
        ask.assert_done()

    def test_full_setup_writes_everything(self):
        client = self.write_client()
        ask = Answers(
            [
                str(client),
                "https://kick.com/NewStreamer/",
                "2026-01-05",
                str(self.work_dir),
                "20",
                "",
                "n",
                "n",
            ]
        )
        secret = Answers(["AIza" + "q" * 34])

        ret = self.run_setup(ask, secret_ask=secret)

        self.assertEqual(ret, 0)
        ask.assert_done()
        secret.assert_done()
        data = json.loads((self.config_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["channel"], "newstreamer")
        self.assertEqual(data["links"], [])
        self.assertEqual(self.auth_calls, [False])
        self.assertEqual(self.timer_calls, [])
        text = "\n".join(self.lines)
        self.assertIn("Setup complete.", text)
        self.assertIn("Timer        : not installed", text)
        self.assertIn("py -m k2y status", text)

    def test_full_setup_installs_timer_when_accepted(self):
        client = self.write_client()
        ask = Answers(
            [
                str(client),
                "newstreamer",
                "2026-01-05",
                str(self.work_dir),
                "15",
                "",
                "y",
                "y",
            ]
        )
        ret = self.run_setup(ask, secret_ask=Answers(["AIza" + "q" * 34]))

        self.assertEqual(ret, 0)
        self.assertEqual(self.timer_calls, [(self.repo_dir, self.work_dir)])
        self.assertIn("Timer        : installed", "\n".join(self.lines))

    def test_skip_auth_and_timer_flags(self):
        client = self.write_client()
        ask = Answers([str(client), "newstreamer", "2026-01-05", str(self.work_dir), "15", ""])
        ret = self.run_setup(
            ask,
            secret_ask=Answers(["AIza" + "q" * 34]),
            do_auth=False,
            do_timer=False,
        )

        self.assertEqual(ret, 0)
        self.assertEqual(self.auth_calls, [])
        self.assertEqual(self.timer_calls, [])
        text = "\n".join(self.lines)
        self.assertIn("not authorized yet", text)
        self.assertIn("not installed", text)

    def test_existing_token_skips_auth(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "youtube_token.json").write_text(
            json.dumps({"refresh_token": "r"}), encoding="utf-8"
        )
        ask = Answers(["n"])
        self.make(ask)._authorize()
        ask.assert_done()
        self.assertEqual(self.auth_calls, [])

    def test_auth_failure_becomes_setup_error(self):
        ask = Answers(["n"])

        def boom(**kwargs):
            raise RuntimeError("access_denied")

        w = self.make(ask, auth_fn=boom)
        with self.assertRaises(wizard.SetupError) as raised:
            w._authorize()
        self.assertIn("access_denied", str(raised.exception))

    def test_missing_ffmpeg_explains_how_to_install(self):
        ask = Answers([])
        w = self.make(ask, which=lambda name: None)
        with patch.object(wizard.sys, "platform", "linux"):
            with self.assertRaises(wizard.SetupError) as raised:
                w._check_tools()
        self.assertIn("sudo apt install ffmpeg", str(raised.exception))

    def test_old_python_is_rejected(self):
        with patch.object(wizard.sys, "version_info", (3, 11, 9)):
            ret = self.run_setup(Answers([]))
        self.assertEqual(ret, 1)
        self.assertIn("Python 3.12 or newer is required", "\n".join(self.lines))

    def test_cancellation_returns_130_and_restores_config_dir(self):
        marker = self.root / "marker"
        config.CONFIG_DIR = marker
        ask = Answers([KeyboardInterrupt()])

        ret = self.run_setup(ask)

        self.assertEqual(ret, 130)
        self.assertIn("Setup cancelled.", "\n".join(self.lines))
        self.assertEqual(config.CONFIG_DIR, marker)

    def test_custom_config_dir_is_used_and_restored(self):
        client = self.write_client()
        alternate = self.root / "alternate"
        ask = Answers([str(client), "newstreamer", "2026-01-05", str(self.work_dir), "15", ""])
        ret = self.run_setup(ask, config_dir=alternate, do_auth=False, do_timer=False)

        self.assertEqual(ret, 0)
        self.assertTrue((alternate / "config.json").exists())
        self.assertTrue((alternate / "client_secret.json").exists())
        self.assertFalse((self.config_dir / "config.json").exists())
        self.assertEqual(config.CONFIG_DIR, self.orig_config_dir)

    def test_work_dir_must_be_writable(self):
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        ask = Answers([str(blocker), str(self.work_dir)])
        result = self.make(ask)._ask_work_dir({})
        ask.assert_done()
        self.assertEqual(result, str(self.work_dir))
        self.assertTrue(any("Cannot write to" in line for line in self.lines))


if __name__ == "__main__":
    unittest.main()

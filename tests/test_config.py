"""Unit tests for k2y.config."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from k2y import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.orig_config_dir = config.CONFIG_DIR
        config.CONFIG_DIR = self.config_dir

    def tearDown(self):
        config.CONFIG_DIR = self.orig_config_dir
        self.tmp.cleanup()

    def test_load_ignores_unknown_keys(self):
        cfg_file = self.config_dir / "config.json"
        cfg_file.write_text(json.dumps({
            "channel": "custom_channel",
            "model": "custom_model",
            "unknown_extra_key_1": "some_value",
            "unknown_nested_dict": {"foo": "bar"},
        }), encoding="utf-8")

        loaded = config.load()
        self.assertEqual(loaded.channel, "custom_channel")
        self.assertEqual(loaded.model, "custom_model")
        self.assertFalse(hasattr(loaded, "unknown_extra_key_1"))

    def test_load_reads_with_utf8_sig_bom(self):
        cfg_file = self.config_dir / "config.json"
        # Write JSON with UTF-8 BOM
        content = json.dumps({"channel": "bom_channel", "model": "gemini-3.8-flash"})
        cfg_file.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))

        loaded = config.load()
        self.assertEqual(loaded.channel, "bom_channel")

    def test_read_secret_with_utf8_sig_bom(self):
        sec_file = self.config_dir / "gemini.key"
        sec_file.write_bytes(b"\xef\xbb\xbfmy-secret-key\n")

        val = config.read_secret("gemini.key")
        self.assertEqual(val, "my-secret-key")

    def test_read_secret_missing_raises_missing_secret(self):
        with self.assertRaises(config.MissingSecret) as ctx:
            config.read_secret("nonexistent.key")
        self.assertIn("missing secret", str(ctx.exception))

    def test_write_secret_atomic_and_permissions(self):
        config.write_secret("client_secret.json", '{"installed": true}')
        sec_file = self.config_dir / "client_secret.json"
        self.assertTrue(sec_file.exists())
        self.assertEqual(sec_file.read_text(encoding="utf-8"), '{"installed": true}')

        # Verify permissions on POSIX
        if os.name != "nt":
            mode = stat.S_IMODE(sec_file.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_ensure_since_sets_today_when_no_config(self):
        cfg = config.Config(channel="chan")
        cfg_after = config.ensure_since(cfg)
        self.assertIsNotNone(cfg_after.since)
        cfg_file = self.config_dir / "config.json"
        self.assertTrue(cfg_file.exists())
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("since"), cfg_after.since)

    def test_ensure_since_invalid_json_raises_clear_error_and_does_not_overwrite(self):
        cfg_file = self.config_dir / "config.json"
        bad_json = "{\ninvalid json content..."
        cfg_file.write_text(bad_json, encoding="utf-8")

        cfg = config.Config(channel="chan")
        with self.assertRaises(ValueError) as ctx:
            config.ensure_since(cfg)

        self.assertIn("Invalid JSON in config file", str(ctx.exception))
        # Ensure file was not overwritten
        self.assertEqual(cfg_file.read_text(encoding="utf-8"), bad_json)


if __name__ == "__main__":
    unittest.main()

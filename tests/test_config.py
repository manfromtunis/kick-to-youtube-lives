"""Unit tests for k2y.config."""
from __future__ import annotations

import json
from pathlib import Path
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
        }))

        loaded = config.load()
        self.assertEqual(loaded.channel, "custom_channel")
        self.assertEqual(loaded.model, "custom_model")
        self.assertFalse(hasattr(loaded, "unknown_extra_key_1"))

    def test_read_secret_missing_raises_missing_secret(self):
        with self.assertRaises(config.MissingSecret) as ctx:
            config.read_secret("nonexistent.key")
        self.assertIn("missing secret", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

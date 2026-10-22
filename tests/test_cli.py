"""Unit tests for k2y.cli."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from k2y import cli, config, db


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = Path(self.tmp.name) / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.orig_config_dir = config.CONFIG_DIR
        config.CONFIG_DIR = self.config_dir

        # Write config.json
        cfg_path = self.config_dir / "config.json"
        cfg_path.write_text(json.dumps({
            "channel": "randomguy42",
            "work_dir": str(self.work_dir),
            "since": "2026-10-22",
        }))

        self.cfg = config.load()
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self):
        config.CONFIG_DIR = self.orig_config_dir
        self.conn.close()
        self.tmp.cleanup()

    def test_status_json_output_shape(self):
        vod = {
            "id": 601,
            "title": "Stream Status JSON",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/601",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.upsert_upload(self.conn, 601, 0, "Full VOD", 0, 3600)
        db.update_upload(self.conn, 601, 0, youtube_id="yt_601")
        db.upsert_upload(self.conn, 601, 1, "Part 1", 0, 1800)
        # 1 out of 2 uploads done

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            ret = cli.main(["status", "--json"])

        self.assertEqual(ret, 0)
        data = json.loads(stdout.getvalue())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

        item = data[0]
        expected_keys = {"id", "date", "title", "state", "uploads", "attempts", "error"}
        self.assertEqual(set(item.keys()), expected_keys)
        self.assertEqual(item["id"], 601)
        self.assertEqual(item["date"], "2026-10-23")
        self.assertEqual(item["title"], "Stream Status JSON")
        self.assertEqual(item["state"], "new")
        self.assertEqual(item["uploads"], "1/2")
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(item["error"], "")

    def test_retry_from_failed_picks_right_state(self):
        # 1. No source file -> 'new'
        v1 = {
            "id": 701,
            "title": "V1",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/701",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, v1)
        db.record_error(self.conn, 701, "Error", max_attempts=1)  # moves to failed
        self.assertEqual(db.get_vod(self.conn, 701)["state"], "failed")

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "701"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 701)["state"], "new")
        self.assertEqual(db.get_vod(self.conn, 701)["attempts"], 0)

        # 2. Source file exists, but no plan -> 'downloaded'
        v2 = {
            "id": 702,
            "title": "V2",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/702",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, v2)
        v2_dir = self.cfg.vod_dir(702)
        v2_dir.mkdir(parents=True, exist_ok=True)
        (v2_dir / "source.mp4").write_bytes(b"source")
        db.record_error(self.conn, 702, "Error", max_attempts=1)

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "702"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 702)["state"], "downloaded")

        # 3. Source file and plan exist, but no upload rows -> 'planned'
        v3 = {
            "id": 703,
            "title": "V3",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/703",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, v3)
        v3_dir = self.cfg.vod_dir(703)
        v3_dir.mkdir(parents=True, exist_ok=True)
        (v3_dir / "source.mp4").write_bytes(b"source")
        db.save_plan(self.conn, 703, [{"start_s": 0, "end_s": 3600, "title": "P"}])
        db.record_error(self.conn, 703, "Error", max_attempts=1)

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "703"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 703)["state"], "planned")

        # 4. Upload rows exist -> 'cut'
        v4 = {
            "id": 704,
            "title": "V4",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/704",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, v4)
        v4_dir = self.cfg.vod_dir(704)
        v4_dir.mkdir(parents=True, exist_ok=True)
        (v4_dir / "source.mp4").write_bytes(b"source")
        db.save_plan(self.conn, 704, [{"start_s": 0, "end_s": 3600, "title": "P"}])
        db.upsert_upload(self.conn, 704, 0, "Full VOD", 0, 3600)
        db.record_error(self.conn, 704, "Error", max_attempts=1)

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "704"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 704)["state"], "cut")

    def test_skip_command(self):
        vod = {
            "id": 801,
            "title": "Stream Skip",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/801",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["skip", "801"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 801)["state"], "skipped")

    def test_backfill_command(self):
        old_vod = {
            "id": 901,
            "title": "Old Stream Backfill",
            "started_at": "2026-10-10T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/901",
            "category": "Gaming",
            "is_live": False,
        }

        with patch("k2y.kick.list_vods", return_value=[old_vod]), patch("sys.stdout", io.StringIO()):
            ret = cli.main(["backfill", "901"])

        self.assertEqual(ret, 0)
        row = db.get_vod(self.conn, 901)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Old Stream Backfill")

    def test_plan_command_clears_unuploaded_rows(self):
        vod_id = 950
        vod = {
            "id": vod_id,
            "title": "Stream Plan Test",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/950",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        vod_dir = self.cfg.vod_dir(vod_id)
        vod_dir.mkdir(parents=True, exist_ok=True)
        (vod_dir / "source.mp4").write_bytes(b"source data")

        # Row 0 has youtube_id; row 1 does not
        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600)
        db.update_upload(self.conn, vod_id, 0, youtube_id="yt_keep_0")
        db.upsert_upload(self.conn, vod_id, 1, "Old Part", 0, 1800)

        fake_segment = types.ModuleType("k2y.segment")
        plan_result = [\
            {"start_s": 0, "end_s": 1800, "title": "New Part 1", "description": "D1", "key_moments": []},
            {"start_s": 1800, "end_s": 3600, "title": "New Part 2", "description": "D2", "key_moments": []},
        ]
        fake_segment.plan = MagicMock(return_value=plan_result)

        stdout = io.StringIO()
        with patch.object(sys.modules.get("k2y"), "segment", fake_segment, create=True), \
             patch.dict(sys.modules, {"k2y.segment": fake_segment}), \
             patch("sys.stdout", stdout):
            ret = cli.main(["plan", str(vod_id)])

        self.assertEqual(ret, 0)
        output = stdout.getvalue()
        self.assertIn("New Part 1", output)
        self.assertIn("New Part 2", output)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "planned")
        saved_plan = json.loads(row["plan_json"])
        self.assertEqual(len(saved_plan), 2)

        # Verify row 0 kept and row 1 deleted
        ups = db.uploads(self.conn, vod_id)
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0]["idx"], 0)
        self.assertEqual(ups[0]["youtube_id"], "yt_keep_0")

    def test_install_timer_windows(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("k2y.cli._is_windows", return_value=True), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stdout", stdout), \
             patch("sys.stderr", stderr):
            ret = cli.main(["install-timer"])

        self.assertEqual(ret, 0)
        cmd_file = self.work_dir / "k2y-run.cmd"
        self.assertTrue(cmd_file.exists())
        content = cmd_file.read_text(encoding="utf-8")
        self.assertIn('cd /d "', content)
        self.assertIn(f'"{sys.executable}" -m k2y run >> "', content)
        self.assertIn('k2y.log" 2>&1', content)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "schtasks")
        self.assertEqual(args[1], "/Create")
        self.assertIn("/TN", args)
        self.assertIn("k2y", args)

        out_text = stdout.getvalue()
        self.assertIn("schtasks /Query /TN k2y", out_text)
        self.assertIn("schtasks /Delete /TN k2y /F", out_text)

    def test_install_timer_linux(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("k2y.cli._is_windows", return_value=False), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stdout", stdout), \
             patch("sys.stderr", stderr):
            ret = cli.main(["install-timer"])

        self.assertEqual(ret, 0)
        mock_run.assert_not_called()
        cmd_file = self.work_dir / "k2y-run.cmd"
        self.assertFalse(cmd_file.exists())

        out_text = stdout.getvalue()
        self.assertTrue(out_text.strip().startswith("*/10 * * * * cd "))
        self.assertIn("-m k2y run >> ", out_text)
        self.assertIn("k2y.log 2>&1", out_text)

    def test_auth_paste_command(self):
        with patch("k2y.youtube.auth") as mock_auth, patch("sys.stdout", io.StringIO()):
            ret = cli.main(["auth", "--paste"])
        self.assertEqual(ret, 0)
        mock_auth.assert_called_once_with(paste=True)

    def test_exit_codes(self):
        # 2 on bad usage / missing command
        with patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main([]), 2)
            # 1 on not found VOD
            self.assertEqual(cli.main(["retry", "9999"]), 1)

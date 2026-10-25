"""Unit tests for k2y.cli."""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from k2y import cli, config, db, pipeline, youtube


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
        }), encoding="utf-8")

        self.stderr_patcher = patch("sys.stderr", io.StringIO())
        self.mock_stderr = self.stderr_patcher.start()

        self.cfg = config.load()
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self):
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)
            h.close()
        pipeline._logging_configured = False
        self.stderr_patcher.stop()

        config.CONFIG_DIR = self.orig_config_dir
        self.conn.close()
        # Clean up any leftover locks
        lock_path = self.work_dir / "k2y.lock"
        pipeline.release_lock(lock_path)
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
        db.record_error(self.conn, 701, "Error", max_attempts=1)
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

        # 4. Upload rows exist but incomplete or missing files -> 'planned'
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
        src4 = v4_dir / "source.mp4"
        src4.write_bytes(b"source")
        plan4 = [{"start_s": 0, "end_s": 1800, "title": "P1"}, {"start_s": 1800, "end_s": 3600, "title": "P2"}]
        db.save_plan(self.conn, 704, plan4)  # expected 3 rows
        db.upsert_upload(self.conn, 704, 0, "Full VOD", 0, 3600, file=src4)
        db.record_error(self.conn, 704, "Error", max_attempts=1)

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "704"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 704)["state"], "planned")

        # 5. All expected rows exist with all files -> 'cut'
        p1 = v4_dir / "part_1.mp4"
        p1.write_bytes(b"p1")
        p2 = v4_dir / "part_2.mp4"
        p2.write_bytes(b"p2")
        t0 = v4_dir / "thumb_0.jpg"
        t0.write_bytes(b"t0")
        t1 = v4_dir / "thumb_1.jpg"
        t1.write_bytes(b"t1")
        t2 = v4_dir / "thumb_2.jpg"
        t2.write_bytes(b"t2")
        db.upsert_upload(self.conn, 704, 0, "Full VOD", 0, 3600, file=src4, thumb=t0)
        db.upsert_upload(self.conn, 704, 1, "P1", 0, 1800, file=p1, thumb=t1)
        db.upsert_upload(self.conn, 704, 2, "P2", 1800, 3600, file=p2, thumb=t2)
        db.record_error(self.conn, 704, "Error", max_attempts=1)

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "704"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 704)["state"], "cut")

    def test_skip_command_cleans_up_vod_folder(self):
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
        v_dir = self.cfg.vod_dir(801)
        v_dir.mkdir(parents=True, exist_ok=True)
        (v_dir / "temp.txt").write_text("temporary")

        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["skip", "801"])
        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 801)["state"], "skipped")
        self.assertFalse(v_dir.exists())

    def test_skip_logs_warning_on_rmtree_failure(self):
        vod = {
            "id": 802,
            "title": "Stream Skip Fail",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/802",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        v_dir = self.cfg.vod_dir(802)
        v_dir.mkdir(parents=True, exist_ok=True)

        with patch("shutil.rmtree", side_effect=OSError("locked folder")), \
             self.assertLogs("k2y", level="WARNING") as cm, \
             patch("sys.stdout", io.StringIO()):
            ret = cli.main(["skip", "802"])

        self.assertEqual(ret, 0)
        self.assertEqual(db.get_vod(self.conn, 802)["state"], "skipped")
        self.assertTrue(any("Failed to remove VOD directory" in msg for msg in cm.output))

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

        # First backfill: newly inserted
        stdout1 = io.StringIO()
        with patch("k2y.kick.list_vods", return_value=[old_vod]), patch("sys.stdout", stdout1):
            ret = cli.main(["backfill", "901"])

        self.assertEqual(ret, 0)
        self.assertIn("Backfilled VOD 901: Old Stream Backfill", stdout1.getvalue())

        # Second backfill: already known (with state)
        stdout2 = io.StringIO()
        with patch("k2y.kick.list_vods", return_value=[old_vod]), patch("sys.stdout", stdout2):
            ret = cli.main(["backfill", "901"])

        self.assertEqual(ret, 0)
        self.assertIn("VOD 901 already known (state new)", stdout2.getvalue())

    def test_lock_held_prints_message_and_exits_1(self):
        lock_path = self.work_dir / "k2y.lock"
        # Hold lock using OS lock
        self.assertTrue(pipeline.acquire_lock(lock_path))

        try:
            for cmd_args in [["plan", "123"], ["retry", "123"], ["skip", "123"], ["backfill", "123"], ["auth"], ["set-upload", "123", "0", "yt_1"], ["refresh-metadata", "123"]]:
                stderr = io.StringIO()
                with patch("sys.stderr", stderr):
                    ret = cli.main(cmd_args)
                self.assertEqual(ret, 1)
                self.assertIn("a run is in progress, try again later", stderr.getvalue())
        finally:
            pipeline.release_lock(lock_path)

    def test_plan_command_refuses_when_done_or_skipped(self):
        v_done = {
            "id": 910,
            "title": "Done VOD",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/910",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, v_done)
        db.set_state(self.conn, 910, "done")

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["plan", "910"])
        self.assertEqual(ret, 1)
        self.assertIn("Cannot re-plan VOD 910", stderr.getvalue())

        db.set_state(self.conn, 910, "skipped")
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["plan", "910"])
        self.assertEqual(ret, 1)
        self.assertIn("Cannot re-plan VOD 910", stderr.getvalue())

    def test_plan_command_refuses_when_uploads_have_youtube_id_or_session(self):
        v = {
            "id": 920,
            "title": "Uploaded VOD",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/920",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, v)
        db.upsert_upload(self.conn, 920, 0, "Full VOD", 0, 3600)
        db.update_upload(self.conn, 920, 0, youtube_id="yt_already")

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["plan", "920"])
        self.assertEqual(ret, 1)
        self.assertIn("Cannot re-plan VOD 920", stderr.getvalue())

        # Test upload_session
        db.update_upload(self.conn, 920, 0, youtube_id=None, upload_session="http://session")
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["plan", "920"])
        self.assertEqual(ret, 1)
        self.assertIn("Cannot re-plan VOD 920", stderr.getvalue())

    def test_plan_command_reuses_pipeline_step_download_and_step_plan(self):
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

        plan_result = [
            {"start_s": 0, "end_s": 1800, "title": "New Part 1", "description": "D1", "key_moments": []},
            {"start_s": 1800, "end_s": 3600, "title": "New Part 2", "description": "D2", "key_moments": []},
        ]

        def fake_step_download(c, cfg, v):
            vod_dir.mkdir(parents=True, exist_ok=True)
            (vod_dir / "source.mp4").write_bytes(b"mock source")
            db.set_duration(c, v["id"], 3600)
            return "downloaded"

        def fake_step_plan(c, cfg, v):
            db.save_plan(c, v["id"], plan_result)
            return "planned"

        stdout = io.StringIO()
        with patch("k2y.pipeline.step_download", side_effect=fake_step_download) as mock_dl, \
             patch("k2y.pipeline.step_plan", side_effect=fake_step_plan) as mock_plan, \
             patch("sys.stdout", stdout):
            ret = cli.main(["plan", str(vod_id)])

        self.assertEqual(ret, 0)
        mock_dl.assert_called_once()
        mock_plan.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("New Part 1", output)
        self.assertIn("New Part 2", output)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "planned")

    def test_setup_dispatches_before_config_load(self):
        target = Path(self.tmp.name) / "elsewhere"
        with patch("k2y.wizard.run_setup", return_value=0) as mock_setup:
            ret = cli.main(
                ["setup", "--config-dir", str(target), "--no-auth", "--no-timer"]
            )

        self.assertEqual(ret, 0)
        mock_setup.assert_called_once_with(
            config_dir=target,
            repo_dir=cli.REPO_DIR,
            do_auth=False,
            do_timer=False,
        )

    def test_install_timer_windows_exact_tr_with_spaces_and_battery_note(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        fake_python = r"C:\Program Files\Python 3.12\python.exe"
        fake_pythonw = r"C:\Program Files\Python 3.12\pythonw.exe"
        fake_repo = Path(r"C:\Users\User Name\Projects\kick-to-youtube-lives")

        import pathlib
        with patch("k2y.timer.is_windows", return_value=True), \
             patch("k2y.timer.Path", pathlib.PureWindowsPath), \
             patch("sys.executable", fake_python), \
             patch("os.path.exists", return_value=True), \
             patch("k2y.cli.REPO_DIR", fake_repo), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stdout", stdout), \
             patch("sys.stderr", stderr):
            ret = cli.main(["install-timer"])

        self.assertEqual(ret, 0)
        # Ensure .cmd is NOT generated
        cmd_file = self.work_dir / "k2y-run.cmd"
        self.assertFalse(cmd_file.exists())

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "schtasks")
        self.assertEqual(args[1], "/Create")
        self.assertIn("/TN", args)
        self.assertIn("k2y", args)
        self.assertIn("/TR", args)
        tr_index = args.index("/TR")

        expected_tr = f'"{fake_pythonw}" "{str(fake_repo / "k2y-task.pyw").replace("/", "\\")}"'
        self.assertEqual(args[tr_index + 1], expected_tr)

        out_text = stdout.getvalue()
        self.assertIn("schtasks /Query /TN k2y", out_text)
        self.assertIn("schtasks /Delete /TN k2y /F", out_text)
        self.assertIn("battery", out_text.lower())

    def test_install_timer_linux(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("k2y.timer.is_windows", return_value=False), \
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
        self.assertIn("k2y-task.log 2>&1", out_text)

    def test_auth_paste_command(self):
        with patch("k2y.youtube.auth") as mock_auth, patch("sys.stdout", io.StringIO()):
            ret = cli.main(["auth", "--paste"])
        self.assertEqual(ret, 0)
        mock_auth.assert_called_once_with(paste=True)

    def test_missing_secret_exits_1_and_prints_to_stderr(self):
        stderr = io.StringIO()
        with patch("k2y.config.load", side_effect=config.MissingSecret("missing secret client_secret.json")), \
             patch("sys.stderr", stderr):
            ret = cli.main(["run"])

        self.assertEqual(ret, 1)
        self.assertIn("missing secret client_secret.json", stderr.getvalue())

    def test_exit_codes(self):
        with patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main([]), 2)
            self.assertEqual(cli.main(["retry", "9999"]), 1)

    def test_retry_with_corrupt_plan_replans(self):
        db.upsert_vod(self.conn, {
            "id": 704, "title": "V4", "started_at": "2026-10-23T10:00:00Z", "duration_s": 3600,
            "source_url": "u", "category": "Gaming", "is_live": False,
        })
        d = self.cfg.vod_dir(704)
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.mp4").write_bytes(b"source")
        self.conn.execute("UPDATE vods SET plan_json = '{not json' WHERE id = 704")
        self.conn.commit()
        db.record_error(self.conn, 704, "Error", max_attempts=1)
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["retry", "704"]), 0)
        self.assertEqual(db.get_vod(self.conn, 704)["state"], "downloaded")

    def test_retry_restart_upload_clears_sessions(self):
        vod = {
            "id": 750,
            "title": "VOD 750",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/750",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.upsert_upload(self.conn, 750, 0, "Full VOD", 0, 3600)
        db.update_upload(self.conn, 750, 0, upload_session="http://session/0")
        db.upsert_upload(self.conn, 750, 1, "Part 1", 0, 1800)
        db.update_upload(self.conn, 750, 1, youtube_id="yt_1", upload_session="http://session/1")

        # Plain retry does NOT clear upload sessions
        with patch("sys.stdout", io.StringIO()):
            ret = cli.main(["retry", "750"])
        self.assertEqual(ret, 0)
        ups = db.uploads(self.conn, 750)
        self.assertEqual(ups[0]["upload_session"], "http://session/0")

        # Retry with --restart-upload clears sessions on rows without youtube_id
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            ret = cli.main(["retry", "750", "--restart-upload"])
        self.assertEqual(ret, 0)
        self.assertIn("Cleared 1 upload session(s) for VOD 750", stdout.getvalue())

        ups = db.uploads(self.conn, 750)
        self.assertIsNone(ups[0]["upload_session"])
        self.assertEqual(ups[1]["upload_session"], "http://session/1")

    def test_set_upload_command(self):
        vod = {
            "id": 760,
            "title": "VOD 760",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/760",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.upsert_upload(self.conn, 760, 0, "Full VOD", 0, 3600)
        db.update_upload(self.conn, 760, 0, upload_session="http://session/0", thumb_done=1)

        # Successful set-upload
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            ret = cli.main(["set-upload", "760", "0", "yt_already_there"])
        self.assertEqual(ret, 0)
        self.assertIn("Recorded VOD 760 part 0 as yt_already_there", stdout.getvalue())

        row = db.get_upload(self.conn, 760, 0)
        self.assertEqual(row["youtube_id"], "yt_already_there")
        self.assertIsNone(row["upload_session"])
        self.assertEqual(row["thumb_done"], 1)

        # Refuses non-existent row
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["set-upload", "760", "99", "yt_fake"])
        self.assertEqual(ret, 1)
        self.assertIn("Upload row 99 for VOD 760 does not exist", stderr.getvalue())

    def test_cli_main_tolerates_none_stderr(self):
        with patch("sys.stderr", None), patch("sys.stdout", io.StringIO()):
            # Help when no command
            self.assertEqual(cli.main([]), 2)
            # Failure on unknown VOD
            self.assertEqual(cli.main(["retry", "99999"]), 1)

    def test_cli_main_logs_to_k2y_log_on_config_failure(self):
        # Write broken config.json
        (self.config_dir / "config.json").write_text(
            '{\n  "work_dir": "' + str(self.work_dir) + '",\n  "broken": \n}',
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["run"])
        self.assertEqual(ret, 1)

        log_file = self.work_dir / "k2y.log"
        self.assertTrue(log_file.exists())
        log_content = log_file.read_text(encoding="utf-8")
        self.assertIn("JSONDecodeError", log_content)



    def test_refresh_metadata_cli_single_and_all(self):
        vod1 = {
            "id": 961,
            "title": "VOD 961 Done",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/961",
            "category": "Gaming",
            "is_live": False,
        }
        vod2 = {
            "id": 962,
            "title": "VOD 962 Done",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/962",
            "category": "Gaming",
            "is_live": False,
        }
        vod3 = {
            "id": 963,
            "title": "VOD 963 Not Done",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/963",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)
        db.set_state(self.conn, 961, "done")
        db.upsert_vod(self.conn, vod2)
        db.set_state(self.conn, 962, "done")
        db.upsert_vod(self.conn, vod3)
        db.set_state(self.conn, 963, "cut")

        refreshed_ids = []
        def fake_refresh(conn, cfg, vod_id, force=False):
            refreshed_ids.append(vod_id)

        # 1. Single VOD
        stdout1 = io.StringIO()
        with patch("k2y.pipeline.refresh_metadata", side_effect=fake_refresh), patch("sys.stdout", stdout1):
            ret = cli.main(["refresh-metadata", "961"])
        self.assertEqual(ret, 0)
        self.assertEqual(refreshed_ids, [961])
        self.assertIn("Successfully refreshed metadata for VOD 961", stdout1.getvalue())

        # 2. --all (should refresh 961 and 962, but not 963)
        refreshed_ids.clear()
        stdout2 = io.StringIO()
        with patch("k2y.pipeline.refresh_metadata", side_effect=fake_refresh), patch("sys.stdout", stdout2):
            ret = cli.main(["refresh-metadata", "--all"])
        self.assertEqual(ret, 0)
        self.assertEqual(sorted(refreshed_ids), [961, 962])

        # 2b. --all goes on past a per-VOD error, returns 1, leaves no error on done rows
        refreshed_ids.clear()
        def fail_first(conn, cfg, vod_id, force=False):
            if vod_id == 961:
                raise RuntimeError("video gone")
            refreshed_ids.append(vod_id)
        err = io.StringIO()
        with patch("k2y.pipeline.refresh_metadata", side_effect=fail_first), \
             patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            ret = cli.main(["refresh-metadata", "--all"])
        self.assertEqual(ret, 1)
        self.assertEqual(refreshed_ids, [962], err.getvalue())
        self.assertIsNone(db.get_vod(self.conn, 961)["error"])

        # 2c. quota stops --all at once
        refreshed_ids.clear()
        def quota(conn, cfg, vod_id, force=False):
            refreshed_ids.append(vod_id)
            raise youtube.QuotaExceeded("quota")
        with patch("k2y.pipeline.refresh_metadata", side_effect=quota), \
             patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            ret = cli.main(["refresh-metadata", "--all"])
        self.assertEqual(ret, 1)
        self.assertEqual(len(refreshed_ids), 1)
        self.assertIsNone(db.get_vod(self.conn, refreshed_ids[0])["error"])

        # 3. Refuses when not done
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            ret = cli.main(["refresh-metadata", "963"])
        self.assertEqual(ret, 1)
        self.assertIn("must be 'done'", stderr.getvalue())

        # 4. Arg validation
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertEqual(cli.main(["refresh-metadata"]), 2)
            self.assertEqual(cli.main(["refresh-metadata", "961", "--all"]), 2)


if __name__ == "__main__":
    unittest.main()

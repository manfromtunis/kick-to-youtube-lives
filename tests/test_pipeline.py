"""Unit tests for k2y.pipeline."""
from __future__ import annotations

import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from k2y import config, db, kick, pipeline, segment, youtube


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = Path(self.tmp.name) / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.stderr_patcher = patch("sys.stderr", io.StringIO())
        self.mock_stderr = self.stderr_patcher.start()

        class FakeAuthRequired(Exception):
            pass

        self.auth_req_patcher = patch.object(youtube, "AuthRequired", FakeAuthRequired, create=True)
        self.FakeAuthRequired = self.auth_req_patcher.start()

        self.orig_config_dir = config.CONFIG_DIR
        config.CONFIG_DIR = self.config_dir

        self.orig_steps = dict(pipeline.STEPS)

        self.cfg = config.Config(
            channel="randomguy42",
            work_dir=self.work_dir,
            since="2026-10-22",
        )
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self):
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)
            h.close()
        pipeline._logging_configured = False
        pipeline.STEPS = self.orig_steps
        config.CONFIG_DIR = self.orig_config_dir
        self.conn.close()
        self.auth_req_patcher.stop()
        self.stderr_patcher.stop()
        self.tmp.cleanup()

    def test_expected_upload_count(self):
        self.assertEqual(pipeline.expected_upload_count([]), 1)
        self.assertEqual(pipeline.expected_upload_count([{"title": "P1"}]), 1)
        self.assertEqual(pipeline.expected_upload_count([{"title": "P1"}, {"title": "P2"}]), 3)
        self.assertEqual(pipeline.expected_upload_count([{"title": "P1"}, {"title": "P2"}, {"title": "P3"}]), 4)

    def test_vod_new_to_done_all_steps_in_order(self):
        vod = {
            "id": 100,
            "title": "Stream Test",
            "started_at": "2026-10-23T12:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod100.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        called = []

        def fake_download(c, cfg, v):
            called.append("new")
            return "downloaded"

        def fake_plan(c, cfg, v):
            called.append("downloaded")
            return "planned"

        def fake_cut(c, cfg, v):
            called.append("planned")
            return "cut"

        def fake_cut_ready(c, cfg, v):
            called.append("cut")
            return "uploading"

        def fake_upload(c, cfg, v):
            called.append("uploading")
            return "done"

        pipeline.STEPS = {
            "new": fake_download,
            "downloaded": fake_plan,
            "planned": fake_cut,
            "cut": fake_cut_ready,
            "uploading": fake_upload,
        }

        with patch("k2y.kick.list_vods", return_value=[]):
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertEqual(called, ["new", "downloaded", "planned", "cut", "uploading"])
        row = db.get_vod(self.conn, 100)
        self.assertEqual(row["state"], "done")

    def test_error_records_attempt_and_leaves_state(self):
        vod = {
            "id": 101,
            "title": "Stream Error",
            "started_at": "2026-10-23T12:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod101.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        def failing_step(c, cfg, v):
            raise RuntimeError("transient network failure")

        pipeline.STEPS = {"new": failing_step}

        with patch("k2y.kick.list_vods", return_value=[]):
            pipeline.run(self.cfg)

        row = db.get_vod(self.conn, 101)
        self.assertEqual(row["state"], "new")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("RuntimeError: transient network failure", row["error"])

    def test_three_errors_marks_failed(self):
        vod = {
            "id": 102,
            "title": "Stream Failing",
            "started_at": "2026-10-23T12:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod102.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        def failing_step(c, cfg, v):
            raise RuntimeError("persistent crash")

        pipeline.STEPS = {"new": failing_step}

        with patch("k2y.kick.list_vods", return_value=[]):
            for _ in range(3):
                pipeline.run(self.cfg)

        row = db.get_vod(self.conn, 102)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 3)
        self.assertIn("persistent crash", row["error"])

    def test_missing_secret_stops_run_without_attempt_and_logs_error(self):
        vod1 = {
            "id": 201,
            "title": "Stream 201",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod201.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)

        def step_missing_sec(c, cfg, v):
            raise config.MissingSecret("missing secret client_secret.json (see docs...)")

        pipeline.STEPS = {"new": step_missing_sec}

        with patch("k2y.kick.list_vods", return_value=[]), self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertTrue(any("missing secret client_secret.json" in msg and "run `py -m k2y auth`" in msg for msg in cm.output))

        row1 = db.get_vod(self.conn, 201)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIsNone(row1["error"])

    def test_auth_required_stops_run_without_attempt_and_logs_error(self):
        vod1 = {
            "id": 202,
            "title": "Stream 202",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod202.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)

        def step_auth_req(c, cfg, v):
            raise self.FakeAuthRequired("invalid_grant token expired")

        pipeline.STEPS = {"new": step_auth_req}

        with patch("k2y.kick.list_vods", return_value=[]), self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertTrue(any("invalid_grant" in msg and "run `py -m k2y auth`" in msg for msg in cm.output))

        row1 = db.get_vod(self.conn, 202)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIsNone(row1["error"])

    def test_quota_exceeded_stops_run_without_attempt_or_next_vod(self):
        vod1 = {
            "id": 201,
            "title": "Stream 201",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod201.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        vod2 = {
            "id": 202,
            "title": "Stream 202",
            "started_at": "2026-10-23T11:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod202.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)
        db.upsert_vod(self.conn, vod2)

        vod2_called = []

        def step_vod1(c, cfg, v):
            if v["id"] == 201:
                raise youtube.QuotaExceeded("Daily YouTube upload quota exceeded")
            vod2_called.append(v["id"])
            return "downloaded"

        pipeline.STEPS = {"new": step_vod1}

        with patch("k2y.kick.list_vods", return_value=[]):
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertEqual(vod2_called, [])

        row1 = db.get_vod(self.conn, 201)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIsNone(row1["error"])

        row2 = db.get_vod(self.conn, 202)
        self.assertEqual(row2["state"], "new")
        self.assertEqual(row2["attempts"], 0)

    def test_segment_daily_quota_stops_run_without_attempt(self):
        vod = {
            "id": 203,
            "title": "Stream 203",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod203.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        def step_quota(c, cfg, v):
            raise segment.DailyQuota("Gemini quota exhausted")

        pipeline.STEPS = {"new": step_quota}

        with patch("k2y.kick.list_vods", return_value=[]):
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        row = db.get_vod(self.conn, 203)
        self.assertEqual(row["state"], "new")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

    def test_step_plan_does_not_call_gemini_if_plan_json_exists(self):
        vod_id = 205
        plan = [{"start_s": 0, "end_s": 1800, "title": "P1"}]
        vod = {
            "id": vod_id,
            "title": "Stream 205",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 1800,
            "source_url": "http://kick.com/vod205.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.save_plan(self.conn, vod_id, plan)
        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        (v_dir / "source.mp4").write_bytes(b"content")

        row = db.get_vod(self.conn, vod_id)
        with patch("k2y.segment.plan") as mock_plan:
            res = pipeline.step_plan(self.conn, self.cfg, row)
            self.assertEqual(res, "planned")
            mock_plan.assert_not_called()

    def test_step_download_probes_duration_and_stores_in_db(self):
        vod_id = 206
        vod = {
            "id": vod_id,
            "title": "Stream 206",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod206.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        with patch("k2y.kick.download") as mock_dl, patch("k2y.kick.probe_duration", return_value=3542.9):
            row = db.get_vod(self.conn, vod_id)
            res = pipeline.step_download(self.conn, self.cfg, row)
            self.assertEqual(res, "downloaded")

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["duration_s"], 3542)

    def test_advance_resets_attempts_and_error_on_step_success(self):
        vod_id = 207
        vod = {
            "id": vod_id,
            "title": "Stream 207",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod207.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.record_error(self.conn, vod_id, "transient error")
        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "transient error")

        def succeed_download(c, cfg, v):
            return "downloaded"

        pipeline.STEPS = {"new": succeed_download}
        pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "downloaded")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

    def test_step_cut_ready_and_upload_incomplete_rows_return_planned(self):
        vod_id = 208
        plan = [{"start_s": 0, "end_s": 1800, "title": "P1"}, {"start_s": 1800, "end_s": 3600, "title": "P2"}]
        # expected rows: 3 (idx 0, 1, 2)
        vod = {
            "id": vod_id,
            "title": "Stream 208",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod208.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.save_plan(self.conn, vod_id, plan)
        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        src = v_dir / "source.mp4"
        src.write_bytes(b"source")

        # Insert only idx 0 and idx 1 (missing idx 2)
        p1 = v_dir / "part_1.mp4"
        p1.write_bytes(b"p1")
        t0 = v_dir / "thumb_0.jpg"
        t0.write_bytes(b"t0")
        t1 = v_dir / "thumb_1.jpg"
        t1.write_bytes(b"t1")
        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600, file=src, thumb=t0)
        db.upsert_upload(self.conn, vod_id, 1, "P1", 0, 1800, file=p1, thumb=t1)

        row = db.get_vod(self.conn, vod_id)
        # Missing row 2 with source existing -> returns "planned"
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "planned")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "planned")

        # Missing row 2 with source missing -> returns "new"
        src.unlink()
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "new")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "new")

    def test_step_cut_ready_missing_cut_file_returns_planned(self):
        vod_id = 209
        plan = [{"start_s": 0, "end_s": 3600, "title": "P1"}]  # 1 part -> expected count 1 (idx 0)
        vod = {
            "id": vod_id,
            "title": "Stream 209",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod209.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.save_plan(self.conn, vod_id, plan)
        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        src = v_dir / "source.mp4"
        src.write_bytes(b"source")
        thumb = v_dir / "thumb_0.jpg"
        # file exists in db but file does not exist on disk
        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600, file=v_dir / "missing.mp4", thumb=thumb)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "planned")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "planned")

        # If source is also missing -> returns "new"
        src.unlink()
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "new")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "new")

    def test_step_upload_rmtree_warning_on_error(self):
        vod_id = 210
        vod = {
            "id": vod_id,
            "title": "Stream 210",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod210.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        plan = [{"start_s": 0, "end_s": 3600, "title": "P1"}]
        db.save_plan(self.conn, vod_id, plan)
        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        src = v_dir / "source.mp4"
        src.write_bytes(b"source")
        thumb = v_dir / "thumb_0.jpg"
        thumb.write_bytes(b"thumb")
        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600, file=src, thumb=thumb)
        db.update_upload(self.conn, vod_id, 0, youtube_id="yt_already", thumb_done=1)

        row = db.get_vod(self.conn, vod_id)
        with patch("shutil.rmtree", side_effect=OSError("permission denied")), self.assertLogs("k2y", level="WARNING") as cm:
            res = pipeline.step_upload(self.conn, self.cfg, row)
            self.assertEqual(res, "done")
            self.assertTrue(any("Failed to remove VOD directory" in msg for msg in cm.output))

    def test_lock_prevents_second_concurrent_run(self):
        lock_path = self.work_dir / "k2y.lock"
        lock_path.write_text(f"{os.getpid()} {int(time.time())}\n")

        with patch("k2y.kick.list_vods") as mock_list:
            ret = pipeline.run(self.cfg)
            self.assertEqual(ret, 0)
            mock_list.assert_not_called()

    def test_lock_with_live_pid_never_stale_even_if_old(self):
        lock_path = self.work_dir / "k2y.lock"
        # 9 hours old with live PID -> NOT stale
        stale_time = int(time.time()) - (9 * 3600)
        lock_path.write_text(f"{os.getpid()} {stale_time}\n")

        with patch("k2y.kick.list_vods") as mock_list:
            ret = pipeline.run(self.cfg)
            self.assertEqual(ret, 0)
            mock_list.assert_not_called()

    def test_lock_replaces_stale_lock_by_dead_pid(self):
        lock_path = self.work_dir / "k2y.lock"
        dead_pid = 99999999
        lock_path.write_text(f"{dead_pid} {int(time.time())}\n")

        with patch("k2y.pipeline._is_pid_running", return_value=False):
            with patch("k2y.kick.list_vods", return_value=[]) as mock_list:
                ret = pipeline.run(self.cfg)
                self.assertEqual(ret, 0)
                mock_list.assert_called_once()

    def test_lock_staleness_when_pid_cannot_be_checked(self):
        lock_path = self.work_dir / "k2y.lock"
        now = int(time.time())

        # 9 hours old and PID check returns None -> stale, run proceeds
        lock_path.write_text(f"12345 {now - 9 * 3600}\n")
        with patch("k2y.pipeline._is_pid_running", return_value=None):
            with patch("k2y.kick.list_vods", return_value=[]) as mock_list:
                ret = pipeline.run(self.cfg)
                self.assertEqual(ret, 0)
                mock_list.assert_called_once()

        # 1 hour old and PID check returns None -> NOT stale, run skips
        lock_path.write_text(f"12345 {now - 3600}\n")
        with patch("k2y.pipeline._is_pid_running", return_value=None):
            with patch("k2y.kick.list_vods", return_value=[]) as mock_list:
                ret = pipeline.run(self.cfg)
                self.assertEqual(ret, 0)
                mock_list.assert_not_called()

    def test_lock_empty_file_staleness(self):
        lock_path = self.work_dir / "k2y.lock"

        # Empty and fresh (mtime = now) -> NOT stale, run skips
        lock_path.write_text("")
        with patch("k2y.kick.list_vods") as mock_list:
            ret = pipeline.run(self.cfg)
            self.assertEqual(ret, 0)
            mock_list.assert_not_called()

        # Empty and old (> 60s) -> stale, run proceeds
        lock_path.write_text("")
        old_time = time.time() - 100
        os.utime(lock_path, (old_time, old_time))
        with patch("k2y.kick.list_vods", return_value=[]) as mock_list:
            ret = pipeline.run(self.cfg)
            self.assertEqual(ret, 0)
            mock_list.assert_called_once()

    def test_lock_released_in_finally(self):
        lock_path = self.work_dir / "k2y.lock"
        self.assertFalse(lock_path.exists())

        with patch("k2y.kick.list_vods", return_value=[]):
            ret = pipeline.run(self.cfg)
            self.assertEqual(ret, 0)

        self.assertFalse(lock_path.exists())

    def test_setup_logging_tty_and_idempotent(self):
        log_dir = self.work_dir / "log_test"
        pipeline._logging_configured = False

        # When stderr is not a tty, no StreamHandler is added
        with patch.object(sys.stderr, "isatty", return_value=False):
            pipeline.setup_logging(log_dir)
            self.assertTrue(pipeline._logging_configured)
            root_handlers = logging.getLogger().handlers
            stream_handlers = [h for h in root_handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)]
            self.assertEqual(len(stream_handlers), 0)

        # Idempotent call does not duplicate
        pipeline.setup_logging(log_dir)
        self.assertTrue((log_dir / "k2y.log").exists())

    def test_discovery_skips_live_and_before_since(self):
        vod_live = {
            "id": 301,
            "title": "Live Stream",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/301",
            "category": "Gaming",
            "is_live": True,
        }
        vod_old = {
            "id": 302,
            "title": "Old Stream",
            "started_at": "2026-10-21T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/302",
            "category": "Gaming",
            "is_live": False,
        }
        vod_valid = {
            "id": 303,
            "title": "Valid Stream",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/303",
            "category": "Gaming",
            "is_live": False,
        }

        pipeline.STEPS = {}

        with patch("k2y.kick.list_vods", return_value=[vod_live, vod_old, vod_valid]):
            pipeline.run(self.cfg)

        self.assertIsNone(db.get_vod(self.conn, 301))
        self.assertIsNone(db.get_vod(self.conn, 302))
        self.assertIsNotNone(db.get_vod(self.conn, 303))

    def test_upload_resumes_with_session_and_skips_uploaded(self):
        vod_id = 401
        vod = {
            "id": vod_id,
            "title": "Stream 401",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/401",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.set_state(self.conn, vod_id, "uploading")
        plan = [
            {"start_s": 0, "end_s": 1800, "title": "Part 1", "description": "Desc 1"},
            {"start_s": 1800, "end_s": 3600, "title": "Part 2", "description": "Desc 2"},
        ]
        db.save_plan(self.conn, vod_id, plan)

        vod_dir = self.cfg.vod_dir(vod_id)
        vod_dir.mkdir(parents=True, exist_ok=True)
        file_0 = vod_dir / "source.mp4"
        file_0.write_bytes(b"dummy source")
        thumb_0 = vod_dir / "thumb_0.jpg"
        thumb_0.write_bytes(b"dummy thumb 0")

        file_1 = vod_dir / "part_1.mp4"
        file_1.write_bytes(b"dummy part 1")
        thumb_1 = vod_dir / "thumb_1.jpg"
        thumb_1.write_bytes(b"dummy thumb 1")

        file_2 = vod_dir / "part_2.mp4"
        file_2.write_bytes(b"dummy part 2")
        thumb_2 = vod_dir / "thumb_2.jpg"
        thumb_2.write_bytes(b"dummy thumb 2")

        # Row 0: already uploaded
        db.upsert_upload(
            self.conn,
            vod_id=vod_id,
            idx=0,
            title="Full VOD",
            start_s=0,
            end_s=3600,
            file=file_0,
            thumb=thumb_0,
        )
        db.update_upload(self.conn, vod_id, 0, youtube_id="yt_already_0", thumb_done=1)

        # Row 1: has session_url, no youtube_id yet
        db.upsert_upload(
            self.conn,
            vod_id=vod_id,
            idx=1,
            title="Part 1",
            start_s=0,
            end_s=1800,
            file=file_1,
            thumb=thumb_1,
        )
        db.update_upload(self.conn, vod_id, 1, upload_session="http://session/part1")

        # Row 2: already uploaded
        db.upsert_upload(
            self.conn,
            vod_id=vod_id,
            idx=2,
            title="Part 2",
            start_s=1800,
            end_s=3600,
            file=file_2,
            thumb=thumb_2,
        )
        db.update_upload(self.conn, vod_id, 2, youtube_id="yt_already_2", thumb_done=1)

        uploaded_files = []
        session_urls = []

        def fake_upload(path, meta, session_url, on_session):
            uploaded_files.append(Path(path).name)
            session_urls.append(session_url)
            return "yt_new_1"

        thumb_calls = []

        def fake_thumb(video_id, path):
            thumb_calls.append((video_id, Path(path).name))

        with patch("k2y.youtube.upload", side_effect=fake_upload), \
             patch("k2y.youtube.set_thumbnail", side_effect=fake_thumb):
            row = db.get_vod(self.conn, vod_id)
            res = pipeline.step_upload(self.conn, self.cfg, row)

        self.assertEqual(res, "done")
        self.assertEqual(uploaded_files, ["part_1.mp4"])
        self.assertEqual(session_urls, ["http://session/part1"])
        self.assertEqual(thumb_calls, [("yt_new_1", "thumb_1.jpg")])

        ups = db.uploads(self.conn, vod_id)
        self.assertEqual(ups[0]["youtube_id"], "yt_already_0")
        self.assertEqual(ups[1]["youtube_id"], "yt_new_1")
        self.assertEqual(ups[1]["thumb_done"], 1)
        self.assertEqual(ups[2]["youtube_id"], "yt_already_2")

    def test_missing_file_goes_back_to_new(self):
        vod_id = 501
        vod = {
            "id": vod_id,
            "title": "Stream 501",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/501",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.set_state(self.conn, vod_id, "downloaded")

        # source.mp4 does not exist in vod_dir
        pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "new")


if __name__ == "__main__":
    unittest.main()

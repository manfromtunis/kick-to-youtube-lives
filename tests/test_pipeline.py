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

from k2y import config, db, kick, media, pipeline, segment, youtube


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = Path(self.tmp.name) / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.stderr_patcher = patch("sys.stderr", io.StringIO())
        self.mock_stderr = self.stderr_patcher.start()

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
        # Clean up any open locks
        lock_path = self.work_dir / "k2y.lock"
        pipeline.release_lock(lock_path)
        pipeline.release_lock()
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
            raise config.MissingSecret("missing secret /path/client_secret.json (see docs...)")

        pipeline.STEPS = {"new": step_missing_sec}

        with patch("k2y.kick.list_vods", return_value=[]), self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertTrue(any("client_secret.json" in msg and "put the Google OAuth Desktop client file at that path (spec section 5)" in msg for msg in cm.output))

        row1 = db.get_vod(self.conn, 201)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIn("put the Google OAuth Desktop client file at that path (spec section 5)", row1["error"])

    def test_missing_secret_gemini_key_logs_str_e_without_auth_hint(self):
        vod1 = {
            "id": 2011,
            "title": "Stream 2011",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2011.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)

        def step_missing_gemini(c, cfg, v):
            raise config.MissingSecret("missing secret /path/gemini.key (see docs...)")

        pipeline.STEPS = {"new": step_missing_gemini}

        with patch("k2y.kick.list_vods", return_value=[]), self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        # Should log str(e), with NO auth hint
        self.assertTrue(any("gemini.key" in msg and "put the API key there" in msg for msg in cm.output))
        self.assertFalse(any("run `py -m k2y auth`" in msg for msg in cm.output))

        row1 = db.get_vod(self.conn, 2011)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIn("put the API key there", row1["error"])

    def test_auth_required_stops_run_without_attempt_and_logs_error_once(self):
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
            raise youtube.AuthRequired(youtube.AUTH_REQUIRED_MSG)

        pipeline.STEPS = {"new": step_auth_req}

        with patch("k2y.kick.list_vods", return_value=[]), self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        # Auth hint must appear exactly once in the logged message
        auth_hint = "run `py -m k2y auth`"
        matching = [msg for msg in cm.output if auth_hint in msg]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].count(auth_hint), 1)

        row1 = db.get_vod(self.conn, 202)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertEqual(row1["error"], youtube.AUTH_REQUIRED_MSG)

    def test_thumbnails_forbidden_stops_run_without_attempt_and_logs(self):
        vod1 = {
            "id": 2022,
            "title": "Stream 2022",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2022.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)

        class CustomThumbnailsForbidden(Exception):
            pass

        def step_thumb_forbid(c, cfg, v):
            raise CustomThumbnailsForbidden("Channel not allowed custom thumbnails; verify channel at https://www.youtube.com/verify")

        pipeline.STEPS = {"new": step_thumb_forbid}

        with patch.object(youtube, "ThumbnailsForbidden", CustomThumbnailsForbidden, create=True), \
             patch("k2y.kick.list_vods", return_value=[]), \
             self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertTrue(any("https://www.youtube.com/verify" in msg for msg in cm.output))

        row1 = db.get_vod(self.conn, 2022)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIn("https://www.youtube.com/verify", row1["error"])

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
        self.assertEqual(row1["error"], "Daily YouTube upload quota exceeded")

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
        self.assertEqual(row["error"], "Gemini quota exhausted")

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

        with patch("k2y.kick.download") as mock_dl, patch("k2y.kick.probe_duration", return_value=3542.9) as mock_probe:
            row = db.get_vod(self.conn, vod_id)
            res = pipeline.step_download(self.conn, self.cfg, row)
            self.assertEqual(res, "downloaded")
            # Pipeline probes at most once
            mock_probe.assert_called_once()

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["duration_s"], 3542)

    def test_advance_resets_attempts_and_error_on_forward_transition(self):
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

    def test_advance_keeps_attempts_on_backward_transition(self):
        vod_id = 2071
        vod = {
            "id": vod_id,
            "title": "Stream 2071",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2071.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.set_state(self.conn, vod_id, "cut")
        db.record_error(self.conn, vod_id, "transient error")
        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["attempts"], 1)

        # Backward transition: cut -> planned
        def fallback_step(c, cfg, v):
            return "planned"

        pipeline.STEPS = {"cut": fallback_step}
        pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "planned")
        # Backward transition keeps attempts!
        self.assertEqual(row["attempts"], 1)

    def test_advance_stops_after_20_transitions_and_records_counted_error(self):
        vod_id = 2072
        vod = {
            "id": vod_id,
            "title": "Stream 2072",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2072.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)

        def step_a(c, cfg, v):
            return "downloaded"

        def step_b(c, cfg, v):
            return "new"

        pipeline.STEPS = {"new": step_a, "downloaded": step_b}
        pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "too many state transitions")

    def test_step_cut_deletes_stale_upload_rows_without_youtube_id_or_session(self):
        vod_id = 2073
        vod = {
            "id": vod_id,
            "title": "Stream 2073",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2073.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        plan = [{"start_s": 0, "end_s": 3600, "title": "Single Part"}]  # 1 part -> expected = 1 (idx 0)
        db.save_plan(self.conn, vod_id, plan)

        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        src = v_dir / "source.mp4"
        src.write_bytes(b"content")

        # Stale row idx 1 (idx >= expected) without youtube_id or session
        db.upsert_upload(self.conn, vod_id, 1, "Stale Part 1", 1800, 3600)
        self.assertEqual(len(db.uploads(self.conn, vod_id)), 1)

        row = db.get_vod(self.conn, vod_id)
        with patch("k2y.media.thumbnail"):
            res = pipeline.step_cut(self.conn, self.cfg, row)
            self.assertEqual(res, "cut")

        ups = db.uploads(self.conn, vod_id)
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0]["idx"], 0)

    def test_step_cut_raises_on_stale_row_with_youtube_id_or_session(self):
        vod_id = 2074
        vod = {
            "id": vod_id,
            "title": "Stream 2074",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2074.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        plan = [{"start_s": 0, "end_s": 3600, "title": "Single Part"}]
        db.save_plan(self.conn, vod_id, plan)

        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        src = v_dir / "source.mp4"
        src.write_bytes(b"content")

        # Stale row with youtube_id
        db.upsert_upload(self.conn, vod_id, 2, "Stale Part 2", 1800, 3600)
        db.update_upload(self.conn, vod_id, 2, youtube_id="yt_already_stale")

        row = db.get_vod(self.conn, vod_id)
        with patch("k2y.media.thumbnail"), self.assertRaises(RuntimeError) as ctx:
            pipeline.step_cut(self.conn, self.cfg, row)

        self.assertIn("stale upload row idx 2 already on YouTube as yt_already_stale; resolve manually", str(ctx.exception))

        # Stale row with upload_session
        db.update_upload(self.conn, vod_id, 2, youtube_id=None, upload_session="http://session/stale")
        with patch("k2y.media.thumbnail"), self.assertRaises(RuntimeError) as ctx:
            pipeline.step_cut(self.conn, self.cfg, row)

        self.assertIn("stale upload row idx 2 upload in progress; resolve manually", str(ctx.exception))
        self.assertNotIn("http://session/stale", str(ctx.exception))

    def test_step_cut_ready_and_upload_incomplete_rows_return_planned(self):
        vod_id = 208
        plan = [{"start_s": 0, "end_s": 1800, "title": "P1"}, {"start_s": 1800, "end_s": 3600, "title": "P2"}]
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

        p1 = v_dir / "part_1.mp4"
        p1.write_bytes(b"p1")
        t0 = v_dir / "thumb_0.jpg"
        t0.write_bytes(b"t0")
        t1 = v_dir / "thumb_1.jpg"
        t1.write_bytes(b"t1")
        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600, file=src, thumb=t0)
        db.upsert_upload(self.conn, vod_id, 1, "P1", 0, 1800, file=p1, thumb=t1)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "planned")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "planned")

        src.unlink()
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "new")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "new")

    def test_step_cut_ready_missing_cut_file_returns_planned(self):
        vod_id = 209
        plan = [{"start_s": 0, "end_s": 3600, "title": "P1"}]
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
        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600, file=v_dir / "missing.mp4", thumb=thumb)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(pipeline.step_cut_ready(self.conn, self.cfg, row), "planned")
        self.assertEqual(pipeline.step_upload(self.conn, self.cfg, row), "planned")

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

    def test_lock_linux_second_acquire_fails_and_succeeds_after_release(self):
        lock_path = self.work_dir / "k2y.lock"
        # First acquire succeeds
        self.assertTrue(pipeline.acquire_lock(lock_path))

        # Second acquire in the same process via a separate fd fails while the first is held
        self.assertFalse(pipeline.acquire_lock(lock_path))

        # Release first lock
        pipeline.release_lock(lock_path)

        # Second acquire now succeeds
        self.assertTrue(pipeline.acquire_lock(lock_path))

        # Lock file is never deleted
        self.assertTrue(lock_path.exists())

        pipeline.release_lock(lock_path)
        self.assertTrue(lock_path.exists())

    def test_lock_windows_branch_msvcrt_mock(self):
        fake_msvcrt = MagicMock()
        fake_msvcrt.LK_NBLCK = 1
        fake_msvcrt.LK_UNLCK = 2
        lock_path = self.work_dir / "k2y_win.lock"

        with patch("k2y.pipeline.os.name", "nt"), patch.dict("sys.modules", {"msvcrt": fake_msvcrt}):
            self.assertTrue(pipeline.acquire_lock(lock_path))
            fake_msvcrt.locking.assert_called_once()
            call_args = fake_msvcrt.locking.call_args[0]
            self.assertEqual(call_args[1], fake_msvcrt.LK_NBLCK)
            self.assertEqual(call_args[2], 1)

            pipeline.release_lock(lock_path)
            self.assertEqual(fake_msvcrt.locking.call_count, 2)
            rel_args = fake_msvcrt.locking.call_args[0]
            self.assertEqual(rel_args[1], fake_msvcrt.LK_UNLCK)
            self.assertEqual(rel_args[2], 1)
            self.assertTrue(lock_path.exists())

        # Test failure to acquire when msvcrt.locking raises OSError
        fake_msvcrt_fail = MagicMock()
        fake_msvcrt_fail.LK_NBLCK = 1
        fake_msvcrt_fail.locking.side_effect = OSError(13, "Permission denied")

        with patch("k2y.pipeline.os.name", "nt"), patch.dict("sys.modules", {"msvcrt": fake_msvcrt_fail}):
            self.assertFalse(pipeline.acquire_lock(self.work_dir / "k2y_win_fail.lock"))

    def test_lock_prevents_second_concurrent_run_and_logs(self):
        lock_path = self.work_dir / "k2y.lock"
        self.assertTrue(pipeline.acquire_lock(lock_path))

        try:
            with patch("k2y.kick.list_vods") as mock_list, self.assertLogs("k2y", level="INFO") as cm:
                ret = pipeline.run(self.cfg)
                self.assertEqual(ret, 0)
                mock_list.assert_not_called()
                self.assertTrue(any("another run in progress" in msg for msg in cm.output))
        finally:
            pipeline.release_lock(lock_path)

    def test_discovery_robustness_skips_malformed_vod_and_continues(self):
        vod_valid_1 = {
            "id": 310,
            "title": "Valid Stream 1",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/310",
            "category": "Gaming",
            "is_live": False,
        }
        vod_malformed = {
            "id": 311,
            "started_at": "2026-10-23T10:30:00Z",
            # missing title, duration_s, source_url -> raises KeyError in upsert_vod
        }
        vod_valid_2 = {
            "id": 312,
            "title": "Valid Stream 2",
            "started_at": "2026-10-23T11:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/312",
            "category": "Gaming",
            "is_live": False,
        }

        pipeline.STEPS = {}

        with patch("k2y.kick.list_vods", return_value=[vod_valid_1, vod_malformed, vod_valid_2]), \
             self.assertLogs("k2y", level="WARNING") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertTrue(any("skipping Kick VOD 311" in msg for msg in cm.output))
        self.assertIsNotNone(db.get_vod(self.conn, 310))
        self.assertIsNotNone(db.get_vod(self.conn, 312))

    def test_setup_logging_tty_and_idempotent(self):
        log_dir = self.work_dir / "log_test"
        pipeline._logging_configured = False

        with patch.object(sys.stderr, "isatty", return_value=False):
            pipeline.setup_logging(log_dir)
            self.assertTrue(pipeline._logging_configured)
            root_handlers = logging.getLogger().handlers
            stream_handlers = [h for h in root_handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)]
            self.assertEqual(len(stream_handlers), 0)

        pipeline.setup_logging(log_dir)
        self.assertTrue((log_dir / "k2y.log").exists())

    def test_discovery_skips_before_since(self):
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

        with patch("k2y.kick.list_vods", return_value=[vod_old, vod_valid]):
            pipeline.run(self.cfg)

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

        pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "new")

    def test_k2y_task_pyw_logs_traceback_on_exception(self):
        import subprocess
        task_script = Path(__file__).resolve().parent.parent / "k2y-task.pyw"
        self.assertTrue(task_script.exists())

        env = dict(os.environ)
        env["K2Y_CONFIG_DIR"] = str(self.config_dir)
        (self.config_dir / "config.json").write_text(
            '{\n  "work_dir": "' + str(self.work_dir) + '",\n  "broken_syntax": \n}',
            encoding="utf-8",
        )

        proc = subprocess.run([sys.executable, str(task_script)], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)

        log_file = self.work_dir / "k2y.log"
        self.assertTrue(log_file.exists())
        log_content = log_file.read_text(encoding="utf-8")
        self.assertIn("JSONDecodeError", log_content)

    def test_missing_secret_youtube_token_logs_auth_hint(self):
        vod1 = {
            "id": 2012,
            "title": "Stream 2012",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod2012.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod1)

        def step_missing_yt(c, cfg, v):
            raise config.MissingSecret("missing secret /path/youtube_token.json (see docs...)")

        pipeline.STEPS = {"new": step_missing_yt}

        with patch("k2y.kick.list_vods", return_value=[]), self.assertLogs("k2y", level="ERROR") as cm:
            ret = pipeline.run(self.cfg)

        self.assertEqual(ret, 0)
        self.assertTrue(any("youtube_token.json" in msg and "run `py -m k2y auth`" in msg for msg in cm.output))

        row1 = db.get_vod(self.conn, 2012)
        self.assertEqual(row1["state"], "new")
        self.assertEqual(row1["attempts"], 0)
        self.assertIn("run `py -m k2y auth`", row1["error"])

    def test_expired_session_error_appends_vod_id_to_recorded_error(self):
        vod_id = 980
        vod = {
            "id": vod_id,
            "title": "Stream 980",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod980.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.set_state(self.conn, vod_id, "uploading")

        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        src = v_dir / "source.mp4"
        src.write_bytes(b"content")
        thumb = v_dir / "thumb_0.jpg"
        thumb.write_bytes(b"thumb")

        db.upsert_upload(self.conn, vod_id, 0, "Full VOD", 0, 3600, file=src, thumb=thumb)
        db.update_upload(self.conn, vod_id, 0, upload_session="http://expired.session")

        expired_msg = "upload session expired; check YouTube Studio for a finished duplicate of 'Stream 980', then run `py -m k2y retry <vod> --restart-upload`"

        with patch("k2y.youtube.upload", side_effect=youtube.YouTubeError(expired_msg)):
            pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertIn("then run `py -m k2y retry <vod> --restart-upload` (vod 980)", row["error"])

    def test_forward_transition_clears_error_from_stop_run(self):
        vod_id = 981
        vod = {
            "id": vod_id,
            "title": "Stream 981",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod981.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        db.set_error(self.conn, vod_id, "Temporary StopRun error")
        self.assertEqual(db.get_vod(self.conn, vod_id)["error"], "Temporary StopRun error")

        pipeline.STEPS = {"new": lambda c, cfg, v: "downloaded"}
        pipeline.advance(self.conn, self.cfg, vod_id)

        row = db.get_vod(self.conn, vod_id)
        self.assertEqual(row["state"], "downloaded")
        self.assertIsNone(row["error"])



    def test_pick_face_deterministic_and_different_per_idx(self):
        faces_dir = self.config_dir / "faces"
        faces_dir.mkdir(parents=True, exist_ok=True)
        (faces_dir / "b.png").write_bytes(b"b")
        (faces_dir / "a.jpg").write_bytes(b"a")
        (faces_dir / "d.webp").write_bytes(b"d")
        (faces_dir / "c.jpeg").write_bytes(b"c")
        (faces_dir / "ignore.txt").write_bytes(b"ignore")

        # sorted files: a.jpg, b.png, c.jpeg, d.webp (length 4)
        vod_id = 12
        # (12 * 31 + 0) % 4 = 372 % 4 = 0 -> a.jpg
        face0 = pipeline.pick_face(vod_id, 0, faces_dir=faces_dir)
        self.assertEqual(face0.name, "a.jpg")

        # (12 * 31 + 1) % 4 = 373 % 4 = 1 -> b.png
        face1 = pipeline.pick_face(vod_id, 1, faces_dir=faces_dir)
        self.assertEqual(face1.name, "b.png")
        self.assertNotEqual(face0, face1)

        # Deterministic: repeated call returns identical path
        self.assertEqual(pipeline.pick_face(vod_id, 0, faces_dir=faces_dir), face0)

    def test_pick_face_empty_or_missing_warns_once(self):
        pipeline.reset_faces_warned()
        missing_dir = self.config_dir / "non_existent_faces"

        with self.assertLogs("k2y", level="WARNING") as cm:
            res1 = pipeline.pick_face(1, 0, faces_dir=missing_dir)
            res2 = pipeline.pick_face(1, 1, faces_dir=missing_dir)

        self.assertIsNone(res1)
        self.assertIsNone(res2)
        # Warning logged only once
        self.assertEqual(len([m for m in cm.output if "Faces directory" in m]), 1)

        # Reset and test empty dir
        pipeline.reset_faces_warned()
        empty_dir = self.config_dir / "empty_faces"
        empty_dir.mkdir(parents=True, exist_ok=True)
        with self.assertLogs("k2y", level="WARNING") as cm2:
            res3 = pipeline.pick_face(2, 0, faces_dir=empty_dir)
        self.assertIsNone(res3)
        self.assertEqual(len([m for m in cm2.output if "Faces directory" in m]), 1)

    def test_load_plan_legacy_list_and_v2_dict(self):
        self.assertEqual(pipeline.load_plan(None), ([], ""))
        self.assertEqual(pipeline.load_plan(""), ([], ""))
        self.assertEqual(pipeline.load_plan("not-json"), ([], ""))

        # Legacy plan format (list)
        legacy_json = json.dumps([{"start_s": 0, "end_s": 100, "title": "Part 1"}])
        parts, vod_thumb = pipeline.load_plan(legacy_json)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["title"], "Part 1")
        self.assertEqual(vod_thumb, "")

        # v2 plan format (dict)
        v2_json = json.dumps({
            "parts": [{"start_s": 0, "end_s": 100, "title": "Part 1", "thumb_text": "HOOK P1"}],
            "vod_thumb_text": "HOOK VOD"
        })
        parts_v2, vod_thumb_v2 = pipeline.load_plan(v2_json)
        self.assertEqual(len(parts_v2), 1)
        self.assertEqual(parts_v2[0]["thumb_text"], "HOOK P1")
        self.assertEqual(vod_thumb_v2, "HOOK VOD")

    def test_expected_upload_count_with_dict(self):
        self.assertEqual(pipeline.expected_upload_count({"parts": []}), 1)
        self.assertEqual(pipeline.expected_upload_count({"parts": [{"title": "P1"}]}), 1)
        self.assertEqual(pipeline.expected_upload_count({"parts": [{"title": "P1"}, {"title": "P2"}]}), 3)

    def test_step_cut_with_legacy_and_v2_plans(self):
        faces_dir = self.config_dir / "faces"
        faces_dir.mkdir(parents=True, exist_ok=True)
        (faces_dir / "face1.png").write_bytes(b"fake face")

        # 1. Test with legacy plan
        vod_id = 991
        vod = {
            "id": vod_id,
            "title": "Stream Titre Important",
            "started_at": "2026-10-23T10:00:00Z",
            "duration_s": 3600,
            "source_url": "http://kick.com/vod991.m3u8",
            "category": "Gaming",
            "is_live": False,
        }
        db.upsert_vod(self.conn, vod)
        legacy_plan = [
            {"start_s": 0, "end_s": 1800, "title": "Partie Un Epique"},
            {"start_s": 1800, "end_s": 3600, "title": "Partie Deux Finale"},
        ]
        db.save_plan(self.conn, vod_id, legacy_plan)

        v_dir = self.cfg.vod_dir(vod_id)
        v_dir.mkdir(parents=True, exist_ok=True)
        (v_dir / "source.mp4").write_bytes(b"fake video")

        row = db.get_vod(self.conn, vod_id)
        thumb_calls = []

        def fake_thumb(src, at_s, hook, dest, face=None):
            thumb_calls.append((dest.name, hook, face.name if face else None))
            dest.write_bytes(b"fake thumb")
            return dest

        with (
            patch("k2y.media.cut", return_value=v_dir / "part_1.mp4"),
            patch("k2y.media.thumbnail", side_effect=fake_thumb),
        ):
            res = pipeline.step_cut(self.conn, self.cfg, row)
            self.assertEqual(res, "cut")

        # Full VOD (thumb_0) hook falls back to stream title, parts fall back to part titles
        self.assertEqual(len(thumb_calls), 3)
        self.assertEqual(thumb_calls[0][0], "thumb_0.jpg")
        self.assertEqual(thumb_calls[0][1], "STREAM TITRE IMPORTANT")
        self.assertEqual(thumb_calls[0][2], "face1.png")

        self.assertEqual(thumb_calls[1][0], "thumb_1.jpg")
        self.assertEqual(thumb_calls[1][1], "PARTIE UN EPIQUE")

        self.assertEqual(thumb_calls[2][0], "thumb_2.jpg")
        self.assertEqual(thumb_calls[2][1], "PARTIE DEUX FINALE")

if __name__ == "__main__":
    unittest.main()

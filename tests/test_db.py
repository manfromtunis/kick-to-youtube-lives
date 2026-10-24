"""Tests for k2y/db.py."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from k2y import db


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "sub" / "k2y.db"
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_connect_creates_parent_dir_and_tables(self):
        self.assertTrue(self.db_path.exists())
        # Check WAL mode
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        # Check foreign keys
        fk = self.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)

        # Check tables
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("vods", tables)
        self.assertIn("uploads", tables)

    def test_upsert_vod(self):
        vod = {
            "id": 101,
            "title": "Stream 101",
            "started_at": "2026-10-22T20:00:00Z",
            "duration_s": 7200,
            "source_url": "https://stream.kick.com/101/master.m3u8",
            "category": "Gaming",
        }
        # First insert returns True
        self.assertTrue(db.upsert_vod(self.conn, vod))

        row = db.get_vod(self.conn, 101)
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], 101)
        self.assertEqual(row["title"], "Stream 101")
        self.assertEqual(row["state"], "new")
        self.assertEqual(row["category"], "Gaming")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

        # Second insert returns False and doesn't overwrite
        modified_vod = dict(vod, title="Overwritten Title")
        self.assertFalse(db.upsert_vod(self.conn, modified_vod))
        row = db.get_vod(self.conn, 101)
        self.assertEqual(row["title"], "Stream 101")

    def test_pending_vods_order_and_states(self):
        v1 = {"id": 1, "title": "V1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        v2 = {"id": 2, "title": "V2", "started_at": "2026-10-22T08:00:00Z", "duration_s": 100, "source_url": "url2"}
        v3 = {"id": 3, "title": "V3", "started_at": "2026-10-22T12:00:00Z", "duration_s": 100, "source_url": "url3"}
        v4 = {"id": 4, "title": "V4", "started_at": "2026-10-22T09:00:00Z", "duration_s": 100, "source_url": "url4"}
        v5 = {"id": 5, "title": "V5", "started_at": "2026-10-22T07:00:00Z", "duration_s": 100, "source_url": "url5"}

        for v in [v1, v2, v3, v4, v5]:
            db.upsert_vod(self.conn, v)

        # Mark terminal states
        db.set_state(self.conn, 1, "done")
        db.set_state(self.conn, 3, "failed")
        db.set_state(self.conn, 5, "skipped")

        # Pending should be v2 (08:00) then v4 (09:00)
        pending = db.pending_vods(self.conn)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["id"], 2)
        self.assertEqual(pending[1]["id"], 4)

    def test_set_state_resets_attempts_and_updates_error(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)

        # Set error
        db.set_state(self.conn, 1, "downloaded", error="Network timeout")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "downloaded")
        self.assertEqual(row["error"], "Network timeout")

        # Record attempt
        db.record_error(self.conn, 1, "Another error", max_attempts=3)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["attempts"], 1)

        # Reset attempts on state change when error is None
        db.set_state(self.conn, 1, "planned")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "planned")
        self.assertIsNone(row["error"])
        self.assertEqual(row["attempts"], 0)

    def test_set_duration(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)

        db.set_duration(self.conn, 1, 3599)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["duration_s"], 3599)

    def test_delete_uploads(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)
        db.upsert_upload(self.conn, 1, 0, "Full VOD", 0, 100)
        db.upsert_upload(self.conn, 1, 1, "Part 1", 0, 50)
        self.assertEqual(len(db.uploads(self.conn, 1)), 2)

        # Rows without youtube_id or upload_session are deleted cleanly
        db.delete_uploads(self.conn, 1)
        self.assertEqual(len(db.uploads(self.conn, 1)), 0)

    def test_delete_uploads_raises_when_rows_have_youtube_id_or_session(self):
        vod = {"id": 2, "title": "VOD 2", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url2"}
        db.upsert_vod(self.conn, vod)
        db.upsert_upload(self.conn, 2, 0, "Full VOD", 0, 100)
        db.upsert_upload(self.conn, 2, 1, "Part 1", 0, 50)
        db.update_upload(self.conn, 2, 0, youtube_id="yt_existing")

        with self.assertRaises(RuntimeError) as ctx:
            db.delete_uploads(self.conn, 2)
        self.assertIn("cannot delete uploads for VOD 2", str(ctx.exception))

        # Check that NO rows were deleted (both idx 0 and idx 1 remain)
        ups = db.uploads(self.conn, 2)
        self.assertEqual(len(ups), 2)

        # Test upload_session as well
        db.update_upload(self.conn, 2, 1, upload_session="http://upload.session")
        with self.assertRaises(RuntimeError):
            db.delete_uploads(self.conn, 2)
        self.assertEqual(len(db.uploads(self.conn, 2)), 2)

    def test_set_error_does_not_increment_attempts_or_change_state(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)
        db.set_state(self.conn, 1, "uploading")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "uploading")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

        db.set_error(self.conn, 1, "Quota exceeded")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "uploading")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["error"], "Quota exceeded")

    def test_clear_upload_sessions(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)
        db.upsert_upload(self.conn, 1, 0, "Full VOD", 0, 100)
        db.update_upload(self.conn, 1, 0, upload_session="http://session/0")
        db.upsert_upload(self.conn, 1, 1, "Part 1", 0, 50)
        db.update_upload(self.conn, 1, 1, youtube_id="yt_1", upload_session="http://session/1")

        cleared = db.clear_upload_sessions(self.conn, 1)
        self.assertEqual(cleared, 1)

        ups = db.uploads(self.conn, 1)
        self.assertIsNone(ups[0]["upload_session"])
        self.assertEqual(ups[1]["upload_session"], "http://session/1")

    def test_get_upload_and_set_upload_youtube_id(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)
        self.assertIsNone(db.get_upload(self.conn, 1, 0))

        db.upsert_upload(self.conn, 1, 0, "Full VOD", 0, 100)
        db.update_upload(self.conn, 1, 0, upload_session="http://session/0", thumb_done=1)

        row = db.get_upload(self.conn, 1, 0)
        self.assertIsNotNone(row)
        self.assertEqual(row["idx"], 0)
        self.assertEqual(row["upload_session"], "http://session/0")

        db.set_upload_youtube_id(self.conn, 1, 0, "yt_manual_id")
        updated = db.get_upload(self.conn, 1, 0)
        self.assertEqual(updated["youtube_id"], "yt_manual_id")
        self.assertIsNone(updated["upload_session"])
        self.assertEqual(updated["thumb_done"], 1)

    def test_record_error_increments_and_fails_at_max(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)

        db.record_error(self.conn, 1, "err 1", max_attempts=3)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "err 1")
        self.assertEqual(row["state"], "new")

        db.record_error(self.conn, 1, "err 2", max_attempts=3)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["error"], "err 2")
        self.assertEqual(row["state"], "new")

        db.record_error(self.conn, 1, "err 3", max_attempts=3)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["error"], "err 3")
        self.assertEqual(row["state"], "failed")

    def test_reset(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)
        db.record_error(self.conn, 1, "err 1", max_attempts=1)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "failed")

        db.reset(self.conn, 1, "downloaded")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "downloaded")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

    def test_save_plan(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)

        plan = [{"part": 1, "start_s": 0, "end_s": 100, "title": "Intro"}]
        db.save_plan(self.conn, 1, plan)

        row = db.get_vod(self.conn, 1)
        self.assertEqual(json.loads(row["plan_json"]), plan)

    def test_uploads_upsert_and_update(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)

        # Insert uploads
        db.upsert_upload(self.conn, 1, 1, "Part 1", 0, 50, file=Path("/tmp/p1.mp4"), thumb=Path("/tmp/p1.jpg"))
        db.upsert_upload(self.conn, 1, 0, "Full VOD", 0, 100)

        ups = db.uploads(self.conn, 1)
        self.assertEqual(len(ups), 2)
        # Should be ordered by idx
        self.assertEqual(ups[0]["idx"], 0)
        self.assertEqual(ups[1]["idx"], 1)
        self.assertEqual(ups[1]["file"], "/tmp/p1.mp4")
        self.assertEqual(ups[1]["thumb"], "/tmp/p1.jpg")

        # Update upload with youtube_id and thumb_done
        db.update_upload(self.conn, 1, 0, youtube_id="yt_123", thumb_done=1, non_existent_col="ignored")
        row = db.uploads(self.conn, 1)[0]
        self.assertEqual(row["youtube_id"], "yt_123")
        self.assertEqual(row["thumb_done"], 1)

        # Upserting again preserves youtube_id
        db.upsert_upload(self.conn, 1, 0, "Full VOD Updated", 0, 100)
        row = db.uploads(self.conn, 1)[0]
        self.assertEqual(row["title"], "Full VOD Updated")
        self.assertEqual(row["youtube_id"], "yt_123")


if __name__ == "__main__":
    unittest.main()

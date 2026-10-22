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

    def test_get_vod_not_found(self):
        self.assertIsNone(db.get_vod(self.conn, 999))

    def test_pending_vods_ordering_and_filtering(self):
        vods = [
            {"id": 1, "title": "VOD 1", "started_at": "2026-10-20T10:00:00Z", "duration_s": 100, "source_url": "url1"},
            {"id": 2, "title": "VOD 2", "started_at": "2026-10-18T10:00:00Z", "duration_s": 100, "source_url": "url2"},
            {"id": 3, "title": "VOD 3", "started_at": "2026-10-19T10:00:00Z", "duration_s": 100, "source_url": "url3"},
            {"id": 4, "title": "VOD 4", "started_at": "2026-10-21T10:00:00Z", "duration_s": 100, "source_url": "url4"},
            {"id": 5, "title": "VOD 5", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url5"},
        ]
        for v in vods:
            db.upsert_vod(self.conn, v)

        db.set_state(self.conn, 1, "done")
        db.set_state(self.conn, 4, "failed")
        db.set_state(self.conn, 5, "skipped")
        db.set_state(self.conn, 2, "downloaded")
        db.set_state(self.conn, 3, "new")

        pending = db.pending_vods(self.conn)
        pending_ids = [r["id"] for r in pending]
        # Should exclude 1, 4, 5, and order by started_at (VOD 2 was 2026-10-18, VOD 3 was 2026-10-19)
        self.assertEqual(pending_ids, [2, 3])

    def test_set_state_resets_attempts_and_error(self):
        vod = {"id": 1, "title": "VOD 1", "started_at": "2026-10-22T10:00:00Z", "duration_s": 100, "source_url": "url1"}
        db.upsert_vod(self.conn, vod)

        db.record_error(self.conn, 1, "transient error", max_attempts=3)
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "transient error")

        # Step succeeds: set_state called without error clears attempts and error
        db.set_state(self.conn, 1, "downloaded")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["state"], "downloaded")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

        # When error is explicitly provided, it sets the error
        db.set_state(self.conn, 1, "downloaded", error="explicit error")
        row = db.get_vod(self.conn, 1)
        self.assertEqual(row["error"], "explicit error")

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

        db.delete_uploads(self.conn, 1)
        self.assertEqual(len(db.uploads(self.conn, 1)), 0)

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

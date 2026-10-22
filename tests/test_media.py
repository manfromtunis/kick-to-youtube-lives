"""Tests for k2y/media.py."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from k2y.media import (
    _filter_path,
    chapters,
    cut,
    full_description,
    full_title,
    hms,
    part_description,
    part_title,
    probe_duration,
    thumbnail,
    wrap_title,
)


class TestHMS(unittest.TestCase):
    def test_under_one_hour(self):
        self.assertEqual(hms(0), "0:00")
        self.assertEqual(hms(5), "0:05")
        self.assertEqual(hms(59), "0:59")
        self.assertEqual(hms(60), "1:00")
        self.assertEqual(hms(75), "1:15")
        self.assertEqual(hms(3599), "59:59")

    def test_over_one_hour(self):
        self.assertEqual(hms(3600), "1:00:00")
        self.assertEqual(hms(3725), "1:02:05")
        self.assertEqual(hms(7200), "2:00:00")
        self.assertEqual(hms(7265), "2:01:05")


class TestChapters(unittest.TestCase):
    def test_accepts_good_list(self):
        items = [(0, "Intro"), (15, "Middle"), (30, "Outro")]
        expected = "0:00 Intro\n0:15 Middle\n0:30 Outro"
        self.assertEqual(chapters(items), expected)

    def test_sorts_by_time_first(self):
        items = [(30, "Outro"), (0, "Intro"), (15, "Middle")]
        expected = "0:00 Intro\n0:15 Middle\n0:30 Outro"
        self.assertEqual(chapters(items), expected)

    def test_rejects_no_zero_start(self):
        items = [(5, "Intro"), (20, "Middle"), (40, "Outro")]
        self.assertEqual(chapters(items), "")

    def test_rejects_fewer_than_three_items(self):
        self.assertEqual(chapters([]), "")
        self.assertEqual(chapters([(0, "Intro")]), "")
        self.assertEqual(chapters([(0, "Intro"), (15, "Outro")]), "")

    def test_rejects_gap_under_ten_seconds(self):
        # Gap between item 0 and 1 is 9s (< 10s)
        items = [(0, "Intro"), (9, "Middle"), (25, "Outro")]
        self.assertEqual(chapters(items), "")

        # Gap between item 1 and 2 is 8s (< 10s)
        items2 = [(0, "Intro"), (15, "Middle"), (23, "Outro")]
        self.assertEqual(chapters(items2), "")

        # Duplicate timestamps (gap 0s)
        items3 = [(0, "Intro"), (0, "Intro 2"), (20, "Outro")]
        self.assertEqual(chapters(items3), "")

    def test_optional_end_s(self):
        items = [(0, "Intro"), (15, "Middle"), (30, "Outro")]
        # Gap to end is 5s (< 10s)
        self.assertEqual(chapters(items, end_s=35), "")
        # Gap to end is 10s (>= 10s)
        self.assertNotEqual(chapters(items, end_s=40), "")


class TestTitles(unittest.TestCase):
    def test_full_title_normal(self):
        res = full_title("Chill gaming session", "2026-10-22 12:15:22")
        self.assertEqual(res, "Chill gaming session | Stream du 22/10/2026")
        self.assertLessEqual(len(res), 100)

    def test_full_title_iso_format(self):
        res = full_title("Chill gaming session", "2026-10-22T12:15:22Z")
        self.assertEqual(res, "Chill gaming session | Stream du 22/10/2026")

    def test_full_title_truncation(self):
        long_title = "A" * 150
        res = full_title(long_title, "2026-10-22 12:15:22")
        self.assertLessEqual(len(res), 100)
        self.assertTrue(res.endswith(" | Stream du 22/10/2026"))
        self.assertEqual(len(res), 100)

    def test_part_title_normal(self):
        res = part_title("Boss Fight", 1, 3)
        self.assertEqual(res, "Boss Fight | Partie 1/3")
        self.assertLessEqual(len(res), 100)

    def test_part_title_truncation(self):
        long_title = "B" * 150
        res = part_title(long_title, 2, 5)
        self.assertLessEqual(len(res), 100)
        self.assertTrue(res.endswith(" | Partie 2/5"))
        self.assertEqual(len(res), 100)


class TestDescriptions(unittest.TestCase):
    def test_full_description_with_qualifying_chapters(self):
        parts = [
            {"start_s": 0, "title": "Part 1: Setup"},
            {"start_s": 60, "title": "Part 2: Gameplay"},
            {"start_s": 120, "title": "Part 3: Boss fight"},
        ]
        desc = full_description(parts, "randomguy42")
        expected_chapters = "0:00 Part 1: Setup\n1:00 Part 2: Gameplay\n2:00 Part 3: Boss fight"
        expected = f"{expected_chapters}\n\nLive sur https://kick.com/randomguy42"
        self.assertEqual(desc, expected)

    def test_full_description_without_qualifying_chapters(self):
        parts = [
            {"start_s": 0, "title": "Part 1: Setup"},
        ]
        desc = full_description(parts, "randomguy42")
        self.assertEqual(desc, "Live sur https://kick.com/randomguy42")

    def test_full_description_truncation(self):
        parts = [
            {"start_s": 0, "title": "P0"},
            {"start_s": 20, "title": "P1"},
            {"start_s": 40, "title": "P2"},
        ]
        desc = full_description(parts, "x" * 6000)
        self.assertLessEqual(len(desc), 5000)

    def test_part_description_with_full_video_id(self):
        part = {
            "start_s": 100,
            "end_s": 300,
            "title": "Exploration",
            "description": "Exploring the open world.",
            "key_moments": [
                {"at_s": 130, "what": "Found a chest"},
                {"at_s": 200, "what": "Fought wolves"},
            ],
        }
        desc = part_description(part, "abc123xyz", "randomguy42")
        expected_chaps = "0:00 Exploration\n0:30 Found a chest\n1:40 Fought wolves"
        expected_links = "Stream complet: https://youtu.be/abc123xyz\nLive sur https://kick.com/randomguy42"
        expected = f"Exploring the open world.\n\n{expected_chaps}\n\n{expected_links}"
        self.assertEqual(desc, expected)

    def test_part_description_moment_at_start_not_duplicated(self):
        part = {
            "start_s": 100,
            "end_s": 300,
            "title": "Exploration",
            "description": "Exploring.",
            "key_moments": [
                {"at_s": 100, "what": "Custom Start"},
                {"at_s": 130, "what": "Found a chest"},
                {"at_s": 200, "what": "Fought wolves"},
            ],
        }
        desc = part_description(part, None, "randomguy42")
        expected_chaps = "0:00 Custom Start\n0:30 Found a chest\n1:40 Fought wolves"
        expected = f"Exploring.\n\n{expected_chaps}\n\nLive sur https://kick.com/randomguy42"
        self.assertEqual(desc, expected)
        self.assertNotIn("Stream complet:", desc)

    def test_part_description_without_qualifying_chapters(self):
        part = {
            "start_s": 0,
            "end_s": 100,
            "title": "Exploration",
            "description": "Just playing.",
            "key_moments": [],
        }
        desc = part_description(part, "vid456", "randomguy42")
        expected = "Just playing.\n\nStream complet: https://youtu.be/vid456\nLive sur https://kick.com/randomguy42"
        self.assertEqual(desc, expected)


class TestWrapTitle(unittest.TestCase):
    def test_wrap_short_title(self):
        self.assertEqual(wrap_title("Short"), "Short")

    def test_wrap_two_lines(self):
        t = "This is a medium length title that fits on two"
        wrapped = wrap_title(t)
        lines = wrapped.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line), 24)

    def test_wrap_three_lines_truncated(self):
        t = "A very very very very long title that exceeds two lines completely and goes on"
        wrapped = wrap_title(t)
        lines = wrapped.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("…"))
        for line in lines:
            self.assertLessEqual(len(line), 24)


class TestFilterPath(unittest.TestCase):
    def test_windows_path_conversion_and_escaping(self):
        p = Path(r"C:\Windows\Fonts\arialbd.ttf")
        filtered = _filter_path(p)
        self.assertEqual(filtered, r"C\:/Windows/Fonts/arialbd.ttf")

    def test_windows_path_with_single_quote(self):
        p = Path(r"C:\Users\Melek's Files\font.ttf")
        filtered = _filter_path(p)
        self.assertEqual(filtered, r"C\:/Users/Melek\\\'s Files/font.ttf")

    def test_font_fallback_omits_fontfile(self):
        with patch("k2y.media._find_font", return_value=None), \
             patch("k2y.media.probe_duration", return_value=20.0), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with tempfile.NamedTemporaryFile(suffix=".mp4") as src, \
                 tempfile.NamedTemporaryFile(suffix=".jpg") as dst:
                dst.write(b"fake jpeg content")
                dst.flush()
                thumbnail(Path(src.name), 10, "Title", Path(dst.name))
                args = mock_run.call_args[0][0]
                vf_idx = args.index("-vf")
                vf_str = args[vf_idx + 1]
                self.assertNotIn("fontfile=", vf_str)
                self.assertIn("textfile=", vf_str)


class TestMediaTimeouts(unittest.TestCase):
    @patch("subprocess.run")
    def test_cut_timeout(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "cut_timeout.mp4"
            dest.write_bytes(b"partial")
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=7200)
            with self.assertRaises(RuntimeError) as ctx:
                cut(Path("src.mp4"), 0, 10, dest)
            self.assertIn("timed out after 2 hours", str(ctx.exception))
            self.assertFalse(dest.exists())

    @patch("subprocess.run")
    def test_probe_duration_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=120)
        with self.assertRaises(RuntimeError) as ctx:
            probe_duration(Path("src.mp4"))
        self.assertIn("timed out after 120 seconds", str(ctx.exception))

    @patch("k2y.media.probe_duration", return_value=50.0)
    @patch("subprocess.run")
    def test_thumbnail_timeout(self, mock_run, mock_probe):
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "thumb_timeout.jpg"
            dest.write_bytes(b"partial")
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)
            with self.assertRaises(RuntimeError) as ctx:
                thumbnail(Path("src.mp4"), 10, "Title", dest)
            self.assertIn("timed out after 120 seconds", str(ctx.exception))
            self.assertFalse(dest.exists())


class TestMediaFFmpeg(unittest.TestCase):
    td = None
    synthetic_video = None

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.temp_dir = Path(cls.td.name)
        cls.synthetic_video = cls.temp_dir / "synthetic_20s.mp4"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "lavfi", "-i", "testsrc=duration=20:size=1280x720:rate=30",
            "-f", "lavfi", "-i", "sine=duration=20",
            "-shortest",
            "-c:v", "libx264",
            "-g", "30",
            "-c:a", "aac",
            str(cls.synthetic_video),
        ]
        subprocess.run(cmd, check=True)

    @classmethod
    def tearDownClass(cls):
        if cls.td is not None:
            cls.td.cleanup()

    def test_cut(self):
        dest = self.temp_dir / "cut_5_15.mp4"
        out_path = cut(self.synthetic_video, 5, 15, dest)
        self.assertEqual(out_path, dest)
        self.assertTrue(dest.exists())

        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(dest),
        ]
        res = subprocess.run(probe_cmd, check=True, capture_output=True, text=True)
        duration = float(res.stdout.strip())
        self.assertAlmostEqual(duration, 10.0, delta=2.0)

    def test_cut_failure_raises_runtime_error(self):
        bad_src = self.temp_dir / "non_existent.mp4"
        dest = self.temp_dir / "failed.mp4"
        with self.assertRaises(RuntimeError):
            cut(bad_src, 0, 5, dest)

    def test_thumbnail_with_special_characters(self):
        title = "L'aventure continue : début en beauté"
        dest = self.temp_dir / "thumb_special.jpg"
        out_path = thumbnail(self.synthetic_video, 3, title, dest)
        self.assertEqual(out_path, dest)
        self.assertTrue(dest.exists())

        size = dest.stat().st_size
        self.assertLess(size, 2 * 1024 * 1024)
        self.assertGreater(size, 0)

        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name",
            "-of", "json",
            str(dest),
        ]
        res = subprocess.run(probe_cmd, check=True, capture_output=True, text=True)
        info = json.loads(res.stdout)
        stream = info["streams"][0]
        self.assertEqual(stream["codec_name"], "mjpeg")
        self.assertEqual(stream["width"], 1280)
        self.assertEqual(stream["height"], 720)

    def test_thumbnail_clamps_at_s_and_verifies_non_empty(self):
        # probed_duration is ~20s, max allowed at_s is 19
        dest = self.temp_dir / "thumb_clamped.jpg"
        out_path = thumbnail(self.synthetic_video, at_s=500, title="Far out", dest=dest)
        self.assertEqual(out_path, dest)
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 0)

    def test_thumbnail_empty_dest_raises_runtime_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            dest = self.temp_dir / "non_existent_thumb.jpg"
            dest.unlink(missing_ok=True)
            with self.assertRaises(RuntimeError) as ctx:
                thumbnail(self.synthetic_video, 5, "Title", dest)
            self.assertIn("missing or empty", str(ctx.exception))

    def test_thumbnail_real_ffmpeg_with_apostrophe_and_space_in_dir(self):
        # Real ffmpeg run verifying drawtext text file and apostrophe escaping in directory name
        with tempfile.TemporaryDirectory(dir=self.temp_dir, prefix="test 'dir with space ") as custom_td:
            custom_dir = Path(custom_td)
            dest = custom_dir / "thumb.jpg"
            txt_file = dest.with_suffix(".txt")
            out_path = thumbnail(self.synthetic_video, at_s=2, title="Melek's 'Special' Stream", dest=dest)
            self.assertEqual(out_path, dest)
            self.assertTrue(dest.exists())
            self.assertGreater(dest.stat().st_size, 0)
            # Text file created next to dest and deleted afterwards
            self.assertFalse(txt_file.exists())


if __name__ == "__main__":
    unittest.main()

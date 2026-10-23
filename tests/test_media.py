"""Tests for k2y/media.py."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from k2y.kick import KickError
from k2y.media import (
    _escape_font_path,
    chapters,
    cut,
    format_duration,
    format_link,
    full_description,
    full_title,
    hms,
    part_description,
    part_title,
    probe_duration,
    hook_lines,
    sanitize_hashtags,
    sanitize_hook,
    sanitize_tags,
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


DEFAULT_LINKS_BLOCK = (
    "🔴 En live sur Kick : https://kick.com/randomguy42\n"
    "📸 Instagram : https://www.instagram.com/ghmelek/\n"
    "🛠️ AuraStack : https://aurastack.link/\n"
    "💸 Clip2Earn : https://www.clip2earn.money/\n"
    "📱 Bakchich : https://www.instagram.com/bakchich.app/"
)


class TestDescriptions(unittest.TestCase):
    def test_full_description_with_qualifying_chapters(self):
        parts = [
            {"start_s": 0, "title": "Part 1: Setup"},
            {"start_s": 60, "title": "Part 2: Gameplay"},
            {"start_s": 120, "title": "Part 3: Boss fight"},
        ]
        desc = full_description(parts, "randomguy42")
        expected_chapters = "⏱️ Chapitres\n0:00 Part 1: Setup\n1:00 Part 2: Gameplay\n2:00 Part 3: Boss fight"
        expected = f"{expected_chapters}\n\n{DEFAULT_LINKS_BLOCK}"
        self.assertEqual(desc, expected)

    def test_full_description_without_qualifying_chapters(self):
        parts = [
            {"start_s": 0, "title": "Part 1: Setup"},
        ]
        desc = full_description(parts, "randomguy42")
        self.assertEqual(desc, DEFAULT_LINKS_BLOCK)

    def test_full_description_truncation(self):
        parts = [
            {"start_s": 0, "title": "P0"},
            {"start_s": 20, "title": "P1"},
            {"start_s": 40, "title": "P2"},
        ]
        desc = full_description(parts, "x" * 6000)
        self.assertLessEqual(len(desc.encode("utf-8")), 5000)

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
        all_parts = [
            part,
            {"start_s": 300, "end_s": 500, "title": "Boss", "youtube_id": "part2_id"},
        ]
        desc = part_description(
            part,
            full_video_id="abc123xyz",
            channel="randomguy42",
            all_parts=all_parts,
            full_duration_s=500,
        )
        expected_chaps = "⏱️ Chapitres\n0:00 Exploration\n0:30 Found a chest\n1:40 Fought wolves"
        expected_nav = (
            "📺 Les autres parties de ce live\n"
            "2/2 Boss https://youtu.be/part2_id\n"
            "▶️ Le stream complet (8min) : https://youtu.be/abc123xyz"
        )
        expected = f"Exploring the open world.\n\n{expected_chaps}\n\n{expected_nav}\n\n{DEFAULT_LINKS_BLOCK}"
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
        expected_chaps = "⏱️ Chapitres\n0:00 Custom Start\n0:30 Found a chest\n1:40 Fought wolves"
        expected = f"Exploring.\n\n{expected_chaps}\n\n{DEFAULT_LINKS_BLOCK}"
        self.assertEqual(desc, expected)
        self.assertNotIn("▶️ Le stream complet", desc)

    def test_part_description_without_qualifying_chapters(self):
        part = {
            "start_s": 0,
            "end_s": 100,
            "title": "Exploration",
            "description": "Just playing.",
            "key_moments": [],
        }
        desc = part_description(part, "vid456", "randomguy42")
        expected = f"Just playing.\n\n{DEFAULT_LINKS_BLOCK}"
        self.assertEqual(desc, expected)

    def test_part_description_ignores_out_of_range_key_moments(self):
        part = {
            "start_s": 100,
            "end_s": 300,
            "title": "Exploration",
            "description": "Exploring open world.",
            "key_moments": [
                {"at_s": 50, "what": "Before start - out of range"},
                {"at_s": 130, "what": "Found a chest"},
                {"at_s": 200, "what": "Fought wolves"},
                {"at_s": 300, "what": "At end_s boundary - out of range"},
                {"at_s": 350, "what": "After end_s - out of range"},
            ],
        }
        desc = part_description(part, None, "randomguy42")
        expected_chaps = "⏱️ Chapitres\n0:00 Exploration\n0:30 Found a chest\n1:40 Fought wolves"
        expected = f"Exploring open world.\n\n{expected_chaps}\n\n{DEFAULT_LINKS_BLOCK}"
        self.assertEqual(desc, expected)

    def test_layout_blocks_and_order_full_vod(self):
        parts = [
            {"start_s": 0, "title": "Setup", "youtube_id": "id1"},
            {"start_s": 2400, "title": "Calcul RAM", "youtube_id": "id2"},
            {"start_s": 4800, "title": "Reaction", "youtube_id": "id3"},
        ]
        desc = full_description(
            parts=parts,
            channel="randomguy42",
            vod_hook="Combien de RAM pour DeepSeek ? Je calcule tout en direct.",
            hashtags=["#IA", "#DeepSeek", "#Tunisie"],
            part_videos=parts,
        )
        blocks = desc.split("\n\n")
        self.assertEqual(len(blocks), 5)
        # Block 1: Hook
        self.assertEqual(blocks[0], "Combien de RAM pour DeepSeek ? Je calcule tout en direct.")
        # Block 2: Chapters
        self.assertTrue(blocks[1].startswith("⏱️ Chapitres\n0:00 Setup\n40:00 Calcul RAM\n1:20:00 Reaction"))
        # Block 3: Navigation
        self.assertTrue(blocks[2].startswith("📺 Ce live découpé en 3 vidéos"))
        self.assertIn("1/3 Setup https://youtu.be/id1", blocks[2])
        self.assertIn("2/3 Calcul RAM https://youtu.be/id2", blocks[2])
        self.assertIn("3/3 Reaction https://youtu.be/id3", blocks[2])
        # Block 4: Links
        self.assertEqual(blocks[3], DEFAULT_LINKS_BLOCK)
        # Block 5: Hashtags
        self.assertEqual(blocks[4], "#IA #DeepSeek #Tunisie")

    def test_layout_blocks_and_order_part(self):
        part = {
            "start_s": 2400,
            "end_s": 4800,
            "title": "Calcul de RAM",
            "hook": "Combien de RAM pour faire tourner DeepSeek chez soi ?",
            "hashtags": ["#DeepSeek", "#Hardware"],
            "key_moments": [
                {"at_s": 2400, "what": "Intro calcul"},
                {"at_s": 3000, "what": "Formule math"},
                {"at_s": 4000, "what": "Résultat final"},
            ],
        }
        all_parts = [
            {"start_s": 0, "end_s": 2400, "title": "Intro", "youtube_id": "id1"},
            part,
            {"start_s": 4800, "end_s": 7200, "title": "Outro", "youtube_id": "id3"},
        ]
        desc = part_description(
            part=part,
            full_video_id="full_id_123",
            channel="randomguy42",
            all_parts=all_parts,
            full_duration_s=7200,
        )
        blocks = desc.split("\n\n")
        self.assertEqual(len(blocks), 5)
        # Block 1: Hook
        self.assertEqual(blocks[0], "Combien de RAM pour faire tourner DeepSeek chez soi ?")
        # Block 2: Chapters
        self.assertTrue(blocks[1].startswith("⏱️ Chapitres\n0:00 Intro calcul\n10:00 Formule math\n26:40 Résultat final"))
        # Block 3: Navigation
        self.assertTrue(blocks[2].startswith("📺 Les autres parties de ce live"))
        self.assertIn("1/3 Intro https://youtu.be/id1", blocks[2])
        self.assertNotIn("2/3 Calcul de RAM", blocks[2])
        self.assertIn("3/3 Outro https://youtu.be/id3", blocks[2])
        self.assertIn("▶️ Le stream complet (2h00) : https://youtu.be/full_id_123", blocks[2])
        # Block 4: Links
        self.assertEqual(blocks[3], DEFAULT_LINKS_BLOCK)
        # Block 5: Hashtags
        self.assertEqual(blocks[4], "#DeepSeek #Hardware")

    def test_single_part_vod_no_navigation(self):
        part = {
            "start_s": 0,
            "end_s": 1200,
            "title": "Seule partie",
            "hook": "Un stream complet d'une seule partie.",
            "description": "Description ici.",
        }
        full_desc = full_description([part], channel="randomguy42", vod_hook="Hook complet")
        self.assertNotIn("📺", full_desc)
        self.assertNotIn("Les autres parties", full_desc)
        self.assertNotIn("Ce live découpé", full_desc)

        part_desc = part_description(part, full_video_id="full_id", channel="randomguy42", total_parts=1)
        self.assertNotIn("📺", part_desc)
        self.assertNotIn("Les autres parties", part_desc)
        self.assertNotIn("▶️ Le stream complet", part_desc)

    def test_hook_fallback_for_legacy_plans(self):
        part_legacy = {
            "start_s": 0,
            "end_s": 1000,
            "title": "Partie 1",
            "description": "Ceci est la description classique sans hook.",
        }
        desc_legacy = part_description(part_legacy, channel="randomguy42")
        self.assertTrue(desc_legacy.startswith("Ceci est la description classique sans hook."))

        part_new = {
            "start_s": 0,
            "end_s": 1000,
            "title": "Partie 1",
            "hook": "Mon nouveau hook percutant !",
            "description": "Ancienne description ignorée pour le hook.",
        }
        desc_new = part_description(part_new, channel="randomguy42")
        self.assertTrue(desc_new.startswith("Mon nouveau hook percutant !"))

    def test_links_from_config(self):
        custom_links = [
            ("Mon Site", "https://example.com"),
            ("Twitter", "https://twitter.com/melek"),
        ]
        desc = full_description([], channel="randomguy42", links=custom_links)
        self.assertIn("Mon Site : https://example.com", desc)
        self.assertIn("Twitter : https://twitter.com/melek", desc)
        self.assertNotIn("📸 Instagram", desc)

    def test_tags_and_hashtags_sanitizing(self):
        raw_tags = [" DeepSeek ", "IA", "<hack>", "deepseek", "TUNISIE", "A" * 600]
        sanitized_t = sanitize_tags(raw_tags)
        self.assertEqual(sanitized_t, ["deepseek", "ia", "hack", "tunisie"])

        raw_hashtags = [" IA ", "#DeepSeek", "LLM Tunisie", "<script>", "#", "###IA"]
        sanitized_h = sanitize_hashtags(raw_hashtags)
        self.assertEqual(sanitized_h, ["#IA", "#DeepSeek", "#LLMTunisie", "#script"])

    def test_byte_limit_trimming_order(self):
        parts = [
            {
                "start_s": i * 30,
                "title": f"Partie {i}",
                "youtube_id": f"yt_id_{i}_" + "X" * 80,
            }
            for i in range(60)
        ]
        hook = "Mon super hook " + "H" * 150
        desc = full_description(
            parts,
            channel="randomguy42",
            vod_hook=hook,
            part_videos=parts,
        )
        self.assertLessEqual(len(desc.encode("utf-8")), 5000)
        self.assertIn("⏱️ Chapitres", desc)
        self.assertIn("🔴 En live sur Kick", desc)
        self.assertIn("📸 Instagram", desc)
        self.assertIn("1/60 Partie 0", desc)
        self.assertNotIn("60/60 Partie 59", desc)

        long_hook = "H" * 200
        desc_part = part_description(
            {"start_s": 0, "end_s": 100, "title": "P1", "hook": long_hook},
            channel="randomguy42",
            total_parts=1,
        )
        self.assertLessEqual(len(desc_part.encode("utf-8")), 5000)
        self.assertIn("🔴 En live sur Kick", desc_part)


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



class TestHookSanitizing(unittest.TestCase):
    def test_sanitize_hook_normal(self):
        self.assertEqual(sanitize_hook("top 1 solo carry"), "TOP 1 SOLO CARRY")

    def test_sanitize_hook_strips_and_removes_brackets_and_newlines(self):
        self.assertEqual(sanitize_hook("  <tag> \n hello \r\n world > "), "TAG HELLO WORLD")

    def test_sanitize_hook_fallback_when_empty_or_none(self):
        self.assertEqual(sanitize_hook("", "Chill gaming session | Stream du 22/10/2026"), "CHILL GAMING SESSION")
        self.assertEqual(sanitize_hook(None, "My Great Stream"), "MY GREAT STREAM")
        self.assertEqual(sanitize_hook("   <> \n ", "Short title"), "SHORT TITLE")
        self.assertEqual(sanitize_hook("", "sick stream, malla jaw, tinkering"), "SICK STREAM")
        self.assertEqual(sanitize_hook("", "stream layéli zomboid; mafamech jaw"), "STREAM LAYÉLI ZOMBOID")

    def test_sanitize_hook_cuts_at_22_chars_on_word_boundary(self):
        # 21 chars fits exactly
        self.assertEqual(sanitize_hook("un tournoi incroyable", ""), "UN TOURNOI INCROYABLE")
        # 34 chars cut at 22 word boundary -> "UN TOURNOI INCROYABLE"
        self.assertEqual(sanitize_hook("un tournoi incroyable avec les amis", ""), "UN TOURNOI INCROYABLE")
        self.assertLessEqual(len(sanitize_hook("un tournoi incroyable avec les amis", "")), 22)

    def test_sanitize_hook_cuts_single_long_word(self):
        self.assertEqual(sanitize_hook("supercalifragilisticexpialidocious", ""), "SUPERCALIFRAGILISTICEX")


class TestHookLines(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(hook_lines(""), ("", ""))

    def test_single_word(self):
        self.assertEqual(hook_lines("MOMENT"), ("MOMENT", ""))

    def test_two_words(self):
        self.assertEqual(hook_lines("TOP 1"), ("TOP", "1"))
        self.assertEqual(hook_lines("BIG WIN"), ("BIG", "WIN"))

    def test_three_words_short_first_word(self):
        self.assertEqual(hook_lines("LE BOSS ARRIVE"), ("LE BOSS", "ARRIVE"))
        self.assertEqual(hook_lines("UN GRAND LIVE"), ("UN GRAND", "LIVE"))

    def test_three_words_long_first_word(self):
        self.assertEqual(hook_lines("INCROYABLE FIN STREAM"), ("INCROYABLE", "FIN STREAM"))

    def test_four_words(self):
        self.assertEqual(hook_lines("UN TRES BEAU LIVE"), ("UN TRES", "BEAU LIVE"))
        self.assertEqual(hook_lines("MOMENT UNIQUE DU JOUR"), ("MOMENT", "UNIQUE DU JOUR"))

class TestFilterPath(unittest.TestCase):
    def test_windows_path_conversion_and_escaping(self):
        p = Path(r"C:\Windows\Fonts\arialbd.ttf")
        filtered = _escape_font_path(p)
        self.assertEqual(filtered, r"C\:/Windows/Fonts/arialbd.ttf")

    def test_windows_path_with_single_quote(self):
        p = Path(r"C:\Users\Melek's Files\font.ttf")
        filtered = _escape_font_path(p)
        self.assertEqual(filtered, r"C\:/Users/Melek\'s Files/font.ttf")

    def test_font_fallback_omits_fontfile(self):
        with patch("k2y.media._find_font", return_value=None),              patch("k2y.media.probe_duration", return_value=20.0),              patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with tempfile.NamedTemporaryFile(suffix=".mp4") as src,                  tempfile.NamedTemporaryFile(suffix=".jpg") as dst:
                dst.write(b"fake jpeg content")
                dst.flush()
                thumbnail(Path(src.name), 10, "Title", Path(dst.name))
                args = mock_run.call_args[0][0]
                vf_idx = args.index("-vf")
                vf_str = args[vf_idx + 1]
                self.assertNotIn("fontfile=", vf_str)
                self.assertIn("textfile=", vf_str)
                safe_stem = Path(dst.name).stem
                self.assertIn(f"textfile={safe_stem}_1.txt", vf_str)
                self.assertIn(f"textfile={safe_stem}_2.txt", vf_str)
                self.assertEqual(mock_run.call_args.kwargs.get("cwd"), Path(dst.name).resolve().parent)

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
        with self.assertRaises(KickError) as ctx:
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
        out_path = thumbnail(self.synthetic_video, at_s=500, hook="Far out", dest=dest)
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

    def test_thumbnail_real_ffmpeg_with_special_font_and_dest_paths(self):
        # 1. Test font path containing colon, comma, brackets, apostrophe, space: "C:fonts, [x] o'k"
        dejavu = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if not dejavu.exists():
            self.skipTest("DejaVuSans-Bold.ttf not found on system")

        font_dir = self.temp_dir / "C:fonts, [x] o'k"
        font_dir.mkdir(parents=True, exist_ok=True)
        font_copy = font_dir / "custom_font.ttf"
        shutil.copy(dejavu, font_copy)

        dest1 = self.temp_dir / "thumb_font_test.jpg"
        txt1_1 = dest1.parent / f"{dest1.stem}_1.txt"
        txt1_2 = dest1.parent / f"{dest1.stem}_2.txt"

        with patch("k2y.media._find_font", return_value=font_copy):
            out1 = thumbnail(self.synthetic_video, at_s=2, hook="Font Path Test", dest=dest1)
        self.assertEqual(out1, dest1)
        self.assertTrue(dest1.exists())
        self.assertFalse(txt1_1.exists())
        self.assertFalse(txt1_2.exists())

        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name",
            "-of", "json",
            str(dest1),
        ]
        info1 = json.loads(subprocess.run(probe_cmd, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info1["streams"][0]["codec_name"], "mjpeg")
        self.assertEqual(info1["streams"][0]["width"], 1280)
        self.assertEqual(info1["streams"][0]["height"], 720)

        # 2. Test dest folder named "out, [y] l'a"
        out_dir = self.temp_dir / "out, [y] l'a"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest2 = out_dir / "thumb_dest_test.jpg"
        txt2_1 = dest2.parent / f"{dest2.stem}_1.txt"
        txt2_2 = dest2.parent / f"{dest2.stem}_2.txt"

        out2 = thumbnail(self.synthetic_video, at_s=2, hook="Dest Path Test", dest=dest2)
        self.assertEqual(out2, dest2)
        self.assertTrue(dest2.exists())
        self.assertFalse(txt2_1.exists())
        self.assertFalse(txt2_2.exists())

        info2 = json.loads(subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name",
            "-of", "json",
            str(dest2),
        ], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info2["streams"][0]["codec_name"], "mjpeg")
        self.assertEqual(info2["streams"][0]["width"], 1280)
        self.assertEqual(info2["streams"][0]["height"], 720)



    def test_thumbnail_real_ffmpeg_with_and_without_face_in_hostile_dir(self):
        hostile_dir = self.temp_dir / "out, [y] l'a"
        hostile_dir.mkdir(parents=True, exist_ok=True)

        face_path = hostile_dir / "face.png"
        cmd_face = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=blue:s=500x600",
            "-frames:v", "1",
            str(face_path),
        ]
        subprocess.run(cmd_face, check=True)

        # 1. With face
        dest_face = hostile_dir / "thumb_with_face.jpg"
        out1 = thumbnail(self.synthetic_video, at_s=2, hook="LE BOSS ARRIVE", dest=dest_face, face=face_path)
        self.assertEqual(out1, dest_face)
        self.assertTrue(dest_face.exists())
        self.assertLess(dest_face.stat().st_size, 2 * 1024 * 1024)
        info1 = json.loads(subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name", "-of", "json",
            str(dest_face),
        ], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info1["streams"][0]["width"], 1280)
        self.assertEqual(info1["streams"][0]["height"], 720)
        self.assertEqual(info1["streams"][0]["codec_name"], "mjpeg")
        self.assertFalse((hostile_dir / f"{dest_face.stem}_1.txt").exists())
        self.assertFalse((hostile_dir / f"{dest_face.stem}_2.txt").exists())

        # 2. Without face
        dest_noface = hostile_dir / "thumb_no_face.jpg"
        out2 = thumbnail(self.synthetic_video, at_s=2, hook="LE BOSS ARRIVE", dest=dest_noface, face=None)
        self.assertEqual(out2, dest_noface)
        self.assertTrue(dest_noface.exists())
        self.assertLess(dest_noface.stat().st_size, 2 * 1024 * 1024)
        info2 = json.loads(subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name", "-of", "json",
            str(dest_noface),
        ], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info2["streams"][0]["width"], 1280)
        self.assertEqual(info2["streams"][0]["height"], 720)
        self.assertEqual(info2["streams"][0]["codec_name"], "mjpeg")
        self.assertFalse((hostile_dir / f"{dest_noface.stem}_1.txt").exists())
        self.assertFalse((hostile_dir / f"{dest_noface.stem}_2.txt").exists())


if __name__ == "__main__":
    unittest.main()

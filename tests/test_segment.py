"""Unit tests for k2y/segment.py."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch
import urllib.error

from k2y import segment


class TestRepair(unittest.TestCase):
    def test_repair_short_vod_returns_one_part(self):
        duration_s = 18 * 60  # 18 minutes < 20 minutes
        parts = [
            {"start_s": 0, "title": "Intro", "description": "Intro desc", "key_moments": []},
            {"start_s": 600, "title": "Gaming", "description": "Gaming desc", "key_moments": [{"at_s": 700, "what": "Play"}]},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 600, 700], min_part_s=900)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["start_s"], 0)
        self.assertEqual(res[0]["end_s"], duration_s)
        self.assertEqual(res[0]["title"], "Intro")
        self.assertEqual(len(res[0]["key_moments"]), 1)
        self.assertEqual(res[0]["key_moments"][0]["at_s"], 700)

    def test_repair_short_vod_empty_parts(self):
        duration_s = 15 * 60
        res = segment.repair([], duration_s=duration_s, allowed_s=[0], min_part_s=900)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["start_s"], 0)
        self.assertEqual(res[0]["end_s"], duration_s)
        self.assertEqual(res[0]["title"], "Partie 1")

    def test_repair_start_not_at_0_forced_to_0(self):
        duration_s = 3600
        parts = [
            {"start_s": 300, "end_s": 1800, "title": "Part 1", "description": "D1", "key_moments": []},
            {"start_s": 1800, "end_s": 3600, "title": "Part 2", "description": "D2", "key_moments": []},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 300, 1800], min_part_s=600)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["start_s"], 0)
        self.assertEqual(res[0]["end_s"], 1800)
        self.assertEqual(res[1]["start_s"], 1800)
        self.assertEqual(res[1]["end_s"], 3600)

    def test_repair_gap_overlap_unsorted(self):
        duration_s = 3600
        # Unsorted, overlapping and gaps
        parts = [
            {"start_s": 2000, "end_s": 2500, "title": "Part 3", "description": "D3", "key_moments": []},
            {"start_s": 0, "end_s": 800, "title": "Part 1", "description": "D1", "key_moments": []},
            {"start_s": 1000, "end_s": 1500, "title": "Part 2", "description": "D2", "key_moments": []},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 1000, 2000], min_part_s=300)
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0]["start_s"], 0)
        self.assertEqual(res[0]["end_s"], 1000)
        self.assertEqual(res[1]["start_s"], 1000)
        self.assertEqual(res[1]["end_s"], 2000)
        self.assertEqual(res[2]["start_s"], 2000)
        self.assertEqual(res[2]["end_s"], 3600)

    def test_repair_snapping_to_allowed_values(self):
        duration_s = 3600
        allowed_s = [0, 600, 1200, 1800, 2400]
        parts = [
            {"start_s": 50, "key_moments": [{"at_s": 590, "what": "k1"}]},
            {"start_s": 1250, "key_moments": [{"at_s": 1790, "what": "k2"}]},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=allowed_s, min_part_s=300)
        self.assertEqual(res[0]["start_s"], 0)  # 50 snaps to 0, forced to 0
        self.assertEqual(res[0]["key_moments"][0]["at_s"], 600)  # 590 snaps to 600
        self.assertEqual(res[1]["start_s"], 1200)  # 1250 snaps to 1200
        self.assertEqual(res[1]["key_moments"][0]["at_s"], 1800)  # 1790 snaps to 1800

    def test_repair_part_shorter_than_min_gets_merged(self):
        duration_s = 4000
        min_part_s = 900
        parts = [
            {"start_s": 0, "title": "Part 1", "description": "D1", "key_moments": []},
            {"start_s": 2000, "title": "Short Part", "description": "DS", "key_moments": [{"at_s": 2100, "what": "s1"}]},
            {"start_s": 2200, "title": "Part 3", "description": "D3", "key_moments": [{"at_s": 3000, "what": "s2"}]},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 2000, 2100, 2200, 3000, 4000], min_part_s=min_part_s)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["start_s"], 0)
        self.assertEqual(res[0]["end_s"], 2000)
        self.assertEqual(res[0]["title"], "Part 1")

        self.assertEqual(res[1]["start_s"], 2000)
        self.assertEqual(res[1]["end_s"], 4000)
        self.assertEqual(res[1]["title"], "Part 3")
        self.assertEqual(len(res[1]["key_moments"]), 2)
        self.assertEqual([km["at_s"] for km in res[1]["key_moments"]], [2100, 3000])

    def test_repair_last_end_s_equals_duration(self):
        duration_s = 5000
        parts = [
            {"start_s": 0, "title": "Only Part", "description": "", "key_moments": []},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0], min_part_s=600)
        self.assertEqual(res[-1]["end_s"], duration_s)

    def test_repair_key_moments_capped_at_3(self):
        duration_s = 3000
        min_part_s = 900
        parts = [
            {
                "start_s": 0,
                "title": "Short Part",
                "key_moments": [
                    {"at_s": 100, "what": "km1"},
                    {"at_s": 200, "what": "km2"},
                ],
            },
            {
                "start_s": 300,
                "title": "Long Part",
                "key_moments": [
                    {"at_s": 400, "what": "km3"},
                    {"at_s": 500, "what": "km4"},
                    {"at_s": 600, "what": "km5"},
                ],
            },
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 100, 200, 300, 400, 500, 600], min_part_s=min_part_s)
        self.assertEqual(len(res), 1)
        self.assertLessEqual(len(res[0]["key_moments"]), 3)
        self.assertEqual(len(res[0]["key_moments"]), 3)
        self.assertEqual([km["at_s"] for km in res[0]["key_moments"]], [100, 200, 400])

    def test_repair_drops_key_moments_outside_part_range(self):
        duration_s = 3600
        allowed_s = [0, 100, 1500, 1800, 2000, 2100, 3600]
        parts = [
            {
                "start_s": 0,
                "title": "Part 1",
                "key_moments": [
                    {"at_s": 100, "what": "valid in part 1"},
                    {"at_s": 1800, "what": "falls on end boundary, out of part 1"},
                    {"at_s": 2000, "what": "falls into part 2, out of part 1"},
                ],
            },
            {
                "start_s": 1800,
                "title": "Part 2",
                "key_moments": [
                    {"at_s": 1500, "what": "before part 2 start"},
                    {"at_s": 2100, "what": "valid in part 2"},
                    {"at_s": 3600, "what": "at duration end, out of part 2"},
                ],
            },
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=allowed_s, min_part_s=600)
        self.assertEqual(len(res), 2)
        km1 = res[0]["key_moments"]
        self.assertEqual(len(km1), 1)
        self.assertEqual(km1[0]["at_s"], 100)

        km2 = res[1]["key_moments"]
        self.assertEqual(len(km2), 1)
        self.assertEqual(km2[0]["at_s"], 2100)

    def test_repair_short_vod_drops_out_of_range_key_moments(self):
        duration_s = 15 * 60  # 900s
        allowed_s = [0, 200, 900, 1000]
        parts = [
            {
                "start_s": 0,
                "title": "Short VOD",
                "key_moments": [
                    {"at_s": 200, "what": "valid"},
                    {"at_s": 900, "what": "at duration_s boundary"},
                    {"at_s": 1000, "what": "beyond duration_s"},
                ],
            }
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=allowed_s, min_part_s=900)
        self.assertEqual(len(res), 1)
        kms = res[0]["key_moments"]
        self.assertEqual(len(kms), 1)
        self.assertEqual(kms[0]["at_s"], 200)

    def test_repair_empty_title_fallback(self):
        duration_s = 3600
        parts = [
            {"start_s": 0, "title": ""},
            {"start_s": 1800, "title": "   "},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 1800], min_part_s=600)
        self.assertEqual(res[0]["title"], "Partie 1")
        self.assertEqual(res[1]["title"], "Partie 2")

    def test_repair_empty_parts_fallback(self):
        duration_s = 3600
        res = segment.repair([], duration_s=duration_s, allowed_s=[0], min_part_s=600)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["start_s"], 0)
        self.assertEqual(res[0]["end_s"], duration_s)
        self.assertEqual(res[0]["title"], "Partie 1")


class TestDescribeChunk(unittest.TestCase):
    @patch("k2y.segment.generate")
    def test_describe_chunk_mmss_and_hmmss_parsing_and_clamping(self, mock_generate):
        mock_generate.return_value = {
            "summary_en": "Summary test",
            "activity": "just chatting",
            "segments": [
                {"at": "02:15", "topic": "MMSS topic"},
                {"at": "1:05:30", "topic": "HMMSS topic"},
                {"at": "-00:10", "topic": "Negative topic"},
            ],
        }

        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(b"fake audio")
            tmp.flush()
            path = Path(tmp.name)
            chunk_s = 600
            i = 2
            # i = 2, chunk_start = 1200
            res = segment.describe_chunk(i=i, path=path, chunk_s=chunk_s, models=["m1"])

        self.assertEqual(res["start_s"], 1200)
        self.assertEqual(res["summary_en"], "Summary test")
        self.assertEqual(res["activity"], "just chatting")

        # 02:15 = 135s -> 1200 + 135 = 1335
        # 1:05:30 = 3930s -> clamped to chunk_s - 1 = 599 -> 1200 + 599 = 1799
        # -00:10 = -10s -> clamped to 0 -> 1200 + 0 = 1200
        # Since 1200 exists, chunk start segment exists
        at_s_list = [s["at_s"] for s in res["segments"]]
        self.assertIn(1200, at_s_list)
        self.assertIn(1335, at_s_list)
        self.assertIn(1799, at_s_list)
        self.assertEqual(res["segments"][0]["at_s"], 1200)

    @patch("k2y.segment.generate")
    def test_describe_chunk_ensures_start_segment_when_missing(self, mock_generate):
        mock_generate.return_value = {
            "summary_en": "Summary missing start",
            "activity": "gaming",
            "segments": [
                {"at": "01:30", "topic": "Gaming topic"},
            ],
        }

        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(b"fake audio")
            tmp.flush()
            path = Path(tmp.name)
            res = segment.describe_chunk(i=1, path=path, chunk_s=600, models=["m1"])

        self.assertEqual(res["start_s"], 600)
        self.assertEqual(res["segments"][0]["at_s"], 600)
        self.assertEqual(res["segments"][0]["topic"], "Gaming topic")
        self.assertEqual(res["segments"][1]["at_s"], 690)


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.orig_sleep = segment._sleep
        segment._sleep = MagicMock()

    def tearDown(self):
        segment._sleep = self.orig_sleep

    @patch("urllib.request.urlopen")
    def test_generate_rotates_models_on_503_and_returns_parsed_json(self, mock_urlopen):
        err_503 = urllib.error.HTTPError(
            url="http://fake", code=503, msg="Service Unavailable", hdrs={}, fp=io.BytesIO(b"{}")
        )
        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({"result": "success"})}]
                }
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response

        mock_urlopen.side_effect = [err_503, ok_response]

        res = segment.generate(parts=[{"text": "hi"}], schema={}, models=["model-a", "model-b"])

        self.assertEqual(res, {"result": "success"})
        self.assertEqual(mock_urlopen.call_count, 2)
        req1 = mock_urlopen.call_args_list[0][0][0]
        req2 = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("model-a", req1.full_url)
        self.assertIn("model-b", req2.full_url)
        segment._sleep.assert_called_once_with(5)

    @patch("urllib.request.urlopen")
    def test_generate_raises_daily_quota_only_when_all_models_hit_per_day_429(self, mock_urlopen):
        per_day_payload = json.dumps({
            "error": {
                "code": 429,
                "message": "Resource exhausted",
                "details": [
                    {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                    {"retryDelay": "32876s"},
                ],
            }
        }).encode("utf-8")

        err_429_m1 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(per_day_payload)
        )
        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({"recovered": True})}]
                }
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response

        # Scenario 1: model-a hits PerDay 429, model-b succeeds -> DailyQuota NOT raised
        mock_urlopen.side_effect = [err_429_m1, ok_response]
        res = segment.generate(parts=[], schema={}, models=["model-a", "model-b"])
        self.assertEqual(res, {"recovered": True})

        # Scenario 2: model-a hits PerDay 429, model-b hits 500 repeatedly -> SegmentError, NOT DailyQuota
        mock_urlopen.reset_mock()
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(per_day_payload)
        )
        err_500 = urllib.error.HTTPError(
            url="http://fake", code=500, msg="Server Error", hdrs={}, fp=io.BytesIO(b"{}")
        )
        mock_urlopen.side_effect = [err_429] + [err_500] * 7
        with self.assertRaises(segment.SegmentError):
            segment.generate(parts=[], schema={}, models=["model-a", "model-b"])

        # Scenario 3: both model-a and model-b hit PerDay 429 -> DailyQuota raised
        mock_urlopen.reset_mock()
        err_429_a = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(per_day_payload)
        )
        err_429_b = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(per_day_payload)
        )
        mock_urlopen.side_effect = [err_429_a, err_429_b]
        with self.assertRaises(segment.DailyQuota):
            segment.generate(parts=[], schema={}, models=["model-a", "model-b"])

    @patch("urllib.request.urlopen")
    def test_generate_skips_404_model(self, mock_urlopen):
        err_404 = urllib.error.HTTPError(
            url="http://fake", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b"{}")
        )
        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({"status": "ok"})}]
                }
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response

        mock_urlopen.side_effect = [err_404, ok_response]
        res = segment.generate(parts=[], schema={}, models=["model-404", "model-valid"])
        self.assertEqual(res, {"status": "ok"})
        req1 = mock_urlopen.call_args_list[0][0][0]
        req2 = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("model-404", req1.full_url)
        self.assertIn("model-valid", req2.full_url)

    @patch("urllib.request.urlopen")
    def test_generate_retries_url_error(self, mock_urlopen):
        url_err = urllib.error.URLError("Connection reset")
        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({"status": "recovered"})}]
                }
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response

        mock_urlopen.side_effect = [url_err, ok_response]
        res = segment.generate(parts=[], schema={}, models=["m1"])
        self.assertEqual(res, {"status": "recovered"})
        segment._sleep.assert_called_once_with(5)


class TestChunkAudio(unittest.TestCase):
    @patch("subprocess.run")
    def test_chunk_audio_calls_ffmpeg_with_correct_args(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            src = tmp_path / "source.mp4"
            src.write_bytes(b"video data")

            chunks_dir = tmp_path / "chunks"
            chunks_dir.mkdir()
            c0 = chunks_dir / "c000.ogg"
            c1 = chunks_dir / "c001.ogg"

            def fake_ffmpeg(cmd, *args, **kwargs):
                if "segment" in cmd:
                    c0.write_bytes(b"c0")
                    c1.write_bytes(b"c1")
                return MagicMock(returncode=0)

            mock_run.side_effect = fake_ffmpeg

            res = segment.chunk_audio(src=src, workdir=tmp_path, chunk_s=600)

            self.assertEqual(mock_run.call_count, 2)
            # Check extract command
            cmd1 = mock_run.call_args_list[0][0][0]
            self.assertIn("-ac", cmd1)
            self.assertIn("1", cmd1)
            self.assertIn("-ar", cmd1)
            self.assertIn("16000", cmd1)
            self.assertIn("libopus", cmd1)
            self.assertIn("24k", cmd1)

            # Check segment command
            cmd2 = mock_run.call_args_list[1][0][0]
            self.assertIn("-f", cmd2)
            self.assertIn("segment", cmd2)
            self.assertIn("-segment_time", cmd2)
            self.assertIn("600", cmd2)
            self.assertIn("-c", cmd2)
            self.assertIn("copy", cmd2)

            self.assertEqual(res, [c0, c1])


class TestMerge(unittest.TestCase):
    @patch("k2y.segment.generate")
    def test_merge_sends_required_rules_in_prompt(self, mock_generate):
        mock_generate.return_value = {
            "parts": [
                {
                    "start_s": 0,
                    "title": "Stream khedma fel gueyla",
                    "description": "Melek travaille sur le projet.",
                    "summary_en": "Melek working on project",
                    "key_moments": [{"at_s": 100, "what": "Debut"}],
                }
            ]
        }

        chunks = [
            {
                "start_s": 0,
                "activity": "coding/building",
                "summary_en": "Building python agent",
                "segments": [{"at_s": 0, "topic": "Coding start"}],
            }
        ]
        res = segment.merge(chunks, duration_s=3600, models=["m1"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Stream khedma fel gueyla")

        # Verify prompt rules in generate call
        call_parts = mock_generate.call_args[0][0]
        prompt_text = call_parts[0]["text"]
        self.assertIn("cut where the activity or subject changes", prompt_text.lower())
        self.assertIn("15 and 60 minutes", prompt_text.lower())
        self.assertIn("french", prompt_text.lower())
        self.assertIn("latin-script tunisian darija", prompt_text.lower())


class TestPlan(unittest.TestCase):
    @patch("k2y.segment.merge")
    @patch("k2y.segment.describe_chunk")
    @patch("k2y.segment.chunk_audio")
    def test_plan_falls_back_to_single_part_when_merge_fails_twice(
        self, mock_chunk_audio, mock_describe_chunk, mock_merge
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            chunk_file = tmp_path / "c000.ogg"
            chunk_file.write_bytes(b"dummy")

            mock_chunk_audio.return_value = [chunk_file]
            mock_describe_chunk.return_value = {
                "start_s": 0,
                "summary_en": "Discussion about AI tools",
                "activity": "just chatting",
                "segments": [
                    {"at_s": 0, "topic": "Kick streaming live"},
                    {"at_s": 300, "topic": "Coding demo"},
                ],
            }
            mock_merge.side_effect = [
                segment.SegmentError("Merge 1 failed"),
                segment.SegmentError("Merge 2 failed"),
            ]

            duration_s = 3600
            res = segment.plan(
                src=Path("test.mp4"),
                workdir=tmp_path,
                duration_s=duration_s,
                min_part_minutes=15,
                model="gemini-3.8-flash",
            )

            self.assertEqual(mock_merge.call_count, 2)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["start_s"], 0)
            self.assertEqual(res[0]["end_s"], duration_s)
            self.assertEqual(res[0]["title"], "Kick streaming live")
            self.assertEqual(res[0]["description"], "Discussion about AI tools")

            # Check that chunk files are deleted
            self.assertFalse(chunk_file.exists())

    @patch("k2y.segment.merge")
    @patch("k2y.segment.describe_chunk")
    @patch("k2y.segment.chunk_audio")
    def test_plan_success_flow(
        self, mock_chunk_audio, mock_describe_chunk, mock_merge
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            c0 = tmp_path / "c000.ogg"
            c1 = tmp_path / "c001.ogg"
            c0.write_bytes(b"c0")
            c1.write_bytes(b"c1")

            mock_chunk_audio.return_value = [c0, c1]
            mock_describe_chunk.side_effect = [
                {
                    "start_s": 0,
                    "summary_en": "First part",
                    "activity": "coding",
                    "segments": [{"at_s": 0, "topic": "Setup"}],
                },
                {
                    "start_s": 600,
                    "summary_en": "Second part",
                    "activity": "gaming",
                    "segments": [{"at_s": 600, "topic": "Gaming"}],
                },
            ]
            mock_merge.return_value = [
                {
                    "start_s": 0,
                    "title": "Partie Setup",
                    "description": "Description setup",
                    "summary_en": "Setup summary",
                    "key_moments": [{"at_s": 0, "what": "Debut"}],
                },
                {
                    "start_s": 1200,
                    "title": "Partie Gaming",
                    "description": "Description gaming",
                    "summary_en": "Gaming summary",
                    "key_moments": [{"at_s": 1200, "what": "Game"}],
                },
            ]

            duration_s = 2400
            res = segment.plan(
                src=Path("test.mp4"),
                workdir=tmp_path,
                duration_s=duration_s,
                min_part_minutes=15,
                model="gemini-3.8-flash",
            )

            # Snap to allowed_s [0, 600]
            # 1200 snaps to 600
            # [0, 600] (len 600 < 900) and [600, 2400] (len 1800) -> 600 merged into 1800
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["start_s"], 0)
            self.assertEqual(res[0]["end_s"], 2400)
            self.assertFalse(c0.exists())
            self.assertFalse(c1.exists())


if __name__ == "__main__":
    unittest.main()

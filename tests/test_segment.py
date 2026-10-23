"""Unit tests for k2y/segment.py."""
import ast
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, call, patch
import urllib.error

from k2y import config, segment


class TestDocstring(unittest.TestCase):
    def test_docstring_is_first_statement(self):
        source = Path(segment.__file__).read_text(encoding="utf-8")
        parsed = ast.parse(source)
        first_stmt = parsed.body[0]
        self.assertIsInstance(first_stmt, ast.Expr)
        self.assertIsInstance(first_stmt.value, ast.Constant)
        self.assertIsInstance(first_stmt.value.value, str)


class TestRepair(unittest.TestCase):
    def test_repair_preserves_thumb_text(self):
        duration_s = 3600
        parts = [
            {"start_s": 0, "title": "P1", "thumb_text": "HOOK ONE", "description": "", "key_moments": []},
            {"start_s": 1800, "title": "P2", "thumb_text": "HOOK TWO", "description": "", "key_moments": []},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 1800], min_part_s=600)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["thumb_text"], "HOOK ONE")
        self.assertEqual(res[1]["thumb_text"], "HOOK TWO")

    def test_repair_preserves_thumb_text_when_merging_short_parts(self):
        duration_s = 4000
        parts = [
            {"start_s": 0, "title": "Part 1", "thumb_text": "HOOK 1", "description": "D1", "key_moments": []},
            {"start_s": 2000, "title": "Short Part", "thumb_text": "SHORT HOOK", "description": "DS", "key_moments": []},
            {"start_s": 2200, "title": "Part 3", "thumb_text": "HOOK 3", "description": "D3", "key_moments": []},
        ]
        res = segment.repair(parts, duration_s=duration_s, allowed_s=[0, 2000, 2200], min_part_s=900)
        self.assertEqual(len(res), 2)
        self.assertTrue(any(p["thumb_text"] in ("HOOK 1", "SHORT HOOK", "HOOK 3") for p in res))
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
            res = segment.describe_chunk(i=i, path=path, chunk_s=chunk_s, models=["m1"])

        self.assertEqual(res["start_s"], 1200)
        self.assertEqual(res["summary_en"], "Summary test")
        self.assertEqual(res["activity"], "just chatting")

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
    def test_generate_daily_quota_when_active_subset_of_per_day(self, mock_urlopen):
        # Model 404 is skipped; active is model-b. Model-b hits PerDay 429 -> DailyQuota
        per_day_payload = json.dumps({
            "error": {
                "code": 429,
                "message": "Resource exhausted",
                "details": [
                    {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                ],
            }
        }).encode("utf-8")
        err_404 = urllib.error.HTTPError(
            url="http://fake", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b"{}")
        )
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(per_day_payload)
        )
        mock_urlopen.side_effect = [err_404, err_429]
        with self.assertRaises(segment.DailyQuota):
            segment.generate(parts=[], schema={}, models=["model-404", "model-b"])

    @patch("urllib.request.urlopen")
    def test_generate_all_404_raises_segment_error(self, mock_urlopen):
        err_404_a = urllib.error.HTTPError(
            url="http://fake", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b"{}")
        )
        err_404_b = urllib.error.HTTPError(
            url="http://fake", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b"{}")
        )
        mock_urlopen.side_effect = [err_404_a, err_404_b]
        with self.assertRaises(segment.SegmentError):
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

    @patch("urllib.request.urlopen")
    def test_generate_uses_first_non_thought_part(self, mock_urlopen):
        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [
                        {"thought": True, "text": "Thinking process..."},
                        {"text": json.dumps({"actual": "content"})},
                    ]
                }
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response
        mock_urlopen.return_value = ok_response

        res = segment.generate(parts=[], schema={}, models=["m1"])
        self.assertEqual(res, {"actual": "content"})

    @patch("urllib.request.urlopen")
    def test_generate_retries_on_empty_or_safety_blocked_candidate(self, mock_urlopen):
        blocked_response = MagicMock()
        blocked_response.read.return_value = json.dumps({
            "candidates": [{
                "finishReason": "SAFETY",
                "content": {"parts": []},
            }]
        }).encode("utf-8")
        blocked_response.__enter__.return_value = blocked_response

        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps({"passed": True})}]},
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response

        mock_urlopen.side_effect = [blocked_response, ok_response]
        with self.assertLogs("k2y", level="WARNING") as cm:
            res = segment.generate(parts=[], schema={}, models=["m1", "m2"])
        self.assertEqual(res, {"passed": True})
        self.assertTrue(any("finishReason=SAFETY" in m for m in cm.output))

    @patch("urllib.request.urlopen")
    def test_generate_malformed_json_retries_once(self, mock_urlopen):
        bad_json_response = MagicMock()
        bad_json_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": "not a valid json {{"}]},
            }]
        }).encode("utf-8")
        bad_json_response.__enter__.return_value = bad_json_response

        ok_response = MagicMock()
        ok_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": json.dumps({"ok": 2})}]},
            }]
        }).encode("utf-8")
        ok_response.__enter__.return_value = ok_response

        # Scenario 1: Model 1 bad JSON, Model 2 succeeds -> succeeds
        mock_urlopen.side_effect = [bad_json_response, ok_response]
        with self.assertLogs("k2y", level="WARNING") as cm:
            res = segment.generate(parts=[], schema={}, models=["m1", "m2"])
        self.assertEqual(res, {"ok": 2})
        self.assertTrue(any("malformed JSON response" in m for m in cm.output))

        # Scenario 2: Model 1 bad JSON, Model 2 bad JSON -> raises SegmentError
        mock_urlopen.reset_mock()
        mock_urlopen.side_effect = [bad_json_response, bad_json_response]
        with self.assertRaises(segment.SegmentError):
            segment.generate(parts=[], schema={}, models=["m1", "m2"])


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
            cmd1 = mock_run.call_args_list[0][0][0]
            self.assertIn("-ac", cmd1)
            self.assertIn("1", cmd1)
            self.assertIn("-ar", cmd1)
            self.assertIn("16000", cmd1)
            self.assertIn("libopus", cmd1)
            self.assertIn("24k", cmd1)
            self.assertEqual(mock_run.call_args_list[0].kwargs.get("timeout"), 3600)

            cmd2 = mock_run.call_args_list[1][0][0]
            self.assertIn("-f", cmd2)
            self.assertIn("segment", cmd2)
            self.assertIn("-segment_time", cmd2)
            self.assertIn("600", cmd2)
            self.assertIn("-c", cmd2)
            self.assertIn("copy", cmd2)
            self.assertEqual(mock_run.call_args_list[1].kwargs.get("timeout"), 3600)

            self.assertEqual(res, [c0, c1])

    @patch("subprocess.run")
    def test_chunk_audio_timeout_deletes_partial_outputs(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            src = tmp_path / "source.mp4"
            src.write_bytes(b"video")
            audio_file = tmp_path / "audio.ogg"

            def fake_extract(*args, **kwargs):
                audio_file.write_bytes(b"partial audio")
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=3600)

            mock_run.side_effect = fake_extract
            with self.assertRaises(segment.SegmentError) as ctx:
                segment.chunk_audio(src=src, workdir=tmp_path)
            self.assertIn("timed out", str(ctx.exception))
            self.assertFalse(audio_file.exists())


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
        self.assertIn("parts", res)
        self.assertEqual(len(res["parts"]), 1)
        self.assertEqual(res["parts"][0]["title"], "Stream khedma fel gueyla")
        # Check that MERGE_PROMPT contains the new rules
        self.assertIn("Melek", segment.MERGE_PROMPT)
        self.assertIn("thumb_text", segment.MERGE_PROMPT)
        self.assertIn("vod_thumb_text", segment.MERGE_PROMPT)


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
            with self.assertLogs("k2y", level="WARNING") as cm:
                res = segment.plan(
                    src=Path("test.mp4"),
                    workdir=tmp_path,
                    duration_s=duration_s,
                    min_part_minutes=15,
                    model="gemini-3.8-flash",
                )

            self.assertTrue(any("Merge failed twice" in m and "Merge 2 failed" in m for m in cm.output))
            self.assertEqual(mock_merge.call_count, 2)
            self.assertIn("parts", res)
            self.assertEqual(len(res["parts"]), 1)
            self.assertEqual(res["parts"][0]["start_s"], 0)
            self.assertEqual(res["parts"][0]["end_s"], duration_s)
            self.assertEqual(res["parts"][0]["title"], "Stream complet")
            self.assertEqual(res["parts"][0]["description"], "")
            self.assertEqual(res.get("vod_thumb_text"), "")
            self.assertFalse(chunk_file.exists())

    @patch("k2y.segment.chunk_audio")
    @patch("k2y.segment.generate")
    def test_plan_reraises_daily_quota_and_missing_secret(
        self, mock_generate, mock_chunk_audio
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            chunk_file = tmp_path / "c000.ogg"
            chunk_file.write_bytes(b"dummy")
            mock_chunk_audio.return_value = [chunk_file]

            # 1. DailyQuota raised during merge generate call
            def fake_gen(parts, schema, models):
                if schema == segment.CHUNK_SCHEMA:
                    return {
                        "summary_en": "Chunk 0",
                        "activity": "coding",
                        "segments": [{"at": "00:00", "topic": "Start"}],
                    }
                raise segment.DailyQuota("Daily limit hit")

            mock_generate.side_effect = fake_gen
            with self.assertRaises(segment.DailyQuota):
                segment.plan(
                    src=Path("test.mp4"),
                    workdir=tmp_path,
                    duration_s=1800,
                    min_part_minutes=15,
                    model="gemini-3.8-flash",
                )

            # 2. MissingSecret raised during merge generate call
            chunk_file.write_bytes(b"dummy")
            mock_chunk_audio.return_value = [chunk_file]

            def fake_gen_missing(parts, schema, models):
                if schema == segment.CHUNK_SCHEMA:
                    return {
                        "summary_en": "Chunk 0",
                        "activity": "coding",
                        "segments": [{"at": "00:00", "topic": "Start"}],
                    }
                raise config.MissingSecret("gemini.key missing")

            mock_generate.side_effect = fake_gen_missing
            with self.assertRaises(config.MissingSecret):
                segment.plan(
                    src=Path("test.mp4"),
                    workdir=tmp_path,
                    duration_s=1800,
                    min_part_minutes=15,
                    model="gemini-3.8-flash",
                )

    @patch("k2y.segment.chunk_audio")
    @patch("k2y.segment.describe_chunk")
    def test_plan_cancels_remaining_chunks_on_error(
        self, mock_describe_chunk, mock_chunk_audio
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            c0 = tmp_path / "c000.ogg"
            c1 = tmp_path / "c001.ogg"
            c2 = tmp_path / "c002.ogg"
            c0.write_bytes(b"0")
            c1.write_bytes(b"1")
            c2.write_bytes(b"2")
            mock_chunk_audio.return_value = [c0, c1, c2]

            chunk_0_failed = threading.Event()
            chunk_2_ran = False

            def fake_describe(i, path, chunk_s, models):
                nonlocal chunk_2_ran
                if i == 0:
                    chunk_0_failed.set()
                    raise segment.SegmentError("Chunk 0 failed")
                elif i == 1:
                    chunk_0_failed.wait(timeout=5)
                    return {"start_s": 600, "summary_en": "", "activity": "", "segments": []}
                elif i == 2:
                    chunk_2_ran = True
                    return {"start_s": 1200, "summary_en": "", "activity": "", "segments": []}

            mock_describe_chunk.side_effect = fake_describe
            with self.assertRaises(segment.SegmentError):
                segment.plan(
                    src=Path("test.mp4"),
                    workdir=tmp_path,
                    duration_s=1800,
                    min_part_minutes=15,
                    model="gemini-3.8-flash",
                )
            self.assertFalse(chunk_2_ran)


if __name__ == "__main__":
    unittest.main()

"""Tests for k2y/kick.py."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from k2y.kick import KickError, best_rendition, download, list_vods, probe_duration

SAMPLE_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=2441880,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=1280x720,VIDEO="720p30",FRAME-RATE=30.000
720p30/playlist.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1496879,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=852x480,VIDEO="480p30",FRAME-RATE=30.000
480p30/playlist.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=230000,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=284x160,VIDEO="160p30",FRAME-RATE=30.000
160p30/playlist.m3u8
"""


class TestKick(unittest.TestCase):
    def test_best_rendition_sample(self):
        master_url = "https://stream.kick.com/x/media/hls/master.m3u8"
        result = best_rendition(master_url, SAMPLE_MASTER)
        self.assertEqual(result, "https://stream.kick.com/x/media/hls/720p30/playlist.m3u8")

    def test_best_rendition_absolute_url(self):
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000
https://cdn.example.com/stream/500k.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=100000
https://cdn.example.com/stream/100k.m3u8
"""
        result = best_rendition("https://stream.kick.com/master.m3u8", master)
        self.assertEqual(result, "https://cdn.example.com/stream/500k.m3u8")

    def test_best_rendition_no_streams(self):
        with self.assertRaises(KickError):
            best_rendition("https://stream.kick.com/master.m3u8", "#EXTM3U\n")

    @patch("urllib.request.urlopen")
    def test_list_vods_success(self, mock_urlopen):
        payload = [
            {
                "id": 42,
                "session_title": "Gaming Night",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 7200000,
                "source": "https://stream.kick.com/42/master.m3u8",
                "categories": [{"name": "Grand Theft Auto V"}],
                "is_live": False,
            },
            {
                "id": 43,
                "session_title": "Chilling",
                "start_time": "2026-10-22T23:00:00Z",
                "duration": 1800000,
                "source": "https://stream.kick.com/43/master.m3u8",
                "categories": [],
                "is_live": True,
            },
        ]
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        vods = list_vods("testchannel")
        self.assertEqual(len(vods), 2)
        self.assertEqual(vods[0]["id"], 42)
        self.assertEqual(vods[0]["title"], "Gaming Night")
        self.assertEqual(vods[0]["started_at"], "2026-10-22T20:00:00Z")
        self.assertEqual(vods[0]["duration_s"], 7200)
        self.assertEqual(vods[0]["source_url"], "https://stream.kick.com/42/master.m3u8")
        self.assertEqual(vods[0]["category"], "Grand Theft Auto V")
        self.assertFalse(vods[0]["is_live"])

        # Second VOD has empty categories and is_live=True
        self.assertIsNone(vods[1]["category"])
        self.assertTrue(vods[1]["is_live"])

        # Check request headers
        req = mock_urlopen.call_args[0][0]
        self.assertIn("Mozilla", req.headers["User-agent"])

    @patch("urllib.request.urlopen")
    def test_list_vods_unexpected_categories_shapes(self, mock_urlopen):
        payload = [
            {
                "id": 1,
                "session_title": "V1",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 1000,
                "source": "https://stream.kick.com/1/master.m3u8",
                "categories": None,
                "is_live": False,
            },
            {
                "id": 2,
                "session_title": "V2",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 1000,
                "source": "https://stream.kick.com/2/master.m3u8",
                "categories": [None],
                "is_live": False,
            },
            {
                "id": 3,
                "session_title": "V3",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 1000,
                "source": "https://stream.kick.com/3/master.m3u8",
                "categories": ["Gaming"],
                "is_live": False,
            },
            {
                "id": 4,
                "session_title": "V4",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 1000,
                "source": "https://stream.kick.com/4/master.m3u8",
                "categories": [123],
                "is_live": False,
            },
            {
                "id": 5,
                "session_title": "V5",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 1000,
                "source": "https://stream.kick.com/5/master.m3u8",
                "categories": "UnexpectedString",
                "is_live": False,
            },
            {
                "id": 6,
                "session_title": "V6",
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 1000,
                "source": "https://stream.kick.com/6/master.m3u8",
                "categories": [{"name": "ValidGame"}],
                "is_live": False,
            },
        ]
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        vods = list_vods("testchannel")
        self.assertEqual(len(vods), 6)
        self.assertIsNone(vods[0]["category"])
        self.assertIsNone(vods[1]["category"])
        self.assertIsNone(vods[2]["category"])
        self.assertIsNone(vods[3]["category"])
        self.assertIsNone(vods[4]["category"])
        self.assertEqual(vods[5]["category"], "ValidGame")

    @patch("urllib.request.urlopen")
    def test_list_vods_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://kick.com", code=404, msg="Not Found", hdrs={}, fp=None
        )
        with self.assertRaises(KickError):
            list_vods("nonexistent")

    @patch("urllib.request.urlopen")
    def test_list_vods_invalid_json(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html>Service Unavailable</html>"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with self.assertRaises(KickError):
            list_vods("testchannel")

    @patch("urllib.request.urlopen")
    def test_list_vods_missing_key(self, mock_urlopen):
        payload = [
            {
                "id": 42,
                # Missing session_title
                "start_time": "2026-10-22T20:00:00Z",
                "duration": 7200000,
                "source": "https://stream.kick.com/42/master.m3u8",
                "is_live": False,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with self.assertRaises(KickError) as ctx:
            list_vods("testchannel")
        self.assertIn("session_title", str(ctx.exception))

    @patch("subprocess.run")
    def test_probe_duration(self, mock_subproc):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "12044.400000\n"
        mock_subproc.return_value = mock_proc

        dur = probe_duration(Path("/fake/video.mp4"))
        self.assertAlmostEqual(dur, 12044.4)

    @patch("subprocess.run")
    def test_probe_duration_error(self, mock_subproc):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Invalid data found\n"
        mock_subproc.return_value = mock_proc

        with self.assertRaises(KickError):
            probe_duration(Path("/fake/bad.mp4"))

    @patch("k2y.kick.probe_duration")
    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_download_success(self, mock_urlopen, mock_subproc, mock_probe):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "source.mp4"
            part_file = Path(f"{dest}.part")

            # Mock master playlist fetch
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = SAMPLE_MASTER.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            # Mock ffmpeg creating .part file
            def create_part(*args, **kwargs):
                part_file.write_text("dummy video data")
                proc = MagicMock()
                proc.returncode = 0
                return proc

            mock_subproc.side_effect = create_part

            # Probe duration within 1% (expected 1000s, probe 1005s)
            mock_probe.return_value = 1005.0

            vod = {
                "id": 1,
                "source_url": "https://stream.kick.com/x/media/hls/master.m3u8",
                "duration_s": 1000,
            }
            res = download(vod, dest)
            self.assertEqual(res, dest)
            self.assertTrue(dest.exists())
            self.assertFalse(part_file.exists())

    @patch("k2y.kick.probe_duration")
    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_download_duration_mismatch(self, mock_urlopen, mock_subproc, mock_probe):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "source.mp4"
            part_file = Path(f"{dest}.part")

            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = SAMPLE_MASTER.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            def create_part(*args, **kwargs):
                part_file.write_text("dummy video data")
                proc = MagicMock()
                proc.returncode = 0
                return proc

            mock_subproc.side_effect = create_part

            # Probe duration exceeds 1% (expected 1000s, probe 950s -> 5% diff)
            mock_probe.return_value = 950.0

            vod = {
                "id": 1,
                "source_url": "https://stream.kick.com/x/media/hls/master.m3u8",
                "duration_s": 1000,
            }
            with self.assertRaises(KickError) as ctx:
                download(vod, dest)

            self.assertIn("Duration mismatch", str(ctx.exception))
            self.assertFalse(part_file.exists())
            self.assertFalse(dest.exists())

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_download_ffmpeg_failure(self, mock_urlopen, mock_subproc):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "source.mp4"
            part_file = Path(f"{dest}.part")

            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = SAMPLE_MASTER.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            # Mock ffmpeg failing
            def fail_ffmpeg(*args, **kwargs):
                part_file.write_text("corrupted")
                proc = MagicMock()
                proc.returncode = 1
                proc.stderr = "Error reading packets: connection reset\n"
                return proc

            mock_subproc.side_effect = fail_ffmpeg

            vod = {
                "id": 1,
                "source_url": "https://stream.kick.com/x/media/hls/master.m3u8",
                "duration_s": 1000,
            }
            with self.assertRaises(KickError) as ctx:
                download(vod, dest)

            self.assertIn("ffmpeg download failed", str(ctx.exception))
            self.assertFalse(part_file.exists())
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()

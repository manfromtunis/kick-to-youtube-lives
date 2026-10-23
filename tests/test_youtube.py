"""Unit tests for k2y.youtube.

Uses Python stdlib unittest only, no network access (all network mocked).
"""
from __future__ import annotations

import email
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.parse
import urllib.request

from k2y import config, youtube


def _make_headers(headers: dict[str, str] | None = None) -> email.message.Message:
    lines = [f"{k}: {v}" for k, v in (headers or {}).items()]
    return email.message_from_string("\n".join(lines) + ("\n\n" if lines else ""))


class MockResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ):
        self.status = status
        self.code = status
        self.headers = _make_headers(headers)
        self._body = io.BytesIO(body)

    def read(self, *args: int) -> bytes:
        return self._body.read(*args)

    def __enter__(self) -> MockResponse:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        pass


def _make_http_error(
    code: int,
    reason: str = "Error",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://www.googleapis.com/test",
        code,
        reason,
        _make_headers(headers),
        io.BytesIO(body),
    )


class TestYouTube(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.patch_config_dir = patch.object(config, "CONFIG_DIR", self.config_dir)
        self.patch_config_dir.start()

        self.patch_sleep = patch.object(youtube, "_sleep", MagicMock())
        self.mock_sleep = self.patch_sleep.start()

        # Seed valid client_secret.json
        client_secret_data = {
            "installed": {
                "client_id": "mock_client_id.apps.googleusercontent.com",
                "client_secret": "mock_client_secret_xyz",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        config.write_secret("client_secret.json", json.dumps(client_secret_data))

        # Seed valid youtube_token.json
        token_data = {
            "refresh_token": "mock_refresh_token",
            "access_token": "mock_access_token",
            "expires_at": int(time.time()) + 3600,
        }
        config.write_secret("youtube_token.json", json.dumps(token_data))

    def tearDown(self) -> None:
        self.patch_sleep.stop()
        self.patch_config_dir.stop()
        self.temp_dir.cleanup()

    # 1. Quota error parsing raises QuotaExceeded for quotaExceeded, uploadLimitExceeded, dailyLimitExceeded on any 4xx
    def test_quota_error_parsing_raises_quota_exceeded(self) -> None:
        quota_body = json.dumps({
            "error": {
                "code": 403,
                "message": "Quota exceeded",
                "errors": [{"reason": "quotaExceeded", "message": "limit"}],
            }
        }).encode("utf-8")
        err = _make_http_error(403, "Forbidden", quota_body)
        parsed = youtube._parse_youtube_error(err)
        self.assertIsInstance(parsed, youtube.QuotaExceeded)
        self.assertIn("quotaExceeded", str(parsed))

        upload_limit_body = json.dumps({
            "error": {
                "code": 400,
                "message": "User upload limit exceeded.",
                "errors": [{"reason": "uploadLimitExceeded", "message": "limit"}],
            }
        }).encode("utf-8")
        err2 = _make_http_error(400, "Bad Request", upload_limit_body)
        parsed2 = youtube._parse_youtube_error(err2)
        self.assertIsInstance(parsed2, youtube.QuotaExceeded)
        self.assertIn("uploadLimitExceeded", str(parsed2))

        daily_limit_body = json.dumps({
            "error": {
                "code": 429,
                "message": "Daily limit exceeded.",
                "errors": [{"reason": "dailyLimitExceeded", "message": "limit"}],
            }
        }).encode("utf-8")
        err3 = _make_http_error(429, "Too Many Requests", daily_limit_body)
        parsed3 = youtube._parse_youtube_error(err3)
        self.assertIsInstance(parsed3, youtube.QuotaExceeded)
        self.assertIn("dailyLimitExceeded", str(parsed3))

    def test_ratelimit_exceeded_not_quota_exceeded(self) -> None:
        rate_limit_body = json.dumps({
            "error": {
                "code": 403,
                "message": "Rate limit exceeded",
                "errors": [{"reason": "rateLimitExceeded", "message": "rate"}],
            }
        }).encode("utf-8")
        err = _make_http_error(403, "Forbidden", rate_limit_body)
        parsed = youtube._parse_youtube_error(err)
        self.assertNotIsInstance(parsed, youtube.QuotaExceeded)
        self.assertIsInstance(parsed, youtube.YouTubeError)

    # AuthRequired tests
    def test_auth_required_missing_secret(self) -> None:
        (self.config_dir / "youtube_token.json").unlink(missing_ok=True)
        with self.assertRaises(youtube.AuthRequired) as ctx:
            youtube.access_token()
        self.assertIn("YouTube authorization expired or missing: run `py -m k2y auth`", str(ctx.exception))
        self.assertNotIn("mock_refresh_token", str(ctx.exception))

    def test_auth_required_empty_or_invalid_json_or_lacks_refresh_token(self) -> None:
        # Empty
        config.write_secret("youtube_token.json", "")
        with self.assertRaises(youtube.AuthRequired):
            youtube.access_token()

        # Invalid JSON
        config.write_secret("youtube_token.json", "{not_valid_json")
        with self.assertRaises(youtube.AuthRequired):
            youtube.access_token()

        # Lacks refresh_token
        config.write_secret("youtube_token.json", json.dumps({"access_token": "only_acc"}))
        with self.assertRaises(youtube.AuthRequired):
            youtube.access_token()

    @patch("urllib.request.urlopen")
    def test_auth_required_on_invalid_grant(self, mock_urlopen: MagicMock) -> None:
        expired_token_data = {
            "refresh_token": "secret_refresh_token_123",
            "access_token": "expired_token",
            "expires_at": int(time.time()) - 100,
        }
        config.write_secret("youtube_token.json", json.dumps(expired_token_data))

        invalid_grant_body = json.dumps({
            "error": "invalid_grant",
            "error_description": "Bad Request",
        }).encode("utf-8")
        mock_urlopen.side_effect = _make_http_error(400, "Bad Request", invalid_grant_body)

        with self.assertRaises(youtube.AuthRequired) as ctx:
            youtube.access_token()
        self.assertIn("YouTube authorization expired or missing: run `py -m k2y auth`", str(ctx.exception))
        self.assertNotIn("secret_refresh_token_123", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_auth_required_on_invalid_client_and_unauthorized_client(self, mock_urlopen: MagicMock) -> None:
        expired_token_data = {
            "refresh_token": "secret_refresh_token_123",
            "access_token": "expired_token",
            "expires_at": int(time.time()) - 100,
        }
        config.write_secret("youtube_token.json", json.dumps(expired_token_data))

        # invalid_client
        invalid_client_body = json.dumps({
            "error": "invalid_client",
            "error_description": "The OAuth client was not found.",
        }).encode("utf-8")
        mock_urlopen.side_effect = _make_http_error(401, "Unauthorized", invalid_client_body)

        with self.assertRaises(youtube.AuthRequired) as ctx:
            youtube.access_token()
        self.assertIn("YouTube authorization expired or missing: run `py -m k2y auth`", str(ctx.exception))

        # unauthorized_client
        unauthorized_client_body = json.dumps({
            "error": "unauthorized_client",
            "error_description": "The client is not authorized.",
        }).encode("utf-8")
        mock_urlopen.side_effect = _make_http_error(400, "Bad Request", unauthorized_client_body)

        with self.assertRaises(youtube.AuthRequired) as ctx:
            youtube.access_token()
        self.assertIn("YouTube authorization expired or missing: run `py -m k2y auth`", str(ctx.exception))

    # 2. privacyStatus is private in the init request body
    @patch("urllib.request.urlopen")
    def test_privacy_status_is_private_in_init_request_body(
        self, mock_urlopen: MagicMock
    ) -> None:
        dummy_video = self.config_dir / "test_video.mp4"
        dummy_video.write_bytes(b"A" * 1024)

        recorded_requests: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            recorded_requests.append(req)
            if "uploadType=resumable" in req.full_url:
                return MockResponse(
                    200,
                    {"Location": "https://upload.youtube.com/upload/session_123"},
                    b"",
                )
            if "session_123" in req.full_url:
                return MockResponse(200, {}, json.dumps({"id": "vid_xyz"}).encode("utf-8"))
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen

        meta = {
            "title": "Stream title that may be long",
            "description": "Stream description",
            "category_id": 20,
            "privacyStatus": "public",
            "tags": ["gaming", "kick"],
        }
        session_calls: list[str] = []
        video_id = youtube.upload(
            dummy_video,
            meta,
            session_url=None,
            on_session=session_calls.append,
        )
        self.assertEqual(video_id, "vid_xyz")

        init_req = recorded_requests[0]
        init_payload = json.loads(init_req.data.decode("utf-8"))
        self.assertEqual(init_payload["status"]["privacyStatus"], "private")
        self.assertEqual(init_payload["status"]["selfDeclaredMadeForKids"], False)
        self.assertEqual(init_payload["snippet"]["defaultLanguage"], "fr")
        self.assertEqual(init_payload["snippet"]["defaultAudioLanguage"], "ar")
        self.assertEqual(init_payload["snippet"]["categoryId"], "20")
        self.assertEqual(init_payload["snippet"]["tags"], ["gaming", "kick"])

    # 3. on_session is called before the first chunk
    @patch("urllib.request.urlopen")
    def test_on_session_is_called_before_first_chunk(
        self, mock_urlopen: MagicMock
    ) -> None:
        dummy_video = self.config_dir / "test_video.mp4"
        dummy_video.write_bytes(b"B" * 1024)

        event_order: list[str] = []

        def on_session_cb(url: str) -> None:
            event_order.append(f"on_session:{url}")

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            if "uploadType=resumable" in req.full_url:
                event_order.append("init_post")
                return MockResponse(
                    200,
                    {"Location": "https://upload.youtube.com/upload/my_session"},
                    b"",
                )
            if "my_session" in req.full_url:
                event_order.append(f"chunk_put:{req.headers.get('Content-range')}")
                return MockResponse(200, {}, json.dumps({"id": "res_vid"}).encode("utf-8"))
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen

        meta = {"title": "Title", "description": "Desc", "category_id": "22"}
        vid = youtube.upload(dummy_video, meta, session_url=None, on_session=on_session_cb)
        self.assertEqual(vid, "res_vid")

        self.assertEqual(
            event_order,
            [
                "init_post",
                "on_session:https://upload.youtube.com/upload/my_session",
                "chunk_put:bytes 0-1023/1024",
            ],
        )

    # 4. resume from a 308 with Range: bytes=0-8388607 continues at 8388608
    @patch("urllib.request.urlopen")
    def test_resume_from_308_range_continues_at_8388608(
        self, mock_urlopen: MagicMock
    ) -> None:
        total_size = 10 * 1024 * 1024  # 10 MiB
        dummy_video = self.config_dir / "large_video.mp4"
        dummy_video.write_bytes(b"C" * total_size)

        recorded_ranges: list[str] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            content_range = req.headers.get("Content-range")
            recorded_ranges.append(str(content_range))
            if content_range == f"bytes */{total_size}":
                raise _make_http_error(
                    308,
                    "Resume Incomplete",
                    b"",
                    {"Range": "bytes=0-8388607"},
                )
            return MockResponse(200, {}, json.dumps({"id": "resumed_vid"}).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        meta = {"title": "Title", "description": "Desc"}
        vid = youtube.upload(
            dummy_video,
            meta,
            session_url="https://upload.youtube.com/existing_session",
            on_session=lambda _: None,
        )
        self.assertEqual(vid, "resumed_vid")
        self.assertEqual(recorded_ranges[0], f"bytes */{total_size}")
        self.assertEqual(recorded_ranges[1], f"bytes 8388608-{total_size - 1}/{total_size}")

    # Resume from 308 with no Range header continues at 0
    @patch("urllib.request.urlopen")
    def test_resume_from_308_no_range_continues_at_0(self, mock_urlopen: MagicMock) -> None:
        total_size = 1000
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"Z" * total_size)

        recorded_ranges: list[str] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            content_range = req.headers.get("Content-range")
            recorded_ranges.append(str(content_range))
            if content_range == f"bytes */{total_size}":
                raise _make_http_error(308, "Resume Incomplete", b"", {})
            return MockResponse(200, {}, json.dumps({"id": "vid_zero_resumed"}).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        vid = youtube.upload(
            dummy_video,
            {"title": "Title"},
            session_url="https://upload.youtube.com/existing_session",
            on_session=lambda _: None,
        )
        self.assertEqual(vid, "vid_zero_resumed")
        self.assertEqual(recorded_ranges[1], f"bytes 0-{total_size - 1}/{total_size}")

    # 5. 200 on status query returns the id without uploading
    @patch("urllib.request.urlopen")
    def test_200_on_status_query_returns_id_without_uploading(
        self, mock_urlopen: MagicMock
    ) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"D" * 5000)

        call_count = 0

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            nonlocal call_count
            call_count += 1
            self.assertEqual(req.headers.get("Content-range"), "bytes */5000")
            return MockResponse(200, {}, json.dumps({"id": "already_uploaded_id"}).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        vid = youtube.upload(
            dummy_video,
            {"title": "Title"},
            session_url="https://upload.youtube.com/done_session",
            on_session=lambda _: None,
        )
        self.assertEqual(vid, "already_uploaded_id")
        self.assertEqual(call_count, 1)

    # 6. token refresh when expired
    @patch("urllib.request.urlopen")
    def test_token_refresh_when_expired(self, mock_urlopen: MagicMock) -> None:
        expired_token_data = {
            "refresh_token": "refresh_123",
            "access_token": "old_expired_token",
            "expires_at": int(time.time()) - 100,
        }
        config.write_secret("youtube_token.json", json.dumps(expired_token_data))

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            data = urllib.parse.parse_qs(req.data.decode("utf-8"))
            self.assertEqual(data["grant_type"][0], "refresh_token")
            self.assertEqual(data["refresh_token"][0], "refresh_123")
            self.assertEqual(data["client_id"][0], "mock_client_id.apps.googleusercontent.com")
            return MockResponse(
                200,
                {},
                json.dumps({
                    "access_token": "fresh_access_token_999",
                    "expires_in": 3600,
                }).encode("utf-8"),
            )

        mock_urlopen.side_effect = fake_urlopen

        tok = youtube.access_token()
        self.assertEqual(tok, "fresh_access_token_999")

        saved = json.loads(config.read_secret("youtube_token.json"))
        self.assertEqual(saved["access_token"], "fresh_access_token_999")
        self.assertGreater(saved["expires_at"], time.time() + 3500)

    # Test token not refreshed when valid (> 60s)
    @patch("urllib.request.urlopen")
    def test_token_not_refreshed_when_valid(self, mock_urlopen: MagicMock) -> None:
        valid_token_data = {
            "refresh_token": "refresh_123",
            "access_token": "valid_token_abc",
            "expires_at": int(time.time()) + 1800,
        }
        config.write_secret("youtube_token.json", json.dumps(valid_token_data))

        tok = youtube.access_token()
        self.assertEqual(tok, "valid_token_abc")
        mock_urlopen.assert_not_called()

    # 7. thumbnail forbidden raises ThumbnailsForbidden
    @patch("urllib.request.urlopen")
    def test_thumbnail_forbidden_message(self, mock_urlopen: MagicMock) -> None:
        dummy_jpeg = self.config_dir / "thumb.jpg"
        dummy_jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        err_body = json.dumps({
            "error": {
                "code": 403,
                "message": "The caller does not have permission",
                "errors": [
                    {
                        "message": "The caller does not have permission",
                        "domain": "youtube.thumbnail",
                        "reason": "forbidden",
                    }
                ],
            }
        }).encode("utf-8")
        mock_urlopen.side_effect = _make_http_error(403, "Forbidden", err_body)

        with self.assertRaises(youtube.ThumbnailsForbidden) as ctx:
            youtube.set_thumbnail("vid_123", dummy_jpeg)

        self.assertEqual(
            str(ctx.exception),
            "custom thumbnails need a phone-verified channel: https://www.youtube.com/verify",
        )

    # 8. thumbnail success
    @patch("urllib.request.urlopen")
    def test_thumbnail_success(self, mock_urlopen: MagicMock) -> None:
        dummy_jpeg = self.config_dir / "thumb.jpg"
        dummy_jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

        recorded_reqs: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            recorded_reqs.append(req)
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        youtube.set_thumbnail("video_abc", dummy_jpeg)

        self.assertEqual(len(recorded_reqs), 1)
        req = recorded_reqs[0]
        self.assertIn("videoId=video_abc", req.full_url)
        self.assertEqual(req.headers.get("Content-type"), "image/jpeg")
        self.assertEqual(req.headers.get("Authorization"), "Bearer mock_access_token")

    # Thumbnail 401 retry
    @patch("urllib.request.urlopen")
    def test_thumbnail_401_retry(self, mock_urlopen: MagicMock) -> None:
        dummy_jpeg = self.config_dir / "thumb.jpg"
        dummy_jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

        calls = 0

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            nonlocal calls
            calls += 1
            if "thumbnails/set" in req.full_url:
                if calls == 1:
                    raise _make_http_error(401, "Unauthorized", b"")
                return MockResponse(200, {}, b"{}")
            if "oauth2.googleapis.com/token" in req.full_url:
                return MockResponse(
                    200,
                    {},
                    json.dumps({"access_token": "thumb_new_tok", "expires_in": 3600}).encode("utf-8"),
                )
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        youtube.set_thumbnail("video_abc", dummy_jpeg)
        self.assertEqual(calls, 3)

    # 9. 401 retry in upload and thumbnail
    @patch("urllib.request.urlopen")
    def test_401_retry_in_upload_init(self, mock_urlopen: MagicMock) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"E" * 100)

        attempts = 0

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            nonlocal attempts
            attempts += 1
            if "uploadType=resumable" in req.full_url:
                if attempts == 1:
                    raise _make_http_error(401, "Unauthorized", b"")
                return MockResponse(
                    200,
                    {"Location": "https://upload.youtube.com/session_401_recovered"},
                    b"",
                )
            if "oauth2.googleapis.com/token" in req.full_url:
                return MockResponse(
                    200,
                    {},
                    json.dumps({
                        "access_token": "new_refreshed_tok",
                        "expires_in": 3600,
                    }).encode("utf-8"),
                )
            if "session_401_recovered" in req.full_url:
                return MockResponse(200, {}, json.dumps({"id": "rec_vid"}).encode("utf-8"))
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        vid = youtube.upload(dummy_video, {"title": "Title"}, None, lambda _: None)
        self.assertEqual(vid, "rec_vid")
        self.assertEqual(attempts, 4)

    # 10. 5xx retry with backoff and status query recovery
    @patch("urllib.request.urlopen")
    def test_upload_5xx_retry_recovers(
        self, mock_urlopen: MagicMock
    ) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"F" * 500)

        step = 0

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            nonlocal step
            step += 1
            if step == 1:
                raise _make_http_error(503, "Service Unavailable", b"Backend error")
            if step == 2:
                raise _make_http_error(
                    308, "Resume Incomplete", b"", {"Range": "bytes=0-99"}
                )
            if step == 3:
                return MockResponse(200, {}, json.dumps({"id": "vid_5xx_ok"}).encode("utf-8"))
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        vid = youtube.upload(
            dummy_video,
            {"title": "Title"},
            session_url="https://upload.youtube.com/retry_session",
            on_session=lambda _: None,
        )
        self.assertEqual(vid, "vid_5xx_ok")
        self.mock_sleep.assert_called_once_with(5)

    # 11. Restored session returning 404 on status query does NOT restart session
    @patch("urllib.request.urlopen")
    def test_restored_session_404_raises_youtube_error(self, mock_urlopen: MagicMock) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"G" * 300)

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            if "expired_session" in req.full_url:
                raise _make_http_error(404, "Not Found", b"Session expired")
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        with self.assertRaises(youtube.YouTubeError) as ctx:
            youtube.upload(
                dummy_video,
                {"title": "My Stream Title"},
                session_url="https://upload.youtube.com/expired_session",
                on_session=lambda _: None,
            )
        self.assertIn(
            "upload session expired; check YouTube Studio for a finished duplicate of 'My Stream Title', "
            "then run `py -m k2y retry <vod> --restart-upload`",
            str(ctx.exception),
        )

    # Fresh session created inside upload() returning 404 during upload restarts session
    @patch("urllib.request.urlopen")
    def test_fresh_session_404_during_upload_restarts_session(self, mock_urlopen: MagicMock) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"G" * 300)

        sessions: list[str] = []
        calls = 0

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            nonlocal calls
            calls += 1
            if "uploadType=resumable" in req.full_url:
                return MockResponse(
                    200,
                    {"Location": f"https://upload.youtube.com/fresh_session_{calls}"},
                    b"",
                )
            if "fresh_session_1" in req.full_url:
                raise _make_http_error(404, "Not Found", b"Session expired")
            if "fresh_session_" in req.full_url:
                return MockResponse(200, {}, json.dumps({"id": "vid_fresh_ok"}).encode("utf-8"))
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        vid = youtube.upload(
            dummy_video,
            {"title": "Title"},
            session_url=None,
            on_session=sessions.append,
        )
        self.assertEqual(vid, "vid_fresh_ok")
        self.assertEqual(len(sessions), 2)
        self.assertIn("https://upload.youtube.com/fresh_session_1", sessions)
        self.assertIn("https://upload.youtube.com/fresh_session_3", sessions)

    # Session restart cap at 2 for fresh sessions
    @patch("urllib.request.urlopen")
    def test_upload_caps_session_restarts_at_2(self, mock_urlopen: MagicMock) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"G" * 300)

        session_counter = 0

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            nonlocal session_counter
            if "uploadType=resumable" in req.full_url:
                session_counter += 1
                return MockResponse(200, {"Location": f"https://upload.youtube.com/sess_{session_counter}"}, b"")
            if "sess_" in req.full_url:
                # Every session chunk upload returns 404 expired
                raise _make_http_error(404, "Not Found", b"Expired")
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        with self.assertRaises(youtube.YouTubeError) as ctx:
            youtube.upload(
                dummy_video,
                {"title": "Title"},
                session_url=None,
                on_session=lambda _: None,
            )
        self.assertIn("max 2 restarts", str(ctx.exception))
        # Initial session + 2 restarts = 3 sessions initialized
        self.assertEqual(session_counter, 3)

    # 12. OAuth auth() loopback flow and PKCE
    @patch("http.server.HTTPServer")
    @patch("builtins.print")
    @patch("webbrowser.open")
    @patch("urllib.request.urlopen")
    def test_auth_loopback_flow(
        self,
        mock_urlopen: MagicMock,
        mock_browser: MagicMock,
        mock_print: MagicMock,
        mock_http_server: MagicMock,
    ) -> None:
        captured_html: list[bytes] = []

        def fake_server_init(addr: tuple[str, int], handler_cls: type) -> MagicMock:
            server = MagicMock()
            server.server_port = 8080

            def handle_request() -> None:
                auth_url = mock_browser.call_args[0][0]
                state = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]
                handler = handler_cls.__new__(handler_cls)
                handler.path = f"/?state={state}&code=test_auth_code_123"
                handler.wfile = io.BytesIO()
                handler.send_response = MagicMock()
                handler.send_header = MagicMock()
                handler.end_headers = MagicMock()
                handler.do_GET()
                captured_html.append(handler.wfile.getvalue())

            server.handle_request = handle_request
            return server

        mock_http_server.side_effect = fake_server_init

        def fake_token_exchange(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            data = urllib.parse.parse_qs(req.data.decode("utf-8"))
            self.assertEqual(data["code"][0], "test_auth_code_123")
            self.assertEqual(data["grant_type"][0], "authorization_code")
            self.assertIn("code_verifier", data)
            return MockResponse(
                200,
                {},
                json.dumps({
                    "refresh_token": "brand_new_refresh_tok",
                    "access_token": "brand_new_access_tok",
                    "expires_in": 3600,
                }).encode("utf-8"),
            )

        mock_urlopen.side_effect = fake_token_exchange

        youtube.auth()

        self.assertIn(b"k2y: you can close this tab", captured_html[0])
        saved = json.loads(config.read_secret("youtube_token.json"))
        self.assertEqual(saved["refresh_token"], "brand_new_refresh_tok")
        self.assertEqual(saved["access_token"], "brand_new_access_tok")

    # 13. OAuth auth() fails if no refresh token
    @patch("http.server.HTTPServer")
    @patch("builtins.print")
    @patch("webbrowser.open")
    @patch("urllib.request.urlopen")
    def test_auth_missing_refresh_token_raises_youtube_error(
        self,
        mock_urlopen: MagicMock,
        mock_browser: MagicMock,
        mock_print: MagicMock,
        mock_http_server: MagicMock,
    ) -> None:
        def fake_server_init(addr: tuple[str, int], handler_cls: type) -> MagicMock:
            server = MagicMock()
            server.server_port = 8080

            def handle_request() -> None:
                auth_url = mock_browser.call_args[0][0]
                state = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]
                handler = handler_cls.__new__(handler_cls)
                handler.path = f"/?state={state}&code=code_no_refresh"
                handler.wfile = io.BytesIO()
                handler.send_response = MagicMock()
                handler.send_header = MagicMock()
                handler.end_headers = MagicMock()
                handler.do_GET()

            server.handle_request = handle_request
            return server

        mock_http_server.side_effect = fake_server_init

        mock_urlopen.return_value = MockResponse(
            200,
            {},
            json.dumps({"access_token": "acc_only", "expires_in": 3600}).encode("utf-8"),
        )

        with self.assertRaises(youtube.YouTubeError) as ctx:
            youtube.auth()

        self.assertIn("refresh_token", str(ctx.exception))

    # 14. OAuth auth() fails on state mismatch
    @patch("http.server.HTTPServer")
    @patch("builtins.print")
    @patch("webbrowser.open")
    @patch("urllib.request.urlopen")
    def test_auth_state_mismatch_raises_youtube_error(
        self,
        mock_urlopen: MagicMock,
        mock_browser: MagicMock,
        mock_print: MagicMock,
        mock_http_server: MagicMock,
    ) -> None:
        def fake_server_init(addr: tuple[str, int], handler_cls: type) -> MagicMock:
            server = MagicMock()
            server.server_port = 8080

            def handle_request() -> None:
                handler = handler_cls.__new__(handler_cls)
                handler.path = "/?state=wrong_state&code=test_code"
                handler.wfile = io.BytesIO()
                handler.send_response = MagicMock()
                handler.send_header = MagicMock()
                handler.end_headers = MagicMock()
                handler.do_GET()

            server.handle_request = handle_request
            return server

        mock_http_server.side_effect = fake_server_init

        with self.assertRaises(youtube.YouTubeError) as ctx:
            youtube.auth()

        self.assertIn("state mismatch", str(ctx.exception))

    # 15. Sanitization and Truncation of title and description per spec
    @patch("urllib.request.urlopen")
    def test_metadata_sanitization_and_truncation(self, mock_urlopen: MagicMock) -> None:
        dummy_video = self.config_dir / "video.mp4"
        dummy_video.write_bytes(b"H" * 100)

        recorded_reqs: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            recorded_reqs.append(req)
            if "uploadType=resumable" in req.full_url:
                return MockResponse(200, {"Location": "https://upload.youtube.com/sess_trunc"}, b"")
            return MockResponse(200, {}, json.dumps({"id": "trunc_id"}).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        # Contains < and >, exceeds 100 chars
        long_title = "<Cool> " + ("T" * 150)
        # Multibyte arabic chars (2 bytes each), exceeding 5000 bytes
        long_desc = "<Desc> " + ("\u0645" * 3000)
        vid = youtube.upload(
            dummy_video,
            {"title": long_title, "description": long_desc},
            None,
            lambda _: None,
        )
        self.assertEqual(vid, "trunc_id")
        init_data = json.loads(recorded_reqs[0].data.decode("utf-8"))
        res_title = init_data["snippet"]["title"]
        res_desc = init_data["snippet"]["description"]

        self.assertNotIn("<", res_title)
        self.assertNotIn(">", res_title)
        self.assertNotIn("<", res_desc)
        self.assertNotIn(">", res_desc)
        self.assertLessEqual(len(res_title), 100)
        self.assertLessEqual(len(res_desc.encode("utf-8")), 5000)
        # Verify valid UTF-8 string boundary
        res_desc.encode("utf-8").decode("utf-8")

    # 16. Multi-chunk upload (e.g. 18 MiB = two 8 MiB chunks + one 2 MiB chunk)
    @patch("urllib.request.urlopen")
    def test_multi_chunk_upload(self, mock_urlopen: MagicMock) -> None:
        total_size = 18 * 1024 * 1024  # 18 MiB
        dummy_video = self.config_dir / "18mb.mp4"
        dummy_video.write_bytes(b"M" * total_size)

        uploaded_ranges: list[str] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 300) -> MockResponse:
            if "uploadType=resumable" in req.full_url:
                return MockResponse(200, {"Location": "https://upload.youtube.com/sess_multi"}, b"")
            content_range = req.headers.get("Content-range")
            uploaded_ranges.append(str(content_range))
            if content_range == f"bytes 0-{8 * 1024 * 1024 - 1}/{total_size}":
                raise _make_http_error(
                    308, "Resume Incomplete", b"", {"Range": f"bytes=0-{8 * 1024 * 1024 - 1}"}
                )
            if content_range == f"bytes {8 * 1024 * 1024}-{16 * 1024 * 1024 - 1}/{total_size}":
                raise _make_http_error(
                    308, "Resume Incomplete", b"", {"Range": f"bytes=0-{16 * 1024 * 1024 - 1}"}
                )
            return MockResponse(200, {}, json.dumps({"id": "multi_chunk_vid"}).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        vid = youtube.upload(
            dummy_video,
            {"title": "18MB video"},
            session_url=None,
            on_session=lambda _: None,
        )
        self.assertEqual(vid, "multi_chunk_vid")
        self.assertEqual(len(uploaded_ranges), 3)
        self.assertEqual(uploaded_ranges[0], f"bytes 0-{8 * 1024 * 1024 - 1}/{total_size}")
        self.assertEqual(
            uploaded_ranges[1],
            f"bytes {8 * 1024 * 1024}-{16 * 1024 * 1024 - 1}/{total_size}",
        )
        self.assertEqual(
            uploaded_ranges[2],
            f"bytes {16 * 1024 * 1024}-{total_size - 1}/{total_size}",
        )

    # 17. OAuth auth(paste=True) flow
    @patch("builtins.print")
    @patch("builtins.input")
    @patch("urllib.request.urlopen")
    def test_auth_paste_flow(
        self,
        mock_urlopen: MagicMock,
        mock_input: MagicMock,
        mock_print: MagicMock,
    ) -> None:
        captured_auth_url: list[str] = []

        def fake_print(*args: object, **kwargs: object) -> None:
            for a in args:
                if "Open this URL to authorize k2y:" in str(a):
                    captured_auth_url.append(str(a))

        mock_print.side_effect = fake_print

        def fake_token_exchange(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            self.assertEqual(timeout, 60)
            data = urllib.parse.parse_qs(req.data.decode("utf-8"))
            self.assertEqual(data["code"][0], "test_paste_code_456")
            self.assertEqual(data["grant_type"][0], "authorization_code")
            self.assertEqual(data["redirect_uri"][0], "http://127.0.0.1:8765")
            self.assertIn("code_verifier", data)
            return MockResponse(
                200,
                {},
                json.dumps({
                    "refresh_token": "pasted_refresh_token",
                    "access_token": "pasted_access_token",
                    "expires_in": 3600,
                }).encode("utf-8"),
            )

        mock_urlopen.side_effect = fake_token_exchange

        def fake_input(prompt: str = "") -> str:
            raw = captured_auth_url[0]
            url = raw.split("\n")[-1].strip()
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            state = qs["state"][0]
            return f"http://127.0.0.1:8765/?state={state}&code=test_paste_code_456"

        mock_input.side_effect = fake_input

        youtube.auth(paste=True)

        saved = json.loads(config.read_secret("youtube_token.json"))
        self.assertEqual(saved["refresh_token"], "pasted_refresh_token")
        self.assertEqual(saved["access_token"], "pasted_access_token")

    @patch("builtins.print")
    @patch("builtins.input", return_value="http://127.0.0.1:8765/?state=bad_state&code=test_paste_code")
    def test_auth_paste_state_mismatch(self, mock_input: MagicMock, mock_print: MagicMock) -> None:
        with self.assertRaises(youtube.YouTubeError) as ctx:
            youtube.auth(paste=True)
        self.assertIn("state mismatch", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_timeouts_on_urlopen(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = MockResponse(
            200, {}, json.dumps({"access_token": "tok", "expires_in": 3600}).encode("utf-8")
        )
        youtube.access_token(force_refresh=True)
        self.assertEqual(mock_urlopen.call_args.kwargs.get("timeout"), 60)

        mock_urlopen.return_value = MockResponse(308, {"Range": "bytes=0-100"}, b"")
        youtube._query_status_once("https://upload.youtube.com/sess", 1000)
        self.assertEqual(mock_urlopen.call_args.kwargs.get("timeout"), 300)

        mock_urlopen.return_value = MockResponse(308, {"Range": "bytes=0-100"}, b"")
        youtube._upload_chunk_once("https://upload.youtube.com/sess", b"test", 0, 1000)
        self.assertEqual(mock_urlopen.call_args.kwargs.get("timeout"), 300)

    def test_retry_backoff_formula(self) -> None:
        expected = [5, 10, 20, 40, 60, 60]
        actual = [min(60, 5 * (2 ** (a - 1))) for a in range(1, 7)]
        self.assertEqual(actual, expected)


class TestUpdateSnippet(TestYouTube):
    @patch("urllib.request.urlopen")
    def test_update_snippet_request_shape(self, mock_urlopen: MagicMock) -> None:
        recorded_reqs: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            recorded_reqs.append(req)
            return MockResponse(200, {}, json.dumps({"id": "vid123", "snippet": {}}).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen
        meta = {
            "title": "Test Title",
            "description": "Test Desc",
            "category_id": "20",
            "tags": ["gaming", "kick"],
        }
        res = youtube.update_snippet("vid123", meta)
        self.assertEqual(res.get("id"), "vid123")
        self.assertEqual(len(recorded_reqs), 1)

        req = recorded_reqs[0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.full_url, "https://www.googleapis.com/youtube/v3/videos?part=snippet")
        self.assertEqual(req.headers.get("Authorization"), "Bearer mock_access_token")
        self.assertEqual(req.headers.get("Content-type"), "application/json; charset=UTF-8")

        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["id"], "vid123")
        snippet = body["snippet"]
        self.assertEqual(snippet["title"], "Test Title")
        self.assertEqual(snippet["description"], "Test Desc")
        self.assertEqual(snippet["categoryId"], "20")
        self.assertEqual(snippet["defaultLanguage"], "fr")
        self.assertEqual(snippet["defaultAudioLanguage"], "ar")
        self.assertEqual(snippet["tags"], ["gaming", "kick"])

    @patch("urllib.request.urlopen")
    def test_update_snippet_strips_brackets_and_truncates(self, mock_urlopen: MagicMock) -> None:
        recorded_reqs: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            recorded_reqs.append(req)
            return MockResponse(200, {}, b"{}")

        mock_urlopen.side_effect = fake_urlopen
        long_title = "<Title> " + ("T" * 150)
        long_desc = "<Desc> " + ("D" * 6000)
        youtube.update_snippet("vid123", {"title": long_title, "description": long_desc})

        body = json.loads(recorded_reqs[0].data.decode("utf-8"))
        res_title = body["snippet"]["title"]
        res_desc = body["snippet"]["description"]

        self.assertNotIn("<", res_title)
        self.assertNotIn(">", res_title)
        self.assertNotIn("<", res_desc)
        self.assertNotIn(">", res_desc)
        self.assertLessEqual(len(res_title), 100)
        self.assertLessEqual(len(res_desc.encode("utf-8")), 5000)

    @patch("urllib.request.urlopen")
    def test_update_snippet_quota_error_raises_quota_exceeded(self, mock_urlopen: MagicMock) -> None:
        err_body = json.dumps({
            "error": {
                "errors": [{"reason": "quotaExceeded", "message": "Quota exceeded"}],
                "message": "The request cannot be completed because you have exceeded your quota.",
            }
        }).encode("utf-8")

        def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> MockResponse:
            raise _make_http_error(403, "Forbidden", err_body)

        mock_urlopen.side_effect = fake_urlopen
        with self.assertRaises(youtube.QuotaExceeded):
            youtube.update_snippet("vid123", {"title": "Title"})


if __name__ == "__main__":
    unittest.main()

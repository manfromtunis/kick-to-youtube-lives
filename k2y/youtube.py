"""YouTube Data API v3 client for k2y.

Handles OAuth 2.0 (loopback with PKCE and headless paste flow), resumable uploads, and thumbnails.
Uses Python stdlib only.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
from pathlib import Path
import secrets
import socket
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import config

_sleep = time.sleep

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB (multiple of 256 KiB)
OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube"
)


class QuotaExceeded(Exception):
    """Raised when YouTube API quota is exceeded (403 quotaExceeded or uploadLimitExceeded)."""
    pass


class YouTubeError(Exception):
    """General YouTube API error."""
    pass


class _SessionExpired(Exception):
    """Internal sentinel raised when an upload session is 404/410."""
    pass


def _parse_youtube_error(e: urllib.error.HTTPError, raw_body: str | None = None) -> Exception:
    code = e.code
    reason = str(getattr(e, "reason", "HTTPError"))
    msg = ""
    try:
        raw = raw_body if raw_body is not None else e.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            if "error" in data:
                err = data["error"]
                if isinstance(err, dict):
                    msg = err.get("message", "")
                    errors = err.get("errors", [])
                    reasons = [
                        item.get("reason")
                        for item in errors
                        if isinstance(item, dict) and "reason" in item
                    ]
                    if code == 403:
                        for r in reasons:
                            if r in ("quotaExceeded", "uploadLimitExceeded"):
                                return QuotaExceeded(f"{r}: {msg}")
                    if reasons:
                        reason = ", ".join(reasons)
                elif isinstance(err, str):
                    reason = err
                    msg = data.get("error_description", "")
    except Exception:
        pass

    err_str = f"HTTP {code}: {reason}"
    if msg:
        err_str += f" - {msg}"
    return YouTubeError(err_str)


def _load_client_secret() -> tuple[str, str, str, str]:
    raw = config.read_secret("client_secret.json")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise YouTubeError(f"Failed to parse client_secret.json: {e}")
    installed = data.get("installed") or data.get("web") or {}
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not client_id or not client_secret:
        raise YouTubeError("client_secret.json missing client_id or client_secret")
    auth_uri = installed.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")
    token_uri = installed.get("token_uri", "https://oauth2.googleapis.com/token")
    return client_id, client_secret, auth_uri, token_uri


def _exchange_and_save_token(
    client_id: str,
    client_secret: str,
    token_uri: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> None:
    post_data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }).encode("utf-8")

    req = urllib.request.Request(
        token_uri,
        data=post_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            token_resp = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _parse_youtube_error(e)
    except Exception as e:
        raise YouTubeError(f"Token exchange failed: {e}")

    refresh_token = token_resp.get("refresh_token")
    if not refresh_token:
        raise YouTubeError("No refresh_token returned by OAuth server")

    token_data = {
        "refresh_token": refresh_token,
        "access_token": token_resp.get("access_token", ""),
        "expires_at": int(time.time()) + int(token_resp.get("expires_in", 3600)),
    }
    config.write_secret("youtube_token.json", json.dumps(token_data, indent=2))


def auth(paste: bool = False) -> None:
    client_id, client_secret, auth_uri, token_uri = _load_client_secret()

    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(32)

    if paste:
        redirect_uri = "http://127.0.0.1:8765"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{auth_uri}?{urllib.parse.urlencode(params)}"
        print(f"Open this URL to authorize k2y:\n{auth_url}")
        print("approve, your browser will fail to open 127.0.0.1, copy the full address from the address bar")
        pasted = input("Paste the redirected URL: ").strip()

        parsed = urllib.parse.urlparse(pasted)
        qs = urllib.parse.parse_qs(parsed.query)

        req_state = qs.get("state", [None])[0]
        if req_state != state:
            raise YouTubeError("OAuth failed: state mismatch")

        if "error" in qs:
            raise YouTubeError(f"OAuth failed: {qs['error'][0]}")

        codes = qs.get("code")
        if not codes:
            raise YouTubeError("OAuth failed: no code received")

        auth_code = codes[0]
    else:
        auth_code: str | None = None
        auth_error: str | None = None

        class OAuthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                nonlocal auth_code, auth_error
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)

                req_state = qs.get("state", [None])[0]
                if req_state != state:
                    auth_error = "state mismatch"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"State mismatch")
                    return

                if "error" in qs:
                    auth_error = qs["error"][0]
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"Authentication error")
                    return

                codes = qs.get("code")
                if not codes:
                    auth_error = "no code received"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"Missing code")
                    return

                auth_code = codes[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>k2y: you can close this tab</h1></body></html>")

            def log_message(self, format: str, *args: object) -> None:
                # Silence server logging to prevent auth code leakage
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), OAuthHandler)
        port = server.server_port
        redirect_uri = f"http://127.0.0.1:{port}"

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{auth_uri}?{urllib.parse.urlencode(params)}"
        print(f"Open this URL to authorize k2y:\n{auth_url}")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        try:
            server.handle_request()
        finally:
            server.server_close()

        if auth_error:
            raise YouTubeError(f"OAuth failed: {auth_error}")
        if not auth_code:
            raise YouTubeError("OAuth failed: no code received")

    _exchange_and_save_token(
        client_id=client_id,
        client_secret=client_secret,
        token_uri=token_uri,
        code=auth_code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )


def access_token(force_refresh: bool = False) -> str:
    raw = config.read_secret("youtube_token.json")
    try:
        token_data = json.loads(raw)
    except Exception as e:
        raise YouTubeError(f"Failed to parse youtube_token.json: {e}")

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise YouTubeError("Missing refresh_token in youtube_token.json")

    current_token = token_data.get("access_token")
    expires_at = token_data.get("expires_at", 0)

    if not force_refresh and current_token and (expires_at - time.time() > 60):
        return current_token

    client_id, client_secret, _, token_uri = _load_client_secret()
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        token_uri,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _parse_youtube_error(e)
    except Exception as e:
        raise YouTubeError(f"Token refresh failed: {e}")

    new_token = resp_data.get("access_token")
    if not new_token:
        raise YouTubeError("Token refresh response missing access_token")

    token_data["access_token"] = new_token
    token_data["expires_at"] = int(time.time()) + int(resp_data.get("expires_in", 3600))
    if "refresh_token" in resp_data:
        token_data["refresh_token"] = resp_data["refresh_token"]

    config.write_secret("youtube_token.json", json.dumps(token_data, indent=2))
    return new_token


def _init_upload_session(total_size: int, meta: dict) -> str:
    snippet: dict[str, object] = {
        "title": str(meta.get("title", ""))[:100],
        "description": str(meta.get("description", ""))[:5000],
        "categoryId": str(meta.get("category_id", meta.get("categoryId", "22"))),
        "defaultLanguage": "ar",
        "defaultAudioLanguage": "ar",
    }
    if "tags" in meta and meta["tags"] is not None:
        snippet["tags"] = list(meta["tags"])

    body = {
        "snippet": snippet,
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    body_bytes = json.dumps(body).encode("utf-8")
    url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"

    def _post(tok: str) -> str:
        headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(total_size),
            "X-Upload-Content-Type": "video/*",
        }
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            loc = resp.headers.get("Location")
            if not loc:
                raise YouTubeError("Upload init missing Location header")
            return loc

    tok = access_token()
    try:
        return _post(tok)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            tok = access_token(force_refresh=True)
            try:
                return _post(tok)
            except urllib.error.HTTPError as e2:
                raise _parse_youtube_error(e2)
        raise _parse_youtube_error(e)


def _parse_range_next_byte(headers: object) -> int:
    range_header = getattr(headers, "get", lambda k: None)("Range")
    if range_header and "-" in range_header:
        try:
            return int(range_header.split("-")[-1].strip()) + 1
        except ValueError:
            return 0
    return 0


def _query_status_once(session_url: str, total_size: int) -> tuple[int, str | None]:
    headers = {
        "Content-Range": f"bytes */{total_size}",
        "Content-Length": "0",
    }
    req = urllib.request.Request(session_url, data=b"", headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            code = getattr(resp, "status", getattr(resp, "code", 200))
            if code in (200, 201):
                body = resp.read().decode("utf-8")
                return total_size, json.loads(body)["id"]
            if code == 308:
                return _parse_range_next_byte(resp.headers), None
            raise YouTubeError(f"Unexpected status response: HTTP {code}")
    except urllib.error.HTTPError as e:
        if e.code == 308:
            return _parse_range_next_byte(e.headers), None
        if e.code in (200, 201):
            body = e.read().decode("utf-8")
            return total_size, json.loads(body)["id"]
        if e.code in (404, 410):
            raise _SessionExpired()
        if e.code == 403:
            raise _parse_youtube_error(e)
        if 500 <= e.code < 600:
            raise
        raise _parse_youtube_error(e)


def _upload_chunk_once(
    session_url: str, chunk_data: bytes, offset: int, total_size: int
) -> tuple[int, str | None]:
    end_byte = offset + len(chunk_data) - 1 if total_size > 0 else 0
    headers = {
        "Content-Range": (
            f"bytes {offset}-{end_byte}/{total_size}" if total_size > 0 else "bytes */0"
        ),
        "Content-Type": "video/*",
    }
    req = urllib.request.Request(session_url, data=chunk_data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            code = getattr(resp, "status", getattr(resp, "code", 200))
            if code in (200, 201):
                body = resp.read().decode("utf-8")
                return total_size, json.loads(body)["id"]
            if code == 308:
                return _parse_range_next_byte(resp.headers), None
            raise YouTubeError(f"Unexpected upload response: HTTP {code}")
    except urllib.error.HTTPError as e:
        if e.code == 308:
            return _parse_range_next_byte(e.headers), None
        if e.code in (200, 201):
            body = e.read().decode("utf-8")
            return total_size, json.loads(body)["id"]
        if e.code in (404, 410):
            raise _SessionExpired()
        if e.code == 403:
            raise _parse_youtube_error(e)
        if 500 <= e.code < 600:
            raise
        raise _parse_youtube_error(e)


def upload(
    path: Path,
    meta: dict,
    session_url: str | None,
    on_session: Callable[[str], None],
) -> str:
    path = Path(path)
    total_size = path.stat().st_size

    if session_url is None:
        session_url = _init_upload_session(total_size, meta)
        on_session(session_url)
        offset = 0
    else:
        attempts = 0
        while True:
            try:
                offset, video_id = _query_status_once(session_url, total_size)
                if video_id is not None:
                    return video_id
                break
            except _SessionExpired:
                session_url = _init_upload_session(total_size, meta)
                on_session(session_url)
                offset = 0
                break
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                ConnectionError,
                OSError,
                http.client.HTTPException,
            ) as e:
                attempts += 1
                if attempts > 5:
                    raise YouTubeError(f"Query status failed after 5 retries: {e}")
                _sleep(min(60, 5 * (2 ** (attempts - 1))))

    with open(path, "rb") as f:
        attempts = 0
        while offset < total_size or total_size == 0:
            chunk_len = min(CHUNK_SIZE, total_size - offset) if total_size > 0 else 0
            f.seek(offset)
            chunk_data = f.read(chunk_len)

            try:
                next_byte, video_id = _upload_chunk_once(session_url, chunk_data, offset, total_size)
                attempts = 0
                if video_id is not None:
                    return video_id
                offset = next_byte
                if total_size == 0:
                    break
            except _SessionExpired:
                session_url = _init_upload_session(total_size, meta)
                on_session(session_url)
                offset = 0
                attempts = 0
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                ConnectionError,
                OSError,
                http.client.HTTPException,
            ) as e:
                attempts += 1
                if attempts > 5:
                    raise YouTubeError(f"Upload failed after 5 retries: {e}")
                _sleep(min(60, 5 * (2 ** (attempts - 1))))

                try:
                    next_byte, video_id = _query_status_once(session_url, total_size)
                    if video_id is not None:
                        return video_id
                    offset = next_byte
                except _SessionExpired:
                    session_url = _init_upload_session(total_size, meta)
                    on_session(session_url)
                    offset = 0
                    attempts = 0
                except (
                    urllib.error.HTTPError,
                    urllib.error.URLError,
                    ConnectionError,
                    OSError,
                    http.client.HTTPException,
                ):
                    pass

    _, final_id = _query_status_once(session_url, total_size)
    if final_id is not None:
        return final_id
    raise YouTubeError("Upload completed but no video ID returned")


def set_thumbnail(video_id: str, jpeg: Path) -> None:
    jpeg_path = Path(jpeg)
    data = jpeg_path.read_bytes()
    url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={urllib.parse.quote(str(video_id))}"

    def _handle_err(e: urllib.error.HTTPError) -> None:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            err = parsed.get("error", {})
            errors = err.get("errors", [])
            reasons = [
                item.get("reason")
                for item in errors
                if isinstance(item, dict) and "reason" in item
            ]
            if e.code == 403 and "forbidden" in reasons:
                raise YouTubeError(
                    "custom thumbnails need a phone-verified channel: https://www.youtube.com/verify"
                )
        except YouTubeError:
            raise
        except Exception:
            pass
        raise _parse_youtube_error(e, raw_body=raw)

    def _post(tok: str) -> None:
        headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(data)),
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            pass

    tok = access_token()
    try:
        _post(tok)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            tok = access_token(force_refresh=True)
            try:
                _post(tok)
                return
            except urllib.error.HTTPError as e2:
                _handle_err(e2)
        _handle_err(e)

import socket
import tempfile
import unittest
from pathlib import Path

from media_policy import (
    DownloadLimitExceeded,
    MediaPolicy,
    UnsafeUrlError,
    detect_text_source,
    download_public_url,
    sanitize_upload_name,
    validate_public_url,
)


def public_resolver(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def private_resolver(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=8192):
        return iter(self._chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class MediaPolicyTest(unittest.TestCase):
    def test_detects_supported_text_sources(self):
        self.assertEqual(
            detect_text_source("https://www.youtube.com/watch?v=abc"), "youtube"
        )
        self.assertEqual(detect_text_source("https://youtu.be/abc"), "youtube")
        self.assertEqual(
            detect_text_source("复制打开抖音 https://v.douyin.com/abc/ 看视频"),
            "douyin",
        )
        self.assertEqual(
            detect_text_source("https://www.iesdouyin.com/share/video/123"),
            "douyin",
        )
        self.assertEqual(
            detect_text_source("https://sub.iesdouyin.com/share/video/456"),
            "douyin",
        )
        self.assertEqual(
            detect_text_source("https://cdn.example.com/video.mp4"), "video_url"
        )
        self.assertIsNone(detect_text_source("这只是一段普通文本"))

    def test_rejects_non_http_credentials_and_private_hosts(self):
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("file:///etc/passwd", resolver=public_resolver)
        with self.assertRaises(UnsafeUrlError):
            validate_public_url(
                "https://user:password@example.com/a.mp3", resolver=public_resolver
            )
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://localhost/a.mp3", resolver=private_resolver)
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("http://127.0.0.1/a.mp3", resolver=public_resolver)

    def test_accepts_public_http_url(self):
        self.assertEqual(
            validate_public_url("https://example.com/a.mp3", resolver=public_resolver),
            "https://example.com/a.mp3",
        )

    def test_sanitize_upload_name_removes_path_traversal(self):
        name = sanitize_upload_name("../../secret file.mp3")
        self.assertEqual(name, "secret_file.mp3")
        self.assertNotIn("/", name)

    def test_download_validates_redirect_and_stream_size(self):
        policy = MediaPolicy(max_media_bytes=5, max_redirects=2)
        session = FakeSession(
            [
                FakeResponse(status=302, headers={"location": "/real.mp3"}),
                FakeResponse(headers={"content-length": "4"}, chunks=[b"ab", b"cd"]),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            destination = Path(tmp_dir) / "audio.mp3"
            total = download_public_url(
                "https://example.com/start",
                destination,
                policy=policy,
                session=session,
                resolver=public_resolver,
            )
            self.assertEqual(total, 4)
            self.assertEqual(destination.read_bytes(), b"abcd")
            self.assertEqual(len(session.calls), 2)
            self.assertFalse(list(destination.parent.glob("*.part")))

    def test_download_rejects_lying_or_missing_content_length(self):
        policy = MediaPolicy(max_media_bytes=3)
        session = FakeSession(
            [FakeResponse(headers={"content-length": "2"}, chunks=[b"ab", b"cd"])]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            destination = Path(tmp_dir) / "audio.mp3"
            with self.assertRaises(DownloadLimitExceeded):
                download_public_url(
                    "https://example.com/audio.mp3",
                    destination,
                    policy=policy,
                    session=session,
                    resolver=public_resolver,
                )
            self.assertFalse(destination.exists())
            self.assertFalse(list(destination.parent.glob("*.part")))

    def test_download_rejects_private_redirect(self):
        policy = MediaPolicy(max_redirects=2)
        session = FakeSession(
            [
                FakeResponse(
                    status=302,
                    headers={"location": "http://127.0.0.1/internal"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(UnsafeUrlError):
                download_public_url(
                    "https://example.com/start",
                    Path(tmp_dir) / "x",
                    policy=policy,
                    session=session,
                    resolver=public_resolver,
                )


if __name__ == "__main__":
    unittest.main()

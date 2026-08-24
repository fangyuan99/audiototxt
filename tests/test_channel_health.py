import unittest
from types import SimpleNamespace

from channel_health import (
    EmptyChannelResponse,
    check_current_channel,
    diagnose_exception,
    sanitize_error_detail,
)
from key_pool import GeminiKeyPool
from service_config import GlobalSettings


class FakeClient:
    def __init__(self, *, text="hello", error=None):
        self.response_text = text
        self.error = error
        self.calls = []
        self.closed = False
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.response_text)

    def close(self):
        self.closed = True


class StatusError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class ChannelHealthTest(unittest.TestCase):
    def test_vertex_probe_uses_current_model_hi_and_global(self):
        client = FakeClient(text="Hi there")
        captured = {}

        def factory(config, timeout_seconds=None):
            captured["config"] = config
            captured["timeout"] = timeout_seconds
            return client

        settings = GlobalSettings(
            auth_mode="vertex_ai_json",
            vertex_json='{"project_id":"demo"}',
            vertex_project="demo",
            vertex_location="",
            model_name="gemini-current",
        )
        result = check_current_channel(
            settings,
            GeminiKeyPool([]),
            client_factory=factory,
            clock=lambda: 10.0,
        )

        self.assertTrue(result.available)
        self.assertEqual(result.model, "gemini-current")
        self.assertEqual(result.location, "global")
        self.assertEqual(captured["config"].vertex_location, "global")
        self.assertEqual(client.calls[0]["model"], "gemini-current")
        self.assertEqual(client.calls[0]["contents"], "hi")
        self.assertTrue(client.closed)

    def test_gemini_probe_uses_key_pool_and_closes_client(self):
        clients = []

        def factory(config, timeout_seconds=None):
            client = FakeClient(text="ok")
            clients.append((config.api_key, client))
            return client

        settings = GlobalSettings(
            auth_mode="gemini_api_key",
            gemini_api_keys=["key-a", "key-b"],
        )
        pool = GeminiKeyPool([])
        first = check_current_channel(settings, pool, client_factory=factory)
        second = check_current_channel(settings, pool, client_factory=factory)

        self.assertTrue(first.available)
        self.assertTrue(second.available)
        self.assertEqual([item[0] for item in clients], ["key-a", "key-b"])
        self.assertTrue(all(item[1].closed for item in clients))

    def test_probe_reports_empty_response_without_exposing_content(self):
        client = FakeClient(text="   ")
        settings = GlobalSettings(
            auth_mode="vertex_ai_json",
            vertex_json='{"project_id":"demo"}',
            vertex_project="demo",
        )
        result = check_current_channel(
            settings,
            GeminiKeyPool([]),
            client_factory=lambda config, timeout_seconds=None: client,
        )

        self.assertFalse(result.available)
        self.assertEqual(result.code, "empty_response")
        self.assertEqual(result.error_type, "EmptyChannelResponse")
        self.assertTrue(client.closed)

    def test_error_categories_are_actionable(self):
        cases = [
            (StatusError("BILLING_DISABLED", 403), "billing_disabled", "未启用结算"),
            (StatusError("permission denied", 403), "permission_denied", "IAM"),
            (StatusError("model not found", 404), "model_unavailable", "模型"),
            (StatusError("resource exhausted", 429), "quota_exhausted", "额度"),
            (TimeoutError("timed out"), "timeout", "超时"),
            (ConnectionError("connection reset"), "network", "网络"),
            (RuntimeError("ffmpeg: Invalid data found when processing input"), "media_decode", "音视频"),
            (EmptyChannelResponse("empty"), "empty_response", "空响应"),
        ]
        for error, code, phrase in cases:
            with self.subTest(code=code):
                diagnosis = diagnose_exception(error)
                self.assertEqual(diagnosis.code, code)
                self.assertIn(phrase, diagnosis.user_message)

    def test_sanitizer_removes_credentials_paths_and_signed_urls(self):
        raw = (
            "request https://dl.snapcdn.app/get?token=secret-token "
            "key=AIzaSyABCDEFGHIJKLMNOPQRSTUV "
            "bot=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef "
            "account=svc-name@example.iam.gserviceaccount.com "
            "path=/home/audiototxt/data/private.mp3 "
            "jwt=eyJabcdefghijklmnopqrstuv.eyJabcdefghijklmnopqrstuv.abcdefghijklmnopqrstuv "
            "-----BEGIN PRIVATE KEY----- hidden -----END PRIVATE KEY-----"
        )

        sanitized = sanitize_error_detail(raw)

        self.assertIn("<url host=dl.snapcdn.app>", sanitized)
        for secret in (
            "secret-token",
            "AIzaSy",
            "123456789:",
            "svc-name@",
            "/home/audiototxt",
            "eyJabcdefghijkl",
            "hidden",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertLessEqual(len(sanitized), 600)


if __name__ == "__main__":
    unittest.main()

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import main
from channel_health import diagnose_exception


class FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakePart:
    @staticmethod
    def from_bytes(data=None, mime_type=None):
        return {"inline": len(data), "mime_type": mime_type}


class FakeFiles:
    def __init__(self, states):
        self.states = list(states)
        self.uploads = []
        self.deleted = []

    def _file(self):
        return SimpleNamespace(
            name="files/abc", state=SimpleNamespace(name=self.states.pop(0))
        )

    def upload(self, file=None, config=None):
        self.uploads.append((file, config))
        return self._file()

    def get(self, name=None):
        return self._file()

    def delete(self, name=None):
        self.deleted.append(name)


class FakeModels:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("stream failed")
        return [SimpleNamespace(text="hello")]


class FakeClient:
    def __init__(self, states=("ACTIVE",), fail=False):
        self.files = FakeFiles(states)
        self.models = FakeModels(fail)

    def close(self):
        pass


def fake_genai():
    fake_google = ModuleType("google")
    fake_genai_module = ModuleType("google.genai")
    fake_genai_module.types = SimpleNamespace(
        GenerateContentConfig=FakeConfig, Part=FakePart
    )
    fake_google.genai = fake_genai_module
    return patch.dict(
        sys.modules, {"google": fake_google, "google.genai": fake_genai_module}
    )


class LargeAudioTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp_dir.name) / "talk.m4a"
        self.audio.write_bytes(b"x" * 64)

    def tearDown(self):
        self.temp_dir.cleanup()

    def transcribe(self, client, **kwargs):
        with fake_genai(), patch("main.build_genai_client", return_value=client):
            return main.transcribe_audio_streaming(
                api_key="key", audio_path=str(self.audio), on_chunk=lambda _t: None,
                **kwargs,
            )

    def test_small_audio_stays_inline(self):
        client = FakeClient()

        self.assertEqual(self.transcribe(client), "hello")

        self.assertEqual(client.models.calls[0]["contents"][0]["inline"], 64)
        self.assertEqual(client.files.uploads, [])

    def test_large_audio_uses_files_api_and_deletes_upload(self):
        client = FakeClient(states=("PROCESSING", "ACTIVE"))

        with patch.object(main, "INLINE_AUDIO_MAX_BYTES", 10), patch(
            "main.time.sleep"
        ):
            self.assertEqual(self.transcribe(client), "hello")

        self.assertEqual(client.files.uploads[0][1], {"mime_type": "audio/mp4"})
        self.assertEqual(client.models.calls[0]["contents"][0].name, "files/abc")
        self.assertEqual(client.files.deleted, ["files/abc"])

    def test_uploaded_file_is_deleted_when_stream_fails(self):
        client = FakeClient(fail=True)

        with patch.object(main, "INLINE_AUDIO_MAX_BYTES", 10):
            with self.assertRaises(RuntimeError):
                self.transcribe(client)

        self.assertEqual(client.files.deleted, ["files/abc"])

    def test_failed_processing_is_reported_and_cleaned_up(self):
        client = FakeClient(states=("FAILED",))

        with patch.object(main, "INLINE_AUDIO_MAX_BYTES", 10):
            with self.assertRaisesRegex(RuntimeError, "无法处理"):
                self.transcribe(client)

        self.assertEqual(client.models.calls, [])
        self.assertEqual(client.files.deleted, ["files/abc"])

    def test_cancel_while_processing_stops_waiting(self):
        client = FakeClient(states=("PROCESSING", "PROCESSING"))

        class Cancelled(Exception):
            pass

        def checkpoint(_text):
            raise Cancelled()

        with fake_genai(), patch("main.build_genai_client", return_value=client), \
                patch.object(main, "INLINE_AUDIO_MAX_BYTES", 10):
            with self.assertRaises(Cancelled):
                main.transcribe_audio_streaming(
                    api_key="key", audio_path=str(self.audio), on_chunk=checkpoint
                )

        self.assertEqual(client.files.deleted, ["files/abc"])

    def test_vertex_rejects_oversized_audio_with_clear_reason(self):
        with patch.object(main, "INLINE_AUDIO_MAX_BYTES", 10), patch(
            "main.build_auth_config",
            return_value=SimpleNamespace(auth_mode=main.AUTH_MODE_VERTEX_AI_JSON),
        ), patch("main.build_genai_client") as build_client:
            with self.assertRaises(main.InlineAudioTooLarge) as raised:
                main.transcribe_audio_streaming(
                    api_key=None, audio_path=str(self.audio)
                )

        build_client.assert_not_called()
        diagnosis = diagnose_exception(raised.exception)
        self.assertEqual(diagnosis.code, "media_too_large")
        self.assertIn("Gemini API Key", diagnosis.user_message)


if __name__ == "__main__":
    unittest.main()

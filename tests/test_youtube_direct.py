import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import main


YOUTUBE_URL = "https://www.youtube.com/watch?v=3KtWfp0UopM"


class FakeConfig:
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.kwargs = kwargs


class FakeMediaResolution:
    MEDIA_RESOLUTION_LOW = "MEDIA_RESOLUTION_LOW"
    MEDIA_RESOLUTION_HIGH = "MEDIA_RESOLUTION_HIGH"


class FakePart:
    uri_calls = []

    @staticmethod
    def from_uri(file_uri=None, uri=None, mime_type=None):
        resolved_uri = file_uri or uri
        FakePart.uri_calls.append((resolved_uri, mime_type))
        return {"uri": resolved_uri, "mime_type": mime_type}


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        return [
            SimpleNamespace(text="hello"),
            SimpleNamespace(text="hello world"),
        ]


class FakeClient:
    def __init__(self):
        self.models = FakeModels()
        self.closed = False

    def close(self):
        self.closed = True


def install_fake_genai_modules():
    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_types = SimpleNamespace(
        GenerateContentConfig=FakeConfig,
        MediaResolution=FakeMediaResolution,
        Part=FakePart,
    )
    fake_genai.types = fake_types
    fake_google.genai = fake_genai
    return patch.dict(sys.modules, {"google": fake_google, "google.genai": fake_genai})


class YouTubeDirectTest(unittest.TestCase):
    def setUp(self):
        FakeConfig.last_kwargs = None
        FakePart.uri_calls = []

    def test_transcribe_youtube_uses_uri_part_without_download(self):
        client = FakeClient()
        chunks = []

        with install_fake_genai_modules(), patch(
            "main.build_genai_client", return_value=client
        ), patch("main.download_audio_from_youtube") as download_audio:
            transcript = main.transcribe_youtube_url_streaming(
                api_key="test-key",
                youtube_url=YOUTUBE_URL,
                on_chunk=chunks.append,
            )

        self.assertEqual(transcript, "hello world")
        self.assertEqual(chunks, ["hello", " world"])
        self.assertEqual(FakePart.uri_calls, [(YOUTUBE_URL, "video/mp4")])
        self.assertFalse(download_audio.called)
        self.assertEqual(
            FakeConfig.last_kwargs["media_resolution"],
            "MEDIA_RESOLUTION_LOW",
        )
        self.assertEqual(client.models.calls[0]["contents"][1]["uri"], YOUTUBE_URL)
        self.assertTrue(client.closed)

    def test_cli_youtube_path_calls_direct_transcription(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_cwd = os.getcwd()
            os.chdir(tmp_dir)
            try:
                with patch.object(
                    sys,
                    "argv",
                    [
                        "main.py",
                        "--youtube",
                        YOUTUBE_URL,
                        "--api-key",
                        "test-key",
                        "--cleanup-hours",
                        "0",
                    ],
                ), patch("main.ensure_package"), patch(
                    "main.transcribe_youtube_url_streaming", return_value="direct transcript"
                ) as transcribe_youtube, patch(
                    "main.download_audio_from_youtube"
                ) as download_audio:
                    main.main()

                self.assertFalse(download_audio.called)
                transcribe_youtube.assert_called_once()
                self.assertEqual(
                    transcribe_youtube.call_args.kwargs["youtube_url"],
                    YOUTUBE_URL,
                )
                self.assertEqual(
                    transcribe_youtube.call_args.kwargs["media_resolution"],
                    "low",
                )
                out_path = Path(tmp_dir) / "data" / "youtube_3KtWfp0UopM.txt"
                self.assertEqual(out_path.read_text(encoding="utf-8"), "direct transcript")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

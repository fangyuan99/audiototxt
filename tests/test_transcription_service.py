import tempfile
import unittest
from pathlib import Path
from key_pool import GeminiKeyPool
from service_config import GlobalConfigStore
from transcription_service import (
    EmptyTranscriptionError,
    TaskCancelled,
    TaskDeadline,
    TranscriptionRequest,
    TranscriptionService,
)


class FakeFunctions:
    def __init__(self, root: Path):
        self.root = root
        self.calls = []

    def transcribe_audio(self, **kwargs):
        self.calls.append(("audio", kwargs))
        return f"audio via {kwargs.get('api_key') or 'vertex'}"

    def transcribe_youtube(self, **kwargs):
        self.calls.append(("youtube", kwargs))
        return f"youtube via {kwargs.get('api_key') or 'vertex'}"

    def download_video(self, url, output_dir, **kwargs):
        self.calls.append(("download_video", {"url": url, **kwargs}))
        if kwargs.get("on_status"):
            kwargs["on_status"]("extracting")
        path = self.root / "downloaded.m4a"
        path.write_bytes(b"audio")
        return str(path)

    def fetch_douyin(self, text, **kwargs):
        self.calls.append(("fetch_douyin", {"text": text, **kwargs}))
        return "https://cdn.example.com/audio.mp3", "title", "123"

    def download_audio(self, url, output_dir, **kwargs):
        self.calls.append(("download_audio", {"url": url, **kwargs}))
        path = self.root / "douyin.mp3"
        path.write_bytes(b"audio")
        return str(path)


class TranscriptionServiceTest(unittest.TestCase):
    def make_service(self, root, **settings):
        store = GlobalConfigStore(root / "global.json", environ={})
        base = {
            "gemini_api_keys": ["key-a", "key-b"],
            "model_name": "model-test",
        }
        base.update(settings)
        store.update(**base)
        functions = FakeFunctions(root)
        service = TranscriptionService(
            store,
            GeminiKeyPool([]),
            work_dir=root,
            functions=functions,
            transient_retry_delay_seconds=0,
        )
        return service, functions

    def test_audio_uses_global_key_and_reports_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            service, functions = self.make_service(root)
            statuses = []

            result = service.execute(
                TranscriptionRequest(
                    source_type="audio",
                    audio_path=source,
                    original_filename="voice.mp3",
                    cleanup_input=True,
                ),
                on_status=statuses.append,
            )

            self.assertEqual(result.transcript, "audio via key-a")
            self.assertEqual(result.filename_stem, "voice")
            self.assertIn(source, result.cleanup_paths)
            self.assertIn("transcribing", statuses)

    def test_youtube_round_robins_between_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, _ = self.make_service(root)
            request = TranscriptionRequest(
                source_type="youtube",
                text_input="https://www.youtube.com/watch?v=abc",
            )
            first = service.execute(request)
            second = service.execute(request)
            self.assertEqual(first.transcript, "youtube via key-a")
            self.assertEqual(second.transcript, "youtube via key-b")

    def test_video_download_is_reused_for_key_failover(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, functions = self.make_service(root)
            attempts = []

            def transcribe_audio(**kwargs):
                attempts.append(kwargs["api_key"])
                if kwargs["api_key"] == "key-a":
                    error = RuntimeError("quota exhausted")
                    error.status_code = 429
                    raise error
                return "success"

            functions.transcribe_audio = transcribe_audio
            result = service.execute(
                TranscriptionRequest(
                    source_type="video_url",
                    text_input="https://cdn.example.com/video.mp4",
                )
            )

            self.assertEqual(result.transcript, "success")
            self.assertEqual(attempts, ["key-a", "key-b"])
            self.assertEqual(
                len([call for call in functions.calls if call[0] == "download_video"]),
                1,
            )

    def test_transient_download_failure_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, functions = self.make_service(root)
            attempts = {"count": 0}
            statuses = []

            def download_video(url, output_dir, **kwargs):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise ConnectionError("temporary connection reset")
                path = root / "retried.m4a"
                path.write_bytes(b"audio")
                return str(path)

            functions.download_video = download_video
            result = service.execute(
                TranscriptionRequest(
                    source_type="video_url",
                    text_input="https://cdn.example.com/video.mp4",
                ),
                on_status=statuses.append,
            )

            self.assertEqual(result.transcript, "audio via key-a")
            self.assertEqual(attempts["count"], 2)
            self.assertIn("retrying", statuses)

    def test_video_reports_downloading_extracting_then_transcribing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, _ = self.make_service(root)
            statuses = []

            service.execute(
                TranscriptionRequest(
                    source_type="video_url",
                    text_input="https://cdn.example.com/video.mp4",
                ),
                on_status=statuses.append,
            )

            self.assertEqual(
                statuses,
                ["downloading", "extracting", "transcribing"],
            )

    def test_empty_provider_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            service, functions = self.make_service(root)
            functions.transcribe_audio = lambda **kwargs: "   "

            with self.assertRaises(EmptyTranscriptionError):
                service.execute(
                    TranscriptionRequest(
                        source_type="audio",
                        audio_path=source,
                    )
                )

    def test_vertex_does_not_use_key_pool(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, functions = self.make_service(
                root,
                auth_mode="vertex_ai_json",
                vertex_json='{"project_id":"demo"}',
                vertex_project="demo",
            )
            result = service.execute(
                TranscriptionRequest(
                    source_type="youtube",
                    text_input="https://youtu.be/abc",
                )
            )
            self.assertEqual(result.transcript, "youtube via vertex")
            self.assertEqual(functions.calls[-1][1]["auth_mode"], "vertex_ai_json")

    def test_cancellation_and_deadline_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, _ = self.make_service(root)
            with self.assertRaises(TaskCancelled):
                service.execute(
                    TranscriptionRequest(
                        source_type="youtube",
                        text_input="https://youtu.be/abc",
                    ),
                    cancelled=lambda: True,
                )

            deadline = TaskDeadline(timeout_seconds=0, clock=lambda: 10.0)
            with self.assertRaises(TimeoutError):
                service.execute(
                    TranscriptionRequest(
                        source_type="youtube",
                        text_input="https://youtu.be/abc",
                    ),
                    deadline=deadline,
                )

    def test_cancelled_provider_call_removes_owned_audio(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            service, functions = self.make_service(root)
            cancelled = {"value": False}

            def transcribe_audio(**kwargs):
                cancelled["value"] = True
                return "late result"

            functions.transcribe_audio = transcribe_audio
            with self.assertRaises(TaskCancelled):
                service.execute(
                    TranscriptionRequest(
                        source_type="audio",
                        audio_path=source,
                        cleanup_input=True,
                    ),
                    cancelled=lambda: cancelled["value"],
                )

            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()

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

    def test_settings_snapshot_overrides_current_output_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, functions = self.make_service(root, language_hint="en")
            service.execute(
                TranscriptionRequest(
                    source_type="youtube",
                    text_input="https://www.youtube.com/watch?v=abc",
                    settings_snapshot={
                        "model_name": "frozen-model",
                        "language_hint": "",
                        "prompt_append": "keep names",
                        "prompt_override": "",
                    },
                )
            )
            kwargs = functions.calls[-1][1]
            self.assertEqual(kwargs["model_name"], "frozen-model")
            self.assertIsNone(kwargs["language_hint"])
            self.assertEqual(kwargs["promoters"], "keep names")
            # Credentials still come from the live settings.
            self.assertEqual(kwargs["api_key"], "key-a")

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

    def test_progress_counts_restart_after_key_failover(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, functions = self.make_service(root)
            progress = []

            def transcribe_youtube(**kwargs):
                kwargs["on_chunk"]("abc")
                if kwargs["api_key"] == "key-a":
                    error = RuntimeError("quota exhausted")
                    error.status_code = 429
                    raise error
                kwargs["on_chunk"]("de")
                return "abcde"

            functions.transcribe_youtube = transcribe_youtube
            service.execute(
                TranscriptionRequest(
                    source_type="youtube",
                    text_input="https://www.youtube.com/watch?v=abc",
                ),
                on_progress=lambda count, tail: progress.append((count, tail)),
            )

            self.assertEqual(progress, [(3, "abc"), (3, "abc"), (5, "abcde")])

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

    def test_cancel_mid_stream_aborts_without_rotating_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            service, functions = self.make_service(root)
            cancelled = {"value": False}
            keys = []

            def transcribe_audio(**kwargs):
                keys.append(kwargs["api_key"])
                kwargs["on_chunk"]("first words")
                cancelled["value"] = True
                kwargs["on_chunk"]("never collected")
                return "late result"

            functions.transcribe_audio = transcribe_audio
            with self.assertRaises(TaskCancelled):
                service.execute(
                    TranscriptionRequest(source_type="audio", audio_path=source),
                    cancelled=lambda: cancelled["value"],
                )

            self.assertEqual(keys, ["key-a"])
            self.assertTrue(
                all(status.failure_count == 0 for status in service.key_pool.statuses())
            )

    def test_deadline_mid_stream_aborts_youtube_without_rotating_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service, functions = self.make_service(root)
            clock = {"now": 0.0}
            deadline = TaskDeadline(10, clock=lambda: clock["now"])
            keys = []

            def transcribe_youtube(**kwargs):
                keys.append(kwargs["api_key"])
                clock["now"] = 11.0
                try:
                    kwargs["on_chunk"]("words")
                except Exception as exc:
                    raise RuntimeError("YouTube 直连转写失败") from exc
                return "late"

            functions.transcribe_youtube = transcribe_youtube
            with self.assertRaises(RuntimeError) as raised:
                service.execute(
                    TranscriptionRequest(
                        source_type="youtube",
                        text_input="https://www.youtube.com/watch?v=abc",
                    ),
                    deadline=deadline,
                )

            self.assertIsInstance(raised.exception.__cause__, TimeoutError)
            self.assertEqual(keys, ["key-a"])

    def test_finish_reason_is_reported_on_result(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            service, functions = self.make_service(root)

            def transcribe_audio(**kwargs):
                kwargs["on_chunk"]("partial")
                kwargs["on_finish"]("MAX_TOKENS")
                return "partial"

            functions.transcribe_audio = transcribe_audio
            result = service.execute(
                TranscriptionRequest(source_type="audio", audio_path=source)
            )

            self.assertEqual(result.finish_reason, "MAX_TOKENS")
            self.assertTrue(result.incomplete)

    def test_normal_stop_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            service, functions = self.make_service(root)

            def transcribe_audio(**kwargs):
                kwargs["on_finish"]("STOP")
                return "done"

            functions.transcribe_audio = transcribe_audio
            result = service.execute(
                TranscriptionRequest(source_type="audio", audio_path=source)
            )

            self.assertFalse(result.incomplete)


if __name__ == "__main__":
    unittest.main()

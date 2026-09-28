from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from key_pool import GeminiKeyPool
from media_policy import MediaPolicy, sanitize_upload_name
from service_config import GlobalConfigStore, GlobalSettings


class TaskCancelled(RuntimeError):
    pass


class UnsupportedSourceError(ValueError):
    pass


class EmptyTranscriptionError(RuntimeError):
    pass


class TaskDeadline:
    def __init__(
        self,
        timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._expires_at = clock() + max(0.0, float(timeout_seconds))

    def remaining(self) -> float:
        return max(0.0, self._expires_at - self._clock())

    def check(self) -> None:
        if self.remaining() <= 0:
            raise TimeoutError("任务处理超时。")


@dataclass(frozen=True)
class TranscriptionRequest:
    source_type: str
    text_input: Optional[str] = None
    audio_path: Optional[Path] = None
    original_filename: Optional[str] = None
    cleanup_input: bool = False
    # Output settings frozen at submit time; credentials always come live.
    settings_snapshot: Optional[Mapping[str, str]] = None


# Finish reasons that mean the provider ended the answer normally. Anything
# else (MAX_TOKENS, SAFETY, RECITATION, ...) may leave the transcript cut off.
COMPLETE_FINISH_REASONS = frozenset({"", "STOP", "FINISH_REASON_UNSPECIFIED"})


@dataclass(frozen=True)
class TranscriptionResult:
    transcript: str
    filename_stem: str
    cleanup_paths: tuple[Path, ...] = ()
    finish_reason: str = ""

    @property
    def incomplete(self) -> bool:
        return self.finish_reason not in COMPLETE_FINISH_REASONS


PROGRESS_TAIL_CHARS = 200


class _ProviderCall:
    """Per-request hooks shared by every key-pool attempt."""

    def __init__(
        self,
        cancelled: Optional[Callable[[], bool]],
        deadline: "TaskDeadline",
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self.cancelled = cancelled
        self.deadline = deadline
        self.on_progress = on_progress
        self.finish_reason = ""
        self.characters = 0
        self.tail = ""

    def begin_attempt(self) -> None:
        # A key failover restarts the stream, so progress starts over.
        self.characters = 0
        self.tail = ""

    def should_abort(self) -> bool:
        return bool(
            (self.cancelled is not None and self.cancelled())
            or self.deadline.remaining() <= 0
        )

    def on_chunk(self, delta: str) -> None:
        # Raising here aborts the provider stream mid-response.
        if self.cancelled is not None and self.cancelled():
            raise TaskCancelled("任务已取消。")
        self.deadline.check()
        self.characters += len(delta)
        self.tail = (self.tail + delta)[-PROGRESS_TAIL_CHARS:]
        if self.on_progress is not None:
            self.on_progress(self.characters, self.tail)

    def on_finish(self, reason: str) -> None:
        self.finish_reason = reason or ""


class _DefaultFunctions:
    @staticmethod
    def transcribe_audio(**kwargs):
        from main import transcribe_audio_streaming

        return transcribe_audio_streaming(**kwargs)

    @staticmethod
    def transcribe_youtube(**kwargs):
        from main import transcribe_youtube_url_streaming

        return transcribe_youtube_url_streaming(**kwargs)

    @staticmethod
    def download_video(url, output_dir, **kwargs):
        from main import download_video_and_extract_audio

        return download_video_and_extract_audio(url, output_dir, **kwargs)

    @staticmethod
    def fetch_douyin(text, **kwargs):
        from main import fetch_douyin_mp3_via_tiksave

        return fetch_douyin_mp3_via_tiksave(text, **kwargs)

    @staticmethod
    def download_audio(url, output_dir, **kwargs):
        from main import download_audio_from_direct_url

        return download_audio_from_direct_url(url, output_dir, **kwargs)


def _safe_stem(value: str, fallback: str) -> str:
    safe_name = sanitize_upload_name(value or fallback, default=fallback)
    return Path(safe_name).stem[:80] or fallback


def _youtube_stem(value: str) -> str:
    try:
        parsed = urlparse(value)
        video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        if not video_id and parsed.path:
            video_id = [item for item in parsed.path.split("/") if item][-1]
        if video_id:
            safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", video_id).strip("_")
            if safe_id:
                return f"youtube_{safe_id}"
    except (IndexError, ValueError):
        pass
    return f"youtube_{int(time.time())}"


class TranscriptionService:
    def __init__(
        self,
        config_store: GlobalConfigStore,
        key_pool: GeminiKeyPool,
        *,
        work_dir: str | Path,
        media_policy: Optional[MediaPolicy] = None,
        functions=None,
        transient_retry_delay_seconds: float = 1.0,
    ) -> None:
        self.config_store = config_store
        self.key_pool = key_pool
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.media_policy = media_policy or MediaPolicy.from_environ()
        self.functions = functions or _DefaultFunctions()
        self.transient_retry_delay_seconds = max(
            0.0, float(transient_retry_delay_seconds)
        )

    @staticmethod
    def _check(
        cancelled: Optional[Callable[[], bool]],
        deadline: TaskDeadline,
    ) -> None:
        if cancelled is not None and cancelled():
            raise TaskCancelled("任务已取消。")
        deadline.check()

    @staticmethod
    def _cleanup_cancelled_paths(paths: list[Path]) -> None:
        for path in dict.fromkeys(paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    @staticmethod
    def _require_transcript(value: object) -> str:
        transcript = str(value or "").strip()
        if not transcript:
            raise EmptyTranscriptionError("provider returned empty transcript")
        return transcript

    @staticmethod
    def _is_transient_error(exc: BaseException) -> bool:
        current: Optional[BaseException] = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            status = getattr(current, "status_code", None)
            if status is None:
                status = getattr(getattr(current, "response", None), "status_code", None)
            try:
                if int(status) in {408, 425, 429, 500, 502, 503, 504}:
                    return True
            except (TypeError, ValueError):
                pass
            if isinstance(current, (TimeoutError, ConnectionError)):
                return True
            message = str(current).lower()
            if any(
                token in message
                for token in (
                    "timed out",
                    "timeout",
                    "connection reset",
                    "connection aborted",
                    "temporarily unavailable",
                    "temporary failure",
                )
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _run_download_stage(
        self,
        operation: Callable[[], object],
        *,
        emit: Callable[[str], None],
        cancelled: Optional[Callable[[], bool]],
        deadline: TaskDeadline,
    ):
        try:
            return operation()
        except Exception as exc:
            if not self._is_transient_error(exc):
                raise
            self._check(cancelled, deadline)
            emit("retrying")
            delay = min(self.transient_retry_delay_seconds, deadline.remaining())
            if delay > 0:
                time.sleep(delay)
            self._check(cancelled, deadline)
            return operation()

    @staticmethod
    def _provider_kwargs(
        settings: GlobalSettings,
        deadline: TaskDeadline,
        call: _ProviderCall,
    ) -> dict:
        # Built once per provider attempt.
        call.begin_attempt()
        return {
            "model_name": settings.model_name,
            "language_hint": settings.language_hint or None,
            "promoters": settings.prompt_append or None,
            "full_prompt_override": settings.prompt_override or None,
            "auth_mode": settings.auth_mode,
            "vertex_json": settings.vertex_json,
            "vertex_project": settings.vertex_project,
            "vertex_location": settings.vertex_location,
            "request_timeout_seconds": max(1.0, deadline.remaining()),
            "on_chunk": call.on_chunk,
            "on_finish": call.on_finish,
        }

    def _call_provider(
        self,
        function: Callable[..., str],
        settings: GlobalSettings,
        deadline: TaskDeadline,
        call: _ProviderCall,
        **source,
    ) -> str:
        if settings.auth_mode == "vertex_ai_json":
            return function(
                api_key=None,
                **source,
                **self._provider_kwargs(settings, deadline, call),
            )

        self.key_pool.sync(settings.gemini_api_keys)
        return self.key_pool.run(
            lambda key: function(
                api_key=key,
                **source,
                **self._provider_kwargs(settings, deadline, call),
            ),
            should_abort=call.should_abort,
        )

    def _transcribe_audio(
        self,
        path: Path,
        settings: GlobalSettings,
        deadline: TaskDeadline,
        call: _ProviderCall,
    ) -> str:
        return self._call_provider(
            self.functions.transcribe_audio,
            settings,
            deadline,
            call,
            audio_path=str(path),
        )

    def _transcribe_youtube(
        self,
        value: str,
        settings: GlobalSettings,
        deadline: TaskDeadline,
        call: _ProviderCall,
    ) -> str:
        return self._call_provider(
            self.functions.transcribe_youtube,
            settings,
            deadline,
            call,
            youtube_url=value,
        )

    @staticmethod
    def _raise_if_cancelled(
        exc: BaseException,
        cancelled: Optional[Callable[[], bool]],
        cleanup_paths: list[Path],
    ) -> None:
        if cancelled is None or not cancelled():
            return
        TranscriptionService._cleanup_cancelled_paths(cleanup_paths)
        if isinstance(exc, TaskCancelled):
            raise exc
        raise TaskCancelled("任务已取消。") from exc

    def execute(
        self,
        request: TranscriptionRequest,
        *,
        on_status: Optional[Callable[[str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        deadline: Optional[TaskDeadline] = None,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> TranscriptionResult:
        """Run one request. ``on_progress(characters, tail)`` fires on every
        streamed chunk from the worker thread; callers must throttle."""
        active_deadline = deadline or TaskDeadline(
            self.media_policy.task_timeout_seconds
        )
        emit = on_status or (lambda status: None)
        self._check(cancelled, active_deadline)
        settings = self.config_store.get().with_output_snapshot(
            request.settings_snapshot
        )
        cleanup_paths: list[Path] = []
        source_type = request.source_type

        if source_type == "youtube":
            if not request.text_input:
                raise ValueError("缺少 YouTube 链接。")
            emit("transcribing")
            call = _ProviderCall(cancelled, active_deadline, on_progress)
            try:
                transcript = self._require_transcript(
                    self._transcribe_youtube(
                        request.text_input, settings, active_deadline, call
                    )
                )
                self._check(cancelled, active_deadline)
            except Exception as exc:
                self._raise_if_cancelled(exc, cancelled, [])
                raise
            return TranscriptionResult(
                transcript=transcript,
                filename_stem=_youtube_stem(request.text_input),
                finish_reason=call.finish_reason,
            )

        audio_path = request.audio_path
        filename_stem = "transcript"
        if source_type == "audio":
            if audio_path is None or not audio_path.is_file():
                raise ValueError("缺少音频文件。")
            filename_stem = _safe_stem(
                request.original_filename or audio_path.name, "audio"
            )
            if request.cleanup_input:
                cleanup_paths.append(audio_path)
        elif source_type == "video_url":
            if not request.text_input:
                raise ValueError("缺少视频直链。")
            emit("downloading")
            audio_path = Path(
                self._run_download_stage(
                    lambda: self.functions.download_video(
                        request.text_input,
                        str(self.work_dir),
                        media_policy=self.media_policy,
                        cancelled=cancelled,
                        deadline=active_deadline,
                        on_status=emit,
                    ),
                    emit=emit,
                    cancelled=cancelled,
                    deadline=active_deadline,
                )
            )
            cleanup_paths.append(audio_path)
            source_name = Path(urlparse(request.text_input).path).name
            filename_stem = _safe_stem(
                source_name or f"video_{int(time.time())}", "video"
            )
        elif source_type == "douyin":
            if not request.text_input:
                raise ValueError("缺少抖音分享内容。")
            emit("parsing")
            mp3_url, _title, item_id = self._run_download_stage(
                lambda: self.functions.fetch_douyin(
                    request.text_input,
                    timeout_seconds=max(1.0, active_deadline.remaining()),
                    cancelled=cancelled,
                    deadline=active_deadline,
                ),
                emit=emit,
                cancelled=cancelled,
                deadline=active_deadline,
            )
            self._check(cancelled, active_deadline)
            emit("downloading")
            filename_stem = (
                f"douyin_{item_id}"
                if item_id
                else f"douyin_{int(time.time())}"
            )
            audio_path = Path(
                self._run_download_stage(
                    lambda: self.functions.download_audio(
                        mp3_url,
                        str(self.work_dir),
                        preferred_ext="mp3",
                        filename_stem=filename_stem,
                        media_policy=self.media_policy,
                        cancelled=cancelled,
                        deadline=active_deadline,
                    ),
                    emit=emit,
                    cancelled=cancelled,
                    deadline=active_deadline,
                )
            )
            cleanup_paths.append(audio_path)
        else:
            raise UnsupportedSourceError(f"不支持的来源类型：{source_type}")

        self._check(cancelled, active_deadline)
        emit("transcribing")
        call = _ProviderCall(cancelled, active_deadline, on_progress)
        try:
            transcript = self._require_transcript(
                self._transcribe_audio(audio_path, settings, active_deadline, call)
            )
            self._check(cancelled, active_deadline)
        except Exception as exc:
            self._raise_if_cancelled(exc, cancelled, cleanup_paths)
            raise
        return TranscriptionResult(
            transcript=transcript,
            filename_stem=filename_stem,
            cleanup_paths=tuple(dict.fromkeys(cleanup_paths)),
            finish_reason=call.finish_reason,
        )

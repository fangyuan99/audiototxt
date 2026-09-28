from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar

from telegram.error import BadRequest, NetworkError, RetryAfter

from transcription_service import COMPLETE_FINISH_REASONS

T = TypeVar("T")

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class StoredResult:
    transcript: str
    filename_stem: str
    finish_reason: str = ""

    @property
    def incomplete(self) -> bool:
        return self.finish_reason not in COMPLETE_FINISH_REASONS


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


class ResultStore:
    """Transcripts persisted on disk so delivery can resume after failures.

    Files live under the outputs directory and therefore follow its
    retention cleanup.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _paths(self, job_id: str) -> tuple[Path, Path]:
        if not _JOB_ID_RE.fullmatch(job_id or ""):
            raise ValueError("invalid job id")
        return self.root / f"{job_id}.txt", self.root / f"{job_id}.json"

    def text_path(self, job_id: str) -> Path:
        return self._paths(job_id)[0]

    def save(
        self,
        job_id: str,
        transcript: str,
        *,
        filename_stem: str,
        finish_reason: str = "",
    ) -> None:
        text_path, meta_path = self._paths(job_id)
        _atomic_write(text_path, transcript)
        _atomic_write(
            meta_path,
            json.dumps(
                {"filename_stem": filename_stem, "finish_reason": finish_reason},
                ensure_ascii=False,
            ),
        )

    def load(self, job_id: str) -> Optional[StoredResult]:
        try:
            text_path, meta_path = self._paths(job_id)
            transcript = text_path.read_text(encoding="utf-8")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        return StoredResult(
            transcript=transcript,
            filename_stem=str(meta.get("filename_stem") or "transcript"),
            finish_reason=str(meta.get("finish_reason") or ""),
        )


class DeliveryFailed(RuntimeError):
    pass


class ChatSender:
    """Serializes sends per chat, spaces them out, and retries Telegram
    flood-control and network errors."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = 1.0,
        max_attempts: int = 5,
        max_retry_after_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.max_retry_after_seconds = max(1.0, float(max_retry_after_seconds))
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_sent: dict[int, float] = {}

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(chat_id), asyncio.Lock())

    async def _wait_for_slot(self, chat_id: int) -> None:
        last = self._last_sent.get(chat_id)
        if last is None:
            return
        remaining = self.min_interval_seconds - (self._clock() - last)
        if remaining > 0:
            await self._sleep(remaining)

    async def send(self, chat_id: int, operation: Callable[[], Awaitable[T]]) -> T:
        """Run one Telegram send. Callers hold ``chat_lock`` for ordering."""
        chat_id = int(chat_id)
        for attempt in range(1, self.max_attempts + 1):
            await self._wait_for_slot(chat_id)
            try:
                result = await operation()
            except RetryAfter as exc:
                delay = _retry_after_seconds(exc)
                if attempt >= self.max_attempts or delay > self.max_retry_after_seconds:
                    raise DeliveryFailed("Telegram 限流，发送暂停。") from exc
                await self._sleep(delay)
                continue
            except BadRequest:
                # BadRequest subclasses NetworkError but is never transient.
                raise
            except NetworkError as exc:
                # TimedOut is a NetworkError. A timed-out send may still have
                # been delivered, so a rare duplicate chunk is possible.
                if attempt >= self.max_attempts:
                    raise DeliveryFailed("Telegram 网络异常，发送失败。") from exc
                await self._sleep(min(2.0 ** (attempt - 1), 30.0))
                continue
            finally:
                self._last_sent[chat_id] = self._clock()
            return result
        raise DeliveryFailed("发送失败。")


def _retry_after_seconds(exc: RetryAfter) -> float:
    value = getattr(exc, "retry_after", 1)
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        value = total_seconds()
    try:
        return max(0.0, float(value)) + 0.5
    except (TypeError, ValueError):
        return 1.5

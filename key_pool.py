from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, TypeVar


T = TypeVar("T")


class KeyPoolExhausted(RuntimeError):
    pass


class DeterministicGeminiError(RuntimeError):
    pass


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mask_key(key: str) -> str:
    value = (key or "").strip()
    if not value:
        return "未设置"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


@dataclass(frozen=True)
class KeyLease:
    key: str
    fingerprint: str
    masked_key: str


@dataclass(frozen=True)
class KeyStatus:
    masked_key: str
    fingerprint: str
    state: str
    retry_after_seconds: int
    failure_count: int
    reason: str


@dataclass
class _KeyRecord:
    key: str
    fingerprint: str
    disabled: bool = False
    disabled_until: float = 0.0
    failure_count: int = 0
    reason: str = ""


class GeminiKeyPool:
    def __init__(
        self,
        keys: Iterable[str] = (),
        *,
        clock: Callable[[], float] = time.monotonic,
        model_cache_ttl: float = 600.0,
    ) -> None:
        self._clock = clock
        self._model_cache_ttl = model_cache_ttl
        self._lock = threading.RLock()
        self._records: list[_KeyRecord] = []
        self._cursor = 0
        self._model_cache_signature: tuple[str, ...] = ()
        self._model_cache_until = 0.0
        self._model_cache: list[str] = []
        self.sync(keys)

    @staticmethod
    def _normalize(keys: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for candidate in keys:
            key = str(candidate or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result

    def sync(self, keys: Iterable[str]) -> None:
        normalized = self._normalize(keys)
        with self._lock:
            existing = {record.fingerprint: record for record in self._records}
            records: list[_KeyRecord] = []
            for key in normalized:
                fingerprint = key_fingerprint(key)
                previous = existing.get(fingerprint)
                if previous is not None and previous.key == key:
                    records.append(previous)
                else:
                    records.append(_KeyRecord(key=key, fingerprint=fingerprint))
            old_signature = tuple(record.fingerprint for record in self._records)
            new_signature = tuple(record.fingerprint for record in records)
            self._records = records
            self._cursor = self._cursor % len(records) if records else 0
            if old_signature != new_signature:
                self._model_cache_signature = ()
                self._model_cache_until = 0.0
                self._model_cache = []

    def _acquire_locked(self, excluded: set[str]) -> KeyLease:
        if not self._records:
            raise KeyPoolExhausted("未配置 Gemini API Key。")
        now = self._clock()
        count = len(self._records)
        for offset in range(count):
            index = (self._cursor + offset) % count
            record = self._records[index]
            if record.fingerprint in excluded or record.disabled:
                continue
            if record.disabled_until > now:
                continue
            if record.disabled_until:
                record.disabled_until = 0.0
                record.reason = ""
            self._cursor = (index + 1) % count
            return KeyLease(record.key, record.fingerprint, mask_key(record.key))
        raise KeyPoolExhausted("当前没有可用的 Gemini API Key。")

    def acquire(self) -> KeyLease:
        with self._lock:
            return self._acquire_locked(set())

    @staticmethod
    def _status_code(exc: BaseException) -> Optional[int]:
        for candidate in (
            getattr(exc, "status_code", None),
            getattr(exc, "code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def _classify(cls, exc: BaseException) -> str:
        status = cls._status_code(exc)
        message = str(exc).lower()
        if status in {401, 403} or any(
            token in message
            for token in ("invalid api key", "api key not valid", "permission denied")
        ):
            return "permanent"
        if status == 429 or any(
            token in message
            for token in ("quota", "rate limit", "resource exhausted", "too many requests")
        ):
            return "quota"
        if status is not None and 500 <= status <= 599:
            return "transient"
        if isinstance(exc, (TimeoutError, ConnectionError)) or any(
            token in message for token in ("timed out", "timeout", "connection reset")
        ):
            return "transient"
        return "deterministic"

    def _record_failure(self, fingerprint: str, kind: str, reason: str) -> None:
        with self._lock:
            record = next(
                (item for item in self._records if item.fingerprint == fingerprint),
                None,
            )
            if record is None:
                return
            record.failure_count += 1
            record.reason = reason[:160]
            if kind == "permanent":
                record.disabled = True
                record.disabled_until = 0.0
            elif kind == "quota":
                schedule = (60.0, 300.0, 900.0, 3600.0)
                delay = schedule[min(record.failure_count - 1, len(schedule) - 1)]
                record.disabled_until = self._clock() + delay
            elif kind == "transient":
                delay = min(30.0 * (2 ** (record.failure_count - 1)), 300.0)
                record.disabled_until = self._clock() + delay

    def _record_success(self, fingerprint: str) -> None:
        with self._lock:
            record = next(
                (item for item in self._records if item.fingerprint == fingerprint),
                None,
            )
            if record is None or record.disabled:
                return
            record.failure_count = 0
            record.disabled_until = 0.0
            record.reason = ""

    def run(self, operation: Callable[[str], T]) -> T:
        attempted: set[str] = set()
        last_error: Optional[BaseException] = None
        while True:
            try:
                with self._lock:
                    lease = self._acquire_locked(attempted)
            except KeyPoolExhausted as exc:
                if last_error is not None:
                    raise KeyPoolExhausted(str(exc)) from last_error
                raise
            attempted.add(lease.fingerprint)
            try:
                result = operation(lease.key)
            except Exception as exc:
                kind = self._classify(exc)
                if kind == "deterministic":
                    raise DeterministicGeminiError(str(exc)) from exc
                self._record_failure(lease.fingerprint, kind, str(exc))
                last_error = exc
                continue
            self._record_success(lease.fingerprint)
            return result

    def statuses(self) -> list[KeyStatus]:
        with self._lock:
            now = self._clock()
            result: list[KeyStatus] = []
            for record in self._records:
                if record.disabled:
                    state = "disabled"
                    retry_after = 0
                elif record.disabled_until > now:
                    state = "cooldown"
                    retry_after = max(1, int(record.disabled_until - now))
                else:
                    state = "healthy"
                    retry_after = 0
                result.append(
                    KeyStatus(
                        masked_key=mask_key(record.key),
                        fingerprint=record.fingerprint,
                        state=state,
                        retry_after_seconds=retry_after,
                        failure_count=record.failure_count,
                        reason=record.reason,
                    )
                )
            return result

    def list_models(self, loader: Callable[[str], Iterable[object]]) -> list[str]:
        with self._lock:
            signature = tuple(record.fingerprint for record in self._records)
            now = self._clock()
            if (
                signature
                and signature == self._model_cache_signature
                and self._model_cache_until > now
            ):
                return list(self._model_cache)

        raw_models = self.run(loader)
        models = sorted(
            {
                str(getattr(item, "name", item)).strip()
                for item in raw_models
                if str(getattr(item, "name", item)).strip()
            }
        )
        with self._lock:
            self._model_cache_signature = tuple(
                record.fingerprint for record in self._records
            )
            self._model_cache_until = self._clock() + self._model_cache_ttl
            self._model_cache = list(models)
        return models

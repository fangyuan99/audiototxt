from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


JOB_STAGES = {
    "queued",
    "preparing",
    "parsing",
    "downloading",
    "extracting",
    "retrying",
    "transcribing",
    "delivering",
    "completed",
}
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_stage(value: object, default: str = "preparing") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in JOB_STAGES else default


def _safe_failure_fields(exc: BaseException) -> tuple[str, str]:
    code = str(getattr(exc, "error_code", "") or "").strip().lower()
    message = str(getattr(exc, "user_message", "") or "").strip()
    if not _ERROR_CODE_RE.fullmatch(code):
        code = "task_error"
    sensitive_markers = (
        "http://",
        "https://",
        "aiza",
        "private key",
        "token=",
        "/home/",
        "/root/",
    )
    if not message or any(marker in message.lower() for marker in sensitive_markers):
        message = "任务执行失败。"
    return code, message[:240]


@dataclass
class TelegramJob:
    job_id: str
    sequence: int
    user_id: int
    chat_id: int
    source_type: str
    text_input: str = ""
    audio_path: str = ""
    original_filename: str = ""
    source_message_id: int = 0
    status_message_id: int = 0
    status: str = "queued"
    stage: str = "queued"
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    retry_of: str = ""
    restart_notified: bool = False
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramJob":
        allowed = {item.name for item in fields(cls)}
        payload = {key: value for key, value in data.items() if key in allowed}
        return cls(**payload)

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self, storage_path: str | Path) -> None:
        self.storage_path = Path(storage_path)
        self._lock = threading.RLock()

    def load(self) -> dict[str, TelegramJob]:
        with self._lock:
            if not self.storage_path.exists():
                return {}
            try:
                payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"任务状态文件无法读取：{self.storage_path}") from exc
            raw_jobs = payload.get("jobs", {}) if isinstance(payload, dict) else {}
            if not isinstance(raw_jobs, dict):
                raise RuntimeError(f"任务状态文件格式无效：{self.storage_path}")
            os.chmod(self.storage_path, 0o600)
            result: dict[str, TelegramJob] = {}
            for job_id, raw in raw_jobs.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    job = TelegramJob.from_dict(raw)
                except (TypeError, ValueError):
                    continue
                result[str(job_id)] = job
            return result

    def save(self, jobs: dict[str, TelegramJob]) -> None:
        payload = {
            "schema_version": 1,
            "jobs": {job_id: job.to_dict() for job_id, job in jobs.items()},
        }
        with self._lock:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.storage_path.parent, 0o700)
            except OSError:
                pass
            temp_path: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix=f".{self.storage_path.name}.",
                    suffix=".tmp",
                    dir=str(self.storage_path.parent),
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    os.chmod(temp_path, 0o600)
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.storage_path)
                os.chmod(self.storage_path, 0o600)
            finally:
                if temp_path is not None and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass


class TelegramJobManager:
    def __init__(
        self,
        store: JobStore,
        executor: Callable[[TelegramJob, Callable[[], bool]], Awaitable[object]],
        *,
        max_concurrent_jobs: int = 1,
        task_timeout_seconds: float = 1800.0,
        on_update: Optional[
            Callable[[TelegramJob, str, object], Awaitable[None]]
        ] = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self.task_timeout_seconds = max(0.001, float(task_timeout_seconds))
        self.on_update = on_update
        self._jobs = store.load()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._notification_tasks: set[asyncio.Task] = set()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._started = False
        self._lock = threading.RLock()

        changed = False
        for job in self._jobs.values():
            if job.status in {"queued", "running", "cancelling"}:
                job.status = "interrupted"
                job.error_code = "restart"
                job.error_message = "服务重启，任务已中断。"
                job.restart_notified = False
                job.updated_at = _utc_now()
                changed = True
        if changed:
            self.store.save(self._jobs)

    def _next_sequence(self) -> int:
        return max((job.sequence for job in self._jobs.values()), default=0) + 1

    def enqueue(
        self,
        *,
        user_id: int,
        chat_id: int,
        source_type: str,
        text_input: str = "",
        audio_path: str = "",
        original_filename: str = "",
        source_message_id: int = 0,
        status_message_id: int = 0,
        retry_of: str = "",
    ) -> TelegramJob:
        now = _utc_now()
        with self._lock:
            job = TelegramJob(
                job_id=uuid.uuid4().hex,
                sequence=self._next_sequence(),
                user_id=int(user_id),
                chat_id=int(chat_id),
                source_type=source_type,
                text_input=text_input or "",
                audio_path=audio_path or "",
                original_filename=original_filename or "",
                source_message_id=int(source_message_id or 0),
                status_message_id=int(status_message_id or 0),
                retry_of=retry_of or "",
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.job_id] = job
            self.store.save(self._jobs)
            self._queue.put_nowait(job.job_id)
            return replace(job)

    def get(self, job_id: str) -> Optional[TelegramJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            return replace(job) if job is not None else None

    def snapshot(self) -> list[TelegramJob]:
        with self._lock:
            return [
                replace(job)
                for job in sorted(self._jobs.values(), key=lambda item: item.sequence)
            ]

    def interrupted_jobs(self) -> list[TelegramJob]:
        return [
            job
            for job in self.snapshot()
            if job.status == "interrupted" and not job.restart_notified
        ]

    def mark_restart_notified(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "interrupted" or job.restart_notified:
                return
            job.restart_notified = True
            job.updated_at = _utc_now()
            self.store.save(self._jobs)

    def prune_terminal(
        self,
        *,
        max_age_seconds: float = 24 * 3600,
        now: Optional[datetime] = None,
    ) -> int:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc).timestamp() - max(
            0.0, float(max_age_seconds)
        )
        terminal = {"succeeded", "failed", "cancelled", "interrupted"}
        with self._lock:
            expired = []
            for job_id, job in self._jobs.items():
                if job.status not in terminal or not job.updated_at:
                    continue
                try:
                    updated = datetime.fromisoformat(
                        job.updated_at.replace("Z", "+00:00")
                    )
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if updated.astimezone(timezone.utc).timestamp() < cutoff:
                    expired.append(job_id)
            for job_id in expired:
                self._jobs.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
            if expired:
                self.store.save(self._jobs)
            return len(expired)

    def queue_position(self, job_id: str) -> Optional[int]:
        queued = [job for job in self.snapshot() if job.status == "queued"]
        for index, job in enumerate(queued, start=1):
            if job.job_id == job_id:
                return index
        return None

    def _set_status(self, job_id: str, status: str, **changes) -> TelegramJob:
        with self._lock:
            job = self._jobs[job_id]
            job.status = status
            job.updated_at = _utc_now()
            for key, value in changes.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            self.store.save(self._jobs)
            return replace(job)

    def update_stage(self, job_id: str, stage: str) -> Optional[TelegramJob]:
        normalized = _safe_stage(stage, default="")
        if not normalized:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in {"queued", "running", "cancelling"}:
                return None
            if job.stage == normalized:
                return replace(job)
            job.stage = normalized
            job.updated_at = _utc_now()
            self.store.save(self._jobs)
            return replace(job)

    async def _notify(self, job: TelegramJob, event: str, payload=None) -> None:
        if self.on_update is None:
            return
        try:
            await self.on_update(job, event, payload)
        except Exception:
            # Notification failures must not corrupt queue state.
            return

    def _schedule_notify(self, job: TelegramJob, event: str, payload=None) -> None:
        if self.on_update is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._notify(job, event, payload),
            name=f"telegram-job-notify-{job.job_id}",
        )
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    def cancel(self, job_id: str) -> bool:
        queued_terminal: Optional[TelegramJob] = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in {"queued", "running", "cancelling"}:
                return False
            event = self._cancel_events.setdefault(job_id, asyncio.Event())
            event.set()
            if job.status == "queued":
                queued_terminal = self._set_status(
                    job_id, "cancelled", error_code="cancelled"
                )
            elif job.status == "running":
                self._set_status(job_id, "cancelling")
        if queued_terminal is not None:
            self._schedule_notify(queued_terminal, "cancelled")
        return True

    def retry(
        self,
        job_id: str,
        *,
        source_type_override: Optional[str] = None,
    ) -> TelegramJob:
        original = self.get(job_id)
        if original is None:
            raise KeyError(job_id)
        if original.status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("只有失败、取消或中断的任务可以重试。")
        if original.source_type == "audio" and (
            not original.audio_path or not Path(original.audio_path).is_file()
        ):
            raise ValueError("原音频已过期，请重新发送文件。")
        return self.enqueue(
            user_id=original.user_id,
            chat_id=original.chat_id,
            source_type=source_type_override or original.source_type,
            text_input=original.text_input,
            audio_path=original.audio_path,
            original_filename=original.original_filename,
            source_message_id=original.source_message_id,
            retry_of=original.job_id,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"telegram-job-worker-{index}")
            for index in range(self.max_concurrent_jobs)
        ]

    async def stop(self) -> None:
        if self._started:
            self._started = False
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
        with self._lock:
            changed = False
            for job in self._jobs.values():
                if job.status in {"queued", "running", "cancelling"}:
                    job.status = "interrupted"
                    job.error_code = "shutdown"
                    job.error_message = "服务停止，任务已中断。"
                    job.restart_notified = False
                    job.updated_at = _utc_now()
                    changed = True
            if changed:
                self.store.save(self._jobs)
        if self._notification_tasks:
            await asyncio.gather(
                *list(self._notification_tasks), return_exceptions=True
            )

    async def join(self) -> None:
        await self._queue.join()

    async def _worker(self, worker_index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = self.get(job_id)
                if job is None or job.status != "queued":
                    continue
                cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
                if cancel_event.is_set():
                    terminal = self._set_status(
                        job_id, "cancelled", error_code="cancelled"
                    )
                    await self._notify(terminal, "cancelled")
                    continue

                running = self._set_status(
                    job_id,
                    "running",
                    stage="preparing",
                    attempts=job.attempts + 1,
                    error_code="",
                    error_message="",
                )
                await self._notify(running, "running")
                try:
                    result = await asyncio.wait_for(
                        self.executor(running, cancel_event.is_set),
                        timeout=self.task_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    cancel_event.set()
                    terminal = self._set_status(
                        job_id,
                        "failed",
                        error_code="timeout",
                        error_message="任务处理超时。",
                    )
                    await self._notify(terminal, "failed", TimeoutError("任务处理超时。"))
                except asyncio.CancelledError:
                    cancel_event.set()
                    self._set_status(
                        job_id,
                        "interrupted",
                        error_code="shutdown",
                        error_message="服务停止，任务已中断。",
                    )
                    raise
                except Exception as exc:
                    status = "cancelled" if cancel_event.is_set() else "failed"
                    current = self.get(job_id)
                    failure_stage = _safe_stage(
                        getattr(exc, "stage", ""),
                        default=(current.stage if current else "preparing"),
                    )
                    error_code, error_message = _safe_failure_fields(exc)
                    terminal = self._set_status(
                        job_id,
                        status,
                        stage=failure_stage,
                        error_code=(
                            "cancelled" if status == "cancelled" else error_code
                        ),
                        error_message=(
                            "任务已取消。"
                            if status == "cancelled"
                            else error_message
                        ),
                    )
                    await self._notify(terminal, status, exc)
                else:
                    if cancel_event.is_set():
                        terminal = self._set_status(
                            job_id, "cancelled", error_code="cancelled"
                        )
                        await self._notify(terminal, "cancelled", result)
                    else:
                        terminal = self._set_status(
                            job_id, "succeeded", stage="completed"
                        )
                        await self._notify(terminal, "succeeded", result)
            finally:
                self._cancel_events.pop(job_id, None)
                self._queue.task_done()

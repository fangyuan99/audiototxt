from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
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
    telegram_file_id: str = ""
    original_filename: str = ""
    source_message_id: int = 0
    status_message_id: int = 0
    status: str = "queued"
    stage: str = "queued"
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    retry_of: str = ""
    # Output settings (model, language, prompts) frozen at submit. Never
    # holds credentials.
    settings_snapshot: dict = field(default_factory=dict)
    # Delivery runs after the job succeeds: "", pending, sending, delivered
    # or failed. delivered_chunks lets a resend resume where it stopped.
    delivery_status: str = ""
    delivered_chunks: int = 0
    document_sent: bool = False
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


ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})


class QueueFull(ValueError):
    """Raised when enqueueing would exceed the global or per-user limit."""


class TelegramJobManager:
    def __init__(
        self,
        store: JobStore,
        executor: Callable[[TelegramJob, Callable[[], bool]], Awaitable[object]],
        *,
        max_concurrent_jobs: int = 1,
        max_active_jobs: int = 20,
        max_active_jobs_per_user: int = 5,
        task_timeout_seconds: float = 1800.0,
        save_interval_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        on_update: Optional[
            Callable[[TelegramJob, str, object], Awaitable[None]]
        ] = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self.max_active_jobs = max(1, int(max_active_jobs))
        self.max_active_jobs_per_user = max(1, int(max_active_jobs_per_user))
        self.task_timeout_seconds = max(0.001, float(task_timeout_seconds))
        self.on_update = on_update
        # Stage and chunk-progress changes are coalesced into at most one
        # write per interval; status transitions are always written at once.
        self.save_interval_seconds = max(0.0, float(save_interval_seconds))
        self._clock = clock
        self._last_save = float("-inf")
        self._dirty = False
        self._jobs = store.load()
        # One token per enqueued job; workers pick the actual job fairly.
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        # user_id -> dispatch counter at their last start, for round-robin.
        self._last_dispatch: dict[int, int] = {}
        self._dispatch_counter = 0
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
            self._persist_locked()

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
        telegram_file_id: str = "",
        original_filename: str = "",
        source_message_id: int = 0,
        status_message_id: int = 0,
        retry_of: str = "",
        settings_snapshot: Optional[dict] = None,
    ) -> TelegramJob:
        now = _utc_now()
        with self._lock:
            self._check_capacity_locked(int(user_id))
            job = TelegramJob(
                job_id=uuid.uuid4().hex,
                sequence=self._next_sequence(),
                user_id=int(user_id),
                chat_id=int(chat_id),
                source_type=source_type,
                text_input=text_input or "",
                audio_path=audio_path or "",
                telegram_file_id=telegram_file_id or "",
                original_filename=original_filename or "",
                source_message_id=int(source_message_id or 0),
                status_message_id=int(status_message_id or 0),
                retry_of=retry_of or "",
                settings_snapshot=dict(settings_snapshot or {}),
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.job_id] = job
            self._persist_locked()
            self._queue.put_nowait(job.job_id)
            return replace(job)

    def _check_capacity_locked(self, user_id: int) -> None:
        active = [job for job in self._jobs.values() if job.status in ACTIVE_STATUSES]
        if sum(1 for job in active if job.user_id == user_id) >= self.max_active_jobs_per_user:
            raise QueueFull(
                f"你已有 {self.max_active_jobs_per_user} 个任务在排队或执行，"
                "请等待完成或取消部分任务后再提交。"
            )
        if len(active) >= self.max_active_jobs:
            raise QueueFull("任务队列已满，请稍后再试。")

    def _dispatch_order_locked(self) -> list[TelegramJob]:
        """Queued jobs in the order workers will start them.

        Users take turns: the user who started a job least recently goes
        first, and each user's own jobs stay in submission order.
        """
        per_user: dict[int, list[TelegramJob]] = {}
        for job in sorted(self._jobs.values(), key=lambda item: item.sequence):
            if job.status == "queued":
                per_user.setdefault(job.user_id, []).append(job)
        last = dict(self._last_dispatch)
        counter = self._dispatch_counter
        order: list[TelegramJob] = []
        while per_user:
            user_id = min(
                per_user,
                key=lambda uid: (last.get(uid, -1), per_user[uid][0].sequence),
            )
            order.append(per_user[user_id].pop(0))
            if not per_user[user_id]:
                del per_user[user_id]
            counter += 1
            last[user_id] = counter
        return order

    def _claim_next_locked(self) -> Optional[TelegramJob]:
        order = self._dispatch_order_locked()
        if not order:
            return None
        job = order[0]
        self._dispatch_counter += 1
        self._last_dispatch[job.user_id] = self._dispatch_counter
        return job

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
            self._persist_locked()

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
                self._persist_locked()
            return len(expired)

    def queue_position(self, job_id: str) -> Optional[int]:
        with self._lock:
            queued = self._dispatch_order_locked()
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
            self._persist_locked()
            return replace(job)

    def _persist_locked(self, *, critical: bool = True) -> None:
        now = self._clock()
        if critical or now - self._last_save >= self.save_interval_seconds:
            self.store.save(self._jobs)
            self._last_save = now
            self._dirty = False
        else:
            self._dirty = True

    def flush(self) -> None:
        """Write any coalesced changes that have not reached disk yet."""
        with self._lock:
            if self._dirty:
                self._persist_locked()

    def set_audio_path(self, job_id: str, audio_path: str) -> Optional[TelegramJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.audio_path = audio_path or ""
            job.updated_at = _utc_now()
            self._persist_locked()
            return replace(job)

    def update_delivery(self, job_id: str, **changes) -> Optional[TelegramJob]:
        allowed = {"delivery_status", "delivered_chunks", "document_sent"}
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            applied = {key for key in changes if key in allowed}
            for key in applied:
                setattr(job, key, changes[key])
            job.updated_at = _utc_now()
            # Per-chunk progress alone is not critical: losing it on a crash
            # only means a resend may repeat a chunk or two.
            self._persist_locked(critical=applied != {"delivered_chunks"})
            return replace(job)

    def undelivered_jobs(self) -> list[TelegramJob]:
        return [
            job
            for job in self.snapshot()
            if job.status == "succeeded"
            and job.delivery_status in {"pending", "sending"}
        ]

    def active_retry_of(self, job_id: str) -> Optional[TelegramJob]:
        with self._lock:
            for job in self._jobs.values():
                if job.retry_of == job_id and job.status in {
                    "queued",
                    "running",
                    "cancelling",
                }:
                    return replace(job)
            return None

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
            self._persist_locked(critical=False)
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
        status_message_id: Optional[int] = None,
        settings_snapshot_override: Optional[dict] = None,
    ) -> TelegramJob:
        """Re-enqueue a job, reusing its original settings snapshot unless
        ``settings_snapshot_override`` is given."""
        # Hold the lock across the check and enqueue so repeated clicks
        # cannot create more than one active retry for the same job.
        with self._lock:
            original = self.get(job_id)
            if original is None:
                raise KeyError(job_id)
            if original.status not in {"failed", "cancelled", "interrupted"}:
                raise ValueError("只有失败、取消或中断的任务可以重试。")
            existing = self.active_retry_of(job_id)
            if existing is not None:
                raise ValueError(
                    f"该任务已在重试中：{existing.job_id[:8]}，请勿重复提交。"
                )
            audio_available = bool(original.audio_path) and Path(
                original.audio_path
            ).is_file()
            if (
                original.source_type == "audio"
                and not audio_available
                and not original.telegram_file_id
            ):
                raise ValueError("原音频已过期，请重新发送文件。")
            return self.enqueue(
                user_id=original.user_id,
                chat_id=original.chat_id,
                source_type=source_type_override or original.source_type,
                text_input=original.text_input,
                audio_path=original.audio_path if audio_available else "",
                telegram_file_id=original.telegram_file_id,
                original_filename=original.original_filename,
                source_message_id=original.source_message_id,
                status_message_id=(
                    original.status_message_id
                    if status_message_id is None
                    else int(status_message_id or 0)
                ),
                retry_of=original.job_id,
                settings_snapshot=(
                    original.settings_snapshot
                    if settings_snapshot_override is None
                    else settings_snapshot_override
                ),
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
            if changed or self._dirty:
                self._persist_locked()
        if self._notification_tasks:
            await asyncio.gather(
                *list(self._notification_tasks), return_exceptions=True
            )

    async def join(self) -> None:
        await self._queue.join()

    async def _worker(self, worker_index: int) -> None:
        while True:
            await self._queue.get()
            job_id = ""
            try:
                with self._lock:
                    claimed = self._claim_next_locked()
                    if claimed is None:
                        # Its job was cancelled while queued.
                        continue
                    job_id = claimed.job_id
                    cancel_event = self._cancel_events.setdefault(
                        job_id, asyncio.Event()
                    )
                    # Claim and mark running under one lock so no other
                    # worker can start the same job.
                    running = self._set_status(
                        job_id,
                        "running",
                        stage="preparing",
                        attempts=claimed.attempts + 1,
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
                if job_id:
                    self._cancel_events.pop(job_id, None)
                self._queue.task_done()

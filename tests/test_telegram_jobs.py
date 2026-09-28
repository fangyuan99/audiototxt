import asyncio
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_jobs import JobStore, TelegramJobManager


class TelegramJobManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_users_take_turns_and_positions_match_dispatch(self):
        calls = []

        async def executor(job, cancelled):
            calls.append(job.job_id)
            await asyncio.sleep(0)
            return f"result-{job.job_id}"

        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"),
            executor,
            max_concurrent_jobs=1,
        )
        first = manager.enqueue(user_id=1, chat_id=10, source_type="youtube", text_input="a")
        second = manager.enqueue(user_id=1, chat_id=10, source_type="youtube", text_input="b")
        third = manager.enqueue(user_id=2, chat_id=20, source_type="youtube", text_input="c")

        self.assertEqual(manager.queue_position(first.job_id), 1)
        self.assertEqual(manager.queue_position(third.job_id), 2)
        self.assertEqual(manager.queue_position(second.job_id), 3)

        await manager.start()
        await manager.join()
        await manager.stop()

        self.assertEqual(calls, [first.job_id, third.job_id, second.job_id])
        self.assertTrue(all(job.status == "succeeded" for job in manager.snapshot()))

    async def test_single_user_jobs_stay_in_submission_order(self):
        calls = []

        async def executor(job, cancelled):
            calls.append(job.text_input)
            return "ok"

        manager = TelegramJobManager(JobStore(self.root / "jobs.json"), executor)
        for text in "abc":
            manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input=text)

        await manager.start()
        await manager.join()
        await manager.stop()

        self.assertEqual(calls, ["a", "b", "c"])

    async def test_per_user_and_global_limits_reject_new_jobs(self):
        from telegram_jobs import QueueFull

        async def executor(job, cancelled):
            return "ok"

        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"),
            executor,
            max_active_jobs=3,
            max_active_jobs_per_user=2,
        )
        manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="a")
        manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="b")
        with self.assertRaisesRegex(QueueFull, "2 个任务"):
            manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="c")

        manager.enqueue(user_id=2, chat_id=2, source_type="youtube", text_input="d")
        with self.assertRaisesRegex(QueueFull, "队列已满"):
            manager.enqueue(user_id=3, chat_id=3, source_type="youtube", text_input="e")

        # Cancelled jobs free their slot.
        queued = [job for job in manager.snapshot() if job.user_id == 1]
        manager.cancel(queued[0].job_id)
        manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="f")

    async def test_queued_job_can_be_cancelled(self):
        gate = asyncio.Event()
        calls = []

        async def executor(job, cancelled):
            calls.append(job.job_id)
            await gate.wait()
            return "ok"

        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), executor, max_concurrent_jobs=1
        )
        first = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="a")
        second = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="b")
        await manager.start()
        await asyncio.sleep(0.01)

        self.assertTrue(manager.cancel(second.job_id))
        gate.set()
        await manager.join()
        await manager.stop()

        self.assertEqual(calls, [first.job_id])
        self.assertEqual(manager.get(second.job_id).status, "cancelled")

    async def test_queued_cancel_emits_terminal_notification(self):
        notifications = []

        async def executor(job, cancelled):
            return "unused"

        async def on_update(job, event, payload):
            notifications.append((job.job_id, event, payload))

        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"),
            executor,
            on_update=on_update,
        )
        job = manager.enqueue(
            user_id=1,
            chat_id=1,
            source_type="audio",
            audio_path=str(self.root / "audio.mp3"),
        )

        self.assertTrue(manager.cancel(job.job_id))
        await manager.stop()

        self.assertEqual(notifications, [(job.job_id, "cancelled", None)])

    async def test_running_cancel_sets_cooperative_flag_and_discards_result(self):
        started = asyncio.Event()

        async def executor(job, cancelled):
            started.set()
            while not cancelled():
                await asyncio.sleep(0)
            return "late-result"

        manager = TelegramJobManager(JobStore(self.root / "jobs.json"), executor)
        job = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="a")
        await manager.start()
        await started.wait()
        self.assertTrue(manager.cancel(job.job_id))
        await manager.join()
        await manager.stop()

        self.assertEqual(manager.get(job.job_id).status, "cancelled")

    async def test_timeout_marks_job_failed_without_waiting_forever(self):
        async def executor(job, cancelled):
            await asyncio.sleep(1)
            return "late"

        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"),
            executor,
            task_timeout_seconds=0.02,
        )
        job = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="a")
        await manager.start()
        await asyncio.wait_for(manager.join(), timeout=0.5)
        await manager.stop()

        stored = manager.get(job.job_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.error_code, "timeout")

    async def test_timeout_preserves_latest_reported_stage(self):
        manager_holder = {}

        async def executor(job, cancelled):
            manager_holder["manager"].update_stage(job.job_id, "transcribing")
            await asyncio.sleep(1)

        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"),
            executor,
            task_timeout_seconds=0.02,
        )
        manager_holder["manager"] = manager
        job = manager.enqueue(user_id=1, chat_id=1, source_type="youtube")
        await manager.start()
        await manager.join()
        await manager.stop()

        stored = manager.get(job.job_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.stage, "transcribing")

    async def test_failure_persists_only_safe_diagnostic_fields(self):
        class SafeFailure(RuntimeError):
            stage = "transcribing"
            error_code = "billing_disabled"
            user_message = "Google Cloud 项目未启用结算。"

        async def executor(job, cancelled):
            raise SafeFailure("raw signed secret https://example/get?token=secret")

        path = self.root / "jobs.json"
        manager = TelegramJobManager(JobStore(path), executor)
        job = manager.enqueue(user_id=1, chat_id=1, source_type="douyin")
        await manager.start()
        await manager.join()
        await manager.stop()

        stored = manager.get(job.job_id)
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.stage, "transcribing")
        self.assertEqual(stored.error_code, "billing_disabled")
        self.assertEqual(stored.error_message, "Google Cloud 项目未启用结算。")
        serialized = path.read_text(encoding="utf-8")
        self.assertNotIn("signed secret", serialized)
        self.assertNotIn("token=secret", serialized)

    async def test_success_ends_at_completed_stage(self):
        async def executor(job, cancelled):
            return "ok"

        manager = TelegramJobManager(JobStore(self.root / "jobs.json"), executor)
        job = manager.enqueue(user_id=1, chat_id=1, source_type="youtube")
        await manager.start()
        await manager.join()
        await manager.stop()

        self.assertEqual(manager.get(job.job_id).stage, "completed")

    async def test_restart_marks_queued_and_running_jobs_interrupted(self):
        path = self.root / "jobs.json"
        manager = TelegramJobManager(JobStore(path), lambda job, cancelled: None)
        queued = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="a")
        running = manager.enqueue(user_id=2, chat_id=2, source_type="youtube", text_input="b")
        manager._set_status(running.job_id, "running")

        reloaded = TelegramJobManager(JobStore(path), lambda job, cancelled: None)

        self.assertEqual(reloaded.get(queued.job_id).status, "interrupted")
        self.assertEqual(reloaded.get(running.job_id).status, "interrupted")
        self.assertEqual(
            {job.job_id for job in reloaded.interrupted_jobs()},
            {queued.job_id, running.job_id},
        )
        reloaded.mark_restart_notified(queued.job_id)
        loaded_again = TelegramJobManager(JobStore(path), lambda job, cancelled: None)
        self.assertEqual(
            {job.job_id for job in loaded_again.interrupted_jobs()},
            {running.job_id},
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    async def test_prune_terminal_removes_only_expired_metadata(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        old = manager.enqueue(user_id=1, chat_id=1, source_type="youtube")
        recent = manager.enqueue(user_id=1, chat_id=1, source_type="youtube")
        active = manager.enqueue(user_id=1, chat_id=1, source_type="youtube")
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        manager._set_status(
            old.job_id, "failed", updated_at=old_time.isoformat()
        )
        manager._set_status(recent.job_id, "succeeded")

        removed = manager.prune_terminal(
            max_age_seconds=24 * 3600,
            now=datetime.now(timezone.utc),
        )

        self.assertEqual(removed, 1)
        self.assertIsNone(manager.get(old.job_id))
        self.assertIsNotNone(manager.get(recent.job_id))
        self.assertEqual(manager.get(active.job_id).status, "queued")

    async def test_delivery_progress_survives_restart(self):
        async def executor(job, cancelled):
            return "ok"

        path = self.root / "jobs.json"
        manager = TelegramJobManager(JobStore(path), executor)
        pending = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="a")
        done = manager.enqueue(user_id=1, chat_id=1, source_type="youtube", text_input="b")
        manager._set_status(pending.job_id, "succeeded")
        manager._set_status(done.job_id, "succeeded")
        manager.update_delivery(pending.job_id, delivery_status="sending", delivered_chunks=2)
        manager.update_delivery(done.job_id, delivery_status="delivered")

        reloaded = TelegramJobManager(JobStore(path), executor)

        undelivered = reloaded.undelivered_jobs()
        self.assertEqual([job.job_id for job in undelivered], [pending.job_id])
        self.assertEqual(undelivered[0].delivered_chunks, 2)

    async def test_retry_clones_payload_with_new_id(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        original = manager.enqueue(
            user_id=1,
            chat_id=2,
            source_type="video_url",
            text_input="https://example.com/v.mp4",
            original_filename="v.mp4",
        )
        manager._set_status(original.job_id, "failed", error_code="network")

        retried = manager.retry(original.job_id, status_message_id=456)

        self.assertNotEqual(retried.job_id, original.job_id)
        self.assertEqual(retried.retry_of, original.job_id)
        self.assertEqual(retried.text_input, original.text_input)
        self.assertEqual(retried.status, "queued")
        self.assertEqual(retried.status_message_id, 456)

    async def test_retry_inherits_existing_status_message_by_default(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        original = manager.enqueue(
            user_id=1,
            chat_id=2,
            source_type="video_url",
            text_input="https://example.com/v.mp4",
            status_message_id=123,
        )
        manager._set_status(original.job_id, "failed", error_code="network")

        retried = manager.retry(original.job_id)

        self.assertEqual(retried.status_message_id, 123)

    async def test_retry_can_override_legacy_source_classification(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        original = manager.enqueue(
            user_id=1,
            chat_id=2,
            source_type="video_url",
            text_input="https://www.iesdouyin.com/share/video/123",
        )
        manager._set_status(original.job_id, "failed", error_code="media_decode")

        retried = manager.retry(original.job_id, source_type_override="douyin")

        self.assertEqual(retried.source_type, "douyin")
        self.assertEqual(retried.text_input, original.text_input)

    async def test_retry_rejects_expired_audio_source(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        original = manager.enqueue(
            user_id=1,
            chat_id=2,
            source_type="audio",
            audio_path=str(self.root / "missing.mp3"),
        )
        manager._set_status(original.job_id, "failed", error_code="network")

        with self.assertRaisesRegex(ValueError, "重新发送"):
            manager.retry(original.job_id)

    async def test_retry_is_idempotent_while_retry_is_active(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        original = manager.enqueue(
            user_id=1,
            chat_id=2,
            source_type="video_url",
            text_input="https://example.com/v.mp4",
        )
        manager._set_status(original.job_id, "failed", error_code="network")

        first = manager.retry(original.job_id)
        with self.assertRaisesRegex(ValueError, "已在重试中"):
            manager.retry(original.job_id)

        manager._set_status(first.job_id, "failed", error_code="network")
        second = manager.retry(original.job_id)
        self.assertNotEqual(second.job_id, first.job_id)

    async def test_retry_audio_without_local_file_redownloads_by_file_id(self):
        manager = TelegramJobManager(
            JobStore(self.root / "jobs.json"), lambda job, cancelled: None
        )
        original = manager.enqueue(
            user_id=1,
            chat_id=2,
            source_type="audio",
            audio_path=str(self.root / "missing.mp3"),
            telegram_file_id="file-123",
        )
        manager._set_status(original.job_id, "failed", error_code="network")

        retried = manager.retry(original.job_id)

        self.assertEqual(retried.telegram_file_id, "file-123")
        self.assertEqual(retried.audio_path, "")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from telegram_delivery import ChatSender, DeliveryFailed, ResultStore


def failing(*errors, result="sent"):
    remaining = list(errors)
    calls = []

    async def operation():
        calls.append(1)
        if remaining:
            raise remaining.pop(0)
        return result

    return operation, calls


class ChatSenderTest(unittest.IsolatedAsyncioTestCase):
    def make_sender(self, **kwargs):
        sleep = AsyncMock()
        return ChatSender(min_interval_seconds=0, sleep=sleep, **kwargs), sleep

    async def test_retry_after_waits_then_succeeds(self):
        sender, sleep = self.make_sender()
        operation, calls = failing(RetryAfter(timedelta(seconds=4)))

        self.assertEqual(await sender.send(1, operation), "sent")

        self.assertEqual(len(calls), 2)
        sleep.assert_awaited_once_with(4.5)

    async def test_excessive_retry_after_gives_up(self):
        sender, _sleep = self.make_sender(max_retry_after_seconds=10)
        operation, calls = failing(RetryAfter(600))

        with self.assertRaises(DeliveryFailed):
            await sender.send(1, operation)
        self.assertEqual(len(calls), 1)

    async def test_network_errors_back_off_then_fail(self):
        sender, sleep = self.make_sender(max_attempts=3)
        operation, calls = failing(
            TimedOut(), NetworkError("reset"), NetworkError("reset")
        )

        with self.assertRaises(DeliveryFailed):
            await sender.send(1, operation)

        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [1.0, 2.0])

    async def test_bad_request_is_not_retried(self):
        sender, sleep = self.make_sender()
        operation, calls = failing(BadRequest("Chat not found"))

        with self.assertRaises(BadRequest):
            await sender.send(1, operation)

        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()

    async def test_sends_to_same_chat_are_spaced(self):
        now = [100.0]
        sleep = AsyncMock()
        sender = ChatSender(min_interval_seconds=1.0, clock=lambda: now[0], sleep=sleep)
        operation, _calls = failing()

        await sender.send(1, operation)
        now[0] += 0.25
        await sender.send(1, operation)
        await sender.send(2, operation)

        sleep.assert_awaited_once_with(0.75)


class ResultStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ResultStore(Path(self.temp_dir.name) / "results")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_round_trip_preserves_transcript_and_finish_reason(self):
        self.store.save("job-1", "完整结果🙂", filename_stem="talk", finish_reason="MAX_TOKENS")

        stored = self.store.load("job-1")

        self.assertEqual(stored.transcript, "完整结果🙂")
        self.assertEqual(stored.filename_stem, "talk")
        self.assertTrue(stored.incomplete)

    def test_missing_result_loads_as_none(self):
        self.assertIsNone(self.store.load("absent"))

    def test_job_id_cannot_escape_store(self):
        with self.assertRaises(ValueError):
            self.store.save("../escape", "x", filename_stem="x")
        self.assertIsNone(self.store.load("../escape"))


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_state import BotStateStore
from channel_health import ChannelHealthResult
from key_pool import GeminiKeyPool
from service_config import GlobalConfigStore
from telegram_bot import (
    JobExecutionFailure,
    MODEL_CHOICES_KEY,
    PENDING_ACTION_KEY,
    ResultCache,
    _deliver_result,
    _execute_job,
    _handle_setting_input,
    _on_job_update,
    build_application,
    handle_callback_query,
    handle_text_message,
    start_command,
)
from telegram_jobs import TelegramJob
from transcription_service import TranscriptionResult


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.deleted = False
        self.message_id = 99
        self.edits = []
        self.chat = SimpleNamespace(id=42)

    async def reply_text(self, text, **kwargs):
        reply = FakeMessage(text)
        reply.reply_markup = kwargs.get("reply_markup")
        self.replies.append((text, kwargs, reply))
        return reply

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edits.append((text, kwargs))
        return self


class FakeContext:
    def __init__(self, bot_data):
        self.application = SimpleNamespace(bot_data=bot_data)
        self.user_data = {}
        self.args = []


def make_update(message, user_id=42, chat_type="private"):
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(
            id=user_id,
            username="tester",
            first_name="Test",
        ),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
    )


class TelegramBotInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
        self.state_store = BotStateStore(str(root / "state.json"))
        self.config_store = GlobalConfigStore(root / "global.json", environ={})
        self.bot_data = {
            "store": self.state_store,
            "config_store": self.config_store,
            "key_pool": GeminiKeyPool([]),
            "allowed_user_ids": set(),
            "bot_secret": "secret",
        }

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_start_prompts_non_allowlisted_user_for_password(self):
        message = FakeMessage()
        context = FakeContext(self.bot_data)

        await start_command(make_update(message), context)

        self.assertEqual(context.user_data[PENDING_ACTION_KEY], "awaiting_secret")
        self.assertIn("密码", message.replies[-1][0])

    async def test_allowlisted_user_reaches_home_without_password(self):
        message = FakeMessage()
        self.bot_data["allowed_user_ids"] = {42}
        context = FakeContext(self.bot_data)

        await start_command(make_update(message), context)

        self.assertNotIn(PENDING_ACTION_KEY, context.user_data)
        self.assertIn("直接发送", message.replies[-1][0])
        self.assertIsNotNone(message.replies[-1][1]["reply_markup"])

    async def test_password_message_is_deleted_and_authorizes_user(self):
        message = FakeMessage("secret")
        context = FakeContext(self.bot_data)
        context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"

        await handle_text_message(make_update(message), context)

        self.assertTrue(message.deleted)
        self.assertTrue(
            self.state_store.is_user_authorized(42, current_secret="secret")
        )
        self.assertIn("验证成功", message.replies[-1][0])

    async def test_build_application_registers_callback_handler_and_private_handlers(self):
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            "os.environ",
            {
                "ENV_BOT_TOKEN": "123456:ABCDEF_fake_token",
                "ENV_BOT_SECRET": "secret",
                "BOT_DATA_DIR": tmp_dir,
            },
            clear=False,
        ):
            application = build_application()

        try:
            handler_names = {
                type(handler).__name__
                for handlers in application.handlers.values()
                for handler in handlers
            }
            self.assertIn("CallbackQueryHandler", handler_names)
            self.assertIn("MessageHandler", handler_names)
        finally:
            await asyncio.gather(
                *(request.shutdown() for request in application.bot._request),
                return_exceptions=True,
            )

    async def test_saving_vertex_json_defaults_global_and_runs_health_probe(self):
        captured = []

        async def checker(settings, pool):
            captured.append(settings)
            return ChannelHealthResult(
                available=True,
                auth_mode=settings.auth_mode,
                model=settings.model_name,
                location=settings.vertex_location,
                latency_ms=10,
                code="ok",
                user_message="渠道可用。",
            )

        self.bot_data["channel_health_checker"] = checker
        message = FakeMessage(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": "new-project",
                    "private_key": "secret",
                }
            )
        )
        context = FakeContext(self.bot_data)

        handled = await _handle_setting_input(
            make_update(message), context, "vertex_json", message.text
        )

        stored = self.config_store.get()
        self.assertTrue(handled)
        self.assertTrue(message.deleted)
        self.assertEqual(stored.auth_mode, "vertex_ai_json")
        self.assertEqual(stored.vertex_project, "new-project")
        self.assertEqual(stored.vertex_location, "global")
        self.assertEqual(captured[-1].auth_mode, "vertex_ai_json")
        self.assertEqual(captured[-1].vertex_location, "global")
        rendered = "\n".join(
            [item[0] for item in message.replies]
            + [edit[0] for _, _, reply in message.replies for edit in reply.edits]
        )
        self.assertIn("当前渠道可用", rendered)

    async def test_vertex_project_edit_probes_vertex_even_when_gemini_selected(self):
        captured = []

        async def checker(settings, pool):
            captured.append(settings)
            return ChannelHealthResult(
                available=False,
                auth_mode=settings.auth_mode,
                model=settings.model_name,
                location=settings.vertex_location,
                latency_ms=10,
                code="permission_denied",
                user_message="渠道凭据或 IAM 权限不足。",
            )

        self.config_store.update(
            auth_mode="gemini_api_key",
            vertex_json='{"project_id":"old"}',
            vertex_location="",
        )
        self.bot_data["channel_health_checker"] = checker
        message = FakeMessage("new-project")
        context = FakeContext(self.bot_data)

        await _handle_setting_input(
            make_update(message), context, "vertex_project", message.text
        )

        self.assertEqual(self.config_store.get().auth_mode, "gemini_api_key")
        self.assertEqual(captured[-1].auth_mode, "vertex_ai_json")
        self.assertEqual(captured[-1].vertex_project, "new-project")
        self.assertEqual(captured[-1].vertex_location, "global")

    async def test_vertex_model_menu_loads_remote_catalog(self):
        captured = []

        def loader(settings, pool):
            captured.append(settings)
            return ["gemini-current", "gemini-next"]

        self.config_store.update(
            auth_mode="vertex_ai_json",
            vertex_json='{"project_id":"demo"}',
            vertex_project="demo",
            vertex_location="global",
            model_name="gemini-current",
        )
        self.bot_data.update(
            {
                "allowed_user_ids": {42},
                "job_manager": SimpleNamespace(),
                "model_catalog_loader": loader,
            }
        )
        context = FakeContext(self.bot_data)
        query = SimpleNamespace(
            data="settings:model",
            message=FakeMessage(),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type="private"),
        )

        await handle_callback_query(update, context)

        self.assertEqual(captured[-1].auth_mode, "vertex_ai_json")
        self.assertEqual(
            context.user_data[MODEL_CHOICES_KEY],
            ["gemini-current", "gemini-next"],
        )
        rendered = query.edit_message_text.await_args
        self.assertIn("gemini-current", rendered.args[0])
        callbacks = [
            button.callback_data
            for row in rendered.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("model:set:0", callbacks)
        self.assertIn("model:manual", callbacks)

    async def test_model_menu_falls_back_to_manual_without_raw_error(self):
        def loader(settings, pool):
            raise RuntimeError("secret provider response")

        self.config_store.update(auth_mode="vertex_ai_json")
        self.bot_data.update(
            {
                "allowed_user_ids": {42},
                "job_manager": SimpleNamespace(),
                "model_catalog_loader": loader,
            }
        )
        context = FakeContext(self.bot_data)
        message = FakeMessage()
        query = SimpleNamespace(
            data="settings:model",
            message=message,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type="private"),
        )

        await handle_callback_query(update, context)

        self.assertEqual(context.user_data[PENDING_ACTION_KEY], "model_manual")
        self.assertIn("直接发送模型名称", message.replies[-1][0])
        self.assertNotIn("secret provider response", message.replies[-1][0])

    async def test_retry_binds_new_job_to_clicked_message(self):
        original = TelegramJob(
            job_id="failed-job",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="youtube",
            text_input="https://www.youtube.com/watch?v=example",
            status_message_id=12,
            status="failed",
        )
        retried = TelegramJob(
            job_id="retried-job",
            sequence=2,
            user_id=42,
            chat_id=42,
            source_type="youtube",
        )
        manager = SimpleNamespace(
            get=lambda job_id: original if job_id == original.job_id else None,
            retry=unittest.mock.Mock(return_value=retried),
            queue_position=lambda job_id: 1,
        )
        self.bot_data.update(
            {
                "allowed_user_ids": {42},
                "job_manager": manager,
            }
        )
        context = FakeContext(self.bot_data)
        message = FakeMessage()
        message.message_id = 987
        query = SimpleNamespace(
            data=f"job:retry:{original.job_id}",
            message=message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type="private"),
        )

        await handle_callback_query(update, context)

        manager.retry.assert_called_once_with(
            original.job_id,
            source_type_override="youtube",
            status_message_id=987,
        )
        self.assertIn("任务已重新排队", message.replies[-1][0])

    async def test_execute_job_delivers_before_returning_success(self):
        class FakeService:
            def execute(self, request, **kwargs):
                kwargs["on_status"]("transcribing")
                return TranscriptionResult("transcript", "result")

            async def execute_async(self, request, **kwargs):
                return self.execute(request, **kwargs)

        class FakeManager:
            def __init__(self):
                self.stages = []

            def update_stage(self, job_id, stage):
                self.stages.append(stage)

            def get(self, job_id):
                return SimpleNamespace(stage=self.stages[-1] if self.stages else "preparing")

        manager = FakeManager()
        application = SimpleNamespace(
            bot_data={
                "transcription_service": FakeService(),
                "media_policy": SimpleNamespace(task_timeout_seconds=10),
                "job_manager": manager,
            },
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        job = TelegramJob(
            job_id="job-id",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="youtube",
        )

        with patch("telegram_bot._deliver_result", new=AsyncMock()) as deliver:
            result = await _execute_job(application, job, lambda: False)

        self.assertEqual(result.transcript, "transcript")
        deliver.assert_awaited_once()
        self.assertIn("transcribing", manager.stages)
        self.assertEqual(manager.stages[-1], "delivering")

    async def test_delivery_failure_is_wrapped_with_safe_stage(self):
        class FakeService:
            def execute(self, request, **kwargs):
                return TranscriptionResult("transcript", "result")

            async def execute_async(self, request, **kwargs):
                return self.execute(request, **kwargs)

        class FakeManager:
            def __init__(self):
                self.stage = "preparing"

            def update_stage(self, job_id, stage):
                self.stage = stage

            def get(self, job_id):
                return SimpleNamespace(stage=self.stage)

        application = SimpleNamespace(
            bot_data={
                "transcription_service": FakeService(),
                "media_policy": SimpleNamespace(task_timeout_seconds=10),
                "job_manager": FakeManager(),
            },
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        job = TelegramJob(
            job_id="job-id",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="youtube",
        )

        with patch(
            "telegram_bot._deliver_result",
            new=AsyncMock(
                side_effect=RuntimeError(
                    "send failed https://example/get?token=secret"
                )
            ),
        ):
            with self.assertRaises(JobExecutionFailure) as raised:
                await _execute_job(application, job, lambda: False)

        self.assertEqual(raised.exception.stage, "delivering")
        self.assertNotIn("token=secret", str(raised.exception))

    async def test_long_result_is_sent_in_full_across_multiple_messages(self):
        transcript = "第一段。" * 1000 + "\n\n" + "🙂" * 1500
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_document=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        cache = ResultCache()
        application = SimpleNamespace(
            bot=bot,
            bot_data={
                "result_cache": cache,
                "paths": SimpleNamespace(outputs_dir=self.root),
            },
        )
        job = TelegramJob(
            job_id="long-result",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="youtube",
        )

        await _deliver_result(
            application,
            job,
            TranscriptionResult(transcript, "long-transcript"),
        )

        sent_chunks = [
            call.kwargs["text"] for call in bot.send_message.await_args_list
        ]
        self.assertGreater(len(sent_chunks), 1)
        self.assertEqual("".join(sent_chunks), transcript)
        self.assertNotIn("完整内容见", "".join(sent_chunks))
        self.assertEqual(cache.get(job.job_id), transcript)
        bot.send_document.assert_awaited_once()

    async def test_terminal_timeout_logs_one_safe_structured_record(self):
        application = SimpleNamespace(
            bot_data={},
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
        )
        job = TelegramJob(
            job_id="timeout-id",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="douyin",
            status="failed",
            stage="transcribing",
            error_code="timeout",
            error_message="任务处理超时。",
        )

        with self.assertLogs("telegram_bot", level="ERROR") as captured:
            await _on_job_update(application, job, "failed", TimeoutError("timeout"))

        records = [line for line in captured.output if "telegram_job_failed" in line]
        self.assertEqual(len(records), 1)
        self.assertIn("job_id=timeout-id", records[0])
        self.assertIn("stage=transcribing", records[0])
        self.assertIn("category=timeout", records[0])
        self.assertIn("timeout_boundary", records[0])


if __name__ == "__main__":
    unittest.main()

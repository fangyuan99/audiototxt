import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, RetryAfter

from bot_state import BotStateStore
from channel_health import ChannelHealthResult
from key_pool import GeminiKeyPool
from service_config import GlobalConfigStore
from telegram_bot import (
    render_job_progress,
    _settings_changed,
    build_failed_job_keyboard,
    JobExecutionFailure,
    MODEL_CHOICES_KEY,
    PENDING_ACTION_KEY,
    _deliver_job,
    _execute_job,
    _handle_setting_input,
    _on_job_update,
    _resume_undelivered,
    build_application,
    cancel_command,
    handle_audio_message,
    handle_callback_query,
    handle_text_message,
    handle_unsupported_message,
    split_telegram_text,
    start_command,
)
from telegram_delivery import ChatSender, ResultStore
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

    async def test_retry_binds_new_job_to_its_own_status_card(self):
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

        status_card = message.replies[-1][2]
        status_card.message_id = 555
        manager.retry.assert_called_once_with(
            original.job_id,
            source_type_override="youtube",
            status_message_id=99,
            settings_snapshot_override=None,
        )
        self.assertNotEqual(status_card.message_id, 987)
        self.assertIn("任务已重新排队", status_card.text)
        self.assertEqual(message.edits, [])

    async def test_retry_with_current_settings_passes_live_snapshot(self):
        self.config_store.update(model_name="new-model")
        original = TelegramJob(
            job_id="failed-job",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="youtube",
            text_input="https://www.youtube.com/watch?v=example",
            status="failed",
            settings_snapshot={"model_name": "old-model"},
        )
        retried = TelegramJob(
            job_id="retried-job",
            sequence=2,
            user_id=42,
            chat_id=42,
            source_type="youtube",
            settings_snapshot={"model_name": "new-model"},
        )
        manager = SimpleNamespace(
            get=lambda job_id: original if job_id == original.job_id else None,
            retry=unittest.mock.Mock(return_value=retried),
            queue_position=lambda job_id: 1,
        )
        self.bot_data.update({"allowed_user_ids": {42}, "job_manager": manager})
        self.assertTrue(_settings_changed(self.bot_data, original))
        keyboard = build_failed_job_keyboard(original.job_id, settings_changed=True)
        callbacks = [button.callback_data for button in keyboard.inline_keyboard[0]]
        self.assertEqual(
            callbacks, ["job:retry:failed-job", "job:retrycur:failed-job"]
        )

        message = FakeMessage()
        query = SimpleNamespace(
            data=f"job:retrycur:{original.job_id}", message=message, answer=AsyncMock()
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type="private"),
        )
        await handle_callback_query(update, FakeContext(self.bot_data))

        override = manager.retry.call_args.kwargs["settings_snapshot_override"]
        self.assertEqual(override["model_name"], "new-model")
        self.assertNotIn("gemini_api_keys", override)
        self.assertIn("new-model", message.replies[-1][2].text)

    async def test_jobs_without_snapshot_never_offer_current_settings(self):
        job = TelegramJob(job_id="j", sequence=1, user_id=1, chat_id=1, source_type="youtube")
        self.config_store.update(model_name="another")
        self.assertFalse(_settings_changed(self.bot_data, job))

    async def test_duplicate_retry_reports_error_on_new_card(self):
        original = TelegramJob(
            job_id="failed-job",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="youtube",
            text_input="https://www.youtube.com/watch?v=example",
            status="failed",
        )
        manager = SimpleNamespace(
            get=lambda job_id: original,
            retry=unittest.mock.Mock(
                side_effect=ValueError("该任务已在重试中：abcd1234，请勿重复提交。")
            ),
        )
        self.bot_data.update({"allowed_user_ids": {42}, "job_manager": manager})
        context = FakeContext(self.bot_data)
        message = FakeMessage()
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

        self.assertIn("已在重试中", message.replies[-1][2].text)

    async def test_navigation_ends_pending_input_so_link_is_transcribed(self):
        manager = SimpleNamespace(
            snapshot=lambda: [],
            enqueue=unittest.mock.Mock(
                return_value=TelegramJob(
                    job_id="new-job",
                    sequence=1,
                    user_id=42,
                    chat_id=42,
                    source_type="youtube",
                )
            ),
            queue_position=lambda job_id: 1,
        )
        self.bot_data.update({"allowed_user_ids": {42}, "job_manager": manager})
        context = FakeContext(self.bot_data)
        context.user_data[PENDING_ACTION_KEY] = "prompt_append"
        query = SimpleNamespace(
            data="home",
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
        self.assertNotIn(PENDING_ACTION_KEY, context.user_data)

        link = FakeMessage("https://www.youtube.com/watch?v=abc")
        await handle_text_message(make_update(link), context)

        manager.enqueue.assert_called_once()
        self.assertEqual(self.config_store.get().prompt_append, "")

    async def test_cancel_with_pending_input_does_not_cancel_job(self):
        manager = SimpleNamespace(cancel=unittest.mock.Mock())
        self.bot_data.update({"allowed_user_ids": {42}, "job_manager": manager})
        context = FakeContext(self.bot_data)
        context.user_data[PENDING_ACTION_KEY] = "prompt_append"
        message = FakeMessage("/cancel")

        with patch("telegram_bot._latest_user_job") as latest:
            await cancel_command(make_update(message), context)

        latest.assert_not_called()
        manager.cancel.assert_not_called()
        self.assertNotIn(PENDING_ACTION_KEY, context.user_data)
        self.assertIn("已退出输入", message.replies[-1][0])

    async def test_audio_message_enqueues_without_downloading(self):
        manager = SimpleNamespace(
            enqueue=unittest.mock.Mock(
                return_value=TelegramJob(
                    job_id="audio-job",
                    sequence=1,
                    user_id=42,
                    chat_id=42,
                    source_type="audio",
                )
            ),
            queue_position=lambda job_id: 1,
        )
        self.bot_data.update(
            {
                "allowed_user_ids": {42},
                "job_manager": manager,
                "media_policy": SimpleNamespace(max_media_bytes=100 * 1024 * 1024),
            }
        )
        context = FakeContext(self.bot_data)
        message = FakeMessage()
        get_file = AsyncMock()
        message.audio = SimpleNamespace(
            file_id="file-abc",
            file_size=1024,
            file_name="meeting.m4a",
            get_file=get_file,
        )
        message.voice = None
        message.document = None

        await handle_audio_message(make_update(message), context)

        get_file.assert_not_awaited()
        kwargs = manager.enqueue.call_args.kwargs
        self.assertEqual(kwargs["telegram_file_id"], "file-abc")
        self.assertEqual(kwargs["audio_path"], "")
        self.assertIn("已接收", message.replies[0][0])

    async def test_audio_over_bot_api_limit_is_rejected_immediately(self):
        manager = SimpleNamespace(enqueue=unittest.mock.Mock())
        self.bot_data.update(
            {
                "allowed_user_ids": {42},
                "job_manager": manager,
                "media_policy": SimpleNamespace(max_media_bytes=100 * 1024 * 1024),
            }
        )
        context = FakeContext(self.bot_data)
        message = FakeMessage()
        message.audio = SimpleNamespace(
            file_id="file-big", file_size=25 * 1024 * 1024, file_name="big.mp3"
        )
        message.voice = None
        message.document = None

        await handle_audio_message(make_update(message), context)

        manager.enqueue.assert_not_called()
        self.assertIn("20 MB", message.replies[-1][0])

    async def test_video_message_is_enqueued_as_media(self):
        manager = SimpleNamespace(
            enqueue=unittest.mock.Mock(
                return_value=TelegramJob(
                    job_id="video-job", sequence=1, user_id=42, chat_id=42,
                    source_type="audio",
                )
            ),
            queue_position=lambda job_id: 1,
        )
        self.bot_data.update(
            {
                "allowed_user_ids": {42},
                "job_manager": manager,
                "media_policy": SimpleNamespace(max_media_bytes=100 * 1024 * 1024),
            }
        )
        message = FakeMessage()
        message.audio = message.voice = message.document = None
        message.video = SimpleNamespace(file_id="vid", file_size=2048, file_name=None)

        await handle_audio_message(make_update(message), FakeContext(self.bot_data))

        kwargs = manager.enqueue.call_args.kwargs
        self.assertEqual(kwargs["telegram_file_id"], "vid")
        self.assertEqual(kwargs["original_filename"], "video.mp4")

    async def test_full_queue_is_reported_on_status_card(self):
        from telegram_jobs import QueueFull

        manager = SimpleNamespace(
            enqueue=unittest.mock.Mock(side_effect=QueueFull("任务队列已满，请稍后再试。"))
        )
        self.bot_data.update({"allowed_user_ids": {42}, "job_manager": manager})
        message = FakeMessage("https://www.youtube.com/watch?v=abc")

        await handle_text_message(make_update(message), FakeContext(self.bot_data))

        card = message.replies[0][2]
        self.assertIn("队列已满", card.text)
        self.assertIn("未加入队列", card.text)

    async def test_unsupported_message_gets_guidance(self):
        self.bot_data["allowed_user_ids"] = {42}
        message = FakeMessage()

        await handle_unsupported_message(make_update(message), FakeContext(self.bot_data))

        self.assertIn("暂不支持", message.replies[0][0])

    def _audio_job_application(self, service, bot):
        manager = SimpleNamespace(
            stages=[],
            update_stage=lambda job_id, stage: manager.stages.append(stage),
            set_audio_path=lambda job_id, path: None,
            update_delivery=lambda job_id, **changes: None,
        )
        uploads = self.root / "uploads"
        uploads.mkdir(exist_ok=True)
        return manager, SimpleNamespace(
            bot_data={
                "transcription_service": service,
                "media_policy": SimpleNamespace(
                    task_timeout_seconds=10, max_media_bytes=100 * 1024 * 1024
                ),
                "job_manager": manager,
                "paths": SimpleNamespace(uploads_dir=uploads),
                "result_store": ResultStore(self.root / "results"),
            },
            bot=bot,
        )

    async def test_execute_job_downloads_audio_in_worker(self):
        requests = []

        class FakeService:
            async def execute_async(self, request, **kwargs):
                requests.append(request)
                return TranscriptionResult("transcript", "result")

        async def download_to_drive(custom_path):
            Path(custom_path).write_bytes(b"audio")

        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=5, download_to_drive=download_to_drive
                )
            ),
        )
        manager, application = self._audio_job_application(FakeService(), bot)
        job = TelegramJob(
            job_id="job-id",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="audio",
            telegram_file_id="file-abc",
            original_filename="meeting.m4a",
        )

        await _execute_job(application, job, lambda: False)

        bot.get_file.assert_awaited_once_with("file-abc")
        self.assertIn("downloading", manager.stages)
        self.assertTrue(requests[0].audio_path.is_file())

    async def test_execute_job_throttles_progress_edits(self):
        class FakeService:
            async def execute_async(self, request, *, on_status, on_progress, **kwargs):
                on_status("transcribing")
                for count in range(1, 51):
                    on_progress(count, "<b>" + "字" * count)
                return TranscriptionResult("transcript", "result")

        bot = SimpleNamespace(edit_message_text=AsyncMock())
        _manager, application = self._audio_job_application(FakeService(), bot)
        application.bot_data["progress_interval_seconds"] = 3600
        job = TelegramJob(
            job_id="job-id", sequence=1, user_id=42, chat_id=42,
            source_type="youtube", text_input="https://youtu.be/x",
            status_message_id=5, settings_snapshot={"model_name": "m-1"},
        )

        await _execute_job(application, job, lambda: False)
        await asyncio.sleep(0)

        # The stage change edits at once; 50 chunks inside the interval add
        # nothing, and nothing edits the card after the job returns.
        self.assertLessEqual(bot.edit_message_text.await_count, 1)

    def test_progress_card_shows_real_counters_only(self):
        job = TelegramJob(
            job_id="abcdef123456", sequence=1, user_id=1, chat_id=1,
            source_type="youtube", settings_snapshot={"model_name": "m-1"},
        )
        text = render_job_progress(
            job, "transcribing", elapsed_seconds=65, characters=1234,
            tail="x" * 300 + "\nline <script>",
        )
        self.assertIn("正在转写", text)
        self.assertIn("m-1", text)
        self.assertIn("1分05秒", text)
        self.assertIn("1234 字", text)
        self.assertIn("line &lt;script&gt;", text)
        self.assertNotIn("%", text)
        preview = text.split("<blockquote>")[1]
        self.assertLessEqual(len(preview), 160)

    async def test_execute_job_download_failure_is_reported_at_download_stage(self):
        class FakeService:
            async def execute_async(self, request, **kwargs):
                raise AssertionError("must not transcribe")

        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            get_file=AsyncMock(side_effect=BadRequest("File is too big")),
        )
        _manager, application = self._audio_job_application(FakeService(), bot)
        job = TelegramJob(
            job_id="job-id",
            sequence=1,
            user_id=42,
            chat_id=42,
            source_type="audio",
            telegram_file_id="file-abc",
        )

        with self.assertRaises(JobExecutionFailure) as raised:
            await _execute_job(application, job, lambda: False)

        self.assertEqual(raised.exception.stage, "downloading")
        self.assertEqual(raised.exception.error_code, "media_too_large")
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    async def test_execute_job_persists_result_and_leaves_delivery_to_worker(self):
        class FakeService:
            async def execute_async(self, request, **kwargs):
                kwargs["on_status"]("transcribing")
                return TranscriptionResult(
                    "transcript", "result", finish_reason="MAX_TOKENS"
                )

        class FakeManager:
            def __init__(self):
                self.stages = []
                self.delivery = {}

            def update_stage(self, job_id, stage):
                self.stages.append(stage)

            def update_delivery(self, job_id, **changes):
                self.delivery.update(changes)

        manager = FakeManager()
        store = ResultStore(self.root / "results")
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
        application = SimpleNamespace(
            bot_data={
                "transcription_service": FakeService(),
                "media_policy": SimpleNamespace(task_timeout_seconds=10),
                "job_manager": manager,
                "result_store": store,
            },
            bot=bot,
        )
        job = TelegramJob(
            job_id="job-id", sequence=1, user_id=42, chat_id=42, source_type="youtube"
        )

        result = await _execute_job(application, job, lambda: False)

        self.assertEqual(result.transcript, "transcript")
        stored = store.load("job-id")
        self.assertEqual(stored.transcript, "transcript")
        self.assertTrue(stored.incomplete)
        self.assertEqual(manager.delivery["delivery_status"], "pending")
        self.assertNotIn("delivering", manager.stages)
        bot.send_message.assert_not_awaited()

    def _delivery_application(self, job, transcript, bot, finish_reason=""):
        class FakeDeliveryManager:
            def __init__(self, job):
                self.job = job

            def get(self, job_id):
                return replace(self.job) if job_id == self.job.job_id else None

            def update_delivery(self, job_id, **changes):
                for key, value in changes.items():
                    setattr(self.job, key, value)
                return replace(self.job)

            def undelivered_jobs(self):
                if self.job.delivery_status in {"pending", "sending"}:
                    return [replace(self.job)]
                return []

        store = ResultStore(self.root / "results")
        store.save(
            job.job_id, transcript, filename_stem="talk", finish_reason=finish_reason
        )
        manager = FakeDeliveryManager(job)
        application = SimpleNamespace(
            bot=bot,
            bot_data={
                "job_manager": manager,
                "result_store": store,
                "chat_sender": ChatSender(min_interval_seconds=0, sleep=AsyncMock()),
                "allowed_user_ids": {42},
                **{key: value for key, value in self.bot_data.items() if key != "allowed_user_ids"},
            },
        )
        return manager, application

    @staticmethod
    def _recording_bot(fail_at=()):
        sent = []
        attempts = {"count": 0}

        async def send_message(chat_id, text, **kwargs):
            attempts["count"] += 1
            if attempts["count"] in fail_at:
                raise BadRequest("Chat not found")
            sent.append(text)

        bot = SimpleNamespace(
            send_message=send_message,
            send_document=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        return bot, sent

    async def test_long_result_is_sent_in_full_across_multiple_messages(self):
        transcript = "第一段。" * 1000 + "\n\n" + "🙂" * 1500
        bot, sent = self._recording_bot()
        job = TelegramJob(
            job_id="long-result", sequence=1, user_id=42, chat_id=42,
            source_type="youtube", status="succeeded", status_message_id=7,
        )
        manager, application = self._delivery_application(job, transcript, bot)

        await _deliver_job(application, job.job_id)

        self.assertGreater(len(sent), 1)
        self.assertEqual("".join(sent), transcript)
        bot.send_document.assert_awaited_once()
        self.assertEqual(manager.job.delivery_status, "delivered")
        final_markup = bot.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in final_markup.inline_keyboard for b in row]
        self.assertIn("result:full:long-result", callbacks)

    async def test_failed_delivery_offers_resend_and_resumes_without_duplicates(self):
        transcript = "第一段。" * 1000 + "\n\n" + "第二段。" * 1000
        bot, sent = self._recording_bot(fail_at={2})
        job = TelegramJob(
            job_id="resend-me", sequence=1, user_id=42, chat_id=42,
            source_type="youtube", status="succeeded", status_message_id=7,
        )
        manager, application = self._delivery_application(job, transcript, bot)

        await _deliver_job(application, job.job_id)

        self.assertEqual(manager.job.delivery_status, "failed")
        self.assertEqual(manager.job.delivered_chunks, 1)
        bot.send_document.assert_not_awaited()
        edit = bot.edit_message_text.await_args.kwargs
        self.assertIn("发送失败", edit["text"])
        callbacks = [b.callback_data for row in edit["reply_markup"].inline_keyboard for b in row]
        self.assertIn("result:resend:resend-me", callbacks)

        query = SimpleNamespace(
            data="result:resend:resend-me",
            answer=AsyncMock(),
            message=FakeMessage(),
            edit_message_text=AsyncMock(),
        )
        update = make_update(query.message)
        update.callback_query = query
        context = FakeContext(application.bot_data)
        context.application = application
        await handle_callback_query(update, context)
        await asyncio.gather(*application.bot_data["delivery_tasks"].values())

        self.assertEqual("".join(sent), transcript)
        bot.send_document.assert_awaited_once()
        self.assertEqual(manager.job.delivery_status, "delivered")

    async def test_retry_after_is_honoured_during_delivery(self):
        attempts = []

        async def send_message(chat_id, text, **kwargs):
            attempts.append(text)
            if len(attempts) == 1:
                raise RetryAfter(3)

        bot = SimpleNamespace(
            send_message=send_message,
            send_document=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        job = TelegramJob(
            job_id="flood", sequence=1, user_id=42, chat_id=42,
            source_type="youtube", status="succeeded",
        )
        manager, application = self._delivery_application(job, "短结果", bot)

        await _deliver_job(application, job.job_id)

        self.assertEqual(attempts, ["短结果", "短结果"])
        sleep = application.bot_data["chat_sender"]._sleep
        sleep.assert_any_await(3.5)
        self.assertEqual(manager.job.delivery_status, "delivered")

    async def test_incomplete_result_is_flagged_on_status_card(self):
        bot, _sent = self._recording_bot()
        job = TelegramJob(
            job_id="cut", sequence=1, user_id=42, chat_id=42,
            source_type="youtube", status="succeeded", status_message_id=7,
        )
        _manager, application = self._delivery_application(
            job, "被截断的", bot, finish_reason="MAX_TOKENS"
        )

        await _deliver_job(application, job.job_id)

        self.assertIn("不完整", bot.edit_message_text.await_args.kwargs["text"])
        self.assertIn("不完整", bot.send_document.await_args.kwargs["caption"])

    async def test_restart_resumes_pending_delivery_from_saved_progress(self):
        transcript = "第一段。" * 1000 + "\n\n" + "第二段。" * 1000
        bot, sent = self._recording_bot()
        job = TelegramJob(
            job_id="resume", sequence=1, user_id=42, chat_id=42,
            source_type="youtube", status="succeeded",
            delivery_status="sending", delivered_chunks=1,
        )
        manager, application = self._delivery_application(job, transcript, bot)

        self.assertEqual(_resume_undelivered(application), 1)
        await asyncio.gather(*application.bot_data["delivery_tasks"].values())

        store = application.bot_data["result_store"]
        chunks = split_telegram_text(store.load("resume").transcript)
        self.assertEqual(sent, chunks[1:])
        self.assertEqual(manager.job.delivery_status, "delivered")

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

import unittest

from channel_health import ChannelHealthResult
from key_pool import GeminiKeyPool
from service_config import GlobalSettings
from telegram_delivery import StoredResult
from telegram_bot import (
    BOT_COMMANDS,
    MAX_TELEGRAM_TEXT,
    build_help_text,
    build_home_keyboard,
    build_model_keyboard,
    build_settings_keyboard,
    render_channel_health,
    render_job_failure,
    render_result_completion,
    render_settings,
    split_telegram_text,
)


class TelegramBotFormattingTest(unittest.TestCase):
    def test_render_settings_masks_global_credentials(self):
        settings = GlobalSettings(
            auth_mode="vertex_ai_json",
            gemini_api_keys=["abcd12345678", "second-secret-key"],
            vertex_json='{"type":"service_account"}',
            vertex_project="demo-project",
            vertex_location="us-central1",
            model_name="gemini-2.5-flash",
            prompt_append="自定义 Prompt",
        )
        pool = GeminiKeyPool(settings.gemini_api_keys)

        rendered = render_settings(settings, pool.statuses())

        self.assertIn("<b>全局设置</b>", rendered)
        self.assertIn("Gemini Key：2 个", rendered)
        self.assertIn("abcd...5678", rendered)
        self.assertNotIn("abcd12345678", rendered)
        self.assertNotIn("second-secret-key", rendered)
        self.assertIn("Prompt：附加要求", rendered)

    def test_build_help_text_formats_commands_without_raw_backticks(self):
        help_text = build_help_text()

        self.assertIn("直接发送", help_text)
        self.assertIn("<code>/settings</code>", help_text)
        self.assertNotIn("/setsource", help_text)
        self.assertNotIn("video_url", help_text)

    def test_command_menu_is_minimal(self):
        self.assertEqual(
            [command.command for command in BOT_COMMANDS],
            ["start", "settings", "help", "cancel"],
        )

    def test_inline_navigation_contains_expected_actions(self):
        home_callbacks = {
            button.callback_data
            for row in build_home_keyboard().inline_keyboard
            for button in row
        }
        self.assertEqual(home_callbacks, {"home:status", "settings", "queue", "help"})

        settings_callbacks = {
            button.callback_data
            for row in build_settings_keyboard().inline_keyboard
            for button in row
        }
        self.assertTrue(
            {
                "settings:keys",
                "settings:model",
                "settings:prompt",
                "settings:vertex",
                "channel:test",
                "home",
            }
            <= settings_callbacks
        )

    def test_health_result_names_current_model_and_never_renders_log_detail(self):
        success = ChannelHealthResult(
            available=True,
            auth_mode="vertex_ai_json",
            model="gemini-current",
            location="global",
            latency_ms=123,
            code="ok",
            user_message="渠道可用。",
        )
        failure = ChannelHealthResult(
            available=False,
            auth_mode="vertex_ai_json",
            model="gemini-current",
            location="global",
            latency_ms=456,
            code="billing_disabled",
            user_message="Google Cloud 项目未启用结算。",
            error_type="ClientError",
            log_detail="https://secret.example/get?token=hidden",
        )

        rendered_success = render_channel_health(success)
        rendered_failure = render_channel_health(failure)

        self.assertIn("当前渠道可用", rendered_success)
        self.assertIn("gemini-current", rendered_success)
        self.assertIn("global", rendered_success)
        self.assertIn("当前渠道不可用", rendered_failure)
        self.assertIn("未启用结算", rendered_failure)
        self.assertNotIn("secret.example", rendered_failure)
        self.assertNotIn("token=", rendered_failure)
        self.assertNotIn("本次未测试", rendered_success)

    def test_failure_renderer_includes_safe_localized_stage(self):
        rendered = render_job_failure(
            "extracting",
            "下载内容不是可解析的音视频，可能是分享页而非媒体直链。",
        )
        self.assertIn("抽取音频", rendered)
        self.assertIn("分享页", rendered)
        self.assertNotIn("ffmpeg stderr", rendered)

    def test_vertex_blank_location_renders_global(self):
        rendered = render_settings(
            GlobalSettings(auth_mode="vertex_ai_json", vertex_location="")
        )
        self.assertIn("Vertex Location：<code>global</code>", rendered)

    def test_model_keyboard_is_paginated_and_keeps_manual_fallback(self):
        models = [f"models/model-{index}" for index in range(9)]
        markup = build_model_keyboard(models, page=0, page_size=4)
        callbacks = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        self.assertIn("model:set:0", callbacks)
        self.assertIn("model:page:1", callbacks)
        self.assertIn("model:manual", callbacks)

    def test_completion_warns_when_result_may_be_truncated(self):
        complete = render_result_completion(StoredResult("x", "a", "STOP"))
        truncated = render_result_completion(StoredResult("x", "a", "MAX_TOKENS"))

        self.assertNotIn("不完整", complete)
        self.assertIn("不完整", truncated)
        self.assertIn("MAX_TOKENS", truncated)

    def test_split_telegram_text_preserves_every_character(self):
        transcript = "甲" * 2900 + "\n\n" + "乙" * 2500 + "🙂" * 900

        chunks = split_telegram_text(transcript)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), transcript)
        self.assertTrue(chunks[0].endswith("\n\n"))
        self.assertTrue(
            all(
                sum(2 if ord(character) > 0xFFFF else 1 for character in chunk)
                <= MAX_TELEGRAM_TEXT
                for chunk in chunks
            )
        )


if __name__ == "__main__":
    unittest.main()

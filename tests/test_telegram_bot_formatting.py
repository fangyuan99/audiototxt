import unittest

from bot_state import UserSettings
from telegram_bot import build_help_text, render_settings


class TelegramBotFormattingTest(unittest.TestCase):
    def test_render_settings_uses_prompt_label_and_html_code(self):
        settings = UserSettings(
            user_id=1,
            auth_mode="vertex_ai_json",
            api_key="abcd12345678",
            vertex_json='{"type":"service_account"}',
            vertex_project="demo-project",
            vertex_location="us-central1",
            model_name="gemini-2.5-flash",
            source_type="video_url",
            promoters="自定义 Prompt",
        )

        rendered = render_settings(settings)

        self.assertIn("<b>当前配置：</b>", rendered)
        self.assertIn("- Prompt: 已自定义（10 字符）", rendered)
        self.assertIn("<code>video_url</code>", rendered)
        self.assertNotIn("Promoters", rendered)

    def test_build_help_text_formats_commands_without_raw_backticks(self):
        help_text = build_help_text()

        self.assertIn("<code>/setprompt</code>", help_text)
        self.assertIn("<code>video_url</code>", help_text)
        self.assertNotIn("`/setprompt`", help_text)


if __name__ == "__main__":
    unittest.main()

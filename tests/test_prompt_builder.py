import unittest

from main import build_transcription_prompt


class PromptBuilderTest(unittest.TestCase):
    def test_uses_default_prompt_when_promoters_missing(self):
        prompt = build_transcription_prompt(language_hint="zh", promoters=None)
        self.assertIn("你是一名专业的听打员", prompt)
        self.assertIn("主要语言：zh", prompt)

    def test_uses_custom_promoters_when_present(self):
        prompt = build_transcription_prompt(language_hint="en", promoters="只输出英文逐字稿。")
        self.assertIn("你是一名专业的听打员", prompt)
        self.assertIn("附加要求：\n只输出英文逐字稿。", prompt)
        self.assertIn("主要语言：en", prompt)

    def test_defaults_to_original_language_without_literal_quote_artifacts(self):
        prompt = build_transcription_prompt(language_hint=None)
        self.assertIn("主要语言：按音频原语言", prompt)
        self.assertNotIn('：\\n"', prompt)

    def test_explicit_full_override_replaces_base_prompt(self):
        prompt = build_transcription_prompt(
            language_hint="ja",
            promoters="ignored append",
            full_override="完整高级规则",
        )
        self.assertTrue(prompt.startswith("完整高级规则"))
        self.assertNotIn("你是一名专业的听打员", prompt)


if __name__ == "__main__":
    unittest.main()

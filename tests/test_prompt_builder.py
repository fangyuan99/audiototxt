import unittest

from main import build_transcription_prompt


class PromptBuilderTest(unittest.TestCase):
    def test_uses_default_prompt_when_promoters_missing(self):
        prompt = build_transcription_prompt(language_hint="zh", promoters=None)
        self.assertIn("你是一名专业的听打员", prompt)
        self.assertIn("主要语言：zh", prompt)

    def test_uses_custom_promoters_when_present(self):
        prompt = build_transcription_prompt(language_hint="en", promoters="只输出英文逐字稿。")
        self.assertTrue(prompt.startswith("只输出英文逐字稿。"))
        self.assertIn("主要语言：en", prompt)


if __name__ == "__main__":
    unittest.main()

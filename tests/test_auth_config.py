import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import (
    AUTH_MODE_GEMINI_API_KEY,
    AUTH_MODE_VERTEX_AI_JSON,
    build_auth_config,
    normalize_auth_mode,
)


class AuthConfigTest(unittest.TestCase):
    def test_normalize_auth_mode_aliases(self):
        self.assertEqual(normalize_auth_mode("gemini"), AUTH_MODE_GEMINI_API_KEY)
        self.assertEqual(normalize_auth_mode("vertex-ai"), AUTH_MODE_VERTEX_AI_JSON)
        self.assertEqual(normalize_auth_mode("unknown"), AUTH_MODE_GEMINI_API_KEY)

    def test_build_auth_config_prefers_explicit_gemini_key(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-key", "GEMINI_API_KEY": "env-key-2"}, clear=False):
            config = build_auth_config(auth_mode="gemini_api_key", api_key="explicit-key")
        self.assertEqual(config.auth_mode, AUTH_MODE_GEMINI_API_KEY)
        self.assertEqual(config.api_key, "explicit-key")

    def test_build_auth_config_reads_vertex_json_from_env_file(self):
        payload = {"type": "service_account", "project_id": "demo-project"}
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "vertex.json"
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": str(json_path)}, clear=False):
                config = build_auth_config(auth_mode="vertex_ai_json")

        self.assertEqual(config.auth_mode, AUTH_MODE_VERTEX_AI_JSON)
        self.assertIn('"project_id": "demo-project"', config.vertex_json)


if __name__ == "__main__":
    unittest.main()

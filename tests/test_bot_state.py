import tempfile
import unittest
from pathlib import Path

from bot_state import (
    BotStateStore,
    DEFAULT_AUTH_MODE,
    DEFAULT_MODEL_NAME,
    DEFAULT_SOURCE_TYPE,
    DEFAULT_VERTEX_LOCATION,
)


class BotStateStoreTest(unittest.TestCase):
    def test_authorize_and_persist_user_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage_path = Path(tmp_dir) / "bot_state.json"
            store = BotStateStore(str(storage_path))

            settings = store.authorize_user(123, username="tester", first_name="Alice")
            self.assertTrue(settings.authorized)
            self.assertEqual(settings.username, "tester")
            self.assertEqual(settings.first_name, "Alice")
            self.assertEqual(settings.auth_mode, DEFAULT_AUTH_MODE)
            self.assertEqual(settings.model_name, DEFAULT_MODEL_NAME)
            self.assertEqual(settings.source_type, DEFAULT_SOURCE_TYPE)
            self.assertEqual(settings.vertex_location, DEFAULT_VERTEX_LOCATION)

            store.upsert_user(
                123,
                auth_mode="vertex_ai_json",
                api_key="abc123456",
                vertex_json='{"type":"service_account"}',
                vertex_project="demo-project",
                vertex_location="us-central1",
                model_name="gemini-custom",
                source_type="youtube",
            )
            reloaded_store = BotStateStore(str(storage_path))
            reloaded = reloaded_store.get_user(123)

            self.assertEqual(reloaded.auth_mode, "vertex_ai_json")
            self.assertEqual(reloaded.api_key, "abc123456")
            self.assertEqual(reloaded.vertex_json, '{"type":"service_account"}')
            self.assertEqual(reloaded.vertex_project, "demo-project")
            self.assertEqual(reloaded.vertex_location, "us-central1")
            self.assertEqual(reloaded.model_name, "gemini-custom")
            self.assertEqual(reloaded.source_type, "youtube")
            self.assertTrue(reloaded.authorized)


if __name__ == "__main__":
    unittest.main()

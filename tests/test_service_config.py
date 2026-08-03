import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from service_config import (
    BotPaths,
    GlobalConfigStore,
    migrate_legacy_user_settings,
    parse_api_keys,
)


class ServiceConfigTest(unittest.TestCase):
    def test_parse_api_keys_deduplicates_in_order(self):
        self.assertEqual(
            parse_api_keys(" key-a,key-b, key-a ,,key-c "),
            ["key-a", "key-b", "key-c"],
        )

    def test_environment_fallback_and_stored_precedence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "global.json"
            store = GlobalConfigStore(
                path,
                environ={
                    "GOOGLE_API_KEYS": "env-a,env-b",
                    "GOOGLE_API_KEY": "single",
                },
            )

            self.assertEqual(store.get().gemini_api_keys, ["env-a", "env-b"])

            store.replace_api_keys(["stored-a", "stored-b"])
            self.assertEqual(store.get().gemini_api_keys, ["stored-a", "stored-b"])

    def test_secure_store_writes_atomically_with_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "nested" / "global.json"
            store = GlobalConfigStore(path, environ={})

            store.replace_api_keys(["secret-a"])
            store.append_api_keys(["secret-b", "secret-a"])

            self.assertEqual(store.get().gemini_api_keys, ["secret-a", "secret-b"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_migration_merges_keys_and_uses_latest_global_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_path = root / "state.json"
            config_path = root / "global.json"
            state_path.write_text(
                json.dumps(
                    {
                        "users": {
                            "20": {
                                "authorized": True,
                                "updated_at": "2026-01-02T00:00:00+00:00",
                                "auth_mode": "vertex_ai_json",
                                "api_key": "key-new",
                                "vertex_json": '{"project_id":"new"}',
                                "vertex_project": "new-project",
                                "vertex_location": "asia-east1",
                                "model_name": "model-new",
                                "promoters": "prompt-new",
                            },
                            "10": {
                                "authorized": True,
                                "updated_at": "2026-01-01T00:00:00+00:00",
                                "auth_mode": "gemini_api_key",
                                "api_key": "key-old",
                                "vertex_json": '{"project_id":"old"}',
                                "vertex_project": "old-project",
                                "vertex_location": "us-central1",
                                "model_name": "model-old",
                                "promoters": "prompt-old",
                            },
                            "30": {
                                "authorized": False,
                                "updated_at": "2026-01-03T00:00:00+00:00",
                                "api_key": "key-unauthorized",
                                "model_name": "wrong-model",
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.chmod(state_path, 0o644)
            store = GlobalConfigStore(config_path, environ={})

            migrated = migrate_legacy_user_settings(state_path, store)

            self.assertTrue(migrated)
            settings = store.get()
            self.assertEqual(settings.gemini_api_keys, ["key-new", "key-old"])
            self.assertEqual(settings.auth_mode, "vertex_ai_json")
            self.assertEqual(settings.model_name, "model-new")
            self.assertEqual(settings.prompt_append, "prompt-new")
            self.assertEqual(settings.vertex_project, "new-project")
            self.assertEqual(settings.vertex_location, "asia-east1")
            self.assertEqual(settings.vertex_json, '{"project_id":"new"}')

            scrubbed = json.loads(state_path.read_text(encoding="utf-8"))
            for user in scrubbed["users"].values():
                self.assertEqual(user.get("api_key", ""), "")
                self.assertEqual(user.get("vertex_json", ""), "")
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertFalse(migrate_legacy_user_settings(state_path, store))

    def test_failed_global_save_does_not_scrub_legacy_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "users": {
                            "1": {
                                "authorized": True,
                                "updated_at": "2026-01-01T00:00:00+00:00",
                                "api_key": "keep-me",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            class FailingStore:
                def get(self):
                    return GlobalConfigStore(root / "unused.json", environ={}).get()

                def update(self, **changes):
                    raise OSError("disk full")

            with self.assertRaises(OSError):
                migrate_legacy_user_settings(state_path, FailingStore())

            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(data["users"]["1"]["api_key"], "keep-me")

    def test_bot_paths_are_relocated_and_overrides_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = Path(tmp_dir) / "project"
            project.mkdir()
            data_root = Path(tmp_dir) / "isolated"
            paths = BotPaths.from_environ(
                project,
                environ={"BOT_DATA_DIR": str(data_root)},
            )
            self.assertEqual(paths.state_file, data_root / "state.json")
            self.assertEqual(paths.global_config_file, data_root / "global_config.json")
            self.assertEqual(paths.jobs_file, data_root / "jobs.json")

            with self.assertRaises(ValueError):
                BotPaths.from_environ(
                    project,
                    environ={
                        "BOT_DATA_DIR": str(data_root),
                        "BOT_STATE_FILE": str(Path(tmp_dir) / "live-state.json"),
                    },
                )


if __name__ == "__main__":
    unittest.main()

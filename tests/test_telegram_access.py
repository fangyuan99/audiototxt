import tempfile
import unittest
from pathlib import Path

from bot_state import BotStateStore, fingerprint_secret, parse_user_ids


class TelegramAccessTest(unittest.TestCase):
    def test_parse_user_ids_ignores_empty_values(self):
        self.assertEqual(parse_user_ids(" 123,456,,123 "), {123, 456})

    def test_allowlist_bypasses_password_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = BotStateStore(str(Path(tmp_dir) / "state.json"))
            self.assertTrue(
                store.is_user_authorized(
                    42,
                    current_secret="secret",
                    allowed_user_ids={42},
                )
            )

    def test_password_change_invalidates_non_allowlisted_user(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = BotStateStore(str(Path(tmp_dir) / "state.json"))
            store.authorize_user(42, secret="old-secret")

            self.assertTrue(
                store.is_user_authorized(42, current_secret="old-secret")
            )
            self.assertFalse(
                store.is_user_authorized(42, current_secret="new-secret")
            )

    def test_legacy_authorization_can_be_bound_to_current_secret_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = BotStateStore(str(Path(tmp_dir) / "state.json"))
            store.upsert_user(42, authorized=True, auth_secret_fingerprint="")

            changed = store.bind_legacy_authorizations("current-secret")

            self.assertEqual(changed, 1)
            self.assertEqual(
                store.get_user(42).auth_secret_fingerprint,
                fingerprint_secret("current-secret"),
            )
            self.assertEqual(store.bind_legacy_authorizations("other-secret"), 0)


if __name__ == "__main__":
    unittest.main()

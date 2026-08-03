import os
import tempfile
import time
import unittest
from pathlib import Path

from retention import (
    LEGACY_MEDIA_CLEANUP_MIGRATION,
    cleanup_expired_media,
    cleanup_expired_outputs,
    run_legacy_media_cleanup,
)
from service_config import BotPaths, GlobalConfigStore


class RetentionTest(unittest.TestCase):
    def test_legacy_cleanup_removes_uploads_and_starts_seven_day_output_window(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = BotPaths.from_environ(root, {"BOT_DATA_DIR": str(root / "bot")})
            paths.ensure_directories()
            old_upload = paths.uploads_dir / "nested" / "old.mp3"
            old_upload.parent.mkdir()
            old_upload.write_bytes(b"123")
            recent_text = paths.outputs_dir / "recent.txt"
            old_text = paths.outputs_dir / "old.txt"
            recent_text.write_text("recent", encoding="utf-8")
            old_text.write_text("old", encoding="utf-8")
            now = time.time()
            os.utime(old_text, (now - 8 * 86400, now - 8 * 86400))
            store = GlobalConfigStore(paths.global_config_file, environ={})

            stats = run_legacy_media_cleanup(paths, store, now=now)

            self.assertFalse(old_upload.exists())
            self.assertTrue(old_text.exists())
            self.assertTrue(recent_text.exists())
            self.assertEqual(stats.deleted_files, 1)
            self.assertIn(LEGACY_MEDIA_CLEANUP_MIGRATION, store.get().migrations)

            output_stats = cleanup_expired_outputs(
                paths,
                max_age_seconds=7 * 86400,
                now=now + 7 * 86400 + 1,
            )
            self.assertFalse(old_text.exists())
            self.assertFalse(recent_text.exists())
            self.assertEqual(output_stats.deleted_files, 2)

            second = run_legacy_media_cleanup(paths, store, now=now)
            self.assertEqual(second.deleted_files, 0)

    def test_cleanup_expired_media_preserves_active_recent_and_external_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = BotPaths.from_environ(root, {"BOT_DATA_DIR": str(root / "bot")})
            paths.ensure_directories()
            now = time.time()
            active = paths.uploads_dir / "active.mp3"
            old = paths.uploads_dir / "old.mp3"
            recent = paths.uploads_dir / "recent.mp3"
            external = root / "external.mp3"
            for path in (active, old, recent, external):
                path.write_bytes(b"x")
            for path in (active, old, external):
                os.utime(path, (now - 25 * 3600, now - 25 * 3600))

            stats = cleanup_expired_media(
                paths,
                active_paths={active, external},
                max_age_seconds=24 * 3600,
                now=now,
            )

            self.assertTrue(active.exists())
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(external.exists())
            self.assertEqual(stats.deleted_files, 1)

    def test_cleanup_does_not_follow_symlink_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = BotPaths.from_environ(root, {"BOT_DATA_DIR": str(root / "bot")})
            paths.ensure_directories()
            external = root / "external.mp3"
            external.write_bytes(b"secret")
            link = paths.uploads_dir / "linked.mp3"
            link.symlink_to(external)

            cleanup_expired_media(
                paths,
                active_paths=set(),
                max_age_seconds=0,
                now=time.time() + 1,
            )

            self.assertTrue(external.exists())


if __name__ == "__main__":
    unittest.main()

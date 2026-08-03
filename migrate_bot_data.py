from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from bot_state import BotStateStore
from retention import run_legacy_media_cleanup
from service_config import BotPaths, GlobalConfigStore, migrate_legacy_user_settings


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


def _file_count_and_bytes(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    if not root.exists():
        return count, size
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        count += 1
        size += path.stat().st_size
    return count, size


def migrate(data_dir: Path) -> dict[str, object]:
    environ = dict(os.environ)
    environ["BOT_DATA_DIR"] = str(data_dir.resolve())
    # A CLI-selected root must relocate every file; inherited overrides could
    # otherwise point a dry run back at live state.
    for name in ("BOT_STATE_FILE", "BOT_GLOBAL_CONFIG_FILE", "BOT_JOBS_FILE"):
        environ.pop(name, None)

    paths = BotPaths.from_environ(ROOT_DIR, environ)
    paths.ensure_directories()
    before_count, before_bytes = _file_count_and_bytes(paths.data_dir)
    config_store = GlobalConfigStore(paths.global_config_file, environ=environ)
    settings_migrated = migrate_legacy_user_settings(paths.state_file, config_store)
    cleanup = run_legacy_media_cleanup(paths, config_store)
    state_store = BotStateStore(str(paths.state_file))
    bound_users = state_store.bind_legacy_authorizations(
        environ.get("ENV_BOT_SECRET", "").strip()
    )
    settings = config_store.get()
    after_count, after_bytes = _file_count_and_bytes(paths.data_dir)

    def mode(path: Path):
        return oct(path.stat().st_mode & 0o777) if path.exists() else None

    return {
        "data_dir": str(paths.data_dir),
        "settings_migrated": settings_migrated,
        "legacy_authorizations_bound": bound_users,
        "gemini_key_count": len(settings.gemini_api_keys),
        "vertex_configured": bool(settings.vertex_json),
        "deleted_file_count": cleanup.deleted_files,
        "deleted_bytes": cleanup.deleted_bytes,
        "before_file_count": before_count,
        "before_bytes": before_bytes,
        "after_file_count": after_count,
        "after_bytes": after_bytes,
        "state_mode": mode(paths.state_file),
        "global_config_mode": mode(paths.global_config_file),
        "migration_markers": sorted(settings.migrations),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Telegram bot state and apply the one-time retention policy."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("BOT_DATA_DIR", ROOT_DIR / "data" / "telegram_bot")),
        help="Telegram data root. Use a copied directory for a dry run.",
    )
    parser.add_argument(
        "--dry-run-from",
        type=Path,
        help="Copy this data root into a temporary directory, migrate the copy, then remove it.",
    )
    args = parser.parse_args()
    if args.dry_run_from:
        source = args.dry_run_from.resolve()
        with tempfile.TemporaryDirectory(prefix="audiototxt-migration-") as tmp_dir:
            copied = Path(tmp_dir) / "telegram_bot"
            if source.exists():
                shutil.copytree(source, copied, symlinks=True)
            result = migrate(copied)
            result["dry_run"] = True
    else:
        result = migrate(args.data_dir)
        result["dry_run"] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

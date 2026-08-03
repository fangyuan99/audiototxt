from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from service_config import BotPaths, GlobalConfigStore


LEGACY_MEDIA_CLEANUP_MIGRATION = "legacy_media_cleanup_v1"


@dataclass(frozen=True)
class CleanupStats:
    deleted_files: int = 0
    deleted_bytes: int = 0


def _is_contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _delete_file(path: Path, root: Path) -> tuple[int, int]:
    if path.is_symlink() or not _is_contained(path, root):
        return (0, 0)
    try:
        if not path.is_file():
            return (0, 0)
        size = path.stat().st_size
        path.unlink()
        return (1, size)
    except OSError:
        return (0, 0)


def _remove_empty_directories(root: Path) -> None:
    if not root.exists():
        return
    directories = sorted(
        (item for item in root.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def run_legacy_media_cleanup(
    paths: BotPaths,
    config_store: GlobalConfigStore,
    *,
    now: Optional[float] = None,
    output_retention_seconds: float = 7 * 86400,
) -> CleanupStats:
    settings = config_store.get()
    if LEGACY_MEDIA_CLEANUP_MIGRATION in settings.migrations:
        return CleanupStats()
    current_time = time.time() if now is None else now
    deleted_files = 0
    deleted_bytes = 0

    for candidate in list(paths.uploads_dir.rglob("*")):
        count, size = _delete_file(candidate, paths.data_dir)
        deleted_files += count
        deleted_bytes += size

    # Give every pre-migration transcript a fresh seven-day recovery window.
    for candidate in list(paths.outputs_dir.rglob("*")):
        if candidate.is_symlink() or not _is_contained(candidate, paths.data_dir):
            continue
        try:
            if candidate.is_file():
                os.utime(candidate, (current_time, current_time))
        except OSError:
            pass

    _remove_empty_directories(paths.uploads_dir)
    _remove_empty_directories(paths.outputs_dir)
    config_store.update(
        migrations=list(settings.migrations) + [LEGACY_MEDIA_CLEANUP_MIGRATION]
    )
    return CleanupStats(deleted_files, deleted_bytes)


def cleanup_expired_outputs(
    paths: BotPaths,
    *,
    max_age_seconds: float = 7 * 86400,
    now: Optional[float] = None,
) -> CleanupStats:
    current_time = time.time() if now is None else now
    deleted_files = 0
    deleted_bytes = 0
    if not paths.outputs_dir.exists():
        return CleanupStats()
    for candidate in list(paths.outputs_dir.rglob("*")):
        if candidate.is_symlink() or not _is_contained(candidate, paths.data_dir):
            continue
        try:
            expired = candidate.is_file() and (
                current_time - candidate.stat().st_mtime
            ) > max_age_seconds
        except OSError:
            continue
        if not expired:
            continue
        count, size = _delete_file(candidate, paths.data_dir)
        deleted_files += count
        deleted_bytes += size
    _remove_empty_directories(paths.outputs_dir)
    return CleanupStats(deleted_files, deleted_bytes)


def cleanup_expired_media(
    paths: BotPaths,
    *,
    active_paths: Iterable[str | Path],
    max_age_seconds: float = 24 * 3600,
    now: Optional[float] = None,
) -> CleanupStats:
    current_time = time.time() if now is None else now
    active: set[Path] = set()
    for value in active_paths:
        try:
            candidate = Path(value).resolve()
            candidate.relative_to(paths.data_dir.resolve())
        except (OSError, ValueError):
            continue
        active.add(candidate)

    deleted_files = 0
    deleted_bytes = 0
    for root in (paths.uploads_dir, paths.failed_dir):
        if not root.exists():
            continue
        for candidate in list(root.rglob("*")):
            if candidate.is_symlink() or not _is_contained(candidate, paths.data_dir):
                continue
            try:
                resolved = candidate.resolve()
                expired = candidate.is_file() and (
                    current_time - candidate.stat().st_mtime
                ) > max_age_seconds
            except OSError:
                continue
            if resolved in active or not expired:
                continue
            count, size = _delete_file(candidate, paths.data_dir)
            deleted_files += count
            deleted_bytes += size
        _remove_empty_directories(root)
    return CleanupStats(deleted_files, deleted_bytes)

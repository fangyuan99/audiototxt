from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, MutableMapping, Optional


DEFAULT_AUTH_MODE = "gemini_api_key"
DEFAULT_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_VERTEX_LOCATION = "global"
CONFIG_VERSION = 1
LEGACY_USER_SETTINGS_MIGRATION = "legacy_user_settings_v1"


def parse_api_keys(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = value

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate or "").strip()
        if not key or key in seen:
            continue
        result.append(key)
        seen.add(key)
    return result


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


OUTPUT_SETTING_FIELDS = ("model_name", "language_hint", "prompt_append", "prompt_override")


@dataclass
class GlobalSettings:
    schema_version: int = CONFIG_VERSION
    auth_mode: str = DEFAULT_AUTH_MODE
    gemini_api_keys: list[str] = field(default_factory=list)
    vertex_json: str = ""
    vertex_project: str = ""
    vertex_location: str = DEFAULT_VERTEX_LOCATION
    model_name: str = DEFAULT_MODEL_NAME
    language_hint: str = ""
    prompt_append: str = ""
    prompt_override: str = ""
    migrations: list[str] = field(default_factory=list)
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, object]] = None) -> "GlobalSettings":
        raw = dict(data or {})
        allowed = {item.name for item in fields(cls)}
        payload = {key: value for key, value in raw.items() if key in allowed}
        payload["gemini_api_keys"] = parse_api_keys(payload.get("gemini_api_keys"))
        payload["migrations"] = [
            str(item) for item in (payload.get("migrations") or []) if str(item).strip()
        ]
        return cls(**payload)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["gemini_api_keys"] = parse_api_keys(self.gemini_api_keys)
        return payload

    def output_snapshot(self) -> dict[str, str]:
        """Settings that shape the transcript, frozen onto a job at submit.

        Credentials are deliberately excluded so jobs never store secrets.
        """
        return {name: str(getattr(self, name) or "") for name in OUTPUT_SETTING_FIELDS}

    def with_output_snapshot(self, snapshot: Optional[Mapping[str, object]]) -> "GlobalSettings":
        if not snapshot:
            return self
        changes = {
            name: str(snapshot[name] or "")
            for name in OUTPUT_SETTING_FIELDS
            if name in snapshot
        }
        if not changes.get("model_name", "x"):
            changes.pop("model_name")
        return replace(self, **changes)


@dataclass(frozen=True)
class BotPaths:
    data_dir: Path
    uploads_dir: Path
    outputs_dir: Path
    failed_dir: Path
    state_file: Path
    global_config_file: Path
    jobs_file: Path

    @staticmethod
    def _resolve(value: str | Path, root_dir: Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root_dir / candidate
        return candidate.resolve()

    @classmethod
    def from_environ(
        cls,
        root_dir: str | Path,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "BotPaths":
        values = dict(os.environ if environ is None else environ)
        project_root = Path(root_dir).resolve()
        data_dir = cls._resolve(
            values.get("BOT_DATA_DIR", str(project_root / "data" / "telegram_bot")),
            project_root,
        )

        def child_path(env_name: str, default_name: str) -> Path:
            raw = values.get(env_name)
            path = cls._resolve(raw, project_root) if raw else data_dir / default_name
            try:
                path.relative_to(data_dir)
            except ValueError as exc:
                raise ValueError(
                    f"{env_name} 必须位于 BOT_DATA_DIR 内：{path}"
                ) from exc
            return path

        return cls(
            data_dir=data_dir,
            uploads_dir=data_dir / "uploads",
            outputs_dir=data_dir / "outputs",
            failed_dir=data_dir / "failed",
            state_file=child_path("BOT_STATE_FILE", "state.json"),
            global_config_file=child_path(
                "BOT_GLOBAL_CONFIG_FILE", "global_config.json"
            ),
            jobs_file=child_path("BOT_JOBS_FILE", "jobs.json"),
        )

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.uploads_dir,
            self.outputs_dir,
            self.failed_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass


class GlobalConfigStore:
    def __init__(
        self,
        storage_path: str | Path,
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.storage_path = Path(storage_path)
        self.environ = dict(os.environ if environ is None else environ)
        self._lock = threading.RLock()

    def _read_stored_locked(self) -> GlobalSettings:
        if not self.storage_path.exists():
            return GlobalSettings()
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"全局配置文件无法读取：{self.storage_path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"全局配置文件格式无效：{self.storage_path}")
        try:
            os.chmod(self.storage_path, 0o600)
        except OSError:
            pass
        return GlobalSettings.from_dict(data)

    def _environment_api_keys(self) -> list[str]:
        for name in ("GOOGLE_API_KEYS", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
            keys = parse_api_keys(self.environ.get(name, ""))
            if keys:
                return keys
        return []

    def get(self) -> GlobalSettings:
        with self._lock:
            settings = self._read_stored_locked()
            if not settings.gemini_api_keys:
                settings.gemini_api_keys = self._environment_api_keys()
            if not settings.vertex_project:
                settings.vertex_project = (
                    self.environ.get("VERTEX_PROJECT")
                    or self.environ.get("GOOGLE_CLOUD_PROJECT")
                    or ""
                ).strip()
            if not settings.vertex_location:
                settings.vertex_location = (
                    self.environ.get("VERTEX_LOCATION")
                    or self.environ.get("GOOGLE_CLOUD_LOCATION")
                    or DEFAULT_VERTEX_LOCATION
                ).strip()
            return settings

    def update(self, **changes) -> GlobalSettings:
        allowed = {item.name for item in fields(GlobalSettings)}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"未知的全局配置字段：{', '.join(sorted(unknown))}")

        with self._lock:
            settings = self._read_stored_locked()
            if not settings.gemini_api_keys:
                settings.gemini_api_keys = self._environment_api_keys()
            for key, value in changes.items():
                if key == "gemini_api_keys":
                    value = parse_api_keys(value)
                elif key == "migrations":
                    value = list(dict.fromkeys(str(item) for item in (value or [])))
                setattr(settings, key, value)
            settings.schema_version = CONFIG_VERSION
            settings.updated_at = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(self.storage_path, settings.to_dict())
            return GlobalSettings.from_dict(settings.to_dict())

    def replace_api_keys(self, keys) -> GlobalSettings:
        return self.update(gemini_api_keys=parse_api_keys(keys))

    def append_api_keys(self, keys) -> GlobalSettings:
        with self._lock:
            current = self.get()
            return self.update(
                gemini_api_keys=parse_api_keys(
                    current.gemini_api_keys + parse_api_keys(keys)
                )
            )


def _parse_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def migrate_legacy_user_settings(
    state_path: str | Path,
    config_store: GlobalConfigStore,
) -> bool:
    path = Path(state_path)
    current = config_store.get()
    if LEGACY_USER_SETTINGS_MIGRATION in current.migrations:
        return False
    if not path.exists():
        return False

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"旧 Telegram 状态文件无法读取：{path}") from exc
    users = state.get("users") if isinstance(state, dict) else None
    if not isinstance(users, MutableMapping) or not users:
        return False

    authorized_records: list[tuple[int, MutableMapping[str, object]]] = []
    for raw_user_id, raw_record in users.items():
        if not isinstance(raw_record, MutableMapping) or not raw_record.get("authorized"):
            continue
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            continue
        authorized_records.append((user_id, raw_record))
    authorized_records.sort(
        key=lambda item: (_parse_timestamp(item[1].get("updated_at")), -item[0]),
        reverse=True,
    )

    merged_keys = list(current.gemini_api_keys)
    for _, record in authorized_records:
        merged_keys.extend(parse_api_keys(record.get("api_key", "")))
    merged_keys = parse_api_keys(merged_keys)

    changes: dict[str, object] = {
        "gemini_api_keys": merged_keys,
        "migrations": list(current.migrations) + [LEGACY_USER_SETTINGS_MIGRATION],
    }
    if authorized_records:
        latest = authorized_records[0][1]
        changes.update(
            auth_mode=str(latest.get("auth_mode") or current.auth_mode),
            model_name=str(latest.get("model_name") or current.model_name),
            prompt_append=str(latest.get("promoters") or current.prompt_append),
        )

        for _, record in authorized_records:
            vertex_json = str(record.get("vertex_json") or "").strip()
            if not vertex_json:
                continue
            changes.update(
                vertex_json=vertex_json,
                vertex_project=str(record.get("vertex_project") or ""),
                vertex_location=str(
                    record.get("vertex_location") or DEFAULT_VERTEX_LOCATION
                ),
            )
            break

    config_store.update(**changes)
    verified = config_store.get()
    if LEGACY_USER_SETTINGS_MIGRATION not in verified.migrations:
        raise RuntimeError("旧配置迁移验证失败，未清理旧凭据。")
    if any(key not in verified.gemini_api_keys for key in merged_keys):
        raise RuntimeError("旧 Gemini Key 迁移验证失败，未清理旧凭据。")

    for record in users.values():
        if not isinstance(record, MutableMapping):
            continue
        record["api_key"] = ""
        record["vertex_json"] = ""
    _atomic_write_json(path, state)
    return True

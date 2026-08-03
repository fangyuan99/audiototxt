import json
import hashlib
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


DEFAULT_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_SOURCE_TYPE = "douyin"
DEFAULT_AUTH_MODE = "gemini_api_key"
DEFAULT_VERTEX_LOCATION = "global"
SUPPORTED_SOURCE_TYPES = ("audio", "youtube", "video_url", "douyin")
logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint_secret(secret: str) -> str:
    if not secret:
        return ""
    payload = f"audiototxt-auth-v1\0{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_user_ids(value: str) -> set[int]:
    result: set[int] = set()
    for item in (value or "").split(","):
        normalized = item.strip()
        if not normalized:
            continue
        try:
            result.add(int(normalized))
        except ValueError as exc:
            raise ValueError(f"无效的 Telegram user_id：{normalized}") from exc
    return result


def mask_api_key(api_key: str) -> str:
    if not api_key:
        return "未设置"
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


@dataclass
class UserSettings:
    user_id: int
    authorized: bool = False
    auth_secret_fingerprint: str = ""
    authorized_at: str = ""
    username: str = ""
    first_name: str = ""
    auth_mode: str = DEFAULT_AUTH_MODE
    api_key: str = ""
    vertex_json: str = ""
    vertex_project: str = ""
    vertex_location: str = DEFAULT_VERTEX_LOCATION
    model_name: str = DEFAULT_MODEL_NAME
    source_type: str = DEFAULT_SOURCE_TYPE
    promoters: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, user_id: int, data: Optional[Dict[str, object]] = None) -> "UserSettings":
        payload = dict(data or {})
        payload["user_id"] = user_id
        payload.setdefault("updated_at", utc_now_iso())
        payload.setdefault("auth_mode", DEFAULT_AUTH_MODE)
        payload.setdefault("model_name", DEFAULT_MODEL_NAME)
        payload.setdefault("source_type", DEFAULT_SOURCE_TYPE)
        payload.setdefault("promoters", "")
        payload.setdefault("api_key", "")
        payload.setdefault("vertex_json", "")
        payload.setdefault("vertex_project", "")
        payload.setdefault("vertex_location", DEFAULT_VERTEX_LOCATION)
        payload.setdefault("username", "")
        payload.setdefault("first_name", "")
        payload.setdefault("authorized", False)
        payload.setdefault("auth_secret_fingerprint", "")
        payload.setdefault("authorized_at", "")
        return cls(**payload)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class BotStateStore:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> Dict[str, Dict[str, object]]:
        if not os.path.exists(self.storage_path):
            return {"users": {}}

        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                try:
                    os.chmod(self.storage_path, 0o600)
                except OSError:
                    pass
                return data
        except Exception:
            logger.warning("Telegram authorization state could not be loaded.")
        return {"users": {}}

    def _save_locked(self) -> None:
        parent = os.path.dirname(self.storage_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        destination = os.path.abspath(self.storage_path)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{os.path.basename(destination)}.",
                suffix=".tmp",
                dir=parent or ".",
                delete=False,
            ) as f:
                temp_path = f.name
                os.chmod(temp_path, 0o600)
                json.dump(self._state, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, destination)
            os.chmod(destination, 0o600)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def get_user(self, user_id: int) -> UserSettings:
        with self._lock:
            user_data = self._state.setdefault("users", {}).get(str(user_id), {})
            return UserSettings.from_dict(user_id, user_data)

    def upsert_user(self, user_id: int, **changes) -> UserSettings:
        with self._lock:
            users = self._state.setdefault("users", {})
            current = UserSettings.from_dict(user_id, users.get(str(user_id), {}))
            for key, value in changes.items():
                if value is None or not hasattr(current, key):
                    continue
                setattr(current, key, value)
            current.updated_at = utc_now_iso()
            users[str(user_id)] = current.to_dict()
            self._save_locked()
            return current

    def authorize_user(
        self,
        user_id: int,
        username: str = "",
        first_name: str = "",
        secret: str = "",
    ) -> UserSettings:
        return self.upsert_user(
            user_id,
            authorized=True,
            auth_secret_fingerprint=fingerprint_secret(secret),
            authorized_at=utc_now_iso(),
            username=username,
            first_name=first_name,
        )

    def is_user_authorized(
        self,
        user_id: int,
        *,
        current_secret: str = "",
        allowed_user_ids: Optional[set[int]] = None,
    ) -> bool:
        if user_id in (allowed_user_ids or set()):
            return True
        settings = self.get_user(user_id)
        return bool(
            settings.authorized
            and settings.auth_secret_fingerprint
            and settings.auth_secret_fingerprint == fingerprint_secret(current_secret)
        )

    def bind_legacy_authorizations(self, current_secret: str) -> int:
        secret_fingerprint = fingerprint_secret(current_secret)
        if not secret_fingerprint:
            return 0
        with self._lock:
            changed = 0
            users = self._state.setdefault("users", {})
            for raw_user_id, raw_data in list(users.items()):
                try:
                    user_id = int(raw_user_id)
                except (TypeError, ValueError):
                    continue
                current = UserSettings.from_dict(user_id, raw_data)
                if not current.authorized or current.auth_secret_fingerprint:
                    continue
                current.auth_secret_fingerprint = secret_fingerprint
                current.authorized_at = current.authorized_at or utc_now_iso()
                current.updated_at = utc_now_iso()
                users[raw_user_id] = current.to_dict()
                changed += 1
            if changed:
                self._save_locked()
            return changed

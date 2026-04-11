import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


DEFAULT_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_SOURCE_TYPE = "douyin"
DEFAULT_AUTH_MODE = "gemini_api_key"
DEFAULT_VERTEX_LOCATION = "global"
SUPPORTED_SOURCE_TYPES = ("audio", "youtube", "video_url", "douyin")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                return data
        except Exception:
            pass
        return {"users": {}}

    def _save_locked(self) -> None:
        parent = os.path.dirname(self.storage_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

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

    def authorize_user(self, user_id: int, username: str = "", first_name: str = "") -> UserSettings:
        return self.upsert_user(
            user_id,
            authorized=True,
            username=username,
            first_name=first_name,
        )

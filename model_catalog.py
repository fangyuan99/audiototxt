from __future__ import annotations

from contextlib import suppress
from typing import Callable, Optional

from key_pool import GeminiKeyPool
from main import (
    AUTH_MODE_VERTEX_AI_JSON,
    build_auth_config,
    build_genai_client,
)
from service_config import GlobalSettings


MODEL_LIST_PAGE_SIZE = 100
_INCOMPATIBLE_MODEL_MARKERS = ("embedding", "image", "tts", "live")


def _normalized_model_name(model: object, *, vertex: bool) -> str:
    name = str(getattr(model, "name", model) or "").strip()
    if vertex:
        return name.rsplit("/", 1)[-1]
    return name


def _supports_transcription(model: object, name: str) -> bool:
    short_name = name.rsplit("/", 1)[-1].lower()
    if not short_name.startswith("gemini-"):
        return False
    if any(marker in short_name for marker in _INCOMPATIBLE_MODEL_MARKERS):
        return False

    actions = [
        str(action).replace("_", "").lower()
        for action in (getattr(model, "supported_actions", None) or [])
    ]
    return not actions or any("generatecontent" in action for action in actions)


def _list_with_client(
    auth_config,
    *,
    vertex: bool,
    timeout_seconds: float,
    client_factory: Callable,
) -> list[str]:
    client = client_factory(auth_config, timeout_seconds=timeout_seconds)
    try:
        names = []
        for model in client.models.list(
            config={"page_size": MODEL_LIST_PAGE_SIZE, "query_base": True}
        ):
            name = _normalized_model_name(model, vertex=vertex)
            if name and _supports_transcription(model, name):
                names.append(name)
        return sorted(set(names))
    finally:
        with suppress(Exception):
            client.close()


def list_current_channel_models(
    settings: GlobalSettings,
    key_pool: GeminiKeyPool,
    *,
    timeout_seconds: float = 20.0,
    client_factory: Callable = build_genai_client,
) -> list[str]:
    """List transcription-capable models from the currently configured channel."""

    if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON:
        auth_config = build_auth_config(
            auth_mode=settings.auth_mode,
            vertex_json=settings.vertex_json,
            vertex_project=settings.vertex_project,
            vertex_location=settings.vertex_location.strip() or "global",
        )
        return _list_with_client(
            auth_config,
            vertex=True,
            timeout_seconds=timeout_seconds,
            client_factory=client_factory,
        )

    key_pool.sync(settings.gemini_api_keys)

    def load(api_key: Optional[str] = None) -> list[str]:
        auth_config = build_auth_config(
            auth_mode=settings.auth_mode,
            api_key=api_key,
        )
        return _list_with_client(
            auth_config,
            vertex=False,
            timeout_seconds=timeout_seconds,
            client_factory=client_factory,
        )

    return key_pool.list_models(load)

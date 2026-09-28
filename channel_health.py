from __future__ import annotations

import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

from key_pool import GeminiKeyPool
from main import (
    AUTH_MODE_VERTEX_AI_JSON,
    build_auth_config,
    build_genai_client,
)
from service_config import DEFAULT_MODEL_NAME, GlobalSettings


HEALTH_CHECK_PROMPT = "hi"
MAX_LOG_DETAIL = 600


class EmptyChannelResponse(RuntimeError):
    pass


@dataclass(frozen=True)
class ErrorDiagnosis:
    code: str
    user_message: str
    log_detail: str
    error_type: str
    status_code: Optional[int] = None


@dataclass(frozen=True)
class ChannelHealthResult:
    available: bool
    auth_mode: str
    model: str
    location: str
    latency_ms: int
    code: str
    user_message: str
    error_type: str = ""
    log_detail: str = ""


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: RSA)? PRIVATE KEY-----.*?-----END(?: RSA)? PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_GEMINI_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{16,}")
_TELEGRAM_TOKEN_RE = re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"
)
_LOCAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:home|root|tmp|opt|etc|var)/[^\s'\"<>]+"
)


def _replace_url(match: re.Match) -> str:
    value = match.group(0).rstrip(".,;:)]}")
    try:
        host = urlsplit(value).hostname or "redacted"
    except ValueError:
        host = "redacted"
    return f"<url host={host}>"


def sanitize_error_detail(value: object, *, limit: int = MAX_LOG_DETAIL) -> str:
    text = str(value or "")
    text = _PRIVATE_KEY_RE.sub("<private-key>", text)
    text = _URL_RE.sub(_replace_url, text)
    text = _GEMINI_KEY_RE.sub("<gemini-key>", text)
    text = _TELEGRAM_TOKEN_RE.sub("<telegram-token>", text)
    text = _EMAIL_RE.sub("<service-account>", text)
    text = _JWT_RE.sub("<signed-token>", text)
    text = _LOCAL_PATH_RE.sub("<local-path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(1, int(limit))]


def _cause_chain(exc: BaseException) -> list[BaseException]:
    result = []
    current: Optional[BaseException] = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited and len(result) < 8:
        visited.add(id(current))
        result.append(current)
        current = current.__cause__ or current.__context__
    return result


def _status_code(chain: list[BaseException]) -> Optional[int]:
    for exc in chain:
        for candidate in (
            getattr(exc, "status_code", None),
            getattr(exc, "code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue
    return None


def diagnose_exception(exc: BaseException) -> ErrorDiagnosis:
    chain = _cause_chain(exc)
    status = _status_code(chain)
    raw_detail = " | ".join(str(item) for item in chain if str(item).strip())
    normalized = raw_detail.lower()
    class_names = " ".join(type(item).__name__.lower() for item in chain)

    if "billing_disabled" in normalized or "requires billing to be enabled" in normalized:
        code = "billing_disabled"
        message = "Google Cloud 项目未启用结算。"
    elif (
        "ffmpeg" in normalized
        or "invalid data found when processing input" in normalized
        or "moov atom not found" in normalized
    ):
        code = "media_decode"
        message = "下载内容不是可解析的音视频，可能是分享页而非媒体直链。"
    elif "unsafeurl" in class_names or "非公网地址" in normalized:
        code = "unsafe_url"
        message = "链接不是安全的公网地址。"
    elif "inlineaudiotoolarge" in class_names:
        code = "media_too_large"
        message = "音频超过 Vertex AI 单次请求上限，请压缩音频或改用 Gemini API Key 渠道。"
    elif "downloadlimit" in class_names or "超过限制" in normalized:
        code = "media_too_large"
        message = "媒体文件超过服务端大小限制。"
    elif "emptychannelresponse" in class_names or "emptytranscription" in class_names:
        code = "empty_response"
        message = "渠道返回了空响应。"
    elif status == 401 or any(
        token in normalized for token in ("invalid api key", "api key not valid")
    ):
        code = "invalid_credentials"
        message = "渠道凭据无效。"
    elif status == 403 or "permission denied" in normalized:
        code = "permission_denied"
        message = "渠道凭据或 IAM 权限不足。"
    elif status == 404 and "model" in normalized:
        code = "model_unavailable"
        message = "测活模型在当前渠道不可用。"
    elif status == 429 or any(
        token in normalized
        for token in ("resource exhausted", "quota", "rate limit")
    ):
        code = "quota_exhausted"
        message = "渠道额度不足或触发限流。"
    elif any(isinstance(item, TimeoutError) for item in chain) or any(
        token in normalized for token in ("timed out", "timeout")
    ):
        code = "timeout"
        message = "渠道请求超时。"
    elif any(isinstance(item, ConnectionError) for item in chain) or any(
        token in normalized
        for token in (
            "transporterror",
            "networkerror",
            "connection reset",
            "connection aborted",
            "temporary failure in name resolution",
        )
    ):
        code = "network"
        message = "连接渠道时发生网络错误。"
    elif "未配置 gemini api key" in normalized or "keypoolexhausted" in class_names:
        code = "configuration_missing"
        message = "当前渠道缺少可用凭据。"
    else:
        code = "provider_error"
        message = "渠道调用失败。"

    return ErrorDiagnosis(
        code=code,
        user_message=message,
        log_detail=sanitize_error_detail(raw_detail or type(exc).__name__),
        error_type=type(exc).__name__,
        status_code=status,
    )


def check_current_channel(
    settings: GlobalSettings,
    key_pool: GeminiKeyPool,
    *,
    timeout_seconds: float = 20.0,
    client_factory: Callable = build_genai_client,
    clock: Callable[[], float] = time.monotonic,
) -> ChannelHealthResult:
    started = clock()
    probe_model = settings.model_name.strip() or DEFAULT_MODEL_NAME
    location = (
        settings.vertex_location.strip() or "global"
        if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON
        else ""
    )

    def invoke(api_key: Optional[str] = None):
        config = build_auth_config(
            auth_mode=settings.auth_mode,
            api_key=api_key,
            vertex_json=settings.vertex_json,
            vertex_project=settings.vertex_project,
            vertex_location=location or None,
        )
        client = client_factory(config, timeout_seconds=timeout_seconds)
        try:
            response = client.models.generate_content(
                model=probe_model,
                contents=HEALTH_CHECK_PROMPT,
            )
            response_text = (getattr(response, "text", "") or "").strip()
            if not response_text:
                raise EmptyChannelResponse("provider returned empty response")
            return True
        finally:
            with suppress(Exception):
                client.close()

    try:
        if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON:
            invoke()
        else:
            key_pool.sync(settings.gemini_api_keys)
            key_pool.run(invoke)
    except Exception as exc:
        diagnosis = diagnose_exception(exc)
        return ChannelHealthResult(
            available=False,
            auth_mode=settings.auth_mode,
            model=probe_model,
            location=location,
            latency_ms=max(0, round((clock() - started) * 1000)),
            code=diagnosis.code,
            user_message=diagnosis.user_message,
            error_type=diagnosis.error_type,
            log_detail=diagnosis.log_detail,
        )

    return ChannelHealthResult(
        available=True,
        auth_mode=settings.auth_mode,
        model=probe_model,
        location=location,
        latency_ms=max(0, round((clock() - started) * 1000)),
        code="ok",
        user_message="渠道可用。",
    )

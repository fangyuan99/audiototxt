from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import replace
from html import escape
from inspect import iscoroutinefunction
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot_state import BotStateStore, parse_user_ids
from channel_health import (
    ChannelHealthResult,
    ErrorDiagnosis,
    check_current_channel,
    diagnose_exception,
    sanitize_error_detail,
)
from key_pool import GeminiKeyPool, KeyPoolExhausted, KeyStatus, mask_key
from main import (
    AUTH_MODE_GEMINI_API_KEY,
    AUTH_MODE_VERTEX_AI_JSON,
    build_auth_config,
    build_genai_client,
)
from media_policy import (
    DownloadLimitExceeded,
    MediaPolicy,
    UnsafeUrlError,
    detect_text_source,
    extract_first_url,
    sanitize_upload_name,
)
from model_catalog import list_current_channel_models
from retention import (
    cleanup_expired_media,
    cleanup_expired_outputs,
    run_legacy_media_cleanup,
)
from service_config import (
    BotPaths,
    DEFAULT_MODEL_NAME,
    GlobalConfigStore,
    GlobalSettings,
    migrate_legacy_user_settings,
    parse_api_keys,
)
from telegram_jobs import JobStore, TelegramJob, TelegramJobManager
from transcription_service import (
    TaskCancelled,
    TaskDeadline,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
)


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")
logger = logging.getLogger(__name__)

PENDING_ACTION_KEY = "pending_action"
# The hosted Bot API refuses getFile for files above 20 MB.
TELEGRAM_BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024
PENDING_AMBIGUOUS_KEY = "pending_ambiguous"
MODEL_CHOICES_KEY = "model_choices"
MAX_TELEGRAM_TEXT = 3800
MODEL_PAGE_SIZE = 6

STAGE_LABELS = {
    "queued": "排队",
    "preparing": "准备任务",
    "parsing": "解析来源",
    "downloading": "下载媒体",
    "extracting": "抽取音频",
    "retrying": "临时失败后重试",
    "transcribing": "Gemini / Vertex 转写",
    "delivering": "发送结果",
    "completed": "已完成",
}

BOT_COMMANDS = [
    BotCommand("start", "开始使用 / 返回首页"),
    BotCommand("settings", "管理全局设置"),
    BotCommand("help", "查看使用帮助"),
    BotCommand("cancel", "取消当前任务或输入"),
]


def html_code(value: str) -> str:
    return f"<code>{escape(str(value))}</code>"


def split_telegram_text(
    value: str,
    *,
    max_units: int = MAX_TELEGRAM_TEXT,
) -> list[str]:
    """Split text without loss, preferring paragraph and sentence boundaries."""
    text = value or ""
    if not text:
        return []
    if max_units < 2:
        raise ValueError("max_units must be at least 2")

    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = start
        units = 0
        while hard_end < len(text):
            character_units = 2 if ord(text[hard_end]) > 0xFFFF else 1
            if units + character_units > max_units:
                break
            units += character_units
            hard_end += 1

        if hard_end >= len(text):
            chunks.append(text[start:])
            break

        window = text[start:hard_end]
        minimum_break = max(1, len(window) // 2)
        split_at = 0
        for marker in (
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            ". ",
            "! ",
            "? ",
            "；",
            "; ",
            " ",
        ):
            marker_at = window.rfind(marker, minimum_break)
            if marker_at >= 0:
                split_at = marker_at + len(marker)
                break
        if split_at <= 0:
            split_at = len(window)

        chunks.append(window[:split_at])
        start += split_at

    return chunks


class ResultCache:
    def __init__(
        self,
        *,
        max_entries: int = 64,
        max_characters: int = 2_000_000,
        ttl_seconds: float = 24 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_characters = max(1, int(max_characters))
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self.clock = clock
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._characters = 0
        self._lock = threading.RLock()

    def _evict_locked(self) -> None:
        now = self.clock()
        for key, (expires_at, value) in list(self._items.items()):
            if expires_at > now:
                continue
            self._items.pop(key, None)
            self._characters -= len(value)
        while self._items and (
            len(self._items) > self.max_entries
            or (
                self._characters > self.max_characters
                and len(self._items) > 1
            )
        ):
            _key, (_expires, value) = self._items.popitem(last=False)
            self._characters -= len(value)

    def put(self, key: str, value: str) -> None:
        text = value or ""
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._characters -= len(previous[1])
            self._items[key] = (self.clock() + self.ttl_seconds, text)
            self._characters += len(text)
            self._evict_locked()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            self._evict_locked()
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item[1]

    def evict(self) -> None:
        with self._lock:
            self._evict_locked()


def build_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 当前状态", callback_data="home:status"),
                InlineKeyboardButton("⚙️ 全局设置", callback_data="settings"),
            ],
            [
                InlineKeyboardButton("📋 任务队列", callback_data="queue"),
                InlineKeyboardButton("❓ 使用帮助", callback_data="help"),
            ],
        ]
    )


def build_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 Gemini Keys", callback_data="settings:keys")],
            [
                InlineKeyboardButton("🤖 模型", callback_data="settings:model"),
                InlineKeyboardButton("🌐 语言", callback_data="settings:language"),
            ],
            [InlineKeyboardButton("🩺 测试当前渠道", callback_data="channel:test")],
            [InlineKeyboardButton("📝 Prompt", callback_data="settings:prompt")],
            [InlineKeyboardButton("☁️ Vertex 高级设置", callback_data="settings:vertex")],
            [InlineKeyboardButton("⬅️ 返回首页", callback_data="home")],
        ]
    )


def build_key_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("替换全部", callback_data="keys:replace"),
                InlineKeyboardButton("追加 Key", callback_data="keys:append"),
            ],
            [InlineKeyboardButton("⬅️ 返回设置", callback_data="settings")],
        ]
    )


def build_model_keyboard(
    models: list[str],
    *,
    page: int = 0,
    page_size: int = MODEL_PAGE_SIZE,
) -> InlineKeyboardMarkup:
    page_size = max(1, page_size)
    page_count = max(1, (len(models) + page_size - 1) // page_size)
    page = min(max(0, page), page_count - 1)
    start = page * page_size
    rows = [
        [
            InlineKeyboardButton(
                model.removeprefix("models/")[:48],
                callback_data=f"model:set:{index}",
            )
        ]
        for index, model in enumerate(models[start : start + page_size], start=start)
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️ 上一页", callback_data=f"model:page:{page - 1}")
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton("下一页 ➡️", callback_data=f"model:page:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [InlineKeyboardButton("手动输入模型名", callback_data="model:manual")],
            [InlineKeyboardButton("⬅️ 返回设置", callback_data="settings")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("追加要求", callback_data="prompt:append"),
                InlineKeyboardButton("完整覆盖（高级）", callback_data="prompt:override"),
            ],
            [InlineKeyboardButton("恢复默认", callback_data="prompt:reset")],
            [InlineKeyboardButton("⬅️ 返回设置", callback_data="settings")],
        ]
    )


def build_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("自动识别原语言", callback_data="language:auto")],
            [InlineKeyboardButton("手动设置提示", callback_data="language:manual")],
            [InlineKeyboardButton("⬅️ 返回设置", callback_data="settings")],
        ]
    )


def build_vertex_keyboard(settings: GlobalSettings) -> InlineKeyboardMarkup:
    mode_button = (
        InlineKeyboardButton("切换到 Gemini", callback_data="auth:gemini")
        if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON
        else InlineKeyboardButton("切换到 Vertex", callback_data="auth:vertex")
    )
    return InlineKeyboardMarkup(
        [
            [mode_button],
            [InlineKeyboardButton("设置 Service Account JSON", callback_data="vertex:json")],
            [
                InlineKeyboardButton("设置 Project", callback_data="vertex:project"),
                InlineKeyboardButton("设置 Location", callback_data="vertex:location"),
            ],
            [InlineKeyboardButton("⬅️ 返回设置", callback_data="settings")],
        ]
    )


def build_source_choice_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("YouTube", callback_data=f"source:youtube:{token}"),
                InlineKeyboardButton("视频直链", callback_data=f"source:video_url:{token}"),
            ],
            [InlineKeyboardButton("抖音分享", callback_data=f"source:douyin:{token}")],
            [InlineKeyboardButton("取消", callback_data="input:cancel")],
        ]
    )


def build_cancel_job_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("取消任务", callback_data=f"job:cancel:{job_id}")]]
    )


def build_failed_job_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("重试任务", callback_data=f"job:retry:{job_id}")],
            [
                InlineKeyboardButton("打开设置", callback_data="settings"),
                InlineKeyboardButton("返回首页", callback_data="home"),
            ],
        ]
    )


def build_result_keyboard(job_id: str, *, include_full: bool) -> InlineKeyboardMarkup:
    rows = []
    if include_full:
        rows.append(
            [InlineKeyboardButton("重新发送完整文本", callback_data=f"result:full:{job_id}")]
        )
    rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def render_settings(
    settings: GlobalSettings,
    key_statuses: Optional[list[KeyStatus]] = None,
) -> str:
    statuses = key_statuses or []
    key_lines = []
    if statuses:
        icon = {"healthy": "✅", "cooldown": "⏳", "disabled": "❌"}
        for index, status in enumerate(statuses, start=1):
            suffix = ""
            if status.state == "cooldown":
                suffix = f"（{status.retry_after_seconds}s 后重试）"
            elif status.state == "disabled":
                suffix = "（已停用，更新配置后恢复）"
            key_lines.append(
                f"  {index}. {icon.get(status.state, '•')} {html_code(status.masked_key)}{suffix}"
            )
    else:
        key_lines = [
            f"  {index}. {html_code(mask_key(key))}"
            for index, key in enumerate(settings.gemini_api_keys, start=1)
        ]
    if not key_lines:
        key_lines.append("  未配置")

    if settings.prompt_override:
        prompt_status = f"完整覆盖（{len(settings.prompt_override)} 字符）"
    elif settings.prompt_append:
        prompt_status = f"附加要求（{len(settings.prompt_append)} 字符）"
    else:
        prompt_status = "默认忠实逐字稿"

    auth_label = "Vertex AI" if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON else "Gemini API Key"
    lines = [
        "<b>全局设置</b>",
        f"认证方式：{escape(auth_label)}",
        f"Gemini Key：{len(settings.gemini_api_keys)} 个",
        *key_lines,
        f"模型：{html_code(settings.model_name)}",
        f"语言：{html_code(settings.language_hint or '自动识别原语言')}",
        f"Prompt：{escape(prompt_status)}",
    ]
    if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON:
        lines.extend(
            [
                f"Vertex JSON：{'已设置' if settings.vertex_json else '未设置'}",
                f"Vertex Project：{html_code(settings.vertex_project or '未设置')}",
                f"Vertex Location：{html_code(settings.vertex_location or 'global')}",
            ]
        )
    return "\n".join(lines)


def render_channel_health(
    result: ChannelHealthResult,
    settings: Optional[GlobalSettings] = None,
) -> str:
    auth_label = (
        "Vertex AI"
        if result.auth_mode == AUTH_MODE_VERTEX_AI_JSON
        else "Gemini API Key"
    )
    state = "✅ 当前渠道可用" if result.available else "❌ 当前渠道不可用"
    lines = [
        f"<b>{state}</b>",
        f"渠道：{escape(auth_label)}",
        f"测活模型：{html_code(result.model)}",
    ]
    if result.location:
        lines.append(f"地区：{html_code(result.location)}")
    lines.extend(
        [
            f"耗时：{result.latency_ms} ms",
            f"结果：{escape(result.user_message)}",
        ]
    )
    return "\n".join(lines)


def render_job_failure(stage: str, reason: str) -> str:
    label = STAGE_LABELS.get(stage, stage or "未知阶段")
    return (
        "<b>任务失败</b>\n"
        f"失败阶段：{escape(label)}\n"
        f"原因：{escape(reason or '任务执行失败。')}"
    )


def build_help_text(settings: Optional[GlobalSettings] = None) -> str:
    lines = [
        "<b>使用方法</b>",
        "直接发送以下任一内容，机器人会自动识别并排队转写：",
        "• 音频文件、语音或音频文档",
        "• YouTube 链接",
        "• 抖音分享文案或链接",
        "• 公网视频/音频直链",
        "",
        f"用 {html_code('/settings')} 管理全局 Key、模型和 Prompt。",
        f"用 {html_code('/cancel')} 取消自己的最近任务。",
        "机器人仅支持私聊，默认输出忠实逐字稿，不自动翻译或总结。",
    ]
    if settings is not None:
        lines.extend(["", render_settings(settings)])
    return "\n".join(lines)


def safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, JobExecutionFailure):
        return exc.user_message
    if isinstance(exc, KeyPoolExhausted):
        return "当前没有可用的 Gemini Key，请打开设置检查 Key 池。"
    if isinstance(exc, UnsafeUrlError):
        return "链接不安全或不是公网地址，已拒绝处理。"
    if isinstance(exc, DownloadLimitExceeded):
        return "媒体文件超过服务端大小限制。"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "任务处理超时，请稍后重试。"
    if isinstance(exc, TaskCancelled):
        return "任务已取消。"
    message = str(exc).lower()
    if "api key" in message and any(token in message for token in ("invalid", "not valid")):
        return "Gemini Key 无效，请在全局设置中更换。"
    if "quota" in message or "resource exhausted" in message:
        return "当前 Key 额度不足，已尝试切换其它 Key。"
    return "任务执行失败，请重试；如持续失败，请打开设置检查配置。"


class JobExecutionFailure(RuntimeError):
    def __init__(self, stage: str, diagnosis: ErrorDiagnosis) -> None:
        super().__init__(diagnosis.user_message)
        self.stage = stage
        self.error_code = diagnosis.code
        self.user_message = diagnosis.user_message
        self.diagnosis = diagnosis


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and getattr(chat, "type", "private") == "private"


def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False
    store: BotStateStore = context.application.bot_data["store"]
    return store.is_user_authorized(
        user.id,
        current_secret=context.application.bot_data.get("bot_secret", ""),
        allowed_user_ids=context.application.bot_data.get("allowed_user_ids", set()),
    )


async def _reply_html(
    message: Message,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Message:
    return await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def _show_home(message: Message) -> Message:
    return await _reply_html(
        message,
        "<b>AudioToTxt 已就绪</b>\n\n直接发送音频、YouTube、抖音分享或视频直链即可。",
        reply_markup=build_home_keyboard(),
    )


async def _prompt_for_password(message: Message, context) -> None:
    context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
    await _reply_html(message, "请发送机器人密码完成验证。")


async def ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if message is None:
        return False
    if not _is_private(update):
        await message.reply_text("此机器人仅支持私聊。")
        return False
    if _is_authorized(update, context):
        return True
    await _prompt_for_password(message, context)
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not _is_private(update):
        await message.reply_text("此机器人仅支持私聊。")
        return
    if not _is_authorized(update, context):
        await _prompt_for_password(message, context)
        return
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await _show_home(message)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not await ensure_authorized(update, context):
        return
    settings: GlobalSettings = context.application.bot_data["config_store"].get()
    pool: GeminiKeyPool = context.application.bot_data["key_pool"]
    pool.sync(settings.gemini_api_keys)
    await _reply_html(
        message,
        render_settings(settings, pool.statuses()),
        reply_markup=build_settings_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not await ensure_authorized(update, context):
        return
    await _reply_html(message, build_help_text(), reply_markup=build_home_keyboard())


def _latest_user_job(manager: TelegramJobManager, user_id: int) -> Optional[TelegramJob]:
    candidates = [
        job
        for job in manager.snapshot()
        if job.user_id == user_id and job.status in {"queued", "running", "cancelling"}
    ]
    return candidates[-1] if candidates else None


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not await ensure_authorized(update, context):
        return
    # An open input session takes priority: /cancel then only exits the input
    # and never touches a running transcription.
    pending_action = context.user_data.pop(PENDING_ACTION_KEY, None)
    pending_choice = context.user_data.pop(PENDING_AMBIGUOUS_KEY, None)
    if pending_action or pending_choice:
        await _reply_html(message, "已退出输入。", reply_markup=build_home_keyboard())
        return
    manager: TelegramJobManager = context.application.bot_data["job_manager"]
    job = _latest_user_job(manager, user.id)
    if job is None:
        await _reply_html(message, "当前没有可取消的任务或输入流程。")
        return
    manager.cancel(job.job_id)
    await _reply_html(message, f"已请求取消任务 {html_code(job.job_id[:8])}。")


def _validate_api_key(api_key: str) -> bool:
    config = build_auth_config(auth_mode=AUTH_MODE_GEMINI_API_KEY, api_key=api_key)
    client = build_genai_client(config, timeout_seconds=15)
    try:
        iterator = iter(client.models.list(config={"page_size": 1}))
        next(iterator, None)
        return True
    finally:
        client.close()


async def _run_channel_health_check(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    settings_override: Optional[GlobalSettings] = None,
) -> ChannelHealthResult:
    settings = settings_override or context.application.bot_data["config_store"].get()
    pool: GeminiKeyPool = context.application.bot_data["key_pool"]
    checker = context.application.bot_data.get(
        "channel_health_checker", check_current_channel
    )
    try:
        if iscoroutinefunction(checker):
            result = await checker(settings, pool)
        else:
            result = await asyncio.to_thread(checker, settings, pool)
    except Exception as exc:
        diagnosis = diagnose_exception(exc)
        result = ChannelHealthResult(
            available=False,
            auth_mode=settings.auth_mode,
            model=settings.model_name.strip() or DEFAULT_MODEL_NAME,
            location=(
                settings.vertex_location or "global"
                if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON
                else ""
            ),
            latency_ms=0,
            code=diagnosis.code,
            user_message=diagnosis.user_message,
            error_type=diagnosis.error_type,
            log_detail=diagnosis.log_detail,
        )
    log = logger.info if result.available else logger.warning
    log(
        "channel_health auth_mode=%s model=%s location=%s available=%s "
        "category=%s latency_ms=%s error_type=%s detail=%s",
        result.auth_mode,
        result.model,
        result.location or "-",
        result.available,
        result.code,
        result.latency_ms,
        result.error_type or "-",
        result.log_detail or "-",
    )
    return result


async def _report_channel_health(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    settings_override: Optional[GlobalSettings] = None,
) -> ChannelHealthResult:
    settings = settings_override or context.application.bot_data["config_store"].get()
    model_name = settings.model_name.strip() or DEFAULT_MODEL_NAME
    progress = await _reply_html(
        message,
        f"<b>正在测试渠道</b>\n使用 {html_code(model_name)} 发送 hi……",
    )
    result = await _run_channel_health_check(
        context, settings_override=settings_override
    )
    rendered = render_channel_health(result, settings)
    try:
        await progress.edit_text(
            rendered,
            parse_mode=ParseMode.HTML,
            reply_markup=build_settings_keyboard(),
            disable_web_page_preview=True,
        )
    except Exception:
        logger.debug("Unable to edit channel health progress message.")
        await _reply_html(
            message,
            rendered,
            reply_markup=build_settings_keyboard(),
        )
    return result


async def _edit_query(query, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _queue_text(manager: TelegramJobManager, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    jobs = [
        job
        for job in manager.snapshot()
        if job.user_id == user_id
        and job.status in {"queued", "running", "cancelling", "failed", "interrupted"}
    ][-10:]
    if not jobs:
        return "<b>任务队列</b>\n当前没有任务。", build_home_keyboard()
    labels = {
        "queued": "排队中",
        "running": "执行中",
        "cancelling": "取消中",
        "failed": "失败",
        "interrupted": "已中断",
    }
    lines = ["<b>最近任务</b>"]
    rows = []
    for job in jobs:
        position = manager.queue_position(job.job_id)
        suffix = f"（第 {position} 位）" if position else ""
        lines.append(
            f"• {html_code(job.job_id[:8])} · {escape(labels.get(job.status, job.status))}{suffix} · {html_code(job.source_type)}"
        )
        if job.status in {"queued", "running", "cancelling"}:
            rows.append(
                [InlineKeyboardButton(f"取消 {job.job_id[:8]}", callback_data=f"job:cancel:{job.job_id}")]
            )
        elif job.status in {"failed", "interrupted"}:
            rows.append(
                [InlineKeyboardButton(f"重试 {job.job_id[:8]}", callback_data=f"job:retry:{job.job_id}")]
            )
    rows.append([InlineKeyboardButton("⬅️ 返回首页", callback_data="home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or query.message is None or user is None:
        return
    await query.answer()
    if not _is_private(update):
        await query.message.reply_text("此机器人仅支持私聊。")
        return
    if not _is_authorized(update, context):
        await _prompt_for_password(query.message, context)
        return

    data = query.data or ""
    # Any button press ends a pending text input; branches that start a new
    # input set it again below. Otherwise a later link could be saved as a
    # setting value after the user has navigated away.
    context.user_data.pop(PENDING_ACTION_KEY, None)
    config_store: GlobalConfigStore = context.application.bot_data["config_store"]
    key_pool: GeminiKeyPool = context.application.bot_data["key_pool"]
    manager: TelegramJobManager = context.application.bot_data["job_manager"]

    if data in {"home", "home:status"}:
        text = "<b>AudioToTxt 已就绪</b>\n\n直接发送内容即可。"
        if data == "home:status":
            active = len(
                [job for job in manager.snapshot() if job.status in {"queued", "running", "cancelling"}]
            )
            settings = config_store.get()
            text += f"\n\n全局活动任务：{active}\n已配置 Key：{len(settings.gemini_api_keys)}"
        await _edit_query(query, text, build_home_keyboard())
        return
    if data == "help":
        await _edit_query(query, build_help_text(), build_home_keyboard())
        return
    if data == "settings":
        settings = config_store.get()
        key_pool.sync(settings.gemini_api_keys)
        await _edit_query(
            query,
            render_settings(settings, key_pool.statuses()),
            build_settings_keyboard(),
        )
        return
    if data == "channel:test":
        settings = config_store.get()
        model_name = settings.model_name.strip() or DEFAULT_MODEL_NAME
        await _edit_query(
            query,
            f"<b>正在测试当前渠道</b>\n使用 {html_code(model_name)} 发送 hi……",
            build_settings_keyboard(),
        )
        result = await _run_channel_health_check(context)
        await _edit_query(
            query,
            render_channel_health(result, settings),
            build_settings_keyboard(),
        )
        return
    if data == "queue":
        text, markup = _queue_text(manager, user.id)
        await _edit_query(query, text, markup)
        return
    if data == "settings:keys":
        settings = config_store.get()
        key_pool.sync(settings.gemini_api_keys)
        await _edit_query(
            query,
            render_settings(settings, key_pool.statuses()),
            build_key_keyboard(),
        )
        return
    if data in {"keys:replace", "keys:append"}:
        context.user_data[PENDING_ACTION_KEY] = data.replace(":", "_")
        await query.message.reply_text(
            "请发送逗号分隔的 Gemini API Key。该消息会在收到后立即删除。"
        )
        return
    if data == "settings:model":
        settings = config_store.get()
        try:
            models = await asyncio.to_thread(
                context.application.bot_data.get(
                    "model_catalog_loader", list_current_channel_models
                ),
                settings,
                key_pool,
            )
        except Exception as exc:
            diagnosis = diagnose_exception(exc)
            logger.warning(
                "model_catalog_failed auth_mode=%s category=%s error_type=%s detail=%s",
                settings.auth_mode,
                diagnosis.code,
                diagnosis.error_type,
                diagnosis.log_detail or "-",
            )
            models = []
        context.user_data[MODEL_CHOICES_KEY] = models
        if not models:
            context.user_data[PENDING_ACTION_KEY] = "model_manual"
            await query.message.reply_text(
                "无法从当前渠道读取可用模型，请直接发送模型名称。"
            )
            return
        await _edit_query(
            query,
            f"<b>选择模型</b>\n当前：{html_code(settings.model_name)}",
            build_model_keyboard(models),
        )
        return
    if data.startswith("model:page:"):
        models = context.user_data.get(MODEL_CHOICES_KEY, [])
        page = int(data.rsplit(":", 1)[-1])
        await _edit_query(query, "<b>选择模型</b>", build_model_keyboard(models, page=page))
        return
    if data.startswith("model:set:"):
        models = context.user_data.get(MODEL_CHOICES_KEY, [])
        index = int(data.rsplit(":", 1)[-1])
        if index < 0 or index >= len(models):
            await query.message.reply_text("模型列表已过期，请重新打开设置。")
            return
        config_store.update(model_name=models[index])
        await _edit_query(query, f"模型已更新为 {html_code(models[index])}。", build_settings_keyboard())
        return
    if data == "model:manual":
        context.user_data[PENDING_ACTION_KEY] = "model_manual"
        await query.message.reply_text("请发送完整模型名称。")
        return
    if data == "settings:prompt":
        await _edit_query(
            query,
            "<b>Prompt 设置</b>\n默认规则始终保持忠实逐字稿；建议使用“追加要求”。",
            build_prompt_keyboard(),
        )
        return
    if data in {"prompt:append", "prompt:override"}:
        context.user_data[PENDING_ACTION_KEY] = data.replace(":", "_")
        await query.message.reply_text("请发送新的 Prompt 内容。")
        return
    if data == "prompt:reset":
        config_store.update(prompt_append="", prompt_override="")
        await _edit_query(query, "Prompt 已恢复为默认忠实逐字稿。", build_settings_keyboard())
        return
    if data == "settings:language":
        await _edit_query(query, "<b>语言设置</b>", build_language_keyboard())
        return
    if data == "language:auto":
        config_store.update(language_hint="")
        await _edit_query(query, "已改为自动识别并按原语言转写。", build_settings_keyboard())
        return
    if data == "language:manual":
        context.user_data[PENDING_ACTION_KEY] = "language_manual"
        await query.message.reply_text("请发送语言提示，例如 zh、en、ja 或 yue。")
        return
    if data == "settings:vertex":
        settings = config_store.get()
        await _edit_query(
            query,
            render_settings(settings, key_pool.statuses()),
            build_vertex_keyboard(settings),
        )
        return
    if data == "auth:gemini":
        config_store.update(auth_mode=AUTH_MODE_GEMINI_API_KEY)
        await _edit_query(query, "已切换到 Gemini Key 模式。", build_settings_keyboard())
        return
    if data == "auth:vertex":
        config_store.update(auth_mode=AUTH_MODE_VERTEX_AI_JSON)
        await _edit_query(query, "已切换到 Vertex AI 模式。", build_settings_keyboard())
        return
    if data.startswith("vertex:"):
        action = data.split(":", 1)[1]
        context.user_data[PENDING_ACTION_KEY] = f"vertex_{action}"
        prompts = {
            "json": "请发送完整 Service Account JSON；消息会立即删除。",
            "project": "请发送 Vertex Project ID。",
            "location": "请发送 Vertex Location，例如 global。",
        }
        await query.message.reply_text(prompts.get(action, "请发送新值。"))
        return
    if data == "input:cancel":
        context.user_data.pop(PENDING_ACTION_KEY, None)
        context.user_data.pop(PENDING_AMBIGUOUS_KEY, None)
        await _edit_query(query, "已取消输入。", build_home_keyboard())
        return
    if data.startswith("source:"):
        _prefix, source_type, token = data.split(":", 2)
        pending = context.user_data.get(PENDING_AMBIGUOUS_KEY) or {}
        if pending.get("token") != token:
            await query.message.reply_text("这条选择已过期，请重新发送内容。")
            return
        context.user_data.pop(PENDING_AMBIGUOUS_KEY, None)
        await _enqueue_text(
            query.message,
            context,
            user.id,
            source_type,
            pending.get("text", ""),
        )
        return
    if data.startswith("job:cancel:"):
        job_id = data.split(":", 2)[-1]
        job = manager.get(job_id)
        if job is None or job.user_id != user.id:
            await query.message.reply_text("任务不存在或无权操作。")
            return
        manager.cancel(job_id)
        await query.message.reply_text(f"已请求取消任务 {job_id[:8]}。")
        return
    if data.startswith("job:retry:"):
        job_id = data.split(":", 2)[-1]
        job = manager.get(job_id)
        if job is None or job.user_id != user.id:
            await query.message.reply_text("任务不存在或无权操作。")
            return
        # Each retry gets its own status card so the clicked message (a
        # failed card or the queue panel) is never overwritten.
        status_message = await _reply_html(
            query.message, "<b>已接收重试</b>\n正在加入任务队列……"
        )
        try:
            detected_source = (
                detect_text_source(job.text_input) if job.text_input else None
            )
            retried = manager.retry(
                job_id,
                source_type_override=detected_source or job.source_type,
                status_message_id=getattr(status_message, "message_id", 0) or 0,
            )
        except (ValueError, KeyError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "任务不存在。"
            await _edit_message(status_message, escape(reason), build_home_keyboard())
            return
        await _edit_message(
            status_message,
            f"<b>任务已重新排队</b>\n任务：{html_code(retried.job_id[:8])}\n"
            f"当前位置：{manager.queue_position(retried.job_id)}",
            build_cancel_job_keyboard(retried.job_id),
        )
        return
    if data.startswith("result:full:"):
        job_id = data.split(":", 2)[-1]
        job = manager.get(job_id)
        if job is None or job.user_id != user.id:
            await query.message.reply_text("结果不存在或无权访问。")
            return
        cache: ResultCache = context.application.bot_data["result_cache"]
        transcript = cache.get(job_id)
        if transcript is None:
            await query.message.reply_text("结果已过期，请重新转写。")
            return
        for chunk in split_telegram_text(transcript):
            await context.bot.send_message(
                chat_id=job.chat_id,
                text=chunk,
            )
        return


async def _handle_password(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    secret = context.application.bot_data.get("bot_secret", "")
    try:
        await message.delete()
    except Exception:
        logger.debug("Unable to delete the password message.")
    if not secret:
        await _reply_html(message, "服务端未配置机器人密码。")
        return
    if not hmac.compare_digest(text, secret):
        await _reply_html(message, "密码不正确，请重试。")
        return
    store: BotStateStore = context.application.bot_data["store"]
    store.authorize_user(
        user.id,
        username=getattr(user, "username", "") or "",
        first_name=getattr(user, "first_name", "") or "",
        secret=secret,
    )
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await _reply_html(
        message,
        "验证成功。现在可以直接发送音频或链接。",
        reply_markup=build_home_keyboard(),
    )


async def _handle_setting_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    text: str,
) -> bool:
    message = update.effective_message
    if message is None:
        return True
    store: GlobalConfigStore = context.application.bot_data["config_store"]
    pool: GeminiKeyPool = context.application.bot_data["key_pool"]
    vertex_health_settings: Optional[GlobalSettings] = None

    if action in {"keys_replace", "keys_append"}:
        try:
            await message.delete()
        except Exception:
            logger.debug("Unable to delete a Gemini key message.")
        keys = parse_api_keys(text)
        if not keys:
            await _reply_html(message, "没有检测到有效格式的 Key，旧配置保持不变。")
            return True
        validator = context.application.bot_data.get("key_validator", _validate_api_key)
        valid = []
        invalid_count = 0
        for key in keys:
            try:
                is_valid = await asyncio.to_thread(validator, key)
            except Exception:
                is_valid = False
            if is_valid:
                valid.append(key)
            else:
                invalid_count += 1
        if not valid:
            await _reply_html(message, "新 Key 全部验证失败，旧配置保持不变。")
            return True
        settings = (
            store.replace_api_keys(valid)
            if action == "keys_replace"
            else store.append_api_keys(valid)
        )
        pool.sync(settings.gemini_api_keys)
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await _reply_html(
            message,
            f"Key 池已更新：{len(settings.gemini_api_keys)} 个可配置 Key；本次忽略 {invalid_count} 个失败项。",
            reply_markup=build_settings_keyboard(),
        )
        return True
    if action == "model_manual":
        store.update(model_name=text.strip())
    elif action == "prompt_append":
        store.update(prompt_append=text, prompt_override="")
    elif action == "prompt_override":
        store.update(prompt_override=text)
    elif action == "language_manual":
        store.update(language_hint=text.strip())
    elif action == "vertex_json":
        try:
            await message.delete()
        except Exception:
            logger.debug("Unable to delete a Vertex credential message.")
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError
        except Exception:
            await _reply_html(message, "Vertex JSON 格式无效，旧配置保持不变。")
            return True
        current = store.get()
        project = str(parsed.get("project_id") or current.vertex_project).strip()
        saved = store.update(
            vertex_json=json.dumps(parsed, ensure_ascii=False),
            vertex_project=project,
            vertex_location=current.vertex_location.strip() or "global",
            auth_mode=AUTH_MODE_VERTEX_AI_JSON,
        )
        vertex_health_settings = replace(
            saved,
            auth_mode=AUTH_MODE_VERTEX_AI_JSON,
            vertex_location=saved.vertex_location or "global",
        )
    elif action == "vertex_project":
        saved = store.update(vertex_project=text.strip())
        vertex_health_settings = replace(
            saved,
            auth_mode=AUTH_MODE_VERTEX_AI_JSON,
            vertex_location=saved.vertex_location or "global",
        )
    elif action == "vertex_location":
        saved = store.update(vertex_location=text.strip() or "global")
        vertex_health_settings = replace(
            saved,
            auth_mode=AUTH_MODE_VERTEX_AI_JSON,
            vertex_location=saved.vertex_location or "global",
        )
    else:
        return False

    context.user_data.pop(PENDING_ACTION_KEY, None)
    if vertex_health_settings is not None:
        await _report_channel_health(
            message,
            context,
            settings_override=vertex_health_settings,
        )
    else:
        await _reply_html(
            message,
            "全局设置已保存。",
            reply_markup=build_settings_keyboard(),
        )
    return True


async def _enqueue_text(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    source_type: str,
    text: str,
) -> TelegramJob:
    value = text
    if source_type in {"youtube", "video_url"}:
        value = extract_first_url(text) or text
    return await _enqueue_job(
        message,
        context,
        user_id=user_id,
        source_type=source_type,
        text_input=value,
    )


async def _edit_message(
    message: Message,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        logger.debug("Unable to edit a Telegram status message.")


async def _enqueue_job(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    source_type: str,
    text_input: str = "",
    audio_path: str = "",
    telegram_file_id: str = "",
    original_filename: str = "",
) -> TelegramJob:
    status_message = await _reply_html(message, "<b>已接收</b>\n正在加入任务队列……")
    manager: TelegramJobManager = context.application.bot_data["job_manager"]
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        chat_id = getattr(message, "chat_id", user_id)
    job = manager.enqueue(
        user_id=user_id,
        chat_id=chat_id,
        source_type=source_type,
        text_input=text_input,
        audio_path=audio_path,
        telegram_file_id=telegram_file_id,
        original_filename=original_filename,
        source_message_id=getattr(message, "message_id", 0) or 0,
        status_message_id=getattr(status_message, "message_id", 0) or 0,
    )
    position = manager.queue_position(job.job_id)
    try:
        await status_message.edit_text(
            f"<b>排队中</b>\n任务：{html_code(job.job_id[:8])}\n当前位置：{position}",
            parse_mode=ParseMode.HTML,
            reply_markup=build_cancel_job_keyboard(job.job_id),
        )
    except Exception:
        logger.debug("Unable to edit the initial queue status card.")
    return job


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return
    if not _is_private(update):
        await message.reply_text("此机器人仅支持私聊。")
        return
    text = message.text.strip()
    action = context.user_data.get(PENDING_ACTION_KEY)
    if action == "awaiting_secret":
        await _handle_password(update, context, text)
        return
    if not _is_authorized(update, context):
        await _prompt_for_password(message, context)
        return
    if action and await _handle_setting_input(update, context, action, text):
        return

    source_type = detect_text_source(text)
    if source_type is None:
        token = uuid.uuid4().hex[:8]
        context.user_data[PENDING_AMBIGUOUS_KEY] = {"token": token, "text": text}
        await _reply_html(
            message,
            "无法确定这段内容的来源，请选择处理方式。",
            reply_markup=build_source_choice_keyboard(token),
        )
        return
    await _enqueue_text(message, context, user.id, source_type, text)


async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not await ensure_authorized(update, context):
        return
    source_file = message.audio or message.voice or message.document
    if source_file is None:
        await message.reply_text("未检测到音频文件。")
        return
    policy: MediaPolicy = context.application.bot_data["media_policy"]
    file_size = int(getattr(source_file, "file_size", 0) or 0)
    if file_size and file_size > policy.max_media_bytes:
        await message.reply_text("音频文件超过服务端大小限制。")
        return
    if file_size and file_size > TELEGRAM_BOT_API_DOWNLOAD_LIMIT:
        await message.reply_text(
            "音频文件超过 Telegram Bot API 的 20 MB 下载限制，"
            "请压缩后重发，或改为发送媒体直链。"
        )
        return

    original = getattr(source_file, "file_name", "") or "voice.ogg"
    # Download happens in the job worker so this update returns immediately.
    await _enqueue_job(
        message,
        context,
        user_id=user.id,
        source_type="audio",
        telegram_file_id=source_file.file_id,
        original_filename=original,
    )


async def legacy_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await settings_command(update, context)


async def legacy_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not await ensure_authorized(update, context):
        return
    await _reply_html(message, "来源类型现在会自动识别，直接发送内容即可。", reply_markup=build_home_keyboard())


async def reject_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text("此机器人仅支持私聊。")


async def _safe_edit_status(
    application: Application,
    job: TelegramJob,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if not job.status_message_id:
        return
    try:
        await application.bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning(
                "Unable to edit Telegram job status: %s",
                sanitize_error_detail(exc),
            )
    except Exception as exc:
        logger.warning(
            "Unable to edit Telegram job status: %s",
            sanitize_error_detail(exc),
        )


_STATUS_LABELS = {
    "preparing": "正在准备任务",
    "parsing": "正在解析来源",
    "downloading": "正在下载媒体",
    "extracting": "正在抽取音频",
    "retrying": "临时失败，正在重试",
    "transcribing": "正在转写",
    "delivering": "正在发送结果",
}


async def _execute_job(
    application: Application,
    job: TelegramJob,
    cancelled: Callable[[], bool],
) -> TranscriptionResult:
    service: TranscriptionService = application.bot_data["transcription_service"]
    manager: TelegramJobManager = application.bot_data["job_manager"]
    timeout = application.bot_data["media_policy"].task_timeout_seconds
    loop = asyncio.get_running_loop()
    current_stage = {"value": "preparing"}
    stage_lock = threading.Lock()

    def on_status(status: str) -> None:
        with stage_lock:
            current_stage["value"] = status
        manager.update_stage(job.job_id, status)
        label = _STATUS_LABELS.get(status, status)
        operation = _safe_edit_status(
            application,
            job,
            f"<b>{escape(label)}</b>\n任务：{html_code(job.job_id[:8])}",
            build_cancel_job_keyboard(job.job_id),
        )
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(operation)
        else:
            asyncio.run_coroutine_threadsafe(operation, loop)

    try:
        if job.source_type == "audio" and (
            not job.audio_path or not Path(job.audio_path).is_file()
        ):
            on_status("downloading")
            downloaded = await _download_telegram_audio(application, job)
            if cancelled():
                downloaded.unlink(missing_ok=True)
                raise TaskCancelled("任务已取消。")
            job = manager.set_audio_path(job.job_id, str(downloaded)) or replace(
                job, audio_path=str(downloaded)
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise JobExecutionFailure("downloading", diagnose_exception(exc)) from exc

    request = TranscriptionRequest(
        source_type=job.source_type,
        text_input=job.text_input or None,
        audio_path=Path(job.audio_path) if job.audio_path else None,
        original_filename=job.original_filename or None,
        cleanup_input=bool(job.audio_path),
    )
    try:
        async_execute = getattr(service, "execute_async", None)
        if callable(async_execute):
            result = await async_execute(
                request,
                on_status=on_status,
                cancelled=cancelled,
                deadline=TaskDeadline(timeout),
            )
        else:
            result = await asyncio.to_thread(
                service.execute,
                request,
                on_status=on_status,
                cancelled=cancelled,
                deadline=TaskDeadline(timeout),
            )
        if cancelled():
            raise TaskCancelled("任务已取消。")
        on_status("delivering")
        await _deliver_result(application, job, result)
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        with stage_lock:
            stage = current_stage["value"]
        diagnosis = (
            exc.diagnosis
            if isinstance(exc, JobExecutionFailure)
            else diagnose_exception(exc)
        )
        raise JobExecutionFailure(stage, diagnosis) from exc


async def _download_telegram_audio(
    application: Application,
    job: TelegramJob,
) -> Path:
    if not job.telegram_file_id:
        raise FileNotFoundError("原音频已过期，请重新发送文件。")
    policy: MediaPolicy = application.bot_data["media_policy"]
    paths: BotPaths = application.bot_data["paths"]
    safe_name = sanitize_upload_name(job.original_filename, default="voice.ogg")
    destination = (
        paths.uploads_dir / f"tg_{job.user_id}_{uuid.uuid4().hex}_{safe_name}"
    )
    try:
        telegram_file = await application.bot.get_file(job.telegram_file_id)
    except BadRequest as exc:
        if "too big" in str(exc).lower():
            raise DownloadLimitExceeded(
                "文件超过 Telegram Bot API 的下载限制。"
            ) from exc
        raise
    file_size = int(getattr(telegram_file, "file_size", 0) or 0)
    if file_size and file_size > policy.max_media_bytes:
        raise DownloadLimitExceeded("音频文件超过服务端大小限制。")
    try:
        await telegram_file.download_to_drive(custom_path=str(destination))
        if destination.stat().st_size > policy.max_media_bytes:
            raise DownloadLimitExceeded("音频文件超过服务端大小限制。")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _remove_paths(paths) -> None:
    for raw_path in paths:
        try:
            Path(raw_path).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Unable to delete job file: %s", Path(raw_path).name
            )


async def _deliver_result(
    application: Application,
    job: TelegramJob,
    result: TranscriptionResult,
) -> None:
    cache: ResultCache = application.bot_data["result_cache"]
    cache.put(job.job_id, result.transcript)
    transcript = result.transcript or ""
    transcript_chunks = split_telegram_text(transcript)
    is_long = len(transcript_chunks) > 1
    paths: BotPaths = application.bot_data["paths"]
    output_path = paths.outputs_dir / (
        f"{job.job_id}_"
        f"{sanitize_upload_name(result.filename_stem, default='transcript')}.txt"
    )
    delivered = False
    try:
        for chunk in transcript_chunks:
            await application.bot.send_message(
                chat_id=job.chat_id,
                text=chunk,
            )
        output_path.write_text(transcript, encoding="utf-8")
        with output_path.open("rb") as handle:
            await application.bot.send_document(
                chat_id=job.chat_id,
                document=handle,
                filename=f"{result.filename_stem}.txt",
                caption="转写完成。",
            )
        delivered = True
    finally:
        output_path.unlink(missing_ok=True)
        if delivered:
            _remove_paths(result.cleanup_paths)

    await _safe_edit_status(
        application,
        job,
        "<b>转写完成</b>\n结果已发送。",
        build_result_keyboard(job.job_id, include_full=is_long),
    )


def _safe_trace_locations(exc: BaseException) -> str:
    selected = None
    current: Optional[BaseException] = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if current.__traceback__ is not None:
            selected = current
        current = current.__cause__ or current.__context__
    if selected is not None:
        frames = traceback.extract_tb(selected.__traceback__)[-8:]
        if frames:
            return " > ".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in frames
            )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "telegram_jobs.py:_worker(timeout_boundary)"
    return "unavailable"


async def _on_job_update(
    application: Application,
    job: TelegramJob,
    event: str,
    payload,
) -> None:
    if event == "running":
        await _safe_edit_status(
            application,
            job,
            f"<b>开始处理</b>\n任务：{html_code(job.job_id[:8])}",
            build_cancel_job_keyboard(job.job_id),
        )
    elif event == "succeeded":
        return
    elif event == "cancelled":
        cleanup_paths = [job.audio_path] if job.audio_path else []
        if isinstance(payload, TranscriptionResult):
            cleanup_paths.extend(payload.cleanup_paths)
        _remove_paths(cleanup_paths)
        await _safe_edit_status(application, job, "<b>任务已取消</b>", build_home_keyboard())
    elif event == "failed":
        failure = payload if isinstance(payload, BaseException) else RuntimeError()
        diagnosis = (
            failure.diagnosis
            if isinstance(failure, JobExecutionFailure)
            else diagnose_exception(failure)
        )
        trace_locations = _safe_trace_locations(failure)
        logger.error(
            "telegram_job_failed job_id=%s source=%s stage=%s category=%s "
            "error_type=%s detail=%s trace=%s",
            job.job_id,
            job.source_type,
            job.stage or "preparing",
            diagnosis.code,
            diagnosis.error_type,
            diagnosis.log_detail or "-",
            trace_locations,
        )
        reason = job.error_message or diagnosis.user_message
        await _safe_edit_status(
            application,
            job,
            render_job_failure(job.stage, reason),
            build_failed_job_keyboard(job.job_id),
        )


async def configure_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def handle_telegram_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    error = context.error if isinstance(context.error, BaseException) else RuntimeError()
    diagnosis = diagnose_exception(error)
    logger.error(
        "telegram_update_failed update_type=%s category=%s error_type=%s "
        "detail=%s trace=%s",
        type(update).__name__ if update is not None else "None",
        diagnosis.code,
        diagnosis.error_type,
        diagnosis.log_detail or "-",
        _safe_trace_locations(error),
    )


async def _maintenance_loop(application: Application) -> None:
    while True:
        try:
            application.bot_data["result_cache"].evict()
            manager: TelegramJobManager = application.bot_data["job_manager"]
            active_paths = {
                job.audio_path
                for job in manager.snapshot()
                if job.audio_path and job.status in {"queued", "running", "cancelling"}
            }
            cleanup_expired_media(
                application.bot_data["paths"],
                active_paths=active_paths,
                max_age_seconds=24 * 3600,
            )
            cleanup_expired_outputs(
                application.bot_data["paths"],
                max_age_seconds=7 * 86400,
            )
            manager.prune_terminal(max_age_seconds=24 * 3600)
        except Exception:
            logger.exception("Telegram maintenance pass failed")
        await asyncio.sleep(3600)


async def initialize_services(application: Application) -> None:
    if application.bot_data.get("services_started"):
        return
    await configure_bot_commands(application)
    await application.bot_data["job_manager"].start()
    application.bot_data["maintenance_task"] = asyncio.create_task(
        _maintenance_loop(application), name="telegram-maintenance"
    )
    application.bot_data["services_started"] = True

    for job in application.bot_data["job_manager"].interrupted_jobs():
        try:
            await application.bot.send_message(
                chat_id=job.chat_id,
                text=f"任务 {job.job_id[:8]} 因服务重启而中断。",
                reply_markup=build_failed_job_keyboard(job.job_id),
            )
            application.bot_data["job_manager"].mark_restart_notified(job.job_id)
        except Exception:
            logger.warning("Unable to notify interrupted job %s", job.job_id)


async def shutdown_services(application: Application) -> None:
    task = application.bot_data.pop("maintenance_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    manager = application.bot_data.get("job_manager")
    if manager is not None:
        await manager.stop()
    application.bot_data["services_started"] = False


def build_application() -> Application:
    token = os.getenv("ENV_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少 ENV_BOT_TOKEN，请先在 .env 中配置 Telegram Bot Token。")

    paths = BotPaths.from_environ(ROOT_DIR)
    paths.ensure_directories()
    config_store = GlobalConfigStore(paths.global_config_file)
    migrate_legacy_user_settings(paths.state_file, config_store)
    run_legacy_media_cleanup(paths, config_store)
    state_store = BotStateStore(str(paths.state_file))
    secret = os.getenv("ENV_BOT_SECRET", "").strip()
    state_store.bind_legacy_authorizations(secret)
    try:
        allowed_user_ids = parse_user_ids(os.getenv("TG_ALLOWED_USER_IDS", ""))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    settings = config_store.get()
    key_pool = GeminiKeyPool(settings.gemini_api_keys)
    media_policy = MediaPolicy.from_environ()
    transcription_service = TranscriptionService(
        config_store,
        key_pool,
        work_dir=paths.uploads_dir,
        media_policy=media_policy,
    )

    application = (
        Application.builder()
        .token(token)
        .post_init(initialize_services)
        .post_shutdown(shutdown_services)
        .build()
    )

    async def executor(job: TelegramJob, cancelled: Callable[[], bool]):
        return await _execute_job(application, job, cancelled)

    async def on_update(job: TelegramJob, event: str, payload):
        await _on_job_update(application, job, event, payload)

    job_manager = TelegramJobManager(
        JobStore(paths.jobs_file),
        executor,
        max_concurrent_jobs=int(os.getenv("TG_MAX_CONCURRENT_JOBS", "1")),
        task_timeout_seconds=media_policy.task_timeout_seconds,
        on_update=on_update,
    )
    application.bot_data.update(
        {
            "paths": paths,
            "store": state_store,
            "config_store": config_store,
            "key_pool": key_pool,
            "media_policy": media_policy,
            "transcription_service": transcription_service,
            "job_manager": job_manager,
            "result_cache": ResultCache(),
            "allowed_user_ids": allowed_user_ids,
            "bot_secret": secret,
            "key_validator": _validate_api_key,
            "model_catalog_loader": list_current_channel_models,
            "channel_health_checker": check_current_channel,
            "services_started": False,
        }
    )

    application.add_error_handler(handle_telegram_error)

    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start_command, filters=private))
    application.add_handler(CommandHandler("settings", settings_command, filters=private))
    application.add_handler(CommandHandler("help", help_command, filters=private))
    application.add_handler(CommandHandler("cancel", cancel_command, filters=private))
    for command in (
        "setauth",
        "setkey",
        "setvertexjson",
        "setvertexproject",
        "setvertexlocation",
        "setmodel",
        "setprompt",
        "resetprompt",
    ):
        application.add_handler(CommandHandler(command, legacy_settings_command, filters=private))
    application.add_handler(CommandHandler("setsource", legacy_source_command, filters=private))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(
        MessageHandler(
            private & (filters.AUDIO | filters.VOICE | filters.Document.AUDIO),
            handle_audio_message,
        )
    )
    application.add_handler(
        MessageHandler(private & filters.TEXT & ~filters.COMMAND, handle_text_message)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS, reject_group_message)
    )
    return application


async def start_embedded_polling(application: Application) -> None:
    updater = application.updater
    if updater is None:
        raise RuntimeError("Telegram updater 不可用，无法启动 polling。")
    try:
        await application.initialize()
        await initialize_services(application)
        await application.start()
        await updater.start_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        await shutdown_services(application)
        try:
            await application.stop()
        finally:
            await application.shutdown()
        raise
    logger.info("Telegram bot polling started in embedded mode.")


async def stop_embedded_polling(application: Application) -> None:
    updater = application.updater
    try:
        if updater is not None and updater.running:
            await updater.stop()
        await shutdown_services(application)
    finally:
        try:
            if application.running:
                await application.stop()
        finally:
            await application.shutdown()
    logger.info("Telegram bot polling stopped.")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )
    # HTTPX logs Telegram API URLs at INFO level, and those URLs contain the
    # bot token. Keep transport internals out of persistent service logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

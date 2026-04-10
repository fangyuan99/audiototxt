import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except Exception as exc:  # pragma: no cover - runtime guidance only
    raise RuntimeError(
        "缺少 python-dotenv 依赖，请先执行 `pip install -r requirements.txt`。"
    ) from exc

try:
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        MenuButtonCommands,
        Message,
        ReplyKeyboardRemove,
        Update,
    )
    from telegram.constants import ChatAction
    from telegram.error import BadRequest
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except Exception as exc:  # pragma: no cover - runtime guidance only
    raise RuntimeError(
        "缺少 python-telegram-bot 依赖，请先执行 `pip install -r requirements.txt`。"
    ) from exc

from bot_state import (
    BotStateStore,
    DEFAULT_AUTH_MODE,
    DEFAULT_VERTEX_LOCATION,
    SUPPORTED_SOURCE_TYPES,
    UserSettings,
    mask_api_key,
)
from main import (
    AUTH_MODE_GEMINI_API_KEY,
    AUTH_MODE_VERTEX_AI_JSON,
    _extract_first_url,
    build_auth_config,
    download_audio_from_direct_url,
    download_video_and_extract_audio,
    fetch_douyin_mp3_via_tiksave,
    transcribe_audio_streaming,
    transcribe_youtube_url_streaming,
)


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")
logger = logging.getLogger(__name__)

DATA_DIR = ROOT_DIR / "data" / "telegram_bot"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", str(DATA_DIR / "state.json")))
MAX_MESSAGE_LENGTH = 3800
STREAM_EDIT_INTERVAL_SECONDS = 1.0
STREAM_MIN_BUFFER = 80
PENDING_ACTION_KEY = "pending_action"

MENU_START = "开始使用"
MENU_HELP = "帮助"
MENU_SETTINGS = "当前配置"
MENU_SET_AUTH = "设置认证方式"
MENU_SET_KEY = "设置 Gemini Key"
MENU_SET_VERTEX_JSON = "设置 Vertex JSON"
MENU_SET_VERTEX_PROJECT = "设置 Vertex Project"
MENU_SET_VERTEX_LOCATION = "设置 Vertex Location"
MENU_SET_MODEL = "设置模型"
MENU_SET_SOURCE = "设置来源类型"
MENU_SET_PROMPT = "设置 Prompt"
MENU_RESET_PROMPT = "重置 Prompt"
MENU_CANCEL = "取消当前输入"

CALLBACK_AUTH_GEMINI = "auth:gemini"
CALLBACK_AUTH_VERTEX = "auth:vertex"
CALLBACK_SOURCE_AUDIO = "source:audio"
CALLBACK_SOURCE_YOUTUBE = "source:youtube"
CALLBACK_SOURCE_VIDEO_URL = "source:video_url"
CALLBACK_SOURCE_DOUYIN = "source:douyin"

AUTH_CHOICE_GEMINI = "使用 Gemini"
AUTH_CHOICE_VERTEX = "使用 Vertex"

SOURCE_CHOICE_AUDIO = "音频文件"
SOURCE_CHOICE_YOUTUBE = "YouTube 链接"
SOURCE_CHOICE_VIDEO_URL = "视频直链"
SOURCE_CHOICE_DOUYIN = "抖音分享"

AUTH_CHOICE_BUTTONS = {
    AUTH_CHOICE_GEMINI,
    AUTH_CHOICE_VERTEX,
}

SOURCE_CHOICE_BUTTONS = {
    SOURCE_CHOICE_AUDIO,
    SOURCE_CHOICE_YOUTUBE,
    SOURCE_CHOICE_VIDEO_URL,
    SOURCE_CHOICE_DOUYIN,
}

TEXT_INPUT_PENDING_ACTIONS = {
    "awaiting_secret",
    "set_api_key",
    "set_vertex_json",
    "set_vertex_project",
    "set_vertex_location",
    "set_model_name",
    "set_promoters",
}

MENU_BUTTONS = {
    MENU_START,
    MENU_HELP,
    MENU_SETTINGS,
    MENU_SET_AUTH,
    MENU_SET_KEY,
    MENU_SET_VERTEX_JSON,
    MENU_SET_VERTEX_PROJECT,
    MENU_SET_VERTEX_LOCATION,
    MENU_SET_MODEL,
    MENU_SET_SOURCE,
    MENU_SET_PROMPT,
    MENU_RESET_PROMPT,
    MENU_CANCEL,
}

BOT_COMMANDS = [
    BotCommand("start", "开始使用并完成验证"),
    BotCommand("help", "查看使用说明"),
    BotCommand("settings", "查看当前配置"),
    BotCommand("setauth", "设置认证方式"),
    BotCommand("setkey", "设置 Gemini API Key"),
    BotCommand("setvertexjson", "设置 Vertex JSON"),
    BotCommand("setvertexproject", "设置 Vertex Project"),
    BotCommand("setvertexlocation", "设置 Vertex Location"),
    BotCommand("setmodel", "设置模型"),
    BotCommand("setsource", "设置来源类型"),
    BotCommand("setprompt", "设置 Prompt"),
    BotCommand("resetprompt", "重置 Prompt"),
    BotCommand("cancel", "取消当前输入"),
]


@dataclass
class TranscriptionResult:
    transcript: str
    output_path: Path


def make_store() -> BotStateStore:
    return BotStateStore(str(STATE_FILE))


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:80] or f"transcript_{int(time.time())}"


def render_settings(settings: UserSettings) -> str:
    prompt_status = "默认内置 Prompt" if not settings.promoters else f"已自定义（{len(settings.promoters)} 字符）"
    auth_lines = [
        f"- 认证方式: {settings.auth_mode}",
        f"- API Key: {mask_api_key(settings.api_key)}",
    ]
    if settings.auth_mode == AUTH_MODE_VERTEX_AI_JSON:
        auth_lines.extend(
            [
                f"- Vertex JSON: {'已设置' if settings.vertex_json else '未设置'}",
                f"- Vertex Project: {settings.vertex_project or '未设置'}",
                f"- Vertex Location: {settings.vertex_location or DEFAULT_VERTEX_LOCATION}",
            ]
        )
    return (
        "当前配置：\n"
        + "\n".join(auth_lines)
        + "\n"
        f"- 模型: {settings.model_name}\n"
        f"- 来源类型: {settings.source_type}\n"
        f"- Promoters: {prompt_status}"
    )


def build_auth_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(AUTH_CHOICE_GEMINI, callback_data=CALLBACK_AUTH_GEMINI),
                InlineKeyboardButton(AUTH_CHOICE_VERTEX, callback_data=CALLBACK_AUTH_VERTEX),
            ]
        ]
    )


def build_source_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(SOURCE_CHOICE_AUDIO, callback_data=CALLBACK_SOURCE_AUDIO),
                InlineKeyboardButton(SOURCE_CHOICE_YOUTUBE, callback_data=CALLBACK_SOURCE_YOUTUBE),
            ],
            [
                InlineKeyboardButton(SOURCE_CHOICE_VIDEO_URL, callback_data=CALLBACK_SOURCE_VIDEO_URL),
                InlineKeyboardButton(SOURCE_CHOICE_DOUYIN, callback_data=CALLBACK_SOURCE_DOUYIN),
            ],
        ]
    )


def resolve_reply_markup(
    settings: Optional[UserSettings] = None,
    pending_action: Optional[str] = None,
) -> InlineKeyboardMarkup | ReplyKeyboardRemove:
    if pending_action == "set_auth_mode":
        return build_auth_inline_keyboard()
    if pending_action == "set_source_type":
        return build_source_inline_keyboard()
    return ReplyKeyboardRemove()


async def reply_with_state(
    message: Message,
    text: str,
    *,
    settings: Optional[UserSettings] = None,
    pending_action: Optional[str] = None,
) -> None:
    await message.reply_text(
        text,
        reply_markup=resolve_reply_markup(settings=settings, pending_action=pending_action),
    )


def build_help_text(settings: Optional[UserSettings] = None) -> str:
    lines = [
        "使用方式：",
        "1. 优先用 Telegram 左下角菜单触发命令，不需要常驻底部键盘。",
        "2. 点击 `/setauth` 后，我会在消息里弹出 Gemini / Vertex 选择按钮。",
        "3. 点击对应命令后继续发送 Gemini Key / Vertex JSON / Project / Location。",
        "4. 点击 `/setmodel` 可直接发送新的模型名称，默认 `gemini-2.5-flash`。",
        "5. 点击 `/setprompt` 可直接发送新的转写 Prompt。",
        "6. 点击 `/setsource` 后，我会在消息里弹出 audio / youtube / video_url / douyin 选择按钮。",
        "7. 配好后直接发送内容：",
        "   - `audio`: 发送音频文件、语音或音频 document",
        "   - `youtube`: 发送 YouTube 链接",
        "   - `video_url`: 发送视频直链",
        "   - `douyin`: 发送抖音分享文案或短链",
        "8. 点击 `/settings` 查看当前保存的配置。",
        "9. 点击 `/cancel` 可退出当前设置流程。",
        "10. 旧的底部按钮文本如果还留在客户端里，继续点也仍然兼容。",
    ]
    if settings is not None:
        lines.extend(["", render_settings(settings)])
    return "\n".join(lines)


def parse_auth_mode(value: str) -> Optional[str]:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "gemini": AUTH_MODE_GEMINI_API_KEY,
        "api_key": AUTH_MODE_GEMINI_API_KEY,
        "gemini_api_key": AUTH_MODE_GEMINI_API_KEY,
        "gemini api key": AUTH_MODE_GEMINI_API_KEY,
        "使用 gemini": AUTH_MODE_GEMINI_API_KEY,
        "vertex": AUTH_MODE_VERTEX_AI_JSON,
        "vertex_ai": AUTH_MODE_VERTEX_AI_JSON,
        "vertex_json": AUTH_MODE_VERTEX_AI_JSON,
        "vertex_ai_json": AUTH_MODE_VERTEX_AI_JSON,
        "vertex ai json": AUTH_MODE_VERTEX_AI_JSON,
        "使用 vertex": AUTH_MODE_VERTEX_AI_JSON,
    }
    return aliases.get(normalized)


def parse_source_type(value: str) -> Optional[str]:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "audio": "audio",
        "音频": "audio",
        "音频文件": "audio",
        "youtube": "youtube",
        "youtube 链接": "youtube",
        "video_url": "video_url",
        "video url": "video_url",
        "视频直链": "video_url",
        "douyin": "douyin",
        "抖音": "douyin",
        "抖音分享": "douyin",
    }
    return aliases.get(normalized)


async def safe_edit_text(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


async def ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[UserSettings]:
    user = update.effective_user
    if user is None or update.effective_message is None:
        return None

    store = context.application.bot_data["store"]
    settings = store.get_user(user.id)
    if settings.authorized:
        return settings

    context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
    await reply_with_state(
        update.effective_message,
        "请先发送机器人密码完成首次验证。",
        settings=settings,
        pending_action="awaiting_secret",
    )
    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    store = context.application.bot_data["store"]
    settings = store.get_user(user.id)
    if settings.authorized:
        await reply_with_state(
            message,
            "机器人已就绪。\n\n" + build_help_text(settings),
            settings=settings,
        )
        return

    context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
    await reply_with_state(
        message,
        "欢迎使用 AudioToTxt Telegram 机器人。\n"
        "首次使用请先发送密码完成验证。\n"
        "验证通过后，请从 Telegram 左下角菜单继续触发配置命令。",
        settings=settings,
        pending_action="awaiting_secret",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    store = context.application.bot_data["store"]
    settings = store.get_user(user.id)
    if not settings.authorized:
        await reply_with_state(message, "请先点击“开始使用”并发送密码完成验证。", settings=settings)
        return
    await reply_with_state(message, build_help_text(settings), settings=settings)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return
    await reply_with_state(message, render_settings(settings), settings=settings)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    store = context.application.bot_data["store"]
    settings = store.get_user(user.id)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "已取消当前输入流程。", settings=settings)


def _read_command_value(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip()


async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_api_key"
        await reply_with_state(
            message,
            "请直接发送新的 Gemini API Key。",
            settings=settings,
            pending_action="set_api_key",
        )
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, auth_mode=AUTH_MODE_GEMINI_API_KEY, api_key=value)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "API Key 已保存。\n\n" + render_settings(settings), settings=settings)


async def setauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_auth_mode"
        await reply_with_state(
            message,
            "请选择认证方式。也可以直接发送 `/setauth gemini` 或 `/setauth vertex`。",
            settings=settings,
            pending_action="set_auth_mode",
        )
        return

    auth_mode = parse_auth_mode(value)
    if auth_mode is None:
        await reply_with_state(
            message,
            "认证方式无效，请选择 Gemini 或 Vertex，或直接发送 `/setauth gemini` / `/setauth vertex`。",
            settings=settings,
            pending_action="set_auth_mode",
        )
        return

    await apply_auth_mode_selection(message, context, settings, auth_mode)


async def setvertexjson_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_vertex_json"
        await reply_with_state(
            message,
            "请直接发送完整的 Vertex AI service account JSON。",
            settings=settings,
            pending_action="set_vertex_json",
        )
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(
        settings.user_id,
        auth_mode=AUTH_MODE_VERTEX_AI_JSON,
        vertex_json=value,
    )
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "Vertex AI JSON 已保存。\n\n" + render_settings(settings), settings=settings)


async def setvertexproject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_vertex_project"
        await reply_with_state(
            message,
            "请直接发送 Vertex project ID。",
            settings=settings,
            pending_action="set_vertex_project",
        )
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, vertex_project=value.strip())
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "Vertex project 已保存。\n\n" + render_settings(settings), settings=settings)


async def setvertexlocation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_vertex_location"
        await reply_with_state(
            message,
            "请直接发送 Vertex location，例如 `us-central1`。",
            settings=settings,
            pending_action="set_vertex_location",
        )
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, vertex_location=value.strip())
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "Vertex location 已保存。\n\n" + render_settings(settings), settings=settings)


async def setmodel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_model_name"
        await reply_with_state(
            message,
            "请直接发送新的模型名称，例如 `gemini-2.5-flash`。",
            settings=settings,
            pending_action="set_model_name",
        )
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, model_name=value)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "模型已保存。\n\n" + render_settings(settings), settings=settings)


async def setpromoters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_promoters"
        await reply_with_state(
            message,
            "请直接发送新的 Prompt 文案。需要恢复默认可点击“重置 Prompt”。",
            settings=settings,
            pending_action="set_promoters",
        )
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, promoters=value)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "Prompt 已保存。\n\n" + render_settings(settings), settings=settings)


async def resetpromoters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, promoters="")
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(
        message,
        "Prompt 已重置为默认内置内容。\n\n" + render_settings(settings),
        settings=settings,
    )


async def setsource_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    value = _read_command_value(context)
    if not value:
        context.user_data[PENDING_ACTION_KEY] = "set_source_type"
        await reply_with_state(
            message,
            "请选择来源类型。也可以直接发送 `/setsource audio` 等命令。",
            settings=settings,
            pending_action="set_source_type",
        )
        return

    normalized = parse_source_type(value)
    if normalized not in SUPPORTED_SOURCE_TYPES:
        await reply_with_state(
            message,
            "来源类型无效，请选择 audio / youtube / video_url / douyin，或直接发送 `/setsource audio` 等命令。",
            settings=settings,
            pending_action="set_source_type",
        )
        return

    await apply_source_type_selection(message, context, settings, normalized)


async def apply_auth_mode_selection(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: UserSettings,
    auth_mode: str,
) -> None:
    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, auth_mode=auth_mode)
    next_action = None
    next_tip = ""
    if auth_mode == AUTH_MODE_GEMINI_API_KEY and not settings.api_key:
        next_action = "set_api_key"
        context.user_data[PENDING_ACTION_KEY] = next_action
        next_tip = "\n\n下一步请直接发送 Gemini API Key。"
    elif auth_mode == AUTH_MODE_VERTEX_AI_JSON and not settings.vertex_json:
        next_action = "set_vertex_json"
        context.user_data[PENDING_ACTION_KEY] = next_action
        next_tip = "\n\n下一步请直接发送 Vertex AI JSON。"
    else:
        context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(
        message,
        "认证方式已保存。\n\n" + render_settings(settings) + next_tip,
        settings=settings,
        pending_action=next_action,
    )


async def apply_source_type_selection(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: UserSettings,
    source_type: str,
) -> None:
    store = context.application.bot_data["store"]
    settings = store.upsert_user(settings.user_id, source_type=source_type)
    context.user_data.pop(PENDING_ACTION_KEY, None)
    await reply_with_state(message, "来源类型已保存。\n\n" + render_settings(settings), settings=settings)


async def handle_menu_action(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: UserSettings,
    text: str,
) -> bool:
    if text not in MENU_BUTTONS:
        return False

    context.user_data.pop(PENDING_ACTION_KEY, None)

    if text == MENU_START:
        if settings.authorized:
            await reply_with_state(
                message,
                "机器人已就绪。\n\n" + build_help_text(settings),
                settings=settings,
            )
            return True
        context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
        await reply_with_state(
            message,
            "欢迎使用 AudioToTxt Telegram 机器人。\n"
            "首次使用请先发送密码完成验证。\n"
            "验证通过后，请从 Telegram 左下角菜单继续触发配置命令。",
            settings=settings,
            pending_action="awaiting_secret",
        )
        return True

    if text == MENU_HELP:
        if settings.authorized:
            await reply_with_state(message, build_help_text(settings), settings=settings)
        else:
            await reply_with_state(
                message,
                "请先点击“开始使用”并发送密码完成验证。",
                settings=settings,
            )
        return True

    if text == MENU_CANCEL:
        await reply_with_state(message, "已取消当前输入流程。", settings=settings)
        return True

    if not settings.authorized:
        context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
        await reply_with_state(
            message,
            "请先点击“开始使用”并发送密码完成验证。",
            settings=settings,
            pending_action="awaiting_secret",
        )
        return True

    if text == MENU_SETTINGS:
        await reply_with_state(message, render_settings(settings), settings=settings)
        return True

    if text == MENU_SET_AUTH:
        context.user_data[PENDING_ACTION_KEY] = "set_auth_mode"
        await reply_with_state(
            message,
            "请选择认证方式。也可以直接发送 `/setauth gemini` 或 `/setauth vertex`。",
            settings=settings,
            pending_action="set_auth_mode",
        )
        return True

    if text == MENU_SET_KEY:
        context.user_data[PENDING_ACTION_KEY] = "set_api_key"
        await reply_with_state(
            message,
            "请直接发送新的 Gemini API Key。",
            settings=settings,
            pending_action="set_api_key",
        )
        return True

    if text == MENU_SET_VERTEX_JSON:
        context.user_data[PENDING_ACTION_KEY] = "set_vertex_json"
        await reply_with_state(
            message,
            "请直接发送完整的 Vertex AI service account JSON。",
            settings=settings,
            pending_action="set_vertex_json",
        )
        return True

    if text == MENU_SET_VERTEX_PROJECT:
        context.user_data[PENDING_ACTION_KEY] = "set_vertex_project"
        await reply_with_state(
            message,
            "请直接发送 Vertex project ID。",
            settings=settings,
            pending_action="set_vertex_project",
        )
        return True

    if text == MENU_SET_VERTEX_LOCATION:
        context.user_data[PENDING_ACTION_KEY] = "set_vertex_location"
        await reply_with_state(
            message,
            "请直接发送 Vertex location，例如 `us-central1`。",
            settings=settings,
            pending_action="set_vertex_location",
        )
        return True

    if text == MENU_SET_MODEL:
        context.user_data[PENDING_ACTION_KEY] = "set_model_name"
        await reply_with_state(
            message,
            "请直接发送新的模型名称，例如 `gemini-2.5-flash`。",
            settings=settings,
            pending_action="set_model_name",
        )
        return True

    if text == MENU_SET_SOURCE:
        context.user_data[PENDING_ACTION_KEY] = "set_source_type"
        await reply_with_state(
            message,
            "请选择来源类型。也可以直接发送 `/setsource audio` 等命令。",
            settings=settings,
            pending_action="set_source_type",
        )
        return True

    if text == MENU_SET_PROMPT:
        context.user_data[PENDING_ACTION_KEY] = "set_promoters"
        await reply_with_state(
            message,
            "请直接发送新的 Prompt 文案。需要恢复默认可点击“重置 Prompt”。",
            settings=settings,
            pending_action="set_promoters",
        )
        return True

    if text == MENU_RESET_PROMPT:
        store = context.application.bot_data["store"]
        updated_settings = store.upsert_user(settings.user_id, promoters="")
        await reply_with_state(
            message,
            "Prompt 已重置为默认内置内容。\n\n" + render_settings(updated_settings),
            settings=updated_settings,
        )
        return True

    return False


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None:
        return

    await query.answer()

    store = context.application.bot_data["store"]
    settings = store.get_user(user.id)
    message = query.message
    data = query.data or ""

    if not settings.authorized:
        context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
        await reply_with_state(
            message,
            "请先发送 `/start` 并完成密码验证。",
            settings=settings,
            pending_action="awaiting_secret",
        )
        return

    if data == CALLBACK_AUTH_GEMINI:
        await apply_auth_mode_selection(message, context, settings, AUTH_MODE_GEMINI_API_KEY)
        return

    if data == CALLBACK_AUTH_VERTEX:
        await apply_auth_mode_selection(message, context, settings, AUTH_MODE_VERTEX_AI_JSON)
        return

    if data == CALLBACK_SOURCE_AUDIO:
        await apply_source_type_selection(message, context, settings, "audio")
        return

    if data == CALLBACK_SOURCE_YOUTUBE:
        await apply_source_type_selection(message, context, settings, "youtube")
        return

    if data == CALLBACK_SOURCE_VIDEO_URL:
        await apply_source_type_selection(message, context, settings, "video_url")
        return

    if data == CALLBACK_SOURCE_DOUYIN:
        await apply_source_type_selection(message, context, settings, "douyin")
        return


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return

    store = context.application.bot_data["store"]
    settings = store.get_user(user.id)
    pending_action = context.user_data.get(PENDING_ACTION_KEY)
    text = message.text.strip()

    if await handle_menu_action(message, context, settings, text):
        return

    if text in AUTH_CHOICE_BUTTONS:
        if not settings.authorized:
            context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
            await reply_with_state(
                message,
                "请先点击“开始使用”并发送密码完成验证。",
                settings=settings,
                pending_action="awaiting_secret",
            )
            return
        auth_mode = parse_auth_mode(text)
        if auth_mode is None:
            await reply_with_state(
                message,
                "认证方式无效，请选择 Gemini 或 Vertex，或直接发送 `/setauth gemini` / `/setauth vertex`。",
                settings=settings,
                pending_action="set_auth_mode",
            )
            return
        await apply_auth_mode_selection(message, context, settings, auth_mode)
        return

    if text in SOURCE_CHOICE_BUTTONS:
        if not settings.authorized:
            context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
            await reply_with_state(
                message,
                "请先点击“开始使用”并发送密码完成验证。",
                settings=settings,
                pending_action="awaiting_secret",
            )
            return
        source_type = parse_source_type(text)
        if source_type is None:
            await reply_with_state(
                message,
                "来源类型无效，请选择 audio / youtube / video_url / douyin，或直接发送 `/setsource audio` 等命令。",
                settings=settings,
                pending_action="set_source_type",
            )
            return
        await apply_source_type_selection(message, context, settings, source_type)
        return

    if pending_action == "awaiting_secret":
        secret = os.getenv("ENV_BOT_SECRET", "").strip()
        if not secret:
            await reply_with_state(
                message,
                "服务端未配置 ENV_BOT_SECRET，无法完成验证。",
                settings=settings,
                pending_action="awaiting_secret",
            )
            return
        if text != secret:
            await reply_with_state(
                message,
                "密码不正确，请重试。",
                settings=settings,
                pending_action="awaiting_secret",
            )
            return
        settings = store.authorize_user(
            user.id,
            username=user.username or "",
            first_name=user.first_name or "",
        )
        context.user_data[PENDING_ACTION_KEY] = "set_auth_mode"
        await reply_with_state(
            message,
            "验证成功。\n\n"
            "先从左下角菜单点 `/setauth` 选择认证方式；后续设置也都从菜单触发。\n\n"
            + build_help_text(settings),
            settings=settings,
            pending_action="set_auth_mode",
        )
        return

    if not settings.authorized:
        context.user_data[PENDING_ACTION_KEY] = "awaiting_secret"
        await reply_with_state(
            message,
            "请先点击“开始使用”并发送密码完成验证。",
            settings=settings,
            pending_action="awaiting_secret",
        )
        return

    if pending_action == "set_api_key":
        settings = store.upsert_user(user.id, auth_mode=AUTH_MODE_GEMINI_API_KEY, api_key=text)
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await reply_with_state(message, "API Key 已保存。\n\n" + render_settings(settings), settings=settings)
        return

    if pending_action == "set_auth_mode":
        auth_mode = parse_auth_mode(text)
        if auth_mode is None:
            await reply_with_state(
                message,
                "认证方式无效，请选择 Gemini 或 Vertex，或直接发送 `/setauth gemini` / `/setauth vertex`。",
                settings=settings,
                pending_action="set_auth_mode",
            )
            return
        await apply_auth_mode_selection(message, context, settings, auth_mode)
        return

    if pending_action == "set_vertex_json":
        settings = store.upsert_user(
            user.id,
            auth_mode=AUTH_MODE_VERTEX_AI_JSON,
            vertex_json=text,
        )
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await reply_with_state(message, "Vertex AI JSON 已保存。\n\n" + render_settings(settings), settings=settings)
        return

    if pending_action == "set_vertex_project":
        settings = store.upsert_user(user.id, vertex_project=text)
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await reply_with_state(message, "Vertex project 已保存。\n\n" + render_settings(settings), settings=settings)
        return

    if pending_action == "set_vertex_location":
        settings = store.upsert_user(user.id, vertex_location=text)
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await reply_with_state(message, "Vertex location 已保存。\n\n" + render_settings(settings), settings=settings)
        return

    if pending_action == "set_model_name":
        settings = store.upsert_user(user.id, model_name=text)
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await reply_with_state(message, "模型已保存。\n\n" + render_settings(settings), settings=settings)
        return

    if pending_action == "set_promoters":
        settings = store.upsert_user(user.id, promoters=text)
        context.user_data.pop(PENDING_ACTION_KEY, None)
        await reply_with_state(message, "Prompt 已保存。\n\n" + render_settings(settings), settings=settings)
        return

    if pending_action == "set_source_type":
        normalized = parse_source_type(text)
        if normalized not in SUPPORTED_SOURCE_TYPES:
            await reply_with_state(
                message,
                "来源类型无效，请选择 audio / youtube / video_url / douyin，或直接发送 `/setsource audio` 等命令。",
                settings=settings,
                pending_action="set_source_type",
            )
            return
        await apply_source_type_selection(message, context, settings, normalized)
        return

    if settings.source_type == "audio":
        await reply_with_state(
            message,
            "当前来源类型是 audio，请发送音频文件、语音或音频 document。",
            settings=settings,
        )
        return

    if settings.source_type in {"youtube", "video_url"} and not _extract_first_url(text):
        await reply_with_state(
            message,
            "当前来源类型需要链接，请发送完整 URL。",
            settings=settings,
        )
        return

    await process_transcription(update, context, settings, text_input=text)


async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = await ensure_authorized(update, context)
    if message is None or settings is None:
        return

    if settings.source_type != "audio":
        await reply_with_state(
            message,
            f"当前保存的来源类型是 `{settings.source_type}`，请先点击“设置来源类型”并切换到 `audio` 再发送音频。",
            settings=settings,
        )
        return

    source_file = message.audio or message.voice or message.document
    if source_file is None:
        await reply_with_state(message, "未检测到可处理的音频文件。", settings=settings)
        return

    suffix = ".mp3"
    if getattr(source_file, "file_name", None):
        suffix = Path(source_file.file_name).suffix or suffix
    elif getattr(source_file, "mime_type", "") == "audio/ogg":
        suffix = ".ogg"

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"tg_{settings.user_id}_",
        suffix=suffix,
        dir=str(UPLOAD_DIR),
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    telegram_file = await source_file.get_file()
    await telegram_file.download_to_drive(custom_path=str(temp_path))
    await process_transcription(
        update,
        context,
        settings,
        audio_path=temp_path,
        original_filename=getattr(source_file, "file_name", temp_path.name),
    )


def resolve_auth_config(settings: UserSettings):
    auth_mode = settings.auth_mode or os.getenv("AUTH_MODE") or DEFAULT_AUTH_MODE
    return build_auth_config(
        auth_mode=auth_mode,
        api_key=settings.api_key,
        vertex_json=settings.vertex_json,
        vertex_project=settings.vertex_project,
        vertex_location=settings.vertex_location or DEFAULT_VERTEX_LOCATION,
    )


def save_transcript_file(user_id: int, source_type: str, transcript: str, name_hint: Optional[str]) -> Path:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = sanitize_name(name_hint or f"{source_type}_{int(time.time())}")
    output_path = OUTPUT_DIR / f"{user_id}_{stem}.txt"
    output_path.write_text(transcript, encoding="utf-8")
    return output_path


def execute_transcription(
    settings: UserSettings,
    source_type: str,
    *,
    text_input: Optional[str],
    audio_path: Optional[Path],
    original_filename: Optional[str],
    on_chunk,
    on_status,
) -> TranscriptionResult:
    auth_config = resolve_auth_config(settings)
    if auth_config.auth_mode == AUTH_MODE_GEMINI_API_KEY and not auth_config.api_key:
        raise RuntimeError("未设置 Gemini API Key，请先使用 /setkey 设置，或在 .env 里提供 GOOGLE_API_KEY。")
    if auth_config.auth_mode == AUTH_MODE_VERTEX_AI_JSON and not auth_config.vertex_json:
        raise RuntimeError(
            "未设置 Vertex AI JSON，请先使用 /setvertexjson 设置，或在 .env 里提供 GOOGLE_APPLICATION_CREDENTIALS。"
        )

    if source_type == "audio":
        if audio_path is None:
            raise RuntimeError("缺少音频文件。")
        on_status("开始转写音频")
        transcript = transcribe_audio_streaming(
            api_key=auth_config.api_key,
            audio_path=str(audio_path),
            model_name=settings.model_name,
            promoters=settings.promoters or None,
            on_chunk=on_chunk,
            auth_mode=auth_config.auth_mode,
            vertex_json=auth_config.vertex_json,
            vertex_project=auth_config.vertex_project,
            vertex_location=auth_config.vertex_location,
        )
        name_hint = Path(original_filename or audio_path.name).stem
        return TranscriptionResult(
            transcript=transcript,
            output_path=save_transcript_file(settings.user_id, source_type, transcript, name_hint),
        )

    if not text_input:
        raise RuntimeError("缺少文本输入。")

    if source_type == "youtube":
        on_status("开始处理 YouTube 链接")
        transcript = transcribe_youtube_url_streaming(
            api_key=auth_config.api_key,
            youtube_url=text_input,
            model_name=settings.model_name,
            promoters=settings.promoters or None,
            on_chunk=on_chunk,
            auth_mode=auth_config.auth_mode,
            vertex_json=auth_config.vertex_json,
            vertex_project=auth_config.vertex_project,
            vertex_location=auth_config.vertex_location,
        )
        name_hint = _extract_first_url(text_input) or f"youtube_{int(time.time())}"
        return TranscriptionResult(
            transcript=transcript,
            output_path=save_transcript_file(settings.user_id, source_type, transcript, name_hint),
        )

    if source_type == "video_url":
        on_status("下载视频并提取音频")
        local_audio_path = Path(download_video_and_extract_audio(text_input, str(UPLOAD_DIR)))
        on_status("开始转写视频音频")
        transcript = transcribe_audio_streaming(
            api_key=auth_config.api_key,
            audio_path=str(local_audio_path),
            model_name=settings.model_name,
            promoters=settings.promoters or None,
            on_chunk=on_chunk,
            auth_mode=auth_config.auth_mode,
            vertex_json=auth_config.vertex_json,
            vertex_project=auth_config.vertex_project,
            vertex_location=auth_config.vertex_location,
        )
        return TranscriptionResult(
            transcript=transcript,
            output_path=save_transcript_file(settings.user_id, source_type, transcript, local_audio_path.stem),
        )

    if source_type == "douyin":
        on_status("解析抖音分享内容")
        mp3_url, _, tiktok_id = fetch_douyin_mp3_via_tiksave(text_input)
        stem = f"douyin_{tiktok_id}" if tiktok_id else f"douyin_{int(time.time())}"
        on_status("下载抖音音频")
        local_audio_path = Path(
            download_audio_from_direct_url(
                mp3_url,
                output_dir=str(UPLOAD_DIR),
                preferred_ext="mp3",
                filename_stem=stem,
            )
        )
        on_status("开始转写抖音音频")
        transcript = transcribe_audio_streaming(
            api_key=auth_config.api_key,
            audio_path=str(local_audio_path),
            model_name=settings.model_name,
            promoters=settings.promoters or None,
            on_chunk=on_chunk,
            auth_mode=auth_config.auth_mode,
            vertex_json=auth_config.vertex_json,
            vertex_project=auth_config.vertex_project,
            vertex_location=auth_config.vertex_location,
        )
        return TranscriptionResult(
            transcript=transcript,
            output_path=save_transcript_file(settings.user_id, source_type, transcript, stem),
        )

    raise RuntimeError(f"不支持的来源类型：{source_type}")


async def stream_events(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    queue: "asyncio.Queue[Optional[dict]]",
    status_message: Message,
) -> None:
    current_message: Optional[Message] = None
    current_text = ""
    staged = ""
    last_edit_at = 0.0

    while True:
        timed_out = False
        try:
            event = await asyncio.wait_for(queue.get(), timeout=STREAM_EDIT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            event = {}
            timed_out = True

        reached_end = (not timed_out) and (event is None)
        if isinstance(event, dict):
            event_type = event.get("type")
            if event_type == "status":
                await safe_edit_text(status_message, f"状态：{event['data']}")
            elif event_type == "chunk":
                staged += event["data"]

        should_flush = staged and (
            reached_end
            or len(staged) >= STREAM_MIN_BUFFER
            or (time.monotonic() - last_edit_at) >= STREAM_EDIT_INTERVAL_SECONDS
        )

        if should_flush:
            while staged:
                if current_message is None:
                    chunk = staged[:MAX_MESSAGE_LENGTH]
                    staged = staged[MAX_MESSAGE_LENGTH:]
                    current_message = await context.bot.send_message(chat_id=chat_id, text=chunk)
                    current_text = chunk
                    last_edit_at = time.monotonic()
                    continue

                available = MAX_MESSAGE_LENGTH - len(current_text)
                if available <= 0:
                    chunk = staged[:MAX_MESSAGE_LENGTH]
                    staged = staged[MAX_MESSAGE_LENGTH:]
                    current_message = await context.bot.send_message(chat_id=chat_id, text=chunk)
                    current_text = chunk
                    last_edit_at = time.monotonic()
                    continue

                piece = staged[:available]
                staged = staged[available:]
                current_text += piece
                await safe_edit_text(current_message, current_text)
                last_edit_at = time.monotonic()

        if reached_end:
            break


async def process_transcription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: UserSettings,
    *,
    text_input: Optional[str] = None,
    audio_path: Optional[Path] = None,
    original_filename: Optional[str] = None,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    active_jobs = context.application.bot_data.setdefault("active_jobs", set())
    if settings.user_id in active_jobs:
        await message.reply_text("当前已有任务在执行，请等待上一任务完成。")
        return

    active_jobs.add(settings.user_id)
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    status_message = await message.reply_text("状态：任务已接收，准备开始")
    queue: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_chunk(delta: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"type": "chunk", "data": delta})

    def on_status(text: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"type": "status", "data": text})

    stream_task = asyncio.create_task(stream_events(context, chat.id, queue, status_message))

    try:
        result = await asyncio.to_thread(
            execute_transcription,
            settings,
            settings.source_type,
            text_input=text_input,
            audio_path=audio_path,
            original_filename=original_filename,
            on_chunk=on_chunk,
            on_status=on_status,
        )
        await queue.put({"type": "status", "data": "转写完成，正在整理结果"})
        await queue.put(None)
        await stream_task
        await safe_edit_text(status_message, "状态：转写完成")
        with result.output_path.open("rb") as transcript_file:
            await message.reply_document(
                document=transcript_file,
                filename=result.output_path.name,
                caption="转写完成，已附上 txt 文件。",
            )
    except Exception as exc:
        await queue.put(None)
        await stream_task
        await safe_edit_text(status_message, f"状态：任务失败 - {exc}")
        await message.reply_text(f"转写失败：{exc}")
    finally:
        active_jobs.discard(settings.user_id)


def build_application() -> Application:
    token = os.getenv("ENV_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少 ENV_BOT_TOKEN，请先在 .env 中配置 Telegram Bot Token。")

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    application = Application.builder().token(token).post_init(configure_bot_commands).build()
    application.bot_data["store"] = make_store()
    application.bot_data["active_jobs"] = set()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("setauth", setauth_command))
    application.add_handler(CommandHandler("setkey", setkey_command))
    application.add_handler(CommandHandler("setvertexjson", setvertexjson_command))
    application.add_handler(CommandHandler("setvertexproject", setvertexproject_command))
    application.add_handler(CommandHandler("setvertexlocation", setvertexlocation_command))
    application.add_handler(CommandHandler("setmodel", setmodel_command))
    application.add_handler(CommandHandler("setsource", setsource_command))
    application.add_handler(CommandHandler("setpromoters", setpromoters_command))
    application.add_handler(CommandHandler("setprompt", setpromoters_command))
    application.add_handler(CommandHandler("resetpromoters", resetpromoters_command))
    application.add_handler(CommandHandler("resetprompt", resetpromoters_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(
        MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, handle_audio_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    return application


async def configure_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def start_embedded_polling(application: Application) -> None:
    updater = application.updater
    if updater is None:
        raise RuntimeError("Telegram updater 不可用，无法启动 polling。")

    try:
        await application.initialize()
        await configure_bot_commands(application)
        await application.start()
        await updater.start_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
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
    finally:
        try:
            await application.stop()
        finally:
            await application.shutdown()

    logger.info("Telegram bot polling stopped.")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

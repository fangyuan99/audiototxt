## AudioToTxt

[![Made with Python](https://img.shields.io/badge/Made%20with-Python-1f425f.svg)](https://www.python.org/) [![Powered by yt-dlp](https://img.shields.io/badge/powered_by-yt--dlp-brightgreen)](https://github.com/yt-dlp/yt-dlp) [![Get Gemini key](https://img.shields.io/badge/AI-Gemini-4285F4)](https://ai.dev/) 

[English](#english) | [简体中文](#zh) | [Get a gemini key](https://ai.dev/)

👉 [FastAPI（Web UI）](fastapi/README.md)

---

<a id="english"></a>

## English

### What it does
Transcribe audio to text with Google Gen AI. You can authenticate either with a Gemini API key or with a Vertex AI service account JSON. YouTube links are sent directly to Gemini without downloading the video first. Support direct video URL download with automatic proxy detection. Support Douyin short-link/share-text via Downcats to fetch an audio direct link and then transcribe. Default model is `gemini-2.5-flash`.

- Reference: [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- Reference: [Gemini media resolution](https://ai.google.dev/gemini-api/docs/media-resolution)

Tested in this project with YouTube (built-in `--youtube`, public videos only). Use in compliance with sites' ToS and local laws.

### Setup
```bash
pip install -r requirements.txt
```

If you want to run the Telegram bot, create `.env` from `.env.example` and fill in:

```bash
cp .env.example .env
```

Or install manually:

```bash
pip install google-genai
pip install google-auth
pip install yt-dlp
pip install requests
```

Optional: install `ffmpeg` for more robust audio extraction. Without it, the script falls back to the original audio format.

### Authentication
- Option A: Gemini API key
  - Multi-key pool: comma-separated `GOOGLE_API_KEYS`
  - Single-key fallback: `GOOGLE_API_KEY` (or `GEMINI_API_KEY`)
  - Windows CMD: `set GOOGLE_API_KEY=YOUR_KEY`
  - PowerShell: `$env:GOOGLE_API_KEY="YOUR_KEY"`
  - macOS/Linux: `export GOOGLE_API_KEY="YOUR_KEY"`
- Option B: Vertex AI service account JSON
  - CLI: `--auth-mode vertex_ai_json --vertex-json /path/to/service-account.json --vertex-project YOUR_PROJECT --vertex-location us-central1`
  - Telegram: authorized users manage Vertex in `/settings` → advanced settings
  - Environment fallback: `GOOGLE_APPLICATION_CREDENTIALS` or `VERTEX_SERVICE_ACCOUNT_FILE`
- `--auth-mode` defaults to `gemini_api_key`

### Proxy Configuration
The program automatically uses proxy settings from system environment variables. Supported environment variables:
- `HTTP_PROXY` / `http_proxy`: HTTP proxy
- `HTTPS_PROXY` / `https_proxy`: HTTPS proxy

You can also override system proxy settings via command line arguments:
```bash
python main.py --video-url URL --proxy http://127.0.0.1:7890
```

### Usage
- Web UI (disabled by default):
  ```bash
  # .env: WEB_ENABLED=true and WEB_ACCESS_KEY=...
  python fastapi/run.py
  ```
  Every operational HTTP/WebSocket route requires the independent Web login key. The page uses the same global credentials/model/Prompt as Telegram and never stores provider credentials in the browser.

- Local audio:
  ```bash
  python main.py --audio ./path/to/audio.m4a --lang en --api-key YOUR_KEY
  ```

- Telegram bot:
  ```bash
  python telegram_bot.py
  ```
  - Required in `.env`: `ENV_BOT_TOKEN`, `ENV_BOT_SECRET`
  - IDs in `TG_ALLOWED_USER_IDS` enter directly; other private-chat users enter the password once. Changing the password invalidates old password grants
  - Send audio, voice, YouTube, Douyin share text, or a public media URL directly; source type is detected automatically
  - `/settings` uses inline buttons for the global Gemini key pool, model, Prompt, language, and advanced Vertex settings
  - `/settings` → “测试当前渠道” sends only `hi` with the currently configured model and reports channel availability, region, and latency without exposing the response or credentials
  - The model menu remotely lists transcription-compatible models from the current Gemini or Vertex channel; manual model entry remains available
  - Saving Vertex JSON/project/location automatically runs the same Vertex probe; a blank location defaults to `global`
  - `GOOGLE_API_KEYS` and Telegram input accept comma-separated keys. Tasks rotate healthy keys and skip keys in cooldown/disabled state
  - Jobs are queued, cancellable, retryable, and use one compact status message. Long results are sent completely across multiple Telegram messages plus `.txt`, without preview truncation
  - Failures identify the stage (parsing, download, audio extraction, transcription, or delivery); protected server logs record one sanitized diagnostic line per failed job
  - The bot supports private chats only. The command menu contains `/start`, `/settings`, `/help`, and `/cancel`
  - Run embedded polling only when both `WEB_ENABLED=true` and `TELEGRAM_EMBEDDED_ENABLED=true`; otherwise run `telegram_bot.py` separately

  Existing installations can verify migration on an automatically removed temporary copy, then apply it:

  ```bash
  python migrate_bot_data.py --dry-run-from data/telegram_bot
  python migrate_bot_data.py --data-dir data/telegram_bot
  ```

  Migration moves legacy credentials into the `0600` global configuration, scrubs per-user secrets, removes historical media, and gives existing `.txt` files a new seven-day retention window.

- YouTube (Gemini reads the public YouTube URL directly, no local download):
  ```bash
  python main.py --youtube https://www.youtube.com/watch?v=VIDEO_ID --lang en --api-key YOUR_KEY
  ```

- Video direct link (auto download video, extract audio to `./data` and transcribe):
  ```bash
  python main.py --video-url https://example.com/video.mp4 --lang en --api-key YOUR_KEY
  ```

- Douyin short link or share text (via Downcats: extract an audio direct link, download to `./data` and transcribe):
  ```bash
  python main.py --douyin "复制这条口令 https://v.douyin.com/xlaEmh_fVPg/ 打开Dou音..." --lang en --api-key YOUR_KEY
  ```
  - The program posts the share text to `downcats.com`, reads the returned audio URL, downloads it with a unique safe filename, then transcribes it.

- Proxy:
  ```bash
  python main.py --youtube URL --api-key YOUR_KEY --proxy http://127.0.0.1:7890
  ```

- Model selection:
  ```bash
  python main.py --audio ./a.mp3 --model gemini-2.5-flash --api-key YOUR_KEY
  python main.py --audio ./a.mp3 --auth-mode vertex_ai_json --vertex-json ./service-account.json --vertex-project YOUR_PROJECT
  ```

### Output
- Streams incremental transcript to stdout
- Saves the full text to a `.txt` file
  - Local audio: same basename as the input
  - YouTube/Douyin: `./data/<basename>.txt`

### CLI options (excerpt)
- `--audio`, `--youtube`, `--model`, `--lang`, `--out`
- `--proxy`, `--proxy-http`, `--proxy-https`
- `--auth-mode`, `--api-key`
- `--vertex-json`, `--vertex-project`, `--vertex-location`
- env vars: `GOOGLE_API_KEY`/`GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `VERTEX_SERVICE_ACCOUNT_FILE`, `VERTEX_PROJECT`, `VERTEX_LOCATION`

---

<a id="zh"></a>

## 简体中文

### 功能特性
- 本地音频转写（WAV/MP3/M4A 等常见格式）
- 直接通过 Gemini 读取 YouTube 链接并转写（仅支持公开视频，无需先下载）
- 视频直链下载和音频提取（自动使用系统代理）
- 抖音分享口令/短链通过 Downcats 提取音频直链后下载并转写
- 流式输出到标准输出，同时将完整文本保存为 `.txt`
- 提供 `--lang` 语言提示与 `--model` 模型选择
- 支持代理：`--proxy` / `--proxy-http` / `--proxy-https`
- 自动使用系统环境变量中的代理设置

### 支持网站
- 当前已在本项目中亲测站点：
  - YouTube（命令行内置 `--youtube`，通过 Gemini 直连转写）

请在遵守各网站服务条款与当地法律的前提下合规使用。

### 环境与安装
1) 安装依赖（建议使用虚拟环境）：

```bash
pip install -r requirements.txt
```

或者手动安装：

```bash
pip install google-genai
pip install google-auth
pip install yt-dlp
pip install requests
```

2)（可选）安装 `ffmpeg`：用于更稳定的音频提取与转码。未安装时，程序会自动回退为原始音频格式。

3) 如需启用 Telegram 机器人，先复制环境变量示例并填写：

```bash
cp .env.example .env
```

至少需要：
- `ENV_BOT_TOKEN`: Telegram BotFather 创建的机器人 token
- `ENV_BOT_SECRET`: 首次 `/start` 时用户需要输入的密码

### 配置认证
- 方式一：Gemini API Key
  - 设置环境变量 `GOOGLE_API_KEY`（或 `GEMINI_API_KEY`）
  - Windows CMD:
    ```bat
    set GOOGLE_API_KEY=你的密钥
    ```
  - PowerShell:
    ```powershell
    $env:GOOGLE_API_KEY="你的密钥"
    ```
  - macOS/Linux:
    ```bash
    export GOOGLE_API_KEY="你的密钥"
    ```
- 方式二：Vertex AI service account JSON
  - 命令行：
    ```bash
    python main.py --audio ./path/to/audio.m4a --auth-mode vertex_ai_json --vertex-json ./service-account.json --vertex-project YOUR_PROJECT --vertex-location us-central1
    ```
  - Telegram：授权用户可在 `/settings` 的高级设置中管理
  - 环境变量兜底：`GOOGLE_APPLICATION_CREDENTIALS` 或 `VERTEX_SERVICE_ACCOUNT_FILE`

### 代理配置
程序会自动使用系统环境变量中的代理设置，支持以下环境变量：
- `HTTP_PROXY` / `http_proxy`: HTTP代理
- `HTTPS_PROXY` / `https_proxy`: HTTPS代理

设置示例：
- Windows CMD:
  ```bat
  set HTTP_PROXY=http://127.0.0.1:7890
  set HTTPS_PROXY=http://127.0.0.1:7890
  ```
- PowerShell:
  ```powershell
  $env:HTTP_PROXY="http://127.0.0.1:7890"
  $env:HTTPS_PROXY="http://127.0.0.1:7890"
  ```
- macOS/Linux:
  ```bash
  export HTTP_PROXY="http://127.0.0.1:7890"
  export HTTPS_PROXY="http://127.0.0.1:7890"
  ```

也可以通过命令行参数覆盖系统代理设置：
```bash
python main.py --video-url URL --proxy http://127.0.0.1:7890
```

### 基本用法
- Web UI（默认关闭）：
  ```bash
  # 先在 .env 设置 WEB_ENABLED=true 与 WEB_ACCESS_KEY=...
  python fastapi/run.py
  ```
  所有操作型 HTTP/WebSocket 接口都要求独立的 Web 登录密钥；页面与 Telegram 共用全局凭据、模型和 Prompt，不会在浏览器中保存服务商凭据。

- 本地音频转写：
  ```bash
  python main.py --audio ./path/to/audio.m4a --lang zh --api-key YOUR_KEY
  python main.py --audio ./path/to/audio.m4a --auth-mode vertex_ai_json --vertex-json ./service-account.json --vertex-project YOUR_PROJECT
  ```

- Telegram 机器人：
  ```bash
  python telegram_bot.py
  ```
  - `TG_ALLOWED_USER_IDS` 中的账号直接进入；其他私聊用户输入一次 `ENV_BOT_SECRET`。服务端密码变化后旧授权自动失效
  - 直接发送音频、语音、YouTube、抖音分享文案或公网媒体直链，机器人会自动识别来源
  - `/settings` 使用消息内按钮管理全局 Key 池、模型、Prompt、语言及 Vertex 高级设置
  - `/settings` 中的“测试当前渠道”会使用当前配置模型发送一句 `hi`，仅返回渠道可用性、地区和耗时，不展示响应正文或凭据
  - 模型菜单会从当前 Gemini 或 Vertex 渠道远程读取适合转写的模型，仍保留手动输入模型名
  - 保存 Vertex JSON、Project 或 Location 后会自动执行同样的 Vertex 测活；Location 留空时默认使用 `global`
  - `GOOGLE_API_KEYS` 和 Telegram 输入都支持逗号分隔多 Key；任务会健康轮询并跳过限流、额度不足或失效 Key
  - 任务支持排队、取消和重试；过程中只更新一条状态消息，长文本会拆成多条 Telegram 消息完整发送，并附带 `.txt`，不再截断预览
  - 失败消息会指出解析、下载、抽音、转写或结果发送阶段；受保护的服务日志为每个失败任务记录一条脱敏诊断
  - 仅支持私聊；命令菜单只包含 `/start`、`/settings`、`/help`、`/cancel`
  - 只有同时设置 `WEB_ENABLED=true` 与 `TELEGRAM_EMBEDDED_ENABLED=true` 才会随 Web 启动嵌入式 polling

  旧版本升级可先对自动销毁的临时副本演练，再应用真实迁移：

  ```bash
  python migrate_bot_data.py --dry-run-from data/telegram_bot
  python migrate_bot_data.py --data-dir data/telegram_bot
  ```

  迁移会把旧凭据写入权限为 `0600` 的全局配置、清除用户状态中的明文凭据、删除历史媒体，并让现有 `.txt` 重新获得 7 天保留窗口。

- 直接处理 YouTube 链接（通过 Gemini 直连转写，不先下载视频）：
  ```bash
  python main.py --youtube https://www.youtube.com/watch?v=VIDEO_ID --lang zh --api-key YOUR_KEY
  ```

- 处理视频直链（自动下载视频，提取音频到 `./data` 后转写）：
  ```bash
  python main.py --video-url https://example.com/video.mp4 --lang zh --api-key YOUR_KEY
  ```

- 处理抖音分享口令或短链（通过 Downcats 提取音频直链，下载到 `./data` 并转写）：
  ```bash
  python main.py --douyin "0.25 aNJ:/ ... https://v.douyin.com/xlaEmh_fVPg/ 复制此链接，打开Dou音搜索，直接观看视频！" --lang zh --api-key YOUR_KEY
  ```
  - 程序会向 `downcats.com` 提交分享文案/短链，读取返回的音频直链，以唯一且安全的文件名下载后进行转写。

- 使用代理（如本地 HTTP 代理 127.0.0.1:7890）：
  ```bash
  python main.py --youtube URL --api-key YOUR_KEY --proxy http://127.0.0.1:7890
  ```

- 指定模型：
  ```bash
  python main.py --audio ./a.mp3 --model gemini-2.5-flash --api-key YOUR_KEY
  ```

### 输出说明
- 转写时会将增量结果流式打印到标准输出
- 若使用 `--out` 指定文件路径则保存到对应位置；未指定时：
  - 处理本地音频：保存为与音频同名的 `.txt`
  - 处理 YouTube/抖音：保存到 `./data/同名.txt`

### 抖音（Downcats）注意事项
- 该功能依赖第三方服务 Downcats 的可用性，若接口或返回结构变化，可能导致解析失败。
- 若访问 Downcats 较慢或失败，请确认你的网络或代理设置是否正常。

### 可用参数（摘录）
- `--audio`: 本地音频文件路径
- `--youtube`: YouTube 视频链接（Gemini 直连转写，仅支持公开视频）
- `--video-url`: 视频直链URL（自动下载视频并提取音频）
- `--douyin`: 抖音分享口令或短链（自动解析并下载音频）
- `--model`: 模型名称（默认 `gemini-2.5-flash`）
- `--lang`: 语言提示（如 `zh`/`en`/`ja`）
- `--out`: 输出文本路径（可选）
- `--media-resolution`: YouTube 直连时传给 Gemini 的媒体分辨率，默认 `low`
- `--proxy` / `--proxy-http` / `--proxy-https`: 代理设置
- `--auth-mode`: `gemini_api_key` 或 `vertex_ai_json`
- `--api-key`: Gemini API Key（或使用环境变量）
- `--vertex-json`: Vertex AI service account JSON 文件路径
- `--vertex-project` / `--vertex-location`: Vertex AI 认证参数

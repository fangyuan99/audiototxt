## AudioToTxt

[![Made with Python](https://img.shields.io/badge/Made%20with-Python-1f425f.svg)](https://www.python.org/) [![Powered by yt-dlp](https://img.shields.io/badge/powered_by-yt--dlp-brightgreen)](https://github.com/yt-dlp/yt-dlp) [![Get Gemini key](https://img.shields.io/badge/AI-Gemini-4285F4)](https://ai.dev/) 

[English](#english) | [简体中文](#zh) | [Get a gemini key](https://ai.dev/)

👉 [FastAPI（Web UI）](fastapi/README.md)

---

<a id="english"></a>

## English

### What it does
Transcribe audio to text with Google Generative AI (Gemini). Optionally fetch audio from video sites via `yt-dlp` and then transcribe. Support direct video URL download with automatic proxy detection. Support Douyin short-link/share-text via Tiksave to fetch MP3 direct link and then transcribe. Default model is `gemini-2.5-flash`.

- Reference: [`yt-dlp` README](https://github.com/yt-dlp/yt-dlp/blob/master/README.md)
- Supported sites: [`yt-dlp` Supported Sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

Tested in this project with YouTube (built-in `--youtube`) and Bilibili (download via `yt-dlp` first, then pass `--audio`). Use in compliance with sites' ToS and local laws.

### Setup
```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install google-generativeai
pip install yt-dlp
pip install requests
```

Optional: install `ffmpeg` for more robust audio extraction. Without it, the script falls back to the original audio format.

### API Key
- Option A: Environment variable `GOOGLE_API_KEY` (or `GEMINI_API_KEY`)
  - Windows CMD: `set GOOGLE_API_KEY=YOUR_KEY`
  - PowerShell: `$env:GOOGLE_API_KEY="YOUR_KEY"`
  - macOS/Linux: `export GOOGLE_API_KEY="YOUR_KEY"`
- Option B: `--api-key YOUR_KEY` CLI flag

### Proxy Configuration
The program automatically uses proxy settings from system environment variables. Supported environment variables:
- `HTTP_PROXY` / `http_proxy`: HTTP proxy
- `HTTPS_PROXY` / `https_proxy`: HTTPS proxy

You can also override system proxy settings via command line arguments:
```bash
python main.py --video-url URL --proxy http://127.0.0.1:7890
```

### Usage
- Local audio:
  ```bash
  python main.py --audio ./path/to/audio.m4a --lang en --api-key YOUR_KEY
  ```

- YouTube (auto download to `./data` and transcribe, In theory, all websites in the yt-dlp list support):
  ```bash
  python main.py --youtube https://www.youtube.com/watch?v=VIDEO_ID --lang en --api-key YOUR_KEY
  ```

- Video direct link (auto download video, extract audio to `./data` and transcribe):
  ```bash
  python main.py --video-url https://example.com/video.mp4 --lang en --api-key YOUR_KEY
  ```

- Douyin short link or share text (via Tiksave: extract MP3 direct link, download to `./data` and transcribe):
  ```bash
  python main.py --douyin "复制这条口令 https://v.douyin.com/xlaEmh_fVPg/ 打开Dou音..." --lang en --api-key YOUR_KEY
  ```
  - The program posts to `https://tiksave.io/api/ajaxSearch` with your share text, parses returned HTML for the “Download MP3” link (e.g. `https://dl.snapcdn.app/get?...`), downloads MP3 as `./data/douyin_{TikTokId}.mp3` (or timestamped if missing), then transcribes.

- Proxy:
  ```bash
  python main.py --youtube URL --api-key YOUR_KEY --proxy http://127.0.0.1:7890
  ```

- Model selection:
  ```bash
  python main.py --audio ./a.mp3 --model gemini-2.5-flash --api-key YOUR_KEY
  ```

### Output
- Streams incremental transcript to stdout
- Saves the full text to a `.txt` file
  - Local audio: same basename as the input
  - YouTube/Douyin: `./data/<basename>.txt`

### CLI options (excerpt)
- `--audio`, `--youtube`, `--model`, `--lang`, `--out`
- `--proxy`, `--proxy-http`, `--proxy-https`
- `--api-key` (or env var `GOOGLE_API_KEY`/`GEMINI_API_KEY`)

---

<a id="zh"></a>

## 简体中文

### 功能特性
- 本地音频转写（WAV/MP3/M4A 等常见格式）
- 一键下载 YouTube 音频并转写（需 `yt-dlp`，可选安装 `ffmpeg` 以获得更高质量/更好兼容的音频）
- 视频直链下载和音频提取（自动使用系统代理）
- 抖音分享口令/短链通过 Tiksave 提取 MP3 直链后下载并转写
- 流式输出到标准输出，同时将完整文本保存为 `.txt`
- 提供 `--lang` 语言提示与 `--model` 模型选择
- 支持代理：`--proxy` / `--proxy-http` / `--proxy-https`
- 自动使用系统环境变量中的代理设置

### 支持网站
- 完整站点列表请见：`yt-dlp` 的支持网站页面
  - https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- 当前已在本项目中亲测站点：
  - YouTube（命令行内置 `--youtube` 直连下载与转写）
  - Bilibili（通过 `yt-dlp` 先下载音频，再使用 `--audio` 转写）

请在遵守各网站服务条款与当地法律的前提下合规使用。

### 环境与安装
1) 安装依赖（建议使用虚拟环境）：

```bash
pip install -r requirements.txt
```

或者手动安装：

```bash
pip install google-generativeai
pip install yt-dlp
pip install requests
```

2)（可选）安装 `ffmpeg`：用于更稳定的音频提取与转码。未安装时，程序会自动回退为原始音频格式。

### 配置 API Key
- 方式一（推荐）：设置环境变量 `GOOGLE_API_KEY`（或 `GEMINI_API_KEY`）
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
- 方式二：运行时通过 `--api-key` 参数传入

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
- 本地音频转写：
  ```bash
  python main.py --audio ./path/to/audio.m4a --lang zh --api-key YOUR_KEY
  ```

- 直接处理 YouTube 链接（自动下载到 `./data` 后转写，理论上 yt-dlp 列表内的网站都支持）：
  ```bash
  python main.py --youtube https://www.youtube.com/watch?v=VIDEO_ID --lang zh --api-key YOUR_KEY
  ```

- 处理视频直链（自动下载视频，提取音频到 `./data` 后转写）：
  ```bash
  python main.py --video-url https://example.com/video.mp4 --lang zh --api-key YOUR_KEY
  ```

- 处理抖音分享口令或短链（通过 Tiksave 提取 MP3 直链，下载到 `./data` 并转写）：
  ```bash
  python main.py --douyin "0.25 aNJ:/ ... https://v.douyin.com/xlaEmh_fVPg/ 复制此链接，打开Dou音搜索，直接观看视频！" --lang zh --api-key YOUR_KEY
  ```
  - 程序会向 `https://tiksave.io/api/ajaxSearch` 提交你的分享文案/短链，解析返回的 HTML，提取“下载 MP3”按钮的直链（如 `https://dl.snapcdn.app/get?...`），将音频保存为 `./data/douyin_{TikTokId}.mp3`（若缺失则用时间戳），随后进行转写。

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

### 抖音（Tiksave）注意事项
- 该功能依赖第三方服务 Tiksave 的可用性，若接口或页面结构变化，可能导致解析失败。
- 若访问 Tiksave 较慢或失败，请确认你的网络或代理设置是否可访问境外站点。

### 可用参数（摘录）
- `--audio`: 本地音频文件路径
- `--youtube`: YouTube 视频链接（自动下载音频）
- `--video-url`: 视频直链URL（自动下载视频并提取音频）
- `--douyin`: 抖音分享口令或短链（自动解析并下载音频）
- `--model`: 模型名称（默认 `gemini-2.5-flash`）
- `--lang`: 语言提示（如 `zh`/`en`/`ja`）
- `--out`: 输出文本路径（可选）
- `--proxy` / `--proxy-http` / `--proxy-https`: 代理设置
- `--api-key`: 直接传入 API Key（或使用环境变量）



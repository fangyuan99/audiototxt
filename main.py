import argparse
import os
import sys
import time
import re
from typing import Optional


def set_proxies(proxy: Optional[str], proxy_http: Optional[str], proxy_https: Optional[str]) -> None:
    """Configure HTTP(S) proxy via environment variables for the current process.

    If a single proxy is provided, apply it to both HTTP and HTTPS.
    Explicit protocol-specific args override the single proxy.
    If no proxy is provided, keep existing system environment variables.
    """
    # Normalize: prefer explicit options over the shared one
    http_proxy = proxy_http or proxy
    https_proxy = proxy_https or proxy

    if http_proxy:
        os.environ["HTTP_PROXY"] = http_proxy
        os.environ["http_proxy"] = http_proxy
    if https_proxy:
        os.environ["HTTPS_PROXY"] = https_proxy
        os.environ["https_proxy"] = https_proxy


def ensure_package() -> None:
    """Ensure google-generativeai is available; otherwise, guide the user."""
    try:
        import google.generativeai  # noqa: F401
    except Exception:  # pragma: no cover - runtime guidance only
        print(
            "未检测到 google-generativeai 包。请先安装依赖：\n"
            "  pip install -r requirements.txt\n"
            "或：\n"
            "  pip install google-generativeai>=0.7.2,<1.0.0",
            file=sys.stderr,
        )
        sys.exit(2)


def wait_for_file_active(genai, file_obj, timeout_seconds: int = 120) -> None:
    """Poll until uploaded file becomes ACTIVE or timeout. 输出简单进度到 stderr。"""
    start_ts = time.time()
    sleep_seconds = 1.0
    try:
        print("等待文件处理", end="", file=sys.stderr, flush=True)
    except Exception:
        pass
    while True:
        file_obj = genai.get_file(file_obj.name)
        state = getattr(file_obj, "state", None)
        state_name = getattr(state, "name", state)
        if state_name == "ACTIVE":
            try:
                print(" ✓", file=sys.stderr, flush=True)
            except Exception:
                pass
            return
        if state_name == "FAILED" or (time.time() - start_ts) > timeout_seconds:
            try:
                print(file=sys.stderr)
            except Exception:
                pass
            raise RuntimeError(f"文件处理失败或超时，state={state_name}")
        time.sleep(sleep_seconds)
        # Exponential backoff, cap to 5s
        sleep_seconds = min(sleep_seconds * 1.5, 5.0)
        try:
            print(".", end="", file=sys.stderr, flush=True)
        except Exception:
            pass


def transcribe_audio_streaming(
    api_key: str,
    audio_path: str,
    model_name: str = "gemini-2.5-flash",
    language_hint: Optional[str] = 'zh',
    on_chunk=None,
) -> str:
    """Use Gemini to transcribe an audio file into text with streaming output.

    Returns the full transcript while yielding chunks via on_chunk or stdout.
    """
    import google.generativeai as genai

    genai.configure(api_key=api_key)

    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"找不到音频文件：{audio_path}")

    # Upload the audio to the File API for reliable handling of larger files
    try:
        print(f"上传音频：{os.path.basename(audio_path)}", file=sys.stderr)
    except Exception:
        pass
    uploaded_file = genai.upload_file(audio_path)
    wait_for_file_active(genai, uploaded_file)

    # 系统级约束，尽可能强地限制输出风格，避免跑题/扩写/翻译
    language_line = f"主要语言：{language_hint}" if language_hint else "主要语言：按音频原语言"
    system_instruction = (
        "你是一名专业的听打员，只做逐字转写以及进行合理的合并与分段，不做任何总结、解释或翻译。\n"
        f"{language_line}语言输出。若内容不确定或听不清，请在原位以方括号标注（如：[听不清 00:01:23]、[不确定：人名?]）。\n"
        "只输出纯文字稿，不要添加标题、前后缀或任何其它说明。"
    )

    prompt = (
        "按音频内容逐字转写：\n"
        "- 仅做最小必要的错别字/口误更正，不改变原意；\n"
        "- 保留口头语和重复；\n"
        "- 仅添加基础标点；\n"
        "- 严禁翻译或补充外部信息；\n"
        "- 输出为纯文本。"
    )

    generation_config = {
        "temperature": 0.0,
        "top_p": 0.9,
        "top_k": 40,
        "response_mime_type": "text/plain",
    }

    model = genai.GenerativeModel(
        model_name,
        system_instruction=system_instruction,
        generation_config=generation_config,
    )

    # 注意顺序：先给指令，再给音频
    try:
        print("开始转写...", file=sys.stderr)
    except Exception:
        pass
    response_stream = model.generate_content([
        prompt,
        uploaded_file,
    ], stream=True)

    # 增量去重：规避某些实现中分片重复回放导致的重复内容
    emitted_text = ""
    full_parts = []
    for chunk in response_stream:
        text_piece = getattr(chunk, "text", None)
        if not text_piece:
            try:
                candidates = getattr(chunk, "candidates", [])
                if candidates and candidates[0].content and candidates[0].content.parts:
                    text_piece = "".join(
                        part.text for part in candidates[0].content.parts if hasattr(part, "text")
                    )
            except Exception:
                text_piece = None
        if text_piece:
            # 如果 text_piece 是累计文本，计算新追加的增量；否则按原始片段输出
            if emitted_text and text_piece.startswith(emitted_text):
                delta = text_piece[len(emitted_text):]
            else:
                delta = text_piece
            if delta:
                if on_chunk:
                    on_chunk(delta)
                else:
                    print(delta, end="", flush=True)
                full_parts.append(delta)
                emitted_text += delta

    # Finalize to ensure the aggregated response is complete
    try:
        response_stream.resolve()
    except Exception:
        pass

    transcript = "".join(full_parts).strip()
    try:
        print(f"转写完成（约 {len(transcript)} 字符）", file=sys.stderr)
    except Exception:
        pass
    return transcript


def transcribe_youtube_url_streaming(
    api_key: str,
    youtube_url: str,
    model_name: str = "gemini-2.5-flash",
    language_hint: Optional[str] = 'zh',
    on_chunk=None,
) -> str:
    """Use Gemini to transcribe a YouTube URL directly via file_uri with streaming output.

    This avoids downloading the audio locally. Requires google-genai SDK.
    """
    # 延迟导入，避免未安装时影响其他路径
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 google-genai 依赖，请先安装：pip install google-genai"
        ) from e

    client = genai.Client(api_key=api_key)

    language_line = f"主要语言：{language_hint}" if language_hint else "主要语言：按音频原语言"
    system_instruction_text = (
        "你是一名专业的听打员，只做逐字转写，不做任何总结、解释或翻译，注意分段！注意分段！一段太长不方便阅读。\n"
        f"{language_line}。若内容不确定或听不清，请在原位以方括号标注（如：[听不清 00:01:23]、[不确定：人名?]）。\n"
        "只输出纯文字稿，不要添加标题、前后缀或任何其它说明。"
        "严禁所有文字放到一个段落里，要分段。"
    )
    prompt_text = (
        "按音频内容逐字转写：\n"
        "- 仅做最小必要的错别字/口误更正，不改变原意，并进行合理的分段；\n"
        "- 每一段不要太长，会影响理解；\n"
        "- 保留口头语和重复；\n"
        "- 仅添加基础标点；\n"
        "- 严禁翻译或补充外部信息；\n"
        "- 输出为纯文本。"
    )

    contents = [
        genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    file_data=genai_types.FileData(
                        file_uri=youtube_url,
                        mime_type="video/*",
                    )
                ),
            ],
        ),
        genai_types.Content(role="model", parts=[]),
        genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=prompt_text)]),
    ]

    generate_content_config = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        system_instruction=[genai_types.Part.from_text(text=system_instruction_text)],
    )

    try:
        print("开始转写（YouTube 直连）...", file=sys.stderr)
    except Exception:
        pass

    full_parts = []
    emitted_text = ""
    stream = client.models.generate_content_stream(
        model=model_name,
        contents=contents,
        config=generate_content_config,
    )
    for chunk in stream:
        text_piece = getattr(chunk, "text", None)
        if text_piece:
            if emitted_text and text_piece.startswith(emitted_text):
                delta = text_piece[len(emitted_text):]
            else:
                delta = text_piece
            if delta:
                if on_chunk:
                    on_chunk(delta)
                else:
                    print(delta, end="", flush=True)
                full_parts.append(delta)
                emitted_text += delta

    transcript = "".join(full_parts).strip()
    try:
        print(f"转写完成（约 {len(transcript)} 字符）", file=sys.stderr)
    except Exception:
        pass
    return transcript


def download_audio_from_youtube(
    youtube_url: str,
    output_dir: str = "./data",
    preferred_audio_codec: str = "m4a",
    cookies_path: Optional[str] = None,
) -> str:
    """使用 yt-dlp 下载 YouTube 音频并返回本地文件路径。

    优先尝试提取为指定编码（默认 m4a，需要本机可用的 ffmpeg）。
    若转码失败（可能未安装 ffmpeg），则回退为原始音频格式。
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        import yt_dlp  # type: ignore
    except Exception:
        print(
            "未检测到 yt-dlp 包。请先安装依赖：\n  pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(2)

    base_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": os.path.join(output_dir, "%(title)s [%(id)s].%(ext)s"),
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": False,
    }

    last_pct_holder = {"pct": -5}

    def _progress_hook(d):
        try:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes")
                if total and downloaded:
                    pct = int(downloaded * 100 / max(total, 1))
                    if pct >= last_pct_holder["pct"] + 5:
                        last_pct_holder["pct"] = pct
                        print(f"下载进度：{pct}%", file=sys.stderr)
            elif status == "finished":
                print("下载完成，开始后处理...", file=sys.stderr)
        except Exception:
            pass

    def _extract_path_with_ydl(ydl_obj, info_dict) -> str:
        requested = info_dict.get("requested_downloads")
        if isinstance(requested, list) and requested:
            for item in requested:
                fp = item.get("filepath") or item.get("_filename")
                if fp and os.path.exists(fp):
                    return fp
        fp = info_dict.get("filepath") or info_dict.get("_filename")
        if fp and os.path.exists(fp):
            return fp
        try:
            prepared = ydl_obj.prepare_filename(info_dict)
            if prepared and os.path.exists(prepared):
                return prepared
        except Exception:
            pass
        vid = info_dict.get("id")
        if vid:
            for name in os.listdir(output_dir):
                if f"[{vid}]" in name:
                    candidate = os.path.join(output_dir, name)
                    if os.path.isfile(candidate):
                        return candidate
        raise RuntimeError("未能确定下载的音频文件路径")

    try:
        opts = dict(base_opts)
        if cookies_path:
            print(f"使用 cookies 文件：{cookies_path}", file=sys.stderr)
            opts["cookiefile"] = cookies_path
        opts["progress_hooks"] = [_progress_hook]
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": preferred_audio_codec,
                "preferredquality": "0",
            }
        ]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            return _extract_path_with_ydl(ydl, info)
    except Exception as e:
        try:
            from yt_dlp.utils import PostProcessingError  # type: ignore
        except Exception:
            PostProcessingError = Exception  # type: ignore
        if isinstance(e, PostProcessingError):
            print(
                "音频转码失败（可能未安装 ffmpeg），将使用原始音频格式。",
                file=sys.stderr,
            )
        else:
            print(f"转码失败：{e}，尝试以原始格式下载。", file=sys.stderr)

        try:
            opts = dict(base_opts)
            if cookies_path:
                print(f"使用 cookies 文件：{cookies_path}", file=sys.stderr)
                opts["cookiefile"] = cookies_path
            opts["progress_hooks"] = [_progress_hook]
            opts.pop("postprocessors", None)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                return _extract_path_with_ydl(ydl, info)
        except Exception as e2:
            raise RuntimeError(f"下载失败：{e2}") from e

def download_video_and_extract_audio(
    video_url: str,
    output_dir: str = "./data",
    preferred_audio_codec: str = "m4a",
) -> str:
    """从视频直链下载视频，使用ffmpeg提取音频并返回本地音频文件路径。
    
    Args:
        video_url: 视频直链URL
        output_dir: 输出目录，默认为./data
        preferred_audio_codec: 首选音频编码，默认为m4a
        
    Returns:
        str: 提取的音频文件路径
        
    Raises:
        RuntimeError: 下载或音频提取失败时抛出
    """
    import requests
    import subprocess
    import tempfile
    from urllib.parse import urlparse
    
    os.makedirs(output_dir, exist_ok=True)
    
    # 从URL中提取文件名，如果没有则使用时间戳
    parsed_url = urlparse(video_url)
    url_path = parsed_url.path
    if url_path and '.' in url_path:
        # 尝试从URL路径中提取文件名
        original_filename = os.path.basename(url_path)
        name, ext = os.path.splitext(original_filename)
        if not name:
            name = f"video_{int(time.time())}"
    else:
        name = f"video_{int(time.time())}"
        ext = ".mp4"  # 默认扩展名
    
    # 临时视频文件路径
    temp_video_path = os.path.join(output_dir, f"{name}_temp{ext}")
    # 最终音频文件路径
    audio_path = os.path.join(output_dir, f"{name}.{preferred_audio_codec}")
    
    try:
        print(f"开始下载视频：{video_url}", file=sys.stderr)
        
        # 下载视频文件
        # 获取系统代理设置
        proxies = _get_system_proxies()
        response = requests.get(video_url, stream=True, proxies=proxies if proxies else None)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0
        last_pct = -5
        
        with open(temp_video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    
                    # 显示下载进度
                    if total_size > 0:
                        pct = int(downloaded_size * 100 / total_size)
                        if pct >= last_pct + 5:
                            last_pct = pct
                            print(f"下载进度：{pct}%", file=sys.stderr)
        
        print("视频下载完成，开始提取音频...", file=sys.stderr)
        
        # 使用ffmpeg提取音频
        # 使用ffmpeg提取音频
        ffmpeg_codec = _get_ffmpeg_audio_codec(preferred_audio_codec)
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', temp_video_path,
            '-vn',  # 不包含视频
            '-acodec', ffmpeg_codec,
            '-y',  # 覆盖输出文件
            audio_path
        ]
        
        try:
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                check=True
            )
            print("音频提取完成", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"ffmpeg提取音频失败：{e.stderr}", file=sys.stderr)
            # 尝试使用mp3格式作为备选
            if preferred_audio_codec != "mp3":
                print("尝试使用mp3格式重新提取...", file=sys.stderr)
                audio_path = os.path.join(output_dir, f"{name}.mp3")
                ffmpeg_codec = _get_ffmpeg_audio_codec("mp3")
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-i', temp_video_path,
                    '-vn',  # 不包含视频
                    '-acodec', ffmpeg_codec,
                    '-y',  # 覆盖输出文件
                    audio_path
                ]
                try:
                    subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True)
                    print("音频提取完成（mp3格式）", file=sys.stderr)
                except subprocess.CalledProcessError as e2:
                    raise RuntimeError(f"音频提取失败：{e2.stderr}")
            else:
                raise RuntimeError(f"音频提取失败：{e.stderr}")
        except FileNotFoundError:
            raise RuntimeError("未找到ffmpeg，请确保已安装ffmpeg并添加到系统PATH中")
        
        return audio_path
        
    except requests.RequestException as e:
        raise RuntimeError(f"下载视频失败：{e}")
    except Exception as e:
        raise RuntimeError(f"处理视频失败：{e}")
    finally:
        # 清理临时视频文件
        try:
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
        except Exception:
            pass


def _get_ffmpeg_audio_codec(codec_name: str) -> str:
    """根据音频编码器名称返回ffmpeg对应的编码器名称"""
    codec_mapping = {
        "m4a": "aac",
        "mp3": "libmp3lame", 
        "wav": "pcm_s16le",
        "flac": "flac",
        "ogg": "libvorbis",
        "aac": "aac",
        "opus": "libopus"
    }
    return codec_mapping.get(codec_name.lower(), "aac")  # 默认使用aac


def _get_system_proxies() -> dict:
    """获取系统环境变量中的代理设置
    
    优先级：HTTP_PROXY/HTTPS_PROXY > http_proxy/https_proxy
    """
    proxies = {}
    
    # 优先使用大写环境变量
    if os.getenv('HTTP_PROXY'):
        proxies['http'] = os.getenv('HTTP_PROXY')
    elif os.getenv('http_proxy'):
        proxies['http'] = os.getenv('http_proxy')
        
    if os.getenv('HTTPS_PROXY'):
        proxies['https'] = os.getenv('HTTPS_PROXY')
    elif os.getenv('https_proxy'):
        proxies['https'] = os.getenv('https_proxy')
    
    return proxies


def _extract_first_url(text: str) -> Optional[str]:
    """从任意文本中提取第一个 URL。优先返回包含 v.douyin.com 的链接。"""
    if not text:
        return None
    candidates = re.findall(r"https?://[^\s]+", text)
    if not candidates:
        return None
    # 优先短链域名
    for u in candidates:
        if "v.douyin.com" in u:
            return u
    return candidates[0]


def resolve_douyin_aweme_id(short_or_share_text: str) -> str:
    """解析抖音分享口令/短链，调用开放接口换取 aweme_id。"""
    import requests
    from urllib.parse import quote

    short_url = short_or_share_text.strip()
    if not short_url.startswith("http"):
        extracted = _extract_first_url(short_or_share_text)
        if not extracted:
            raise RuntimeError("未在输入中找到有效的抖音短链 URL")
        short_url = extracted

    api = (
        "https://douyin.wtf/api/douyin/web/get_aweme_id?url="
        + quote(short_url, safe="")
    )
    proxies = _get_system_proxies()
    try:
        resp = requests.get(api, timeout=15, proxies=proxies if proxies else None)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"请求 aweme_id 失败：{e}")

    if not isinstance(data, dict) or data.get("code") != 200:
        raise RuntimeError(f"接口返回异常：{data}")
    aweme_id = data.get("data")
    if not aweme_id:
        raise RuntimeError("未获取到 aweme_id")
    return str(aweme_id)


def fetch_douyin_audio_url(aweme_id: str) -> str:
    """根据 aweme_id 获取音频直链 URL。"""
    import requests

    api = f"https://douyin.wtf/api/douyin/web/fetch_one_video?aweme_id={aweme_id}"
    proxies = _get_system_proxies()
    try:
        resp = requests.get(api, timeout=20, proxies=proxies if proxies else None)
        resp.raise_for_status()
        j = resp.json()
    except Exception as e:
        raise RuntimeError(f"请求音频信息失败：{e}")

    try:
        detail = (
            j.get("data", {})
            .get("aweme_detail", {})
            .get("video", {})
        )
        bit_rate_audio = detail.get("bit_rate_audio") or []
        # 优先第一个可用
        for item in bit_rate_audio:
            meta = item.get("audio_meta") or {}
            url_list = meta.get("url_list") or {}
            main_url = url_list.get("main_url") or url_list.get("backup_url_1")
            if main_url:
                return main_url
        # 兜底：某些结构可能直接给 url_list 数组
        url_list = detail.get("play_addr", {}).get("url_list")
        if isinstance(url_list, list) and url_list:
            return url_list[0]
    except Exception:
        pass
    raise RuntimeError("未能从返回数据中解析到音频直链")


def fetch_douyin_mp3_via_tiksave(share_text: str):
    """调用 tiksave 接口，解析返回 HTML，提取 MP3 直链及元信息。

    Returns:
        (mp3_url, title, tiktok_id)
    """
    import requests
    import html as html_lib

    url = "https://tiksave.io/api/ajaxSearch"
    proxies = _get_system_proxies()

    headers = {
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "accept": "*/*",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "connection": "keep-alive",
    }
    data = {
        "q": share_text,
        "lang": "zh-cn",
    }

    try:
        print("请求 tiksave 接口...", file=sys.stderr)
        resp = requests.post(url, headers=headers, data=data, timeout=30, proxies=proxies if proxies else None)
        resp.raise_for_status()
        j = resp.json()
    except Exception as e:
        raise RuntimeError(f"tiksave 接口请求失败：{e}")

    if not isinstance(j, dict) or j.get("status") != "ok":
        raise RuntimeError(f"tiksave 返回异常：{j}")

    html = j.get("data") or ""
    if not html:
        raise RuntimeError("tiksave 未返回有效 HTML 内容")

    # 解析 MP3 链接
    # 形如：<a ... href="https://dl.snapcdn.app/get?..." ...> 下载 MP3
    # print(html)
    pattern = r'https://dl\.snapcdn\.app/get\?token=.*?MP3'

    mp3_match = re.search(pattern, html, re.IGNORECASE)
    if not mp3_match:
        # 容错：有时按钮文本包含 &nbsp; 或其他空白
        mp3_match = re.search(pattern, html, re.IGNORECASE)
    if not mp3_match:
        raise RuntimeError("未能在 tiksave HTML 中找到 MP3 下载链接")
    mp3_url = mp3_match[0].split('"')[0]

    # 可选：标题与 TikTokId，用于命名
    title = None
    try:
        t = re.search(r"<h3>([\s\S]*?)</h3>", html)
        if t:
            title = html_lib.unescape(t.group(1)).strip()
    except Exception:
        title = None

    tiktok_id = None
    try:
        tid = re.search(r'id="TikTokId"\s+value="(\d+)"', html)
        if tid:
            tiktok_id = tid.group(1)
    except Exception:
        tiktok_id = None

    return mp3_url, title, tiktok_id


def download_audio_from_direct_url(
    audio_url: str,
    output_dir: str = "./data",
    preferred_ext: str = "m4a",
    filename_stem: Optional[str] = None,
) -> str:
    """下载音频直链到本地并返回文件路径。默认保存为 m4a。"""
    import requests
    from urllib.parse import urlparse

    os.makedirs(output_dir, exist_ok=True)
    proxies = _get_system_proxies()

    parsed = urlparse(audio_url)
    # 从 URL 推断扩展名
    ext = None
    path = parsed.path or ""
    if "." in os.path.basename(path):
        _, ext = os.path.splitext(os.path.basename(path))
    if not ext:
        ext = "." + preferred_ext.lstrip(".")

    if not filename_stem:
        filename_stem = f"douyin_{int(time.time())}"
    out_path = os.path.join(output_dir, filename_stem + ext)

    try:
        print(f"开始下载音频：{audio_url}", file=sys.stderr)
        with requests.get(audio_url, stream=True, timeout=60, proxies=proxies if proxies else None) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            last_pct = -5
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)
                    if total > 0:
                        downloaded += len(chunk)
                        pct = int(downloaded * 100 / max(total, 1))
                        if pct >= last_pct + 5:
                            last_pct = pct
                            print(f"下载进度：{pct}%", file=sys.stderr)
    except Exception as e:
        raise RuntimeError(f"下载音频失败：{e}")

    return out_path

def main() -> None:
    parser = argparse.ArgumentParser(
        description="使用 Gemini 将音频转为文本（默认模型：gemini-2.5-flash）",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        help="Gemini API Key（也可使用环境变量 GOOGLE_API_KEY 或 GEMINI_API_KEY）",
    )
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument(
        "--audio",
        dest="audio_path",
        help="音频文件路径（支持常见音频格式，如 wav/mp3/m4a 等）",
    )
    src_group.add_argument(
        "--youtube",
        dest="youtube_url",
        help="YouTube 视频链接（自动下载音频到 ./data 并转写）",
    )
    src_group.add_argument(
        "--video-url",
        dest="video_url",
        help="视频直链URL（自动下载视频，提取音频到 ./data 并转写）",
    )
    src_group.add_argument(
        "--douyin",
        dest="douyin_share_or_url",
        help="抖音分享口令或短链（自动解析并下载音频到 ./data 后转写）",
    )
    parser.add_argument(
        "--model",
        dest="model_name",
        default="gemini-2.5-flash",
        help="模型名称，默认 gemini-2.5-flash",
    )
    parser.add_argument(
        "--lang",
        dest="language_hint",
        default=None,
        help="可选，语言提示。例如 zh、en、ja。用于提示转写语种。",
    )
    parser.add_argument(
        "--out",
        dest="out_path",
        help="可选，保存完整文字稿的 txt 路径；默认与音频同名 .txt",
    )
    parser.add_argument(
        "--proxy",
        dest="proxy",
        help="为 HTTP 与 HTTPS 同时设置代理，例如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--proxy-http",
        dest="proxy_http",
        help="仅为 HTTP 设置代理",
    )
    parser.add_argument(
        "--proxy-https",
        dest="proxy_https",
        help="仅为 HTTPS 设置代理",
    )
    parser.add_argument(
        "--cookies",
        dest="cookies_path",
        help="可选，yt-dlp cookies 文件路径（Netscape 格式）。仅对 --youtube 生效",
    )

    args = parser.parse_args()

    # Ensure dependency present
    ensure_package()

    # Configure proxies if provided
    set_proxies(args.proxy, args.proxy_http, args.proxy_https)

    # Resolve API key precedence: CLI > GOOGLE_API_KEY > GEMINI_API_KEY
    api_key = (
        args.api_key
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )
    if not api_key:
        print(
            "缺少 API Key。请通过 --api-key 传入，或设置环境变量 GOOGLE_API_KEY / GEMINI_API_KEY。",
            file=sys.stderr,
        )
        sys.exit(2)

    # 解析音频来源
    if getattr(args, "youtube_url", None):
        data_dir = os.path.join(".", "data")
        try:
            audio_path = download_audio_from_youtube(
                args.youtube_url,
                output_dir=data_dir,
                cookies_path=getattr(args, "cookies_path", None),
            )
        except Exception as e:
            print(f"下载音频失败：{e}", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "video_url", None):
        data_dir = os.path.join(".", "data")
        try:
            audio_path = download_video_and_extract_audio(args.video_url, output_dir=data_dir)
        except Exception as e:
            print(f"下载视频并提取音频失败：{e}", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "douyin_share_or_url", None):
        data_dir = os.path.join(".", "data")
        os.makedirs(data_dir, exist_ok=True)
        try:
            mp3_url, title, tiktok_id = fetch_douyin_mp3_via_tiksave(args.douyin_share_or_url)
            print("获取到 MP3 直链，开始下载...", file=sys.stderr)
            if tiktok_id:
                filename_stem = f"douyin_{tiktok_id}"
            else:
                filename_stem = f"douyin_{int(time.time())}"
            audio_path = download_audio_from_direct_url(
                mp3_url,
                output_dir=data_dir,
                preferred_ext="mp3",
                filename_stem=filename_stem,
            )
        except Exception as e:
            print(f"处理抖音链接失败：{e}", file=sys.stderr)
            sys.exit(1)
    else:
        audio_path = args.audio_path

    try:
        # Stream to stdout and capture full transcript
        result = transcribe_audio_streaming(
            api_key=api_key,
            audio_path=audio_path,
            model_name=args.model_name,
            language_hint=args.language_hint,
        )
    except Exception as e:
        print(f"\n转写失败：{e}", file=sys.stderr)
        sys.exit(1)

    # Ensure a trailing newline after streaming output
    print()

    # Determine output path
    out_path = args.out_path
    if not out_path:
        if getattr(args, "youtube_url", None) or getattr(args, "video_url", None) or getattr(args, "douyin_share_or_url", None):
            data_dir = os.path.join(".", "data")
            os.makedirs(data_dir, exist_ok=True)
            base_name = os.path.splitext(os.path.basename(audio_path))[0]
            out_path = os.path.join(data_dir, base_name + ".txt")
        else:
            base, _ = os.path.splitext(audio_path)
            out_path = base + ".txt"

    # 确保输出目录存在
    try:
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
        # Notify via stderr to avoid polluting stdout transcript
        print(f"已保存到: {out_path}", file=sys.stderr)
    except Exception as e:
        print(f"保存文本失败：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()



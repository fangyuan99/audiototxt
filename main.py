import argparse
import os
import sys
import time
from typing import Optional


def set_proxies(proxy: Optional[str], proxy_http: Optional[str], proxy_https: Optional[str]) -> None:
    """Configure HTTP(S) proxy via environment variables for the current process.

    If a single proxy is provided, apply it to both HTTP and HTTPS.
    Explicit protocol-specific args override the single proxy.
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
        "你是一名专业的听打员，只做逐字转写，不做任何总结、解释或翻译。\n"
        f"{language_line}。若内容不确定或听不清，请在原位以方括号标注（如：[听不清 00:01:23]、[不确定：人名?]）。\n"
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


def download_audio_from_youtube(
    youtube_url: str,
    output_dir: str = "./data",
    preferred_audio_codec: str = "m4a",
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
            opts["progress_hooks"] = [_progress_hook]
            opts.pop("postprocessors", None)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                return _extract_path_with_ydl(ydl, info)
        except Exception as e2:
            raise RuntimeError(f"下载失败：{e2}") from e

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
            audio_path = download_audio_from_youtube(args.youtube_url, output_dir=data_dir)
        except Exception as e:
            print(f"下载音频失败：{e}", file=sys.stderr)
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
        if getattr(args, "youtube_url", None):
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



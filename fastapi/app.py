import os
import sys
import uuid
import asyncio
import io
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


# Ensure we can import project root modules like main.py
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from main import (  # noqa: E402
    transcribe_audio_streaming,
    transcribe_youtube_url_streaming,
    download_audio_from_youtube,
    download_video_and_extract_audio,
    fetch_douyin_mp3_via_tiksave,
    download_audio_from_direct_url,
    set_proxies,
    cleanup_old_files,
    start_cleanup_timer,
)


DATA_DIR = os.path.join(ROOT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# 默认清理间隔24小时
DEFAULT_CLEANUP_HOURS = 24.0

app = FastAPI(title="AudioToTxt UI")

static_dir = os.path.join(os.path.dirname(__file__), "static")
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)


@dataclass
class JobState:
    status: str = "pending"
    message: str = ""
    transcript: str = ""
    output_filename: Optional[str] = None
    queue: "asyncio.Queue[Dict[str, Any]]" = field(default_factory=asyncio.Queue)


jobs: Dict[str, JobState] = {}
jobs_lock = asyncio.Lock()


@app.on_event("startup")
async def startup_event():
    """应用启动时的初始化任务"""
    # 启动定时清理任务
    cleanup_hours = float(os.getenv("CLEANUP_HOURS", DEFAULT_CLEANUP_HOURS))
    if cleanup_hours > 0:
        start_cleanup_timer(DATA_DIR, cleanup_hours)
        print(f"FastAPI: 已启动定时清理任务，每 {cleanup_hours} 小时清理一次", file=sys.stderr)


async def publish(job_id: str, event: Dict[str, Any]) -> None:
    async with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return
    await job.queue.put(event)


def _make_chunk_callback(job_id: str, job: JobState) -> Callable[[str], None]:
    loop = asyncio.get_event_loop()

    def on_chunk(delta: str) -> None:
        job.transcript += delta
        try:
            asyncio.run_coroutine_threadsafe(
                publish(job_id, {"type": "chunk", "data": delta}),
                loop,
            )
        except Exception:
            pass

    return on_chunk


class _WSStderr(io.TextIOBase):
    def __init__(self, job_id: str, loop: asyncio.AbstractEventLoop):
        self.job_id = job_id
        self._buffer = ""
        self._loop = loop

    def write(self, s: str) -> int:  # type: ignore[override]
        if not isinstance(s, str):
            s = str(s)
        self._buffer += s
        # Flush on newline or when buffer grows
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            text = line.strip()
            if text:
                try:
                    asyncio.run_coroutine_threadsafe(
                        publish(self.job_id, {"type": "status", "data": text}),
                        self._loop,
                    )
                except Exception:
                    pass
        # If progress like 'xx%' without newline, occasionally push
        if any(token in self._buffer for token in ["%", "✓", "...", "…"]):
            text = self._buffer.strip()
            if text:
                try:
                    asyncio.run_coroutine_threadsafe(
                        publish(self.job_id, {"type": "status", "data": text}),
                        self._loop,
                    )
                except Exception:
                    pass
                self._buffer = ""
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        if self._buffer.strip():
            try:
                asyncio.run_coroutine_threadsafe(
                    publish(self.job_id, {"type": "status", "data": self._buffer.strip()}),
                    self._loop,
                )
            except Exception:
                pass
        self._buffer = ""


class _capture_stderr:
    def __init__(self, job_id: str, loop: asyncio.AbstractEventLoop):
        self.job_id = job_id
        self._loop = loop
        self._old = None

    def __enter__(self):
        self._old = sys.stderr
        sys.stderr = _WSStderr(self.job_id, self._loop)  # type: ignore[assignment]
        return sys.stderr

    def __exit__(self, exc_type, exc, tb):
        try:
            if hasattr(sys.stderr, "flush"):
                try:
                    sys.stderr.flush()
                except Exception:
                    pass
        finally:
            if self._old is not None:
                sys.stderr = self._old


async def _run_task(
    job_id: str,
    source_type: str,
    api_key: Optional[str],
    model_name: str,
    language_hint: Optional[str],
    uploaded_file: Optional[UploadFile],
    youtube_url: Optional[str],
    video_url: Optional[str],
    douyin_text: Optional[str],
    proxy: Optional[str],
    proxy_http: Optional[str],
    proxy_https: Optional[str],
    cookies_file: Optional[UploadFile] = None,
) -> None:
    async with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return

    try:
        await publish(job_id, {"type": "status", "data": "初始化任务"})

        # Respect proxy configuration (or keep existing env if none provided)
        set_proxies(proxy, proxy_http, proxy_https)

        # Capture progress lines printed to stderr by underlying utilities
        loop = asyncio.get_running_loop()
        with _capture_stderr(job_id, loop):
            # Determine audio source
            audio_path: Optional[str] = None
            file_base_name: Optional[str] = None
            transcript: str = ""

            if source_type == "audio":
                if uploaded_file is None:
                    raise RuntimeError("未接收到上传的音频文件")
                await publish(job_id, {"type": "status", "data": "保存上传文件"})
                # Save to DATA_DIR with a unique name preserving extension
                name, ext = os.path.splitext(uploaded_file.filename or f"upload_{job_id}.m4a")
                safe_ext = ext if ext else ".m4a"
                target = os.path.join(DATA_DIR, f"{name}_{job_id}{safe_ext}")
                content = await uploaded_file.read()
                with open(target, "wb") as f:
                    f.write(content)
                audio_path = target

            elif source_type == "youtube":
                if not youtube_url:
                    raise RuntimeError("缺少 YouTube 链接")
                await publish(job_id, {"type": "status", "data": "开始转写（YouTube 直连）"})
                # Stream transcript directly from YouTube URL without downloading
                chunk_cb = _make_chunk_callback(job_id, job)
                transcript = await asyncio.to_thread(
                    transcribe_youtube_url_streaming,
                    api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "",
                    youtube_url,
                    model_name,
                    language_hint,
                    chunk_cb,
                )
                # Derive a filename from YouTube video id
                try:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(youtube_url)
                    q = parse_qs(parsed.query)
                    vid = (q.get("v") or [None])[0]
                    if not vid and parsed.path:
                        # youtu.be/{id}
                        parts = [p for p in parsed.path.split("/") if p]
                        if parts:
                            vid = parts[-1]
                    file_base_name = f"youtube_{vid}" if vid else f"youtube_{int(asyncio.get_event_loop().time()*1000):.0f}"
                except Exception:
                    file_base_name = f"youtube_{int(asyncio.get_event_loop().time()*1000):.0f}"

            elif source_type == "video_url":
                if not video_url:
                    raise RuntimeError("缺少视频直链 URL")
                await publish(job_id, {"type": "status", "data": "下载视频并提取音频"})
                audio_path = await asyncio.to_thread(download_video_and_extract_audio, video_url, DATA_DIR)

            elif source_type == "douyin":
                if not douyin_text:
                    raise RuntimeError("缺少抖音分享口令或短链")
                await publish(job_id, {"type": "status", "data": "解析抖音直链"})
                mp3_url, title, tiktok_id = fetch_douyin_mp3_via_tiksave(douyin_text)
                stem = f"douyin_{tiktok_id}" if tiktok_id else f"douyin_{int(asyncio.get_event_loop().time()*1000):.0f}"
                await publish(job_id, {"type": "status", "data": "下载抖音音频"})
                audio_path = await asyncio.to_thread(
                    download_audio_from_direct_url,
                    mp3_url,
                    DATA_DIR,
                    "mp3",
                    stem,
                )

            else:
                raise RuntimeError(f"未知的来源类型：{source_type}")

            # For non-YouTube sources we use local audio file transcription
            if source_type != "youtube":
                if not audio_path or not os.path.isfile(audio_path):
                    raise RuntimeError("音频文件不存在或下载失败")
                await publish(job_id, {"type": "status", "data": "开始转写"})
                chunk_cb = _make_chunk_callback(job_id, job)
                transcript = await asyncio.to_thread(
                    transcribe_audio_streaming,
                    api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "",
                    audio_path,
                    model_name,
                    language_hint,
                    chunk_cb,
                )

        # Persist transcript similar to main.py
        if file_base_name:
            base_name = file_base_name
        else:
            base_name = os.path.splitext(os.path.basename(audio_path))[0]
        out_path = os.path.join(DATA_DIR, base_name + ".txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(transcript)

        job.status = "done"
        job.output_filename = os.path.basename(out_path)
        await publish(job_id, {"type": "done", "data": {"output_filename": job.output_filename}})

    except Exception as e:
        job.status = "error"
        job.message = str(e)
        await publish(job_id, {"type": "error", "data": job.message})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/transcribe")
async def api_transcribe(
    request: Request,
    source_type: str = Form(...),
    api_key: Optional[str] = Form(None),
    model_name: str = Form("gemini-2.5-flash"),
    language_hint: Optional[str] = Form(None),
    proxy: Optional[str] = Form(None),
    proxy_http: Optional[str] = Form(None),
    proxy_https: Optional[str] = Form(None),
    youtube_url: Optional[str] = Form(None),
    video_url: Optional[str] = Form(None),
    douyin_text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    cookies_file: Optional[UploadFile] = File(None),
):
    job_id = uuid.uuid4().hex
    job = JobState(status="pending", message="")
    async with jobs_lock:
        jobs[job_id] = job

    # Spawn background task
    asyncio.create_task(
        _run_task(
            job_id=job_id,
            source_type=source_type,
            api_key=api_key,
            model_name=model_name,
            language_hint=language_hint,
            uploaded_file=file,
            youtube_url=youtube_url,
            video_url=video_url,
            douyin_text=douyin_text,
            proxy=proxy,
            proxy_http=proxy_http,
            proxy_https=proxy_https,
            cookies_file=cookies_file,
        )
    )

    return JSONResponse({"job_id": job_id})


@app.websocket("/ws/{job_id}")
async def ws_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        async with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            await websocket.send_json({"type": "error", "data": "任务不存在"})
            await websocket.close()
            return

        # Send initial state
        if job.transcript:
            await websocket.send_json({"type": "chunk", "data": job.transcript})

        while True:
            event = await job.queue.get()
            await websocket.send_json(event)

    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass


@app.get("/download/{filename}")
async def download_result(filename: str):
    # Security: only serve files from DATA_DIR and disallow path traversal
    if os.path.sep in filename or os.path.altsep and os.path.altsep in filename:
        return JSONResponse({"error": "非法文件名"}, status_code=400)
    path = os.path.join(DATA_DIR, filename)
    if not os.path.isfile(path):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(path, filename=filename, media_type="text/plain; charset=utf-8")


@app.post("/api/cleanup")
async def api_cleanup(max_age_hours: Optional[float] = None):
    """手动清理data目录中的过期文件"""
    try:
        age_hours = max_age_hours or DEFAULT_CLEANUP_HOURS
        cleanup_old_files(DATA_DIR, age_hours)
        return JSONResponse({"status": "success", "message": f"已清理超过 {age_hours} 小时的文件"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/files")
async def api_list_files():
    """列出data目录中的文件"""
    try:
        import glob
        pattern = os.path.join(DATA_DIR, "*")
        files = glob.glob(pattern)
        
        file_list = []
        for file_path in files:
            if os.path.isfile(file_path):
                stat = os.stat(file_path)
                file_list.append({
                    "name": os.path.basename(file_path),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "age_hours": (time.time() - stat.st_mtime) / 3600
                })
        
        # 按修改时间排序，最新的在前
        file_list.sort(key=lambda x: x["modified"], reverse=True)
        
        return JSONResponse({
            "status": "success", 
            "files": file_list,
            "total_count": len(file_list)
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)



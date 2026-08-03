from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketException,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from key_pool import GeminiKeyPool
from media_policy import MediaPolicy, sanitize_upload_name
from service_config import BotPaths, GlobalConfigStore
from transcription_service import (
    TaskDeadline,
    TranscriptionRequest,
    TranscriptionService,
)


MODULE_ROOT = Path(__file__).resolve().parent
SESSION_COOKIE = "audiototxt_web_session"


def _env_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _session_token(access_key: str) -> str:
    return hashlib.sha256(f"audiototxt-web-v1\0{access_key}".encode()).hexdigest()


def _disabled_app(mode: str) -> FastAPI:
    app = FastAPI(title="AudioToTxt Web", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health():
        payload = {"status": "ok" if mode == "disabled" else "error", "web": mode}
        return JSONResponse(payload)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def unavailable(path: str):
        return JSONResponse(
            {"error": "Web UI is disabled" if mode == "disabled" else "Web UI is misconfigured"},
            status_code=503,
        )

    return app


@dataclass
class WebJob:
    job_id: str
    request: TranscriptionRequest
    status: str = "pending"
    message: str = ""
    transcript: str = ""
    output_filename: str = ""
    created_at: float = field(default_factory=time.time)
    terminal_at: float = 0.0
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None
    cancel_requested: bool = False


def create_web_app(
    *,
    root_dir: str | Path = MODULE_ROOT,
    data_dir: Optional[str | Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    transcription_service: Optional[TranscriptionService] = None,
    telegram_factory: Optional[Callable[[], object]] = None,
    allow_embedded_telegram: bool = False,
) -> FastAPI:
    values = dict(os.environ if environ is None else environ)
    if not _env_bool(values.get("WEB_ENABLED"), False):
        return _disabled_app("disabled")
    access_key = str(values.get("WEB_ACCESS_KEY", "")).strip()
    if not access_key:
        return _disabled_app("misconfigured")

    project_root = Path(root_dir).resolve()
    asset_root = project_root / "fastapi"
    if not (asset_root / "templates").exists():
        asset_root = MODULE_ROOT / "fastapi"
    web_data_dir = Path(data_dir or values.get("WEB_DATA_DIR") or project_root / "data" / "web").resolve()
    uploads_dir = web_data_dir / "uploads"
    outputs_dir = web_data_dir / "outputs"
    for directory in (web_data_dir, uploads_dir, outputs_dir):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    media_policy = MediaPolicy.from_environ(values)

    bot_paths = BotPaths.from_environ(project_root, values)
    bot_paths.ensure_directories()
    config_store = GlobalConfigStore(bot_paths.global_config_file, environ=values)
    settings = config_store.get()
    key_pool = GeminiKeyPool(settings.gemini_api_keys)
    service = transcription_service or TranscriptionService(
        config_store,
        key_pool,
        work_dir=uploads_dir,
        media_policy=media_policy,
    )

    jobs: dict[str, WebJob] = {}
    jobs_lock = asyncio.Lock()
    expected_session = _session_token(access_key)
    templates = Jinja2Templates(directory=str(asset_root / "templates"))

    async def evict_jobs() -> None:
        while True:
            await asyncio.sleep(3600)
            cutoff = time.time() - 24 * 3600
            async with jobs_lock:
                expired = [
                    job_id
                    for job_id, job in jobs.items()
                    if job.terminal_at and job.terminal_at < cutoff
                ]
                for job_id in expired:
                    job = jobs.pop(job_id)
                    if job.output_filename:
                        (outputs_dir / job.output_filename).unlink(missing_ok=True)
                    if job.request.audio_path:
                        job.request.audio_path.unlink(missing_ok=True)
            active_uploads = {
                job.request.audio_path.resolve()
                for job in jobs.values()
                if job.request.audio_path and job.status in {"pending", "running"}
            }
            cutoff = time.time() - 24 * 3600
            for path in uploads_dir.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if path.resolve() not in active_uploads and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        maintenance = asyncio.create_task(evict_jobs(), name="web-job-maintenance")
        telegram_app = None
        if allow_embedded_telegram and _env_bool(
            values.get("TELEGRAM_EMBEDDED_ENABLED"), False
        ):
            if telegram_factory is None:
                from telegram_bot import build_application

                active_factory = build_application
            else:
                active_factory = telegram_factory
            telegram_app = active_factory()
            from telegram_bot import start_embedded_polling

            await start_embedded_polling(telegram_app)
        try:
            yield
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            async with jobs_lock:
                tasks = [job.task for job in jobs.values() if job.task and not job.task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if telegram_app is not None:
                from telegram_bot import stop_embedded_polling

                await stop_embedded_polling(telegram_app)

    app = FastAPI(
        title="AudioToTxt Web",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.jobs = jobs
    app.state.transcription_service = service
    app.mount("/static", StaticFiles(directory=str(asset_root / "static")), name="static")

    def request_authorized(request: Request) -> bool:
        provided = request.cookies.get(SESSION_COOKIE, "")
        return bool(provided) and hmac.compare_digest(provided, expected_session)

    def websocket_authorized(websocket: WebSocket) -> bool:
        provided = websocket.cookies.get(SESSION_COOKIE, "")
        return bool(provided) and hmac.compare_digest(provided, expected_session)

    def require_request(request: Request) -> None:
        if not request_authorized(request):
            raise HTTPException(status_code=401, detail="Authentication required")

    async def publish(job: WebJob, event: dict[str, Any]) -> None:
        await job.queue.put(event)

    async def run_job(job: WebJob) -> None:
        job.status = "running"
        loop = asyncio.get_running_loop()
        status_labels = {
            "parsing": "正在解析来源",
            "downloading": "正在下载媒体",
            "extracting": "正在抽取音频",
            "retrying": "临时失败，正在重试",
            "transcribing": "正在转写",
        }

        def on_status(value: str) -> None:
            job.message = status_labels.get(value, value)
            asyncio.run_coroutine_threadsafe(
                publish(job, {"type": "status", "data": job.message}), loop
            )

        try:
            async_execute = getattr(service, "execute_async", None)
            if callable(async_execute):
                operation = async_execute(
                    job.request,
                    on_status=on_status,
                    cancelled=lambda: job.cancel_requested,
                    deadline=TaskDeadline(media_policy.task_timeout_seconds),
                )
            else:
                operation = asyncio.to_thread(
                    service.execute,
                    job.request,
                    on_status=on_status,
                    cancelled=lambda: job.cancel_requested,
                    deadline=TaskDeadline(media_policy.task_timeout_seconds),
                )
            result = await asyncio.wait_for(operation, timeout=media_policy.task_timeout_seconds)
            if job.cancel_requested:
                raise asyncio.CancelledError
            job.transcript = result.transcript
            output_filename = f"{job.job_id}_{sanitize_upload_name(result.filename_stem, default='transcript')}.txt"
            output_path = outputs_dir / output_filename
            output_path.write_text(result.transcript, encoding="utf-8")
            os.chmod(output_path, 0o600)
            job.output_filename = output_filename
            job.status = "done"
            await publish(job, {"type": "chunk", "data": result.transcript})
            await publish(
                job,
                {"type": "done", "data": {"output_filename": output_filename}},
            )
            for cleanup_path in result.cleanup_paths:
                Path(cleanup_path).unlink(missing_ok=True)
        except asyncio.TimeoutError:
            job.cancel_requested = True
            job.status = "error"
            job.message = "任务处理超时"
            await publish(job, {"type": "error", "data": job.message})
        except asyncio.CancelledError:
            job.cancel_requested = True
            job.status = "cancelled"
            job.message = "任务已取消"
            if job.request.audio_path:
                job.request.audio_path.unlink(missing_ok=True)
            await publish(job, {"type": "error", "data": "任务已取消"})
        except Exception:
            job.status = "error"
            job.message = "任务执行失败"
            await publish(job, {"type": "error", "data": job.message})
        finally:
            job.terminal_at = time.time()

    @app.get("/health")
    async def health():
        return {"status": "ok", "web": "enabled"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if request_authorized(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": ""})

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, access_key_input: str = Form(alias="access_key")):
        if not hmac.compare_digest(access_key_input, access_key):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "访问密钥不正确"},
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            expected_session,
            max_age=7 * 86400,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        require_request(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not request_authorized(request):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "index.html")

    @app.post("/api/transcribe")
    async def api_transcribe(
        request: Request,
        source_type: str = Form(...),
        youtube_url: Optional[str] = Form(None),
        video_url: Optional[str] = Form(None),
        douyin_text: Optional[str] = Form(None),
        file: Optional[UploadFile] = File(None),
    ):
        require_request(request)
        source_type = source_type.strip().lower()
        if source_type not in {"audio", "youtube", "video_url", "douyin"}:
            raise HTTPException(status_code=400, detail="Unsupported source type")

        audio_path: Optional[Path] = None
        if source_type == "audio":
            if file is None:
                raise HTTPException(status_code=400, detail="Audio file required")
            safe_name = sanitize_upload_name(file.filename or "audio.bin")
            audio_path = uploads_dir / f"web_{uuid.uuid4().hex}_{safe_name}"
            total = 0
            try:
                with audio_path.open("wb") as handle:
                    while True:
                        chunk = await file.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > media_policy.max_media_bytes:
                            raise HTTPException(status_code=413, detail="Upload too large")
                        handle.write(chunk)
                os.chmod(audio_path, 0o600)
            except Exception:
                audio_path.unlink(missing_ok=True)
                raise
            transcription_request = TranscriptionRequest(
                source_type="audio",
                audio_path=audio_path,
                original_filename=file.filename or safe_name,
                cleanup_input=True,
            )
        else:
            values_by_source = {
                "youtube": youtube_url,
                "video_url": video_url,
                "douyin": douyin_text,
            }
            text_input = (values_by_source[source_type] or "").strip()
            if not text_input:
                raise HTTPException(status_code=400, detail="Source input required")
            transcription_request = TranscriptionRequest(
                source_type=source_type,
                text_input=text_input,
            )

        job = WebJob(job_id=uuid.uuid4().hex, request=transcription_request)
        async with jobs_lock:
            jobs[job.job_id] = job
        job.task = asyncio.create_task(run_job(job), name=f"web-job-{job.job_id}")
        return {"job_id": job.job_id}

    @app.get("/api/jobs/{job_id}")
    async def api_job(request: Request, job_id: str):
        require_request(request)
        async with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "message": job.message,
            "output_filename": job.output_filename or None,
        }

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel(request: Request, job_id: str):
        require_request(request)
        async with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status not in {"pending", "running"}:
            return {"status": job.status}
        job.cancel_requested = True
        if job.task is not None:
            job.task.cancel()
        return {"status": "cancelling"}

    @app.websocket("/ws/{job_id}")
    async def websocket_progress(websocket: WebSocket, job_id: str):
        if not websocket_authorized(websocket):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        async with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        await websocket.accept()
        if job.transcript:
            await websocket.send_json({"type": "chunk", "data": job.transcript})
        if job.status == "done":
            await websocket.send_json(
                {"type": "done", "data": {"output_filename": job.output_filename}}
            )
            await websocket.close()
            return
        if job.status in {"error", "cancelled"}:
            await websocket.send_json({"type": "error", "data": job.message})
            await websocket.close()
            return
        while True:
            event = await job.queue.get()
            await websocket.send_json(event)
            if event.get("type") in {"done", "error"}:
                await websocket.close()
                return

    @app.get("/download/{filename}")
    async def download_result(request: Request, filename: str):
        require_request(request)
        if filename != Path(filename).name:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = (outputs_dir / filename).resolve()
        try:
            path.relative_to(outputs_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid filename") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return Response(
            content=path.read_bytes(),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/files")
    async def api_files(request: Request):
        require_request(request)
        files = []
        for path in outputs_dir.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            info = path.stat()
            files.append(
                {"name": path.name, "size": info.st_size, "modified": info.st_mtime}
            )
        files.sort(key=lambda item: item["modified"], reverse=True)
        return {"status": "success", "files": files, "total_count": len(files)}

    @app.post("/api/cleanup")
    async def api_cleanup(request: Request):
        require_request(request)
        cutoff = time.time() - 24 * 3600
        removed = 0
        for path in outputs_dir.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return {"status": "success", "removed": removed}

    return app

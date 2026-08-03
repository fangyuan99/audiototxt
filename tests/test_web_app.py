import asyncio
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
from fastapi import WebSocketException

from transcription_service import TranscriptionResult
from web_app import create_web_app


class FakeTranscriptionService:
    def __init__(self):
        self.requests = []

    def execute(self, request, *, on_status=None, cancelled=None, deadline=None):
        self.requests.append(request)
        if request.audio_path is not None:
            assert request.audio_path.is_file()
        if on_status:
            on_status("transcribing")
        return TranscriptionResult(
            transcript="测试逐字稿",
            filename_stem="result",
            cleanup_paths=(request.audio_path,) if request.audio_path else (),
        )

    async def execute_async(self, request, *, on_status=None, cancelled=None, deadline=None):
        return self.execute(
            request,
            on_status=on_status,
            cancelled=cancelled,
            deadline=deadline,
        )


class WebAppTest(unittest.IsolatedAsyncioTestCase):
    async def request_client(self, app):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        )

    async def test_web_is_disabled_by_default_and_never_starts_embedded_bot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            telegram_factory = Mock(side_effect=AssertionError("must not start"))
            app = create_web_app(
                root_dir=tmp_dir,
                data_dir=Path(tmp_dir) / "web",
                environ={"TELEGRAM_EMBEDDED_ENABLED": "true"},
                telegram_factory=telegram_factory,
                allow_embedded_telegram=True,
            )
            health_route = next(
                route for route in app.routes if getattr(route, "path", "") == "/health"
            )
            catchall_route = next(
                route for route in app.routes if getattr(route, "path", "") == "/{path:path}"
            )
            health = await health_route.endpoint()
            unavailable = await catchall_route.endpoint("")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(json.loads(health.body)["web"], "disabled")
            self.assertEqual(unavailable.status_code, 503)
            telegram_factory.assert_not_called()

    async def test_enabled_without_access_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = create_web_app(
                root_dir=tmp_dir,
                data_dir=Path(tmp_dir) / "web",
                environ={"WEB_ENABLED": "true", "WEB_ACCESS_KEY": "   "},
            )
            async with await self.request_client(app) as client:
                self.assertEqual((await client.get("/health")).json()["web"], "misconfigured")
                self.assertEqual((await client.get("/")).status_code, 503)
                self.assertEqual((await client.get("/api/files")).status_code, 503)

    async def test_login_cookie_protects_http_and_websocket_routes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = create_web_app(
                root_dir=tmp_dir,
                data_dir=Path(tmp_dir) / "web",
                environ={"WEB_ENABLED": "true", "WEB_ACCESS_KEY": "web-secret"},
                transcription_service=FakeTranscriptionService(),
            )
            async with app.router.lifespan_context(app):
                async with await self.request_client(app) as client:
                    self.assertEqual((await client.get("/api/files")).status_code, 401)

                    websocket_route = next(
                        route for route in app.routes if getattr(route, "path", "") == "/ws/{job_id}"
                    )
                    fake_websocket = type("FakeWebSocket", (), {"cookies": {}})()
                    with self.assertRaises(WebSocketException) as raised:
                        await websocket_route.endpoint(fake_websocket, "missing")
                    self.assertEqual(raised.exception.code, 1008)

                    wrong = await client.post(
                    "/login", data={"access_key": "wrong"}, follow_redirects=False
                    )
                    self.assertEqual(wrong.status_code, 401)
                    response = await client.post(
                    "/login", data={"access_key": "web-secret"}, follow_redirects=False
                    )
                    self.assertEqual(response.status_code, 303)
                    cookie = response.headers["set-cookie"].lower()
                    self.assertIn("httponly", cookie)
                    self.assertIn("samesite=strict", cookie)
                    self.assertEqual((await client.get("/api/files")).status_code, 200)

    async def test_upload_is_owned_sanitized_bounded_and_processed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service = FakeTranscriptionService()
            app = create_web_app(
                root_dir=root,
                data_dir=root / "web",
                environ={
                    "WEB_ENABLED": "true",
                    "WEB_ACCESS_KEY": "web-secret",
                    "MAX_MEDIA_BYTES": "32",
                },
                transcription_service=service,
            )
            async with app.router.lifespan_context(app):
                async with await self.request_client(app) as client:
                    await client.post("/login", data={"access_key": "web-secret"})
                    response = await client.post(
                    "/api/transcribe",
                    data={"source_type": "audio"},
                    files={"file": ("../../escape.mp3", b"audio", "audio/mpeg")},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    job_id = response.json()["job_id"]

                    terminal = None
                    for _ in range(100):
                        terminal = (await client.get(f"/api/jobs/{job_id}")).json()
                        if terminal["status"] in {"done", "error", "cancelled"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(terminal["status"], "done")
                    self.assertEqual(len(service.requests), 1)
                    request_path = service.requests[0].audio_path
                    self.assertNotIn("..", request_path.name)
                    self.assertTrue(str(request_path).startswith(str(root / "web")))

                    download = await client.get(f"/download/{terminal['output_filename']}")
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.text, "测试逐字稿")

                    page = await client.get("/")
                    self.assertNotIn('name="api_key"', page.text)
                    self.assertNotIn('name="proxy"', page.text)

    async def test_oversized_upload_is_rejected_before_background_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = FakeTranscriptionService()
            app = create_web_app(
                root_dir=tmp_dir,
                data_dir=Path(tmp_dir) / "web",
                environ={
                    "WEB_ENABLED": "true",
                    "WEB_ACCESS_KEY": "web-secret",
                    "MAX_MEDIA_BYTES": "3",
                },
                transcription_service=service,
            )
            async with app.router.lifespan_context(app):
                async with await self.request_client(app) as client:
                    await client.post("/login", data={"access_key": "web-secret"})
                    response = await client.post(
                    "/api/transcribe",
                    data={"source_type": "audio"},
                    files={"file": ("audio.mp3", b"four", "audio/mpeg")},
                    )
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(service.requests, [])

    async def test_timed_out_job_sets_cancellation_and_terminal_error(self):
        class SlowService:
            async def execute_async(
                self, request, *, on_status=None, cancelled=None, deadline=None
            ):
                await asyncio.sleep(5)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            app = create_web_app(
                root_dir=root,
                data_dir=root / "web",
                environ={
                    "WEB_ENABLED": "true",
                    "WEB_ACCESS_KEY": "web-secret",
                    "MEDIA_TASK_TIMEOUT_SECONDS": "1",
                },
                transcription_service=SlowService(),
            )
            async with app.router.lifespan_context(app):
                async with await self.request_client(app) as client:
                    await client.post("/login", data={"access_key": "web-secret"})
                    response = await client.post(
                        "/api/transcribe",
                        data={"source_type": "youtube", "youtube_url": "https://youtu.be/abc"},
                    )
                    job_id = response.json()["job_id"]

                    terminal = None
                    for _ in range(150):
                        terminal = (await client.get(f"/api/jobs/{job_id}")).json()
                        if terminal["status"] in {"done", "error", "cancelled"}:
                            break
                        await asyncio.sleep(0.01)

                    self.assertEqual(terminal["status"], "error")
                    self.assertEqual(terminal["message"], "任务处理超时")
                    self.assertTrue(app.state.jobs[job_id].cancel_requested)


if __name__ == "__main__":
    unittest.main()

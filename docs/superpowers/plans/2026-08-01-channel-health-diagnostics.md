# Channel Health and Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe `gemini-2.5-flash-lite` channel probe, automatic Vertex validation, correct Douyin routing, and actionable per-stage Telegram failure diagnostics.

**Architecture:** Introduce a small `channel_health.py` boundary for provider probing and exception sanitization. Keep transcription stage production in the shared service, persist only safe terminal diagnostics in the queue, and let Telegram render/log those diagnostics. Existing Gemini/Vertex client construction and key-pool behavior remain the single provider implementation.

**Tech Stack:** Python 3.12, `google-genai`, `python-telegram-bot` 21.x, standard-library `unittest`, Ruff.

---

## File map

**Create**

- `channel_health.py` — fixed-model health probe, safe exception classification/redaction, health result formatting inputs.
- `tests/test_channel_health.py` — Gemini/Vertex probe, billing, timeout, empty response, and redaction tests.

**Modify**

- `media_policy.py` — recognize `iesdouyin.com` as a Douyin source.
- `main.py` — emit extraction stage callbacks and stop printing raw URLs.
- `transcription_service.py` — forward semantic extraction stage and reject empty provider output.
- `telegram_jobs.py` — persist terminal stage and safe diagnostic code/message.
- `telegram_bot.py` — health button/callback, automatic Vertex probe, stage-aware safe logging and status cards.
- `tests/test_media_policy.py` — Douyin alternate-host coverage.
- `tests/test_transcription_service.py` — extraction/empty-output stage coverage.
- `tests/test_telegram_jobs.py` — safe stage persistence coverage.
- `tests/test_telegram_bot_formatting.py` — health button/result/stage rendering coverage.
- `tests/test_telegram_bot_interactions.py` — automatic Vertex probe behavior.
- `README.md` — document health checks and actionable job failures.
- `deploy/supervisor/audiototxt-bot.ini` — remains the source of truth for the running process; no credential changes.

## Task 1: Shared channel probe and safe diagnostics

**Files:**

- Create: `channel_health.py`
- Create: `tests/test_channel_health.py`

- [ ] **Step 1: Write failing result/classification tests**

Cover a successful response, empty response, `403 BILLING_DISABLED`, generic `403`, `404 model`, `429`, timeout/transport errors, FFmpeg invalid media, and redaction of API keys, Telegram tokens, service-account emails, local paths, and signed URLs.

```python
def test_billing_disabled_is_safe_and_actionable():
    error = RuntimeError("403 BILLING_DISABLED for https://example/get?token=secret")
    diagnosis = diagnose_exception(error)
    assert diagnosis.code == "billing_disabled"
    assert diagnosis.user_message == "Google Cloud 项目未启用结算。"
    assert "secret" not in diagnosis.log_detail
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run: `/home/.venv/bin/python -m unittest tests.test_channel_health -v`

Expected: import failure for `channel_health`.

- [ ] **Step 3: Implement safe diagnostic primitives**

Add immutable `ErrorDiagnosis` and `ChannelHealthResult` records, `HEALTH_CHECK_MODEL = "gemini-2.5-flash-lite"`, bounded cause-chain traversal, status extraction, and `sanitize_error_detail`. URL replacements retain only a hostname, e.g. `<url host=dl.snapcdn.app>`.

- [ ] **Step 4: Implement the synchronous fixed-model probe**

```python
def check_current_channel(settings, key_pool, *, timeout_seconds=20.0):
    """Send exactly 'hi' with HEALTH_CHECK_MODEL and never expose response text."""
```

Gemini mode synchronizes and uses `GeminiKeyPool.run`; Vertex mode calls `build_auth_config` with `settings.vertex_location or "global"`. Every client closes in `finally`. A non-empty `response.text` is required.

- [ ] **Step 5: Run focused tests**

Run: `/home/.venv/bin/python -m unittest tests.test_channel_health -v`

Expected: all probe and redaction tests pass without network.

## Task 2: Correct source routing and stage production

**Files:**

- Modify: `media_policy.py`
- Modify: `main.py`
- Modify: `transcription_service.py`
- Modify: `tests/test_media_policy.py`
- Modify: `tests/test_transcription_service.py`

- [ ] **Step 1: Add failing alternate-Douyin and extraction-stage tests**

Assert that both `https://www.iesdouyin.com/share/video/...` and subdomains route to `douyin`. Assert a generic video download can emit `downloading` then `extracting`, and that an empty provider transcript raises a deterministic safe error.

- [ ] **Step 2: Run focused tests and confirm failures**

Run: `/home/.venv/bin/python -m unittest tests.test_media_policy tests.test_transcription_service -v`

- [ ] **Step 3: Expand Douyin host recognition**

Use the existing boundary-safe `_host_matches` helper for `douyin.com` and `iesdouyin.com`; do not use substring matching. When retrying a historical text/URL job, re-run source detection so an old `video_url` record for `iesdouyin.com` is retried as `douyin`.

- [ ] **Step 4: Add extraction callback and eliminate raw job-path output**

Extend `download_video_and_extract_audio(..., on_status=None)` and call `on_status("extracting")` immediately before FFmpeg. Replace URL prints with hostname-only text. Remove full FFmpeg stderr prints and change Telegram-path wrappers from interpolated exceptions to generic messages with chained causes (for example, `raise RuntimeError("音频提取失败") from exc`). Audit every `print` and raised wrapper reachable from `TranscriptionService` so signed URLs and sensitive paths cannot be emitted before structured sanitization.

- [ ] **Step 5: Forward the callback and enforce non-empty output**

Pass `on_status=emit` from `TranscriptionService`. Normalize provider output to text and raise `EmptyTranscriptionError` when blank so malformed/empty generations do not look successful.

- [ ] **Step 6: Run focused tests**

Run: `/home/.venv/bin/python -m unittest tests.test_media_policy tests.test_transcription_service tests.test_youtube_direct -v`

Expected: all pass.

## Task 3: Persist safe terminal stage information

**Files:**

- Modify: `telegram_jobs.py`
- Modify: `tests/test_telegram_jobs.py`

- [ ] **Step 1: Add failing queue diagnostic tests**

Create an executor exception carrying `stage`, `error_code`, and `user_message`. Assert the failed record persists only those safe fields, while successful jobs end at `completed`. Add a timeout test in which the executor reports `transcribing` before blocking; the terminal timeout record must preserve `transcribing`.

- [ ] **Step 2: Run and confirm failures**

Run: `/home/.venv/bin/python -m unittest tests.test_telegram_jobs -v`

- [ ] **Step 3: Add `stage` updates and exception-aware failure storage**

Add a thread-safe `update_stage(job_id, stage)` method so the Telegram execution adapter can persist semantic stages while blocking work runs. Set `preparing` when a worker starts; on queue-level timeout retain the latest persisted stage; on other failures read only safe attributes with conservative defaults; and set `completed` only after the executor has completed both transcription and result delivery. Never store `str(exc)` or traceback content.

- [ ] **Step 4: Run queue tests**

Run: `/home/.venv/bin/python -m unittest tests.test_telegram_jobs -v`

Expected: FIFO/cancel/restart/retry behavior remains green and diagnostics persist safely.

## Task 4: Telegram health UX and stage-aware logging

**Files:**

- Modify: `telegram_bot.py`
- Modify: `tests/test_telegram_bot_formatting.py`
- Modify: `tests/test_telegram_bot_interactions.py`

- [ ] **Step 1: Add failing keyboard and health-rendering tests**

Assert `build_settings_keyboard` includes `channel:test`, success/failure renderings name the fixed probe model, Vertex rendering uses `global`, and failure status includes a localized stage without raw provider detail.

- [ ] **Step 2: Add failing automatic-Vertex probe tests**

Inject a fake health checker. Saving JSON must copy `project_id`, retain/default location `global`, switch to Vertex, delete the credential message, run the checker through `asyncio.to_thread`, and clearly reply available/unavailable. Project/location updates must probe an explicit Vertex snapshot even if the currently selected channel remains Gemini, so they cannot falsely report Gemini health as Vertex health.

- [ ] **Step 3: Implement the manual health callback**

Answer the callback immediately, edit the card to “正在测试”, run `check_current_channel` in a worker thread, log a safe summary, and render a result with the settings keyboard. Existing authorization protects the callback.

- [ ] **Step 4: Implement automatic Vertex probing**

Centralize `_run_channel_health_check(settings_override=None)`. After saving Vertex JSON/project/location, pass a snapshot forced to `auth_mode="vertex_ai_json"` and `vertex_location="global"` when blank, then reply with its result. The manual `channel:test` callback passes no override and therefore tests the currently selected channel. Never echo JSON, project credentials, response text, or raw exception details.

- [ ] **Step 5: Wrap job execution with semantic failure context**

Track each status callback and call `TelegramJobManager.update_stage` from the adapter, so a timeout raised by the manager outside the wrapper still has an accurate stage. On executor exceptions, call `diagnose_exception` and raise a wrapper exposing only `stage`, `error_code`, and `user_message` to `TelegramJobManager`; do not log there.

- [ ] **Step 6: Move delivery inside the executor and add delivery diagnostics**

After transcription returns and cancellation is rechecked, set/persist `delivering` and call `_deliver_result` inside `_execute_job`. Only return to `TelegramJobManager` after Telegram text/document delivery succeeds; `_on_job_update("succeeded")` no longer performs delivery. Therefore a delivery exception propagates through the same safe wrapper and is persisted as a `delivering` failure rather than a false success. Render preparing/parsing/downloading/extracting/transcribing/delivering in status cards and log delivery failure against the same job ID without transcript/document content.

- [ ] **Step 7: Add a global Telegram error handler**

Register a handler that logs exception type/category/bounded sanitized detail. Keep `httpx`/`httpcore` below INFO so Telegram API URLs containing the Bot Token remain excluded.

- [ ] **Step 8: Centralize exactly one structured job-failure record**

Log every terminal `failed` event in `_on_job_update`, after the manager has persisted the final stage. This path covers provider/executor failures, delivery failures, and manager-owned timeouts. Use the safe diagnosis already attached to wrappers or diagnose the timeout payload; derive trace locations with `traceback.extract_tb` when present and use the fixed marker `telegram_jobs.py:_worker(timeout_boundary)` when the timeout payload has no traceback. Emit one record containing job ID, source, stage, category, exception type, bounded detail, and safe trace locations.

- [ ] **Step 9: Run Telegram tests**

Run: `/home/.venv/bin/python -m unittest tests.test_telegram_bot_formatting tests.test_telegram_bot_interactions tests.test_telegram_jobs -v`

Expected: all pass.

## Task 5: Documentation, operational verification, and restart

**Files:**

- Modify: `README.md`
- Verify: `deploy/supervisor/audiototxt-bot.ini`

- [ ] **Step 1: Document the behavior**

Explain the fixed health model, `hi` probe, automatic Vertex probe, `global` default, manual settings button, and sanitized per-stage failure logs.

- [ ] **Step 2: Run the full suite and static checks**

Run:

```bash
timeout 40s /home/.venv/bin/python -m unittest discover -s tests -v
/home/.venv/bin/python -m compileall -q main.py media_policy.py channel_health.py transcription_service.py telegram_jobs.py telegram_bot.py tests
env UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools uvx ruff check --select F,E9 --exclude .codex .
env UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools uvx ruff check --select S channel_health.py media_policy.py transcription_service.py telegram_jobs.py telegram_bot.py
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 3: Re-run the current Vertex probe**

Use the shared health checker against the live global config and print only channel/model/location/availability/category/latency. Expected after the operator supplied `/home/vertex.json`: available on `global` with no secrets; retain the earlier `billing_disabled` result only as diagnosis of the replaced credential.

- [ ] **Step 4: Perform one controlled Supervisor transition**

Run `supervisorctl stop att:att_00`, truncate pre-change protected logs containing signed URLs, and then run `supervisorctl start att:att_00` exactly once. Confirm exactly one `/home/audiototxt/telegram_bot.py` process and stable `RUNNING` status.

- [ ] **Step 5: Audit the protected log**

Without another stop/restart, verify new logs contain no Telegram token, API key, private key marker, signed query, service-account email, full source URL, interpolated provider exception, or full FFmpeg stderr. Confirm the protected parent directory remains mode `0700`.

- [ ] **Step 6: Report diagnosis and live result**

Tell the user that the replaced Vertex credential failed because billing was disabled while `/home/vertex.json` succeeds, identify the `iesdouyin.com` misclassification, note that older remaining failures cannot be reconstructed beyond their last observed stage, and explain how the new health button/logs change future diagnosis.

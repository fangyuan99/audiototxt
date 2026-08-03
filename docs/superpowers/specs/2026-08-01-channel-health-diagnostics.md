# AudioToTxt Channel Health and Failure Diagnostics Specification

## Context

Recent Telegram jobs only persist `task_error / 任务执行失败`, while the service log prints unstructured media progress without job IDs or timestamps. Six inspected failures therefore cannot be reconstructed precisely. The available evidence identifies two concrete problems:

- `www.iesdouyin.com/share/video/...` is classified as `video_url`, so the HTML share page is downloaded and handed to FFmpeg;
- the currently configured Vertex channel fails the required `global + gemini-2.5-flash-lite + "hi"` probe with HTTP 403 and reason `BILLING_DISABLED`.

The current log also prints full signed download URLs. Diagnostics must become more useful without leaking credentials, query tokens, service-account identities, or local sensitive paths.

## Goals

1. Treat `douyin.com` and `iesdouyin.com` hosts as Douyin sources.
2. Add a shared current-channel health probe that sends exactly `hi` with `gemini-2.5-flash-lite`.
3. Run the Vertex probe after saving JSON, project, or location settings and report availability to the authorized user.
4. Add an inline “测试当前渠道” button to global settings for Gemini or Vertex.
5. Default a missing/blank Vertex location to `global` everywhere.
6. Persist and display the stage at which each Telegram job failed.
7. Write structured, sanitized job-failure logs with job ID, source, stage, category, exception type, bounded detail, and stack-frame locations.
8. Stop logging raw source/download URLs and signed query strings.

## Health probe behavior

- The probe snapshots the current global authentication configuration.
- The fixed probe model is `gemini-2.5-flash-lite`; it does not mutate the configured transcription model.
- The prompt is exactly `hi`, the provider timeout is 20 seconds, and the response body is not logged or shown.
- Gemini mode uses the existing healthy key pool and bounded failover.
- Vertex mode uses the stored service-account JSON, project, and location; blank location becomes `global`.
- Success requires a non-empty response text.
- The user sees channel, probe model, region where applicable, latency, and a clear available/unavailable result.
- Failure details are categorized into billing disabled, credentials/IAM, model unavailable, quota/rate limit, network/timeout, empty response, media decoding, unsafe URL, or unknown provider failure.
- No raw API key, private key, Telegram token, signed URL, service-account email, or provider response payload is displayed.

## Vertex settings behavior

- Saving a service-account JSON validates that it is an object.
- If the JSON contains `project_id`, that value becomes the global Vertex project so an old project cannot silently override newly supplied credentials.
- Location remains the existing non-empty value or becomes `global`.
- Saving Vertex JSON, project, or location triggers an explicit Vertex probe immediately and returns its result, even if Gemini remains the currently selected channel while advanced Vertex fields are edited.
- Switching authentication modes does not itself require a probe; the manual button is always available.

## Job-stage diagnostics

Stages are semantic and user-facing:

- `preparing` — preparing the request;
- `parsing` — resolving a share link;
- `downloading` — downloading owned media;
- `extracting` — FFmpeg audio extraction;
- `transcribing` — Gemini/Vertex generation;
- `delivering` — sending Telegram text/document;
- `completed` — terminal success.

The queue persists the latest running stage plus the terminal failure stage and a safe error code/message, including when the queue-level timeout fires outside provider code. A job becomes successful only after Telegram text/document delivery completes; delivery errors remain `delivering` failures. The status card shows the localized stage and safe reason. Raw exceptions never go to Telegram or `jobs.json`.

Server logs use exactly one structured line per terminal task failure, emitted from the manager's terminal failure notification so provider errors, delivery errors, and manager-owned timeouts share the same path. Trace information contains only source filenames, line numbers, and function names; a fixed timeout-boundary marker is used when no exception traceback exists, and frame locals are never serialized. URL values are reduced to hostname-only placeholders, bounded sanitized details are used instead of raw exception dumps, and Telegram-reachable helper code must not print raw FFmpeg stderr or interpolated exception messages before sanitization.

## Operations and acceptance criteria

- The six existing failed jobs remain historical records; new diagnostics apply to subsequent attempts.
- `iesdouyin.com` inputs route through the Douyin parser.
- A fake successful Gemini and Vertex probe reports available; a 403 `BILLING_DISABLED` probe reports “项目未启用结算”.
- The settings keyboard exposes the manual probe callback.
- Saving Vertex JSON with no location stores/uses `global` and automatically runs the probe.
- A simulated FFmpeg/transcription failure persists the correct stage and logs no full URL/token.
- Existing Telegram access control still protects every settings and probe action.
- Full unit tests, syntax checks, Ruff Pyflakes/security rules, and `git diff --check` pass.
- The Supervisor Bot is restarted once, remains the only polling process, and its protected log contains no credential or signed-URL pattern.

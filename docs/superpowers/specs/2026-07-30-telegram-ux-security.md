# AudioToTxt Telegram UX and Security Specification

## Context

AudioToTxt is primarily a private Telegram transcription bot. The current bot requires users to configure authentication and source type through commands, even though credentials are shared operational settings and the source can usually be inferred from the incoming message. A recent YouTube change also removed the original inline keyboards, leaving users to type internal values such as `gemini`, `video_url`, and `douyin`.

The review also found plaintext credentials in a world-readable state file, unbounded retained media, unauthenticated Web APIs, unsafe direct-URL downloads, and duplicated FastAPI implementations.

## Goals

1. Make the normal Telegram workflow: authorize once, then send media or a link.
2. Keep all operational settings global and manageable by every authorized user.
3. Support comma-separated Gemini API keys with health-aware round-robin selection.
4. Add a real queue, cancellation, retry, compact progress, and clean result delivery.
5. Protect credentials, media, and direct downloads with secure defaults.
6. Disable the Web UI by default and require a separate Web access key when enabled.
7. Consolidate local and Vercel Web entrypoints onto one tested implementation.

## Non-goals

- Public multi-tenant billing or per-user credentials.
- Telegram group-chat support.
- Automatic translation, summarization, or meeting-note generation by default.
- Resuming a partially completed Gemini request after process restart.
- Parallel execution by default; concurrency remains configurable.

## Access model

- Only private Telegram chats are accepted.
- IDs in `TG_ALLOWED_USER_IDS` bypass password entry.
- Other users may enter `ENV_BOT_SECRET` in a private chat.
- Successful password authorization persists until `ENV_BOT_SECRET` changes. The stored record contains a one-way fingerprint of the secret version, not the secret.
- Every authorized user may view and change global settings, including Gemini and Vertex credentials. No second password challenge is required.
- Password and credential messages are deleted immediately when Telegram permissions allow it.

## Global configuration

Global configuration is stored in a dedicated JSON file with atomic replacement and mode `0600`. Environment variables remain fallback inputs. Runtime configuration wins over environment fallback.

Global fields:

- authentication mode;
- Gemini API-key list;
- Vertex service-account JSON, project, and location;
- model;
- language hint;
- appended custom Prompt;
- optional advanced full Prompt override;
- configuration schema version and migration markers.

Existing per-user Gemini/Vertex/model/Prompt values are migrated once. All distinct legacy Gemini keys are merged into the new pool in most-recent-user order. Global scalar settings come from the most recently updated authorized user, while Vertex JSON/project/location migrate as one bundle from the most recently updated user with Vertex JSON. Sensitive legacy fields are scrubbed only after the new file is saved, reloaded, and verified. The authorization state file also becomes atomic and mode `0600`.

## Gemini multi-key pool

- Inputs accept comma-separated keys, trim whitespace, remove empty items, and deduplicate while preserving order.
- Settings provide explicit “replace all” and “append” operations.
- Each new Gemini operation selects the next healthy key in round-robin order.
- Authentication and permanent permission failures disable a key until the configuration changes.
- Rate limits, quota exhaustion, timeouts, and service failures place a key in a bounded cooldown.
- Invalid content and other deterministic request errors do not rotate keys.
- A task tries each eligible key at most once per attempt; it never loops forever.
- UI exposes only key count, masked values, and health state.
- Model listing uses a healthy key, caches the result, offers inline choices, and retains a manual model-name fallback.

## Telegram interaction

Bot command menu contains only:

- `/start` — home;
- `/settings` — global configuration;
- `/help` — concise usage help;
- `/cancel` — cancel the current or selected task.

`/start` and callback navigation use inline keyboards. The normal home card exposes current status, global settings, queue, and help. Settings expose Gemini keys, model, Prompt, language, and advanced Vertex settings.

Source selection is automatic:

- Telegram audio, voice, or audio documents become `audio`;
- YouTube domains become `youtube`;
- Douyin share text or Douyin domains become `douyin`;
- other public HTTP(S) URLs become `video_url`;
- ambiguous text receives an inline source-choice prompt instead of being guessed.

## Transcription output

- Default Prompt produces a faithful, readable transcript with reasonable paragraphing and basic punctuation.
- It does not summarize, explain, translate, or invent external information.
- Original language is detected automatically unless a global language hint is set.
- Clearly distinct speakers may be labeled `说话人 1`, `说话人 2`; names are not invented.
- Timestamps are used for unclear portions, not forced onto every paragraph.
- Custom Prompt text appends to the protected base rules by default. Full override remains an explicitly advanced operation.

The bot edits one status card through received, queued, parsing/downloading, transcribing, and terminal states. It does not stream the transcript into chat while generation is in progress.

On success:

- short transcripts are sent in chat and as a `.txt` document;
- long transcripts receive a short preview and `.txt` document;
- an inline “send full text” action sends long text in bounded chunks on demand.

Failures show a safe, categorized message with retry and settings actions. Raw exceptions are logged but not returned to users.

## Queue and lifecycle

- Jobs are FIFO and per-user submission order is preserved.
- Global concurrency defaults to `1` and is controlled by `TG_MAX_CONCURRENT_JOBS`.
- Queued jobs show their position and can be cancelled individually.
- Cancelling a non-interruptible provider call suppresses its eventual result and cleans its files; downloads use cooperative cancellation where possible.
- Transient failures retry once before surfacing failure. Key failover occurs within that attempt according to key-pool rules.
- Minimal job metadata is persisted. On restart, queued/running jobs become interrupted and are not automatically resumed; users receive a retry action when enough input remains.
- `MEDIA_TASK_TIMEOUT_SECONDS` bounds the user-visible end-to-end task. Provider requests receive a remaining-time timeout where supported; the async adapter also stops awaiting, marks the task timed out, suppresses late results, and releases/cleans resources when unavoidable blocking work returns.

## Data retention

- Successful and cancelled job media is removed when no longer needed.
- Sent transcript files are removed after Telegram delivery.
- Failed-job media may remain for retry for at most 24 hours.
- A bounded in-memory result cache retains successful transcript text for up to 24 hours solely for the “send full text” callback. It is capped by entry count and total characters, and loss on restart is acceptable.
- One-time migration removes historical Telegram uploads immediately and retains historical `.txt` output for seven days.
- Cleanup is recursive, bounded to configured data roots, and never follows symlinks outside them.
- `BOT_DATA_DIR` controls the complete Telegram data root so migrations and dry runs can never require hard-coded live paths. Optional legacy file-path overrides are accepted only when they resolve inside that root; an escaping override fails startup. Cleanup runs at bot startup and periodically while the bot is running.

## Media and URL security

- Only `http` and `https` direct URLs are accepted.
- Loopback, private, link-local, multicast, unspecified, and otherwise non-public resolved IPs are rejected.
- Every redirect target is validated.
- Connection/read timeout, redirect count, media byte limit, and task deadline are configurable.
- Downloads abort when streamed bytes exceed the limit even if `Content-Length` is absent or dishonest.
- Upload filenames are sanitized and cannot escape the data directory.

## Web behavior

- `WEB_ENABLED` defaults to false.
- `WEB_ENABLED=true` with an empty `WEB_ACCESS_KEY` fails closed: health reports misconfiguration and no operational route is available.
- Disabled deployments expose only a minimal health response and do not start Web jobs or embedded Telegram polling.
- Enabled deployments require a separate `WEB_ACCESS_KEY` and an HttpOnly, SameSite authentication cookie.
- API, WebSocket, file, listing, and cleanup routes all enforce the same authentication.
- The browser never persists Gemini/Vertex credentials in local storage.
- Web jobs use the same global settings, key pool, media policy, and transcription service as Telegram.
- Uploaded files are copied to owned storage before the request ends.
- Completed Web jobs and queues expire instead of accumulating indefinitely.
- `fastapi/app.py` and `api/index.py` become thin adapters over one shared app factory.

## Acceptance criteria

- An allowlisted user can send an audio file or supported link immediately after `/start`.
- A non-allowlisted user can authorize with the current password; changing the password invalidates that authorization.
- No normal workflow requires typing internal source/auth values.
- Two configured Gemini keys alternate across tasks; an unhealthy key is skipped and a healthy replacement succeeds.
- Global settings changes are visible to all authorized users and survive restart.
- Private credentials are never rendered in full and state/config files are mode `0600`.
- Multiple submissions queue, expose position, and can be cancelled/retried.
- The Web app rejects use when disabled and rejects unauthenticated use when enabled.
- Private-network URLs, oversized streams, and path-traversal filenames are rejected.
- Legacy credentials migrate once and historical media cleanup follows the agreed policy.
- Automated tests cover configuration, access, key rotation, source detection, URL policy, queue behavior, Telegram formatting/navigation, Web gating, and migration.

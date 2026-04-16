# Unified Communications Framework

## Context

Gobby currently supports web chat (WebSocket) and voice as communication channels, but users need to interact with Gobby through external platforms: Telegram, Slack, Discord, Teams, email, SMS. The goal is a single internal framework with pluggable channel adapters — configurable at install time with API keys — supporting both inbound (users message Gobby) and outbound (Gobby notifies users) flows. Inspired by OpenClaw's multi-channel gateway pattern: message normalization, session abstraction, rate limiting, platform-native threading.

This is a large feature. We'll build Phase 1 (core framework) in this session, then create detailed follow-up tasks for Phases 2-4.

---

## Architecture Decisions

1. **Channels are internal adapters, not MCP servers.** Tight daemon integration needed for real-time webhook handling and session bridging. MCP tools are a thin layer on top for agent access.

2. **Webhook routes on existing FastAPI server.** `/api/comms/webhooks/{channel_name}` via standard router pattern. No new process.

3. **Secrets in existing `secrets` table.** Channel API tokens stored via `SecretStore` with Fernet encryption, resolved at adapter init via `$secret:TELEGRAM_BOT_TOKEN` pattern.

4. **`CommunicationsManager` singleton on `ServiceContainer`.** Owns adapter lifecycle, message routing, inbound/outbound coordination. Same pattern as `pipeline_executor`, `memory_manager`, etc.

5. **Identity mapping table for session bridging.** External user IDs (Telegram user, Slack user, email address) map to `comms_identities` → linked to Gobby sessions. Purely additive — no changes to existing session model.

6. **Web chat stays as-is initially.** Phase 4 retrofits it as a channel adapter for full cross-channel bridging.

---

## File Structure

```
src/gobby/
├── communications/                     # NEW module
│   ├── __init__.py                     # Exports CommunicationsManager
│   ├── models.py                       # CommsMessage, ChannelConfig, ChannelCapabilities (~200 lines)
│   ├── manager.py                      # CommunicationsManager: lifecycle, routing, send/receive (~400 lines)
│   ├── router.py                       # Routing rule evaluation (~200 lines)
│   ├── rate_limiter.py                 # Token-bucket rate limiter per channel (~150 lines)
│   ├── polling.py                      # Background polling loop for non-webhook channels (~150 lines)
│   └── adapters/                       # Channel adapter implementations
│       ├── __init__.py                 # Adapter registry
│       ├── base.py                     # BaseChannelAdapter ABC (~150 lines)
│       ├── telegram.py                 # Phase 2
│       ├── slack.py                    # Phase 2
│       ├── discord.py                  # Phase 3
│       ├── teams.py                    # Phase 3
│       ├── email.py                    # Phase 3
│       └── sms.py                      # Phase 3
│
├── config/communications.py            # CommunicationsConfig Pydantic model
├── storage/communications.py           # DB CRUD for comms tables
├── servers/routes/communications.py    # Webhook receiver + channel management routes
├── mcp_proxy/tools/communications.py   # MCP tools: send_message, list_channels, etc.
```

**Files to modify:**
- `src/gobby/config/app.py` — add `CommunicationsConfig` import + field on `DaemonConfig`
- `src/gobby/app_context.py` — add `communications_manager: Any | None = None` to `ServiceContainer`
- `src/gobby/storage/migrations.py` — migration v155 with 4 tables (BASELINE_VERSION currently 154)
- `src/gobby/runner.py` — wire CommunicationsManager start/stop
- `src/gobby/servers/http.py` — register communications router
- `src/gobby/runner_broadcasting.py` — add comms event broadcasting

---

## Database Schema (Migration v155)

```sql
-- Channel configurations
CREATE TABLE comms_channels (
    id TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,            -- 'telegram', 'slack', 'discord', 'teams', 'email', 'sms'
    name TEXT NOT NULL UNIQUE,             -- Human-friendly: "my-telegram", "team-slack"
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}', -- Channel-specific non-secret config
    webhook_secret TEXT,                   -- For verifying inbound webhooks
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- External identity mapping for session bridging
CREATE TABLE comms_identities (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE,
    external_user_id TEXT NOT NULL,
    external_username TEXT,
    session_id TEXT,                        -- Current active Gobby session
    project_id TEXT,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel_id, external_user_id)
);

-- Message history
CREATE TABLE comms_messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE,
    identity_id TEXT REFERENCES comms_identities(id) ON DELETE SET NULL,
    direction TEXT NOT NULL,               -- 'inbound' or 'outbound'
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    platform_message_id TEXT,
    platform_thread_id TEXT,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'sent',   -- 'pending', 'sent', 'delivered', 'failed', 'rate_limited'
    error TEXT,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Routing rules: which events route to which channels
CREATE TABLE comms_routing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    channel_id TEXT NOT NULL REFERENCES comms_channels(id) ON DELETE CASCADE,
    event_pattern TEXT NOT NULL DEFAULT '*',  -- Glob: 'task.*', 'pipeline.approval_needed'
    project_id TEXT,
    session_id TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Plus indexes on channel_id, session_id, direction, created_at, enabled columns.

---

## Message Flow

**Outbound** (Gobby → platform):
```
Event (hook/pipeline/task) → CommunicationsManager.router
  → match routing rules → select channels
  → normalize to CommsMessage
  → rate_limiter check
  → adapter.send_message() → platform API
  → store in comms_messages + broadcast WS event
```

**Inbound** (platform → Gobby):
```
Platform webhook → POST /api/comms/webhooks/{channel_name}
  → verify signature → adapter.parse_webhook()
  → normalize to CommsMessage
  → resolve identity (comms_identities)
  → find/create Gobby session
  → route to session agent
  → agent response → outbound flow back to same channel
```

---

## Key Interfaces

**BaseChannelAdapter ABC** (`communications/adapters/base.py`):
- `channel_type: str` — 'telegram', 'slack', etc.
- `async initialize(config, secret_resolver)` — set up API clients, validate creds
- `async send_message(message: CommsMessage) -> str | None` — send, return platform message ID
- `async shutdown()` — clean close
- `capabilities() -> ChannelCapabilities` — threading, reactions, files, markdown, max length
- `parse_webhook(payload, headers) -> list[CommsMessage]` — normalize inbound
- `verify_webhook(payload, headers, secret) -> bool` — signature check
- `async poll() -> list[CommsMessage]` — for non-webhook channels (email IMAP)
- `max_message_length: int`, `supports_webhooks: bool`, `supports_polling: bool`

**CommunicationsConfig** (`config/communications.py`):
- `enabled: bool = False` — master switch
- `webhook_base_url: str = ""` — public URL for webhook registration (ngrok for local dev)
- `channel_defaults: ChannelDefaults` — rate_limit_per_minute (30), burst (5), retry_count (3), poll_interval (30s), retention_days (90)
- `inbound_enabled: bool = True`
- `outbound_enabled: bool = True`
- `auto_create_sessions: bool = True`

---

## Phase Breakdown

### Phase 1: Core Framework (this session)

**What:** Models, adapter ABC, manager, router, rate limiter, config, DB schema, storage CRUD, wiring into runner/ServiceContainer. No actual channel adapters yet — the framework boots, tables exist, interfaces are stable.

**Files to create:**
- `src/gobby/communications/__init__.py`
- `src/gobby/communications/models.py`
- `src/gobby/communications/adapters/__init__.py`
- `src/gobby/communications/adapters/base.py`
- `src/gobby/communications/manager.py`
- `src/gobby/communications/router.py`
- `src/gobby/communications/rate_limiter.py`
- `src/gobby/config/communications.py`
- `src/gobby/storage/communications.py`

**Files to modify:**
- `src/gobby/config/app.py`
- `src/gobby/app_context.py`
- `src/gobby/storage/migrations.py`
- `src/gobby/runner.py`

**Tests:** Unit tests for models, router rule matching, rate limiter token bucket, storage CRUD.

### Phase 2: Telegram + Slack + Routes + MCP Tools

**What:** First two channel adapters, webhook receiver routes, polling fallback, MCP tools for agent access, broadcasting integration.

**Prompt for future session:**
> Implement Telegram and Slack channel adapters for the Gobby communications framework (Phase 2). The core framework from Phase 1 is complete: `src/gobby/communications/` has models.py (CommsMessage, ChannelConfig, ChannelCapabilities), adapters/base.py (BaseChannelAdapter ABC), manager.py (CommunicationsManager), router.py, rate_limiter.py. Config is in `src/gobby/config/communications.py`, storage CRUD in `src/gobby/storage/communications.py`, DB tables (comms_channels, comms_identities, comms_messages, comms_routing_rules) exist via migration v155.
>
> Build these files:
> 1. `src/gobby/communications/adapters/telegram.py` — Telegram Bot API adapter. Use `httpx` for async HTTP (already a dependency). Implement: initialize (set webhook URL via setWebhook API), send_message (sendMessage with markdown support, chunk at 4096 chars), parse_webhook (Update object → CommsMessage), verify_webhook (token-based). Support reply_to_message_id for threading.
> 2. `src/gobby/communications/adapters/slack.py` — Slack Web API + Events API adapter. Use `httpx`. Implement: initialize (verify bot token via auth.test), send_message (chat.postMessage, chunk at 3000 chars for block kit), parse_webhook (Events API event → CommsMessage, handle url_verification challenge), verify_webhook (signing secret HMAC-SHA256). Support thread_ts for threading.
> 3. `src/gobby/communications/polling.py` — Background polling loop for Telegram getUpdates fallback (when webhook_base_url not set) and future IMAP polling. Asyncio task managed by CommunicationsManager.
> 4. `src/gobby/servers/routes/communications.py` — FastAPI router: POST /api/comms/webhooks/{channel_name} (receive webhooks), GET /api/comms/webhooks/{channel_name} (verification challenges), GET /api/comms/channels (list), POST /api/comms/channels (create), PUT /api/comms/channels/{id} (update), DELETE /api/comms/channels/{id}. Register in http.py.
> 5. `src/gobby/mcp_proxy/tools/communications.py` — MCP tools: send_message(channel, content, session_id?), list_channels(), get_messages(channel?, session_id?, limit?), add_channel(type, name, config), remove_channel(name). Register in mcp_proxy/server.py.
> 6. Wire broadcasting in `src/gobby/runner_broadcasting.py` — broadcast comms events (message_sent, message_received, channel_status) via WebSocket.
>
> API keys go in SecretStore: `$secret:TELEGRAM_BOT_TOKEN`, `$secret:SLACK_BOT_TOKEN`, `$secret:SLACK_SIGNING_SECRET`. Channel config_json holds non-secret settings (chat_id, channel name).
>
> Write tests for each adapter (mock httpx responses), webhook parsing, and MCP tools.

### Phase 3: Discord + Teams + Email + SMS

**Prompt for future session:**
> Implement Discord, Teams, Email, and SMS channel adapters for the Gobby communications framework (Phase 3). Phase 1 (core framework) and Phase 2 (Telegram, Slack, routes, MCP tools) are complete. See `src/gobby/communications/` for the existing code.
>
> Build these files:
> 1. `src/gobby/communications/adapters/discord.py` — Discord Bot adapter via Discord Gateway (websocket) for receiving + REST API for sending. Use `httpx` for REST, `websockets` for gateway. Implement: initialize (connect to gateway, register intents for MESSAGE_CREATE), send_message (POST /channels/{id}/messages, chunk at 2000 chars), parse_webhook (interaction/message event → CommsMessage). Support thread channels. Secret: `$secret:DISCORD_BOT_TOKEN`.
> 2. `src/gobby/communications/adapters/teams.py` — Microsoft Teams Bot Framework adapter. Use `httpx`. Implement: initialize (register bot via Bot Framework), send_message (POST activity to conversation), parse_webhook (Bot Framework activity → CommsMessage). Support adaptive cards for rich messages. Secrets: `$secret:TEAMS_APP_ID`, `$secret:TEAMS_APP_PASSWORD`.
> 3. `src/gobby/communications/adapters/email.py` — Email adapter with dual mode: SMTP for sending, IMAP for receiving (polling). Use `aiosmtplib` for send, `aioimaplib` for receive. Implement: initialize (connect SMTP + IMAP), send_message (compose MIME email), poll (check IMAP INBOX for new messages). Support HTML content_type. Secrets: `$secret:EMAIL_PASSWORD`. Config: smtp_host, smtp_port, imap_host, imap_port, from_address.
> 4. `src/gobby/communications/adapters/sms.py` — SMS via Twilio REST API. Use `httpx`. Implement: initialize (verify account SID + auth token), send_message (POST to Messages resource, chunk at 1600 chars), parse_webhook (Twilio webhook → CommsMessage). Secret: `$secret:TWILIO_AUTH_TOKEN`. Config: account_sid, from_number.
>
> Add `aiosmtplib` and `aioimaplib` as optional dependencies in pyproject.toml under `[project.optional-dependencies.email]`. Discord and Teams adapters should gracefully handle missing `websockets` package.
>
> Write tests for each adapter with mocked HTTP/IMAP responses.

### Phase 4: Advanced Features

**Prompt for future session:**
> Implement advanced communications features for Gobby (Phase 4). Phases 1-3 are complete — all 6 channel adapters work. See `src/gobby/communications/`.
>
> Build:
> 1. **Session bridging** — When an inbound message arrives and the identity has a linked session_id, resume that session. When a user sends from a new channel but matches an existing identity (same external_username pattern or explicit linking via MCP tool `link_identity`), bridge to the existing session. Add `link_identity(channel, external_user_id, session_id)` and `list_identities(session_id?)` MCP tools.
> 2. **Platform-native threading** — Track platform_thread_id per conversation. When replying to an inbound message, use the platform's threading mechanism (Slack thread_ts, Telegram reply_to_message_id, Discord thread channel, email In-Reply-To header). Add thread_id to CommsMessage routing.
> 3. **Reactions as actions** — Map emoji reactions to Gobby actions. Thumbs-up on a pipeline approval message → call pipeline approve API. Configurable reaction→action mapping in comms_routing_rules.config_json.
> 4. **File attachments** — Support sending/receiving files across channels. Store in ~/.gobby/comms_attachments/. Add attachment_url and attachment_type to CommsMessage metadata. Respect platform file size limits.
> 5. **Web chat as channel adapter** — Create `src/gobby/communications/adapters/web_chat.py` that wraps the existing WebSocket ChatMixin. Register it as channel_type="web_chat" so routing rules can target it. Enables cross-channel session continuity.
> 6. **Message cleanup** — Background task in runner_maintenance.py to prune comms_messages older than retention_days config. Run daily.
> 7. **CLI commands** — `gobby comms status` (show enabled channels + health), `gobby comms send <channel> <message>`, `gobby comms channels` (list/add/remove). Add `src/gobby/cli/communications.py`.

---

## Verification (Phase 1)

1. `uv run pytest tests/communications/ -v` — all unit tests pass
2. `uv run ruff check src/gobby/communications/ src/gobby/config/communications.py src/gobby/storage/communications.py`
3. `uv run mypy src/gobby/communications/`
4. Start daemon (`uv run gobby start --verbose`), verify no errors in logs related to communications
5. Verify DB tables exist: `sqlite3 ~/.gobby/gobby-hub.db ".tables" | grep comms`
6. Verify config accessible: check `communications` section in daemon config export

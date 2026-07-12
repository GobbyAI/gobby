# Telegram Channel: Parity+ with OpenClaw & Hermes

## Context

Goal: by the end of this plan, messaging Josh's Telegram bot is a full Gobby-agent conversation — the experience OpenClaw and Nous Hermes Agent deliver on Telegram — validated live. This effort also establishes the reusable pattern for the five other adapters (slack, discord, teams, email, sms) and future gap channels (WhatsApp, Signal, iMessage, Matrix, …).

Research established the parity bar (OpenClaw docs.openclaw.ai/channels/telegram; Hermes hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram). Both are self-hosted gateways: BYO bot token, per-chat persistent agent sessions, allowlist/pairing auth, media in/out with voice STT, mention-gated groups, typing indicators, streaming-by-edit, command menus.

Gobby today: comms framework enabled and polling-capable, but **nothing answers** — `comms.message_received` only broadcasts to the web UI (`runner_broadcasting.py:322-340`). Four defects block even manual two-way flow. The reusable agent-turn engine already exists: the `ChatSession` responder used by web chat and voice (`servers/websocket/chat/_streaming.py:68`, backends in `servers/websocket/chat/backends/` — claude/codex/droid/grok/qwen/acp).

**Decisions made:** responder = ChatSession reuse (no terminal spawns); scope = Tier 1 + streaming-by-edit + voice STT; tracking = parent epic + subtasks in gobby-tasks (no formal plan registration). Access = allowlist, Josh only (numeric ID captured from his first DM). Bot token: provided in-session, throwaway, revoked after.

## Definition of parity+ (acceptance bar)

**In scope (this plan):**
1. DM the bot → full Gobby agent replies; multi-turn context persists across messages and daemon restarts (comms session per sender; `identities.py:46`).
2. Allowlist gate: only configured Telegram user IDs get responses; others ignored + logged.
3. Groups: bot invitable; privacy-mode documented; mention-gated replies (`@botusername`, configurable); per-group sessions; per-sender identities preserved.
4. Media inbound: photos, documents, voice notes (downloaded via `getFile`, stored as attachments; voice transcribed via the existing faster-whisper STT stack from `servers/websocket/voice/`).
5. Media outbound: documents + photos.
6. Typing indicator (`sendChatAction`) during agent turns.
7. Streaming-by-edit: first chunk posted, then throttled `editMessageText` updates until final.
8. Formatting: Markdown → Telegram-safe rendering; 4096-char chunking.
9. Commands: `/new` (fresh session), `/reset`, `/stop` (abort turn), `/status`, `/help`.
10. Reliability: no duplicate processing across restarts; offset persisted; activation errors surfaced.

**The "+":** the agent behind the bot is the whole Gobby platform — tasks, memory, worktrees, multi-provider backends — which neither competitor fronts.

**Backlog (filed as tasks, not this plan):** pairing-code flow, reactions (read/send/ack), stickers, forum + private-chat topics, inline keyboards, TTS voice replies, Mini App dashboard, full command menu via `setMyCommands`, cron home-channel delivery, passive group observation, gap channels.

## Workstreams

Parent epic "Telegram comms parity" in gobby-tasks; one subtask per item below; each: claim → implement → scoped tests → commit → close. Execution order as listed.

### W1 — Foundation fixes (blockers found in code trace)
- **F1 Reply destination:** `TelegramAdapter.parse_webhook` (`telegram.py:222-281`) emits `conversation_reference={"conversation_id": <chat_id>}`; existing plumbing (`inbound.py:62-64`, `outbound.py:47-58`) then resolves session-scoped sends. Last-inbound-wins semantics (same as Teams).
- **F2 Restart duplicates:** generic dedup in `inbound.py:handle_messages` via existing `store.get_message_by_platform_id`; persist Telegram ack offset in channel `config_json`, restore in `initialize` (`telegram.py:38`).
- **F3 Non-text inbound:** parse photo/document/voice/video + captions; download via `getFile` into `communications/attachments.py` pipeline (`~/.gobby/comms_attachments/`).
- **F4 Send route + CLI:** add `POST /api/comms/send` (`servers/routes/communications.py`) wrapping `CommunicationsManager.send_message`; fix `gobby comms status` response parsing (`cli/communications.py:96-114`).
- **F5 Activation errors:** `add_channel` responses (MCP + HTTP) include `init_error` when adapter activation failed (`lifecycle.py:~192`).

### W2 — Responder core (the new capability)
- Fan out the single comms `event_callback` (`runner_broadcasting.py:316-340`) into broadcast + responder consumers.
- New `src/gobby/communications/responder.py` (keep <1000 lines; split if needed): consumes inbound messages for responder-enabled channels. Pipeline: access gate (allowlist / group policy / mention gate) → command router → per-conversation turn queue (serialize turns; concurrent senders don't interleave) → ChatSession turn → outbound delivery.
- **ChatSession transport shim:** extract the websocket-coupled streaming path (`_streaming.py:68`, `ChatStreamTransport`) behind a transport interface; add a comms transport that accumulates streamed text and drives channel delivery (streaming edits on Telegram; plain send fallback for adapters without edit support — that fallback is the cross-channel pattern).
- **Session keying:** DMs → existing per-sender comms session; groups → per-chat session (`external_id=comms:<channel_id>:group:<chat_id>`), extending `identities.py` resolution.
- **Channel config (Telegram, generic keys for reuse):** `responder: {enabled, provider, model}`, `allow_from: [<ids>]`, `group_policy: allowlist|open|disabled`, `require_mention: true`, `groups: {<chat_id>: {...}}`. Josh's ID into `allow_from` after first DM.

### W3 — Telegram adapter capabilities
- `send_typing()` via `sendChatAction`; `edit_message()` via `editMessageText` (throttled ~1.5s by the transport); `sendPhoto` support; Markdown→HTML conversion for outbound; verify 4096 chunking.
- Voice STT: transcribe inbound voice notes with the existing STT service (`servers/websocket/voice/`), transcript becomes the turn text (raw file still stored as attachment).

### W4 — Channel review/parity+ instructional doc (deliverable for future agents)
A **Gobby skill** — `src/gobby/install/shared/skills/channel-parity/SKILL.md` — so Josh can hand it to any future agent ("load channel-parity") when validating the other five channels or building gap channels. Written from what we actually did and learned on Telegram. Contents:
1. **Parity bar:** the OpenClaw/Hermes-derived feature checklist (inbound types, outbound incl. streaming/typing, groups + mention gating, auth gating, sessions, commands, media, reliability), with per-item "how to verify live".
2. **Per-channel workflow:** research the target adapter's competitor feature set (cite both products' channel docs) → code-trace the Gobby adapter for gaps (known gap archetypes: destination resolution, dedup/offsets, non-text parsing, activation-error surfacing) → bring-up + pre-responder sanity → wire responder (event fan-out is done once; per-channel work = capability methods + config) → capability implementation → live validation protocol → parity matrix update + backlog tasks.
3. **Code touchpoints:** responder pipeline (`communications/responder.py`), transport interface + plain-send fallback, adapter capability methods (`send_typing`/`edit_message`/media), generic channel config keys (`responder`, `allow_from`, `group_policy`, `require_mention`), session keying rules (DM per-sender, group per-chat), conversation_reference contract.
4. **Validation protocol template:** the live test script (bring-up → sanity → full parity pass → restart resilience) with expected evidence per step, plus test conventions (`GOBBY_TEST_PROTECT=1`, isolated daemon, scoped pytest).
5. **Reporting format:** parity matrix row updates + backlog task creation rules (labels, provenance) + memory records to write.
- **Parity matrix doc** `docs/reviews/channel-parity-matrix.md`: OpenClaw vs Hermes vs Gobby per feature per channel, done/backlog with task refs — the skill instructs agents to keep it current.
- Rewrite `docs/guides/comm-integrations.md` (stale "Last verified: 2026-05-07"; documents already-fixed limitations).

## Live validation (with Josh, throwaway bot)

1. **Bring-up:** `add_channel` (`channel_type=telegram`, `name=telegram`, `secrets={bot_token}`); verify `active`, `is_polling: true`, clean init.
2. **Pre-responder sanity (post-W1):** DM "ping" → stored + identity/session created; capture Josh's user ID → `allow_from`; session-scoped `send_message` reply arrives; CLI send works.
3. **Full parity pass (post-W3):** multi-turn conversation with real agent replies; typing indicator + streaming edits visible; `/new /reset /stop /status /help`; photo, document, voice note (transcribed) inbound; document + photo outbound; >4096-char reply chunks; second account or friend DM → silently ignored (allowlist); group: `/setprivacy` disabled, bot invited, unmentioned messages ignored, `@bot` mention answered in-group, per-group session confirmed; daemon restart mid-conversation → no duplicates, context resumes.
4. **Wrap:** file backlog tasks; record memories (responder architecture, channel config shape, parity matrix location); commit docs; Josh revokes the test token; pick next channel (suggest Discord).

## Verification

Each subtask lands scoped pytest (`GOBBY_TEST_PROTECT=1 uv run pytest tests/communications/ -v`, plus touched route/CLI test files; isolated test daemon state, never the user daemon, never the full suite). Responder gets unit coverage for gate/queue/commands/transport; adapter capabilities get fixture tests. Final acceptance is the live parity pass above — every numbered item observed working on Josh's phone.

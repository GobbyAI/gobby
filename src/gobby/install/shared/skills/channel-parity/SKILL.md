---
name: channel-parity
description: "Playbook for bringing a Gobby comms channel to parity+ with OpenClaw and Nous Hermes Agent. Covers the parity bar, per-channel workflow, code touchpoints, live validation protocol, and reporting."
version: "1.0.0"
category: comms
triggers: channel parity, comms validation, channel testing, parity matrix, telegram, slack, discord, teams, email, sms, whatsapp, signal, imessage
metadata:
  gobby:
    audience: all
---

# Channel Parity Playbook

You are validating (or building) a Gobby communications channel so that messaging it is a full Gobby-agent conversation, at parity+ with OpenClaw and Nous Hermes Agent. This skill is the pattern extracted from the Telegram parity effort (epic #17888, plan `.gobby/plans/telegram-parity.md`). Follow it start to finish for each channel.

**Parity+** means: the core assistant loop matches both competitors' channel experience, and the agent behind the channel is the whole Gobby platform (tasks, memory, worktrees, multi-provider backends) — which neither competitor fronts.

## The Parity Bar

Derived from OpenClaw (docs.openclaw.ai/channels/&lt;channel&gt;) and Hermes Agent (hermes-agent.nousresearch.com/docs/user-guide/messaging/&lt;channel&gt;). Every item needs live evidence, not just passing tests.

| # | Feature | Requirement | Verify live |
|---|---------|-------------|-------------|
| 1 | Agent replies | Inbound message → full Gobby agent turn → reply on the same channel | Send a question; agent answers in-channel |
| 2 | Multi-turn context | Session persists across messages and daemon restarts | Reference earlier message; restart daemon mid-conversation, context resumes |
| 3 | Access gate | `allow_from` allowlist enforced; unknown senders ignored + logged | Message from non-allowlisted account gets no reply; log line records the drop |
| 4 | Groups/rooms | Joinable; mention-gated replies (configurable); per-chat session; per-sender identities | Unmentioned message ignored; mention answered in-group; two members share the group session |
| 5 | Media inbound | Platform's message types ingest (photos, documents, voice — transcribed via STT) | Send each type; message + attachment rows exist; voice transcript reaches the agent |
| 6 | Media outbound | Documents + images sendable | Agent/tool sends a file and an image; both arrive |
| 7 | Presence | Typing indicator (or platform equivalent) during turns | Indicator visible while agent works |
| 8 | Streaming | Progressive delivery where the platform allows edits; single final send otherwise | Long reply visibly streams (edit-capable) or arrives once (fallback) |
| 9 | Formatting | Markdown → platform-safe rendering; length-limit chunking | Bold/code/links render; over-limit reply splits cleanly |
| 10 | Commands | `/new` `/reset` `/stop` `/status` `/help` | Each command works in-channel |
| 11 | Reliability | No duplicate processing across restarts; offsets/cursors persisted; activation errors surfaced | Restart produces no dupes; bad credentials produce a visible `init_error` on add |

Per-channel deltas (threads/topics, reactions, inline buttons, voice replies, pairing) belong on the backlog unless the channel plan says otherwise. Check both competitors' docs for the channel — their feature lists define the bar for what "parity" means there.

## Per-Channel Workflow

Work top to bottom. Create a per-channel epic in gobby-tasks mirroring the Telegram tree (#17888) before touching code.

### 1. Competitor research
Fetch both competitors' docs for this channel. Enumerate: inbound types, outbound capabilities (formatting, streaming, typing), group/room model, auth model, session model, commands, media limits, transport (webhook vs polling vs socket). Cite URLs in the epic description. Mark anything unverifiable as UNVERIFIED — never guess.

### 2. Gap code-trace
Read the Gobby adapter (`src/gobby/communications/adapters/<channel>.py`) and trace against the known gap archetypes found on Telegram:

- **Destination resolution** — does `parse_webhook` emit `conversation_reference={"conversation_id": <chat/room id>}`? Without it, `send_message(session_id=…)` cannot resolve a destination (`outbound.py` resolves metadata → channel `default_destination` → identity `conversation_reference`).
- **Duplicate delivery** — are offsets/cursors persisted? Does the store path rely on the generic `(channel_id, platform_message_id)` dedup in `inbound.py`?
- **Non-text parsing** — does `parse_webhook` drop media/captions?
- **Activation errors** — any adapter-specific init failure that `add_channel` would swallow (surfaced as `init_error` since F5)?
- **Fixed secret names** — does the adapter resolve channel-scoped `$secret:` refs from `config_json`, or hardcoded global names? (Slack/Teams/SMS historically hardcoded `SLACK_BOT_TOKEN` etc.)
- **Surface drift** — CLI/HTTP/MCP claims vs actual routes.

File one bug task per confirmed gap, parented to the channel epic, with file:line evidence and validation criteria.

### 3. Bring-up
Add the channel with secrets separated from config: `add_channel(channel_type, name, config, secrets={...})` (gobby-communications MCP). Secrets store encrypted as `COMMS_<TYPE>_<KEY>_<NAME>` with `$secret:` refs in config. Verify: `list_channels` shows `active` (+ `is_polling` where relevant), no `init_error`, daemon log confirms adapter init. Never proceed on a silently-failed activation.

### 4. Pre-responder sanity
Before wiring the responder: inbound message stores with correct metadata; identity + session auto-created (`external_id=comms:<channel_id>:<external_user_id>`); session-scoped outbound reply arrives; CLI `gobby comms send` works. This isolates transport problems from responder problems.

### 5. Responder wiring
The responder core (`src/gobby/communications/responder.py`), event fan-out, ChatSession transport, and command router are shared — built once during the Telegram effort. Per-channel work is only:
- **Capability methods** on the adapter: `send_typing()`, `edit_message()` (enables streaming-by-edit), image/file outbound, markdown conversion, chunk-limit constant.
- **Config**: `responder: {enabled, provider, model}`, `allow_from: [<ids>]`, `group_policy: allowlist|open|disabled`, `require_mention: true`, `groups: {<chat_id>: {...}}`.
- **Session keying**: DMs per-sender (default); groups per-chat (`external_id=comms:<channel_id>:group:<chat_id>`).
- **Voice**: route inbound audio through the shared STT service; transcript becomes the turn text, audio stays as attachment.

Adapters without message-edit support use the plain-send fallback automatically — do not fake streaming.

### 6. Live validation
Run the full protocol below with the channel owner. Every parity-bar row needs observed evidence.

### 7. Reporting
Update the matrix, file backlog tasks, record memories (see Reporting).

## Code Touchpoints

| Concern | Where |
|---------|-------|
| Adapter | `src/gobby/communications/adapters/<channel>.py` (base contract: `adapters/base.py`, incl. `platform_destination`) |
| Responder pipeline | `src/gobby/communications/responder.py` (gate → commands → turn queue → ChatSession → delivery) |
| Event fan-out | `runner_broadcasting.py` (`comms.message_received` → broadcast + responder) |
| Agent turns | ChatSession backends `servers/websocket/chat/backends/` via the comms transport (streaming-edit or plain-send fallback) |
| Destination resolution | `communications/outbound.py`; identity `conversation_reference` written by `inbound.py` |
| Sessions/identities | `communications/identities.py` |
| Dedup | `communications/inbound.py` store path |
| Attachments | `communications/attachments.py`, storage `~/.gobby/comms_attachments/` |
| STT | shared voice STT service (`servers/websocket/voice/`) |
| HTTP routes | `servers/routes/communications.py` (`/api/comms/...`, incl. `POST /api/comms/send`) |
| CLI | `cli/communications.py` (`gobby comms ...`) |
| MCP | `mcp_proxy/tools/communications.py` (gobby-communications registry) |
| Tests | `tests/communications/`, adapter fixtures under `tests/communications/adapters/` |

## Live Validation Protocol

Use a throwaway credential the owner revokes afterward. Do not restart the daemon before the reliability step — restarts are part of that test, not incidental.

1. **Bring-up**: channel `active`, polling/webhook confirmed, clean init.
2. **Sanity**: inbound stored; identity/session created; owner's platform ID captured from the first message's metadata → `allow_from`; session-scoped reply arrives; CLI send works.
3. **Parity pass**: walk parity-bar rows 1–10 in order, recording evidence for each (message IDs, screenshots, log lines, DB rows).
4. **Reliability**: restart the daemon mid-conversation → no duplicate ingestion, context resumes. Add a channel with bad credentials → `init_error` surfaced; remove it.
5. **Negative tests**: non-allowlisted sender ignored; unmentioned group message ignored under `require_mention`.

Test conventions: `GOBBY_TEST_PROTECT=1 uv run pytest tests/communications/ -v` scoped to touched files; isolated test daemon state only — never the user's running daemon; never the full suite.

## Reporting

- **Parity matrix** — update `docs/reviews/channel-parity-matrix.md`: one row per parity-bar feature per channel, status `done | planned(#task) | backlog(#task) | gap | n/a`, with the validation date. Keep competitor channel lists current when their docs change.
- **Backlog tasks** — file one task per deferred feature, parented to the channel epic, labeled `comms`, `parity`, `<channel>`. Gap channels (present in a competitor, absent in Gobby) get a `feature` task labeled `comms`, `parity`, `gap-channel`.
- **Memories** — record via gobby-memory: channel config shape and quirks, gaps found and their fixes, anything the next channel's agent would otherwise rediscover the hard way.
- **Docs** — if adapter behavior changed, update `docs/guides/comm-integrations.md` and bump its verified date.

## Competitor Reference (as of 2026-07)

- **OpenClaw**: docs.openclaw.ai/channels — Telegram, WhatsApp, Discord, Slack, Signal, iMessage, SMS (Twilio), Voice Call, Teams, Google Chat, Matrix, Mattermost, IRC, LINE, Feishu/Lark, Nextcloud Talk, Nostr, QQ, Synology Chat, Tlon, Twitch, Zalo, WebChat, and more via plugins.
- **Hermes Agent** (Nous Research): hermes-agent.nousresearch.com/docs/user-guide/messaging/ — Telegram, Discord, Slack, WhatsApp, Signal, SMS, Email, Teams, Google Chat, Matrix, Mattermost, IRC, Home Assistant, ntfy, LINE, BlueBubbles (iMessage), DingTalk, Feishu/Lark, WeCom, Weixin, QQ, Yuanbao, Raft.
- Signature behaviors both share: streaming-by-edit, typing indicators, voice STT in / TTS out, mention-gated groups, allowlist + pairing auth, per-chat persistent sessions, rich command menus.

Re-verify competitor docs at the start of each channel effort — both products move fast; a stale bar produces false parity claims.

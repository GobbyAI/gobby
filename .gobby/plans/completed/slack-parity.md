# Slack Channel: Parity+ with OpenClaw & Hermes

## Context

Goal: messaging Gobby's Slack app is a full Gobby-agent conversation — the experience OpenClaw and Nous Hermes Agent deliver on Slack — validated live. Second channel through the `channel-parity` skill; shared responder infrastructure is built under epic #17888 (Telegram) and is **not re-planned here**.

Competitor bar (fetched 2026-07-11): OpenClaw docs.openclaw.ai/channels/slack — Socket Mode default (also HTTP + relay modes), mrkdwn/Block Kit out, images+audio-STT+PDF in, ack/typing emoji reactions, streaming modes `off|partial|block|progress`, per-channel + per-thread (`:thread:<rootTs>`) sessions, `dmPolicy` pairing/allowlist, `requireMention` default in channels, ~25 commands. Hermes hermes-agent.nousresearch.com/docs/user-guide/messaging/slack — **Socket Mode only** ("your Hermes instance doesn't need to be publicly accessible"), `SLACK_ALLOWED_USERS` deny-by-default, mention-gated channels / mention-free DMs, thread-first replies, files+STT in, TTS audio out, assistant-status typing indicator, native slash commands with `!cmd` fallback in threads (Slack blocks `/` commands in thread replies).

Gobby today: Slack adapter is **webhook-only and therefore deaf in a local-first install** — `supports_polling` is False (`adapters/slack.py:57-59`), `webhook_base_url` defaults to `""` (`config/communications.py:22`), so no inbound path exists at all. Outbound works when a destination is supplied. Nothing answers on any channel until #17894/#17895 land.

**Decisions made (none open):**
- **Transport = Socket Mode** (see below). Events API webhook path already works (`servers/routes/communications.py:32-77` incl. url_verification; signature verify `adapters/slack.py:383-415`) and stays as an optional mode when `webhook_base_url` is set.
- Responder = shared ChatSession pipeline (#17894/#17895); per-channel work is capability methods + config only.
- Session keying: DM per-sender (existing); channel per-chat; **thread per-thread** (Slack threads are first-class; both competitors key thread sessions).
- Typing indicator: **ack reaction** (👀 via `reactions.add`) + early first message that streaming edits into. Stated honestly: Slack's Web API has no typing signal for bot messages; the Assistant-API status both competitors use applies only to assistant threads — backlog, not faked.
- Access: `allow_from` Slack user IDs (`U…`), Josh only, captured from first DM metadata. Pairing = backlog.
- Commands: text commands via the shared router; `!cmd` accepted as alias since Slack intercepts leading-`/` messages (Hermes documents the same workaround).

### Transport decision: Socket Mode (rationale)

1. **Local-first constraint is hard**: Gobby has no public URL by default; the webhook path requires one. A tunnel (ngrok/cloudflared) adds an external dependency, a public attack surface, and manual URL churn — for a daemon whose premise is local-first.
2. **It is the competitor-equivalent answer**: Socket Mode is OpenClaw's default and Hermes' only mode.
3. **Cheap to build**: `websockets>=15.0` already a dependency (`pyproject.toml:36`); protocol is `apps.connections.open` (xapp token) → WSS → ack `envelope_id` ≤3s → `event_callback` payloads identical in shape to the webhook events `parse_webhook` already handles. Hand-rolled with httpx+websockets, same way the Telegram adapter hand-rolls the Bot API — no slack-sdk dependency.
4. Requires: app-level token (`xapp-`, scope `connections:write`) alongside the bot token; **no signing secret needed** in socket mode (webhook-mode-only).

Mode selection: `app_token` present in channel config → socket mode; else webhook mode (requires `webhook_base_url`). Socket loop is an adapter-owned asyncio task (started in `initialize`, stopped in `shutdown`), pushing messages through the existing inbound callback (`adapters/base.py:144-156`); reconnect-with-backoff on `disconnect` frames and drops; connection state surfaced in `get_channel_status`.

## Verified current state (gcode trace, 2026-07-11)

**Confirmed gaps:**
- **G1 — No inbound transport locally** (above). `should_poll` (`inbound_mode.py:9-11`) can't help: adapter has no `poll()`.
- **G2 — No `conversation_reference` from inbound parse.** `parse_webhook` (`adapters/slack.py:289-381`) stores `platform_channel_id` in metadata but never `conversation_reference`; only Teams emits it (`adapters/teams.py:285`). Without it, session-scoped sends can't resolve a destination: `enrich_metadata` (`outbound.py:26-61`) finds nothing and `platform_destination` raises (`adapters/base.py:122-133`). Slack analog of Telegram F1 (#17889).
- **G3 — Non-text inbound dropped.** Parse requires `event_type=="message"`, `not subtype`, and non-empty `channel and text and user` (`adapters/slack.py:324-331`): drops `file_share` (every upload — images, PDFs, audio clips), file-only messages, and edits. No `url_private` download → attachments pipeline. Analog of #17891.
- **G4 — No `app_mention` handling.** Only `message` + `reaction_added` events parsed. Fix: parse `app_mention` too (a mentioned channel message fires *both* events with the same `ts` — generic dedup (#17890) collapses the pair). Bot user id for responder-side mention gating is already captured from `auth.test` (`adapters/slack.py:96`).
- **G5 — No dedup.** `handle_messages` (`inbound.py:25-96`) stores unconditionally. Rides #17890 (generic `(channel_id, platform_message_id)` dedup); Slack-specific additions: envelope ack in socket mode, `x-slack-retry-num` awareness test for webhook mode.
- **G6 — No capability methods.** No `send_typing`/`edit_message` on the adapter or base contract (outlines confirm). Slack mapping: `edit_message` → `chat.update` (throttled ~1.5s; Tier-3 ≈50/min); typing → ack reaction (decision above).
- **G7 — Inbound message id uses raw `ts`** (`adapters/slack.py:335`, fallback `slack_msg_{time.time()}`) — `ts` is only unique per Slack channel; use uuid4 and keep `ts` in `platform_message_id` (contract: `models.py:107` docstring).

**Stale suspects (verified fixed — matrix + guide corrections needed):**
- ~~"Adapter sends to `message.channel_id` (internal UUID)"~~ — all three send paths use `self.platform_destination(message)`: `_send_text` (`slack.py:124`), `_send_blocks` (`slack.py:162`), `send_attachment` (`slack.py:249`). Only G2 (emission side) remains. `docs/guides/comm-integrations.md:189` still documents the old behavior → #17902.
- ~~"Fixed global secret names `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`"~~ — `initialize` resolves channel-scoped refs from `config_json` (`slack.py:66-72`); `add_channel` writes `$secret:COMMS_SLACK_<KEY>_<NAME>` (`lifecycle.py:162-176`); `init_adapter` resolves all `$secret:` refs (`lifecycle.py:95-108`); no slack entry in `_DEFAULT_CHANNEL_SECRET_REFS` (`lifecycle.py:19-22`). Residual: error strings still *name* the old globals (`slack.py:76,80`) — cosmetic fix in W1. Guide lines 159-170 stale → #17902.

**Already working (parity credits):** reaction inbound (`slack.py:352-381`); thread plumbing (`thread_ts`→`platform_thread_id` in `slack.py:337`, outbound `thread_ts` in `slack.py:131-132`, session→thread tracking `inbound.py:78-80`); Block Kit + mrkdwn outbound (`slack.py:146-204`); 3-step external file upload (`slack.py:206-271`); chunking (`base.py:268-301`, limit 3000 `slack.py:47-49` — kept: under both the 3000 block-text and 4000 visible-message limits); webhook signature verify with replay guard, falling back to `self._signing_secret` (`slack.py:383-415`).

## Definition of parity+ (acceptance bar)

**In scope:**
1. DM the app → full Gobby agent replies; multi-turn context persists across messages and daemon restarts (per-sender comms session, `identities.py:46-115`).
2. Allowlist gate: only `allow_from` Slack user IDs answered; others ignored + logged.
3. Channels: app invitable; replies only when `<@bot>` mentioned (`require_mention`, configurable); replies land in the message's thread (thread-first, like both competitors); per-channel session; per-sender identities preserved.
4. Threads: per-thread sessions (`comms:<channel_id>:group:<C…>:thread:<thread_ts>`), continuing without re-mention once active (Hermes behavior; OpenClaw `:thread:` keying).
5. Media inbound: images, files, audio clips via `url_private_download` + bot token → attachments pipeline; audio transcribed via the shared STT stack; transcript becomes turn text.
6. Media outbound: files + images (existing upload path, validated live).
7. Presence: ack reaction (👀) on accepted messages + fast first message that streams.
8. Streaming-by-edit: first chunk posted, throttled `chat.update` edits until final; chunk overflow rolls to a follow-up message.
9. Formatting: Markdown → mrkdwn conversion (bold/italic/links/code); 3000-char chunking.
10. Commands: `/new /reset /stop /status /help` with `!cmd` alias (Slack intercepts leading `/` in composer; `!cmd` also works in threads where Slack blocks slash commands).
11. Reliability: socket reconnect w/ backoff; no duplicate processing across restarts/redeliveries (#17890 + envelope ack); `init_error` surfaced on bad credentials (#17893).

**The "+":** the agent behind the app is the whole Gobby platform — tasks, memory, worktrees, multi-provider backends — which neither competitor fronts.

**Backlog (file as tasks at wrap-up, not this plan):** native slash-command registration; Block Kit interactive buttons/selects; assistant-threads + `assistant.threads.setStatus` native typing; TTS voice replies (Hermes has); pairing-code flow; multi-workspace; chart/table rich blocks (OpenClaw has); custom username/icon; home-channel cron delivery; message-edit/delete event ingestion; Enterprise Grid concerns.

## Workstreams

One subtask per item under a "Slack comms parity" epic (created after plan review); each: claim → implement → scoped tests → commit → close.

### W1 — Slack foundation fixes
- **S1 Socket Mode transport** (the Slack-specific build): adapter-owned WSS loop per "Transport decision" above; envelope ack; reconnect; mode selection via `app_token`; status in `get_channel_status`. Reuses `parse_webhook`'s event parsing for envelope payloads.
- **S2 Inbound parse contract**: emit `conversation_reference={"conversation_id": <channel>}` (pattern: `teams.py:285`; fix class: #17889); parse `app_mention` events; uuid4 message ids (G7); DM/channel type (`channel_type` field im|mpim|channel|group) into metadata for responder gating; correct misleading secret-error strings (`slack.py:76,80`).
- **S3 Non-text inbound**: `file_share` subtype + `files` array → download `url_private_download` (bot-token auth) → `attachments.py` pipeline; file-only messages no longer dropped; captions preserved. `depends_on` #17890 lands first for safe redelivery.

### W2 — Responder integration (shared infra, `depends_on` explicitly)
- **S4 Slack responder wiring** — `depends_on: #17894 (responder pipeline), #17895 (ChatSession transport), #17896 (per-chat group sessions)`. Slack-specific: mention gate via `<@bot_user_id>` text scan (id from `slack.py:96`) honoring `require_mention`; thread-first reply targeting (reply `thread_ts` = inbound thread or inbound `ts` in channels); per-thread session keying extension (`comms:<channel_id>:group:<C…>:thread:<thread_ts>`) on the #17896 mechanism; `!cmd` command alias in the shared router; config keys are the #17894 generics (`responder{enabled,provider,model}`, `allow_from`, `group_policy`, `require_mention`, `groups{}`) — no new key names.

### W3 — Slack adapter capabilities
- **S5 Capability methods** — `depends_on: #17895` (transport interface), pattern from #17897: `edit_message()` via `chat.update` (transport throttles ~1.5s); ack-reaction presence via `reactions.add`; Markdown→mrkdwn conversion for outbound; image outbound validated via existing upload path.
- **S6 Voice STT inbound** — `depends_on: #17898` (shared STT service usage pattern): Slack audio clips (audio/mp4, m4a, webm) through the same transcribe-then-turn flow; raw file kept as attachment.

### W4 — Docs
- Matrix Slack statuses flip from `planned(pending-epic)` to `planned(#task)` when the epic exists; validated rows → `done` after live pass.
- `docs/guides/comm-integrations.md` Slack rows (lines 33, 159-170, 189, 263) corrected under existing #17902.

## Live validation (with Josh, throwaway Slack app)

Prereqs Josh provides: a workspace he controls; create app from the manifest below; **bot token `xoxb-…` + app token `xapp-…`** supplied in-session and revoked after; his Slack user ID auto-captured from first DM.

App manifest (paste into api.slack.com/apps → Create from manifest):
```yaml
display_information: {name: gobby-dev}
features:
  bot_user: {display_name: gobby, always_online: true}
  app_home: {messages_tab_enabled: true, messages_tab_read_only_enabled: false}
oauth_config:
  scopes:
    bot: [chat:write, im:history, mpim:history, channels:history, groups:history,
          app_mentions:read, files:read, files:write, reactions:read, reactions:write, users:read]
settings:
  socket_mode_enabled: true
  event_subscriptions:
    bot_events: [message.im, message.mpim, message.channels, message.groups,
                 app_mention, reaction_added]
```

1. **Bring-up**: `add_channel(channel_type="slack", name="slack", config={}, secrets={bot_token, app_token})`; verify `active`, socket connected, clean init; bad-token add → `init_error` surfaced, removed.
2. **Pre-responder sanity (post-W1)**: DM "ping" → stored with `conversation_reference`; identity + session created; Josh's `U…` id captured → `allow_from`; session-scoped reply arrives; CLI send works.
3. **Full parity pass (post-W3)**: walk acceptance bar 1-11 in order — multi-turn DM; 👀 ack + streaming edits visible; `!new !reset !stop !status !help` (+ `/cmd` in DM composer where deliverable); image, PDF, audio clip inbound (transcript reaches agent); file + image outbound; >3000-char reply chunks; second account DM ignored + logged; channel: app invited, unmentioned message ignored, `@gobby` answered in-thread, thread continues without re-mention, per-channel + per-thread sessions confirmed; daemon restart mid-conversation → no duplicates, context resumes; socket reconnect after network blip.
4. **Wrap**: file backlog tasks; update matrix (`done` rows + validation date); record memories; Josh revokes both tokens.

## Verification

Each subtask lands scoped pytest (`GOBBY_TEST_PROTECT=1 uv run pytest tests/communications/ -v` — adapter fixtures in `tests/communications/adapters/test_slack.py` exist; add socket-mode envelope fixtures, parse-contract tests incl. app_mention/message dedup pair, file_share parsing, mrkdwn conversion, chat.update throttle). Isolated test daemon only; never the full suite. Final acceptance = the live parity pass, every numbered item observed in Slack.

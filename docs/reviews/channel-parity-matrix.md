# Channel Parity Matrix — OpenClaw vs Hermes Agent vs Gobby

Maintained by agents running the `channel-parity` skill; update rows whenever a channel effort lands or competitor docs change. Status values: `done` (validated live), `planned(#task)`, `backlog(#task)`, `gap`, `n/a`.

Competitor sources (fetched 2026-07-11): OpenClaw docs.openclaw.ai/channels; Hermes Agent hermes-agent.nousresearch.com/docs/user-guide/messaging/. Unverified competitor claims are marked UNVERIFIED in the research notes attached to epic #17888.

## Channel Coverage

| Channel | OpenClaw | Hermes | Gobby |
|---------|----------|--------|-------|
| Telegram | ✅ | ✅ | core live parity complete: epic #17888, live pass #17903, and operator checks #18867/#18868 completed |
| Slack | ✅ (Socket Mode default; also HTTP + relay) | ✅ (Socket Mode only) | adapter shipped; parity plan authored (`.gobby/plans/slack-parity.md`), epic pending review |
| Discord | ✅ (plugin) | ✅ | adapter shipped; untested |
| Teams | ✅ (plugin) | ✅ | adapter shipped; untested |
| Email | assistant capability (channel UNVERIFIED) | ✅ | adapter shipped; untested |
| SMS (Twilio) | ✅ (plugin) | ✅ | adapter shipped; untested |
| Web chat | ✅ (WebChat) | n/a (TUI/desktop) | ✅ `gobby_chat` |
| WhatsApp | ✅ (plugin, Baileys) | ✅ | gap |
| Signal | ✅ (plugin, signal-cli) | ✅ | gap |
| iMessage | ✅ (native macOS) | ✅ (BlueBubbles) | gap |
| Matrix | ✅ (plugin) | ✅ | gap |
| Google Chat | ✅ (plugin) | ✅ | gap |
| Mattermost | ✅ (plugin) | ✅ | gap |
| IRC | ✅ (plugin) | ✅ | gap |
| LINE | ✅ (plugin) | ✅ | gap |
| Voice call (telephony) | ✅ (Plivo/Telnyx/Twilio) | — | gap |
| Home Assistant / ntfy | — | ✅ | gap |
| Feishu/Lark, WeCom, Weixin, QQ, DingTalk, Yuanbao | ✅ (several) | ✅ | gap |
| Nostr, Tlon, Twitch, Zalo, Synology, Nextcloud Talk | ✅ (plugins) | — | gap |

Gap-channel backlog tasks are filed per the `channel-parity` skill reporting rules when prioritized.

## Telegram (epic #17888, plan `.gobby/plans/telegram-parity.md`)

Operator configuration and current limits are maintained in the
[Telegram guide](../guides/telegram.md).

| # | Feature | OpenClaw | Hermes | Gobby status |
|---|---------|----------|--------|--------------|
| 1 | Agent replies (inbound → agent turn → reply) | ✅ | ✅ | done (#17888; live #17903) |
| 2 | Multi-turn persistent sessions | ✅ per-chat | ✅ per-chat/thread, survives restarts | done; live restart and context recall #17903 |
| 3 | Access gate | ✅ `allowFrom`, dmPolicy default pairing | ✅ `TELEGRAM_ALLOWED_USERS`, pairing | done (#17894, #18867): numeric allowlist; first exact private `/start` atomically binds an empty allowlist to its sender; other senders are silently denied before persistence |
| 4 | Groups: mention gate, per-chat session, passive context | ✅ requireMention, groupPolicy | ✅ require_mention, wake words, guest mode | done (#17894, #17896, #18859); owner-only and authorized-user live checks completed #18868 |
| 5 | Media inbound (photo/doc/voice STT/video/captions) | ✅ incl. sticker vision | ✅ incl. STT (whisper) | done (#17891, #17898, #18852); live photo, document, voice storage, and transcription #17903; stickers feed vision with metadata fallback |
| 6 | Media outbound (documents, photos, voice) | ✅ + voice notes, stickers | ✅ + TTS voice bubbles | done (#18855); photo and document live #17903; optional TTS sends Ogg Opus voice notes with text fallback |
| 7 | Typing indicator | ✅ incl. forum topics | ✅ | done (#17897); Bot API call passed #17903, with fast replies sometimes too brief to observe visually |
| 8 | Streaming-by-edit | ✅ off/partial/block/progress | ✅ auto/draft/edit/off | done (live #17903; final persistence #18843; placeholder #18846) |
| 9 | Formatting + chunking | ✅ HTML, 4000-char chunks | ✅ Bot API rich messages, MarkdownV2 fallback | done; live send beyond 4,096 characters #17903 |
| 10 | Commands and menu | ~55 cross-channel + menu | ~60 auto-registered + menu | done (#18857): `/new /reset /stop /status /help`; commands live #17903 and synchronized with `setMyCommands` |
| 11 | Reliability (dedup, offset persistence, init errors) | ✅ durable ingress queue | ✅ sessions auto-resume | done (live restart, bad credentials, idle polling #17903; #17890, #17893, #18842, #18844) |
| — | Reactions | ✅ | ✅ | done (#18851): inbound reaction normalization plus standard emoji set/clear |
| — | Forum and private topics | ✅ | ✅ threads | done (#18853): `message_thread_id` preservation, topic sessions, and topic replies |
| — | Inline keyboards | ✅ | ✅ | done (#18854): opaque single-use callbacks scoped to chat/topic/session |
| — | Scheduled home-channel delivery | ✅ | ✅ | done (#18858): cron/routing delivery through `default_destination` with idempotent retry handling |
| — | Link-preview control | ✅ | UNVERIFIED | done (#18860): channel defaults plus per-message overrides retained through edits |
| — | Proxy support | ✅ | ✅ | done (#18861): HTTP, SOCKS5, and SOCKS5H across all Bot API requests |
| — | Reply destination resolution | ✅ | ✅ | done (live #17903; #17889) |
| — | HTTP/CLI/MCP send surfaces | n/a | n/a | done (#17892, #18845); MCP also ships `send_attachment` and `set_channel_project` |
| — | Live validation pass | — | — | complete: core #17903, unknown-DM denial #18867, and group authorization #18868 |

Telegram responders now start with the restricted `comms-agent` persona in normal mode, default to the Personal project, and support a persistent per-channel project selection through `gobby-communications:set_channel_project`.

Completion record: #17888, #17903, #18867, and #18868 are complete. The
post-epic Telegram capability tasks #18850–#18861 have shipped or been
superseded by the documented first-owner design. The only deferred Telegram
surface is the Mini App dashboard (#18498).

## Slack (plan `.gobby/plans/slack-parity.md`, epic pending review)

Competitor sources (fetched 2026-07-11): OpenClaw docs.openclaw.ai/channels/slack, docs.openclaw.ai/gateway/config-channels, docs.openclaw.ai/channels/pairing, docs.openclaw.ai/concepts/session; Hermes hermes-agent.nousresearch.com/docs/user-guide/messaging/slack, …/docs/user-guide/messaging/, …/docs/reference/slash-commands.

| # | Feature | OpenClaw | Hermes | Gobby status |
|---|---------|----------|--------|--------------|
| — | Transport (local-first) | ✅ Socket Mode default; also HTTP Events + relay | ✅ Socket Mode only | planned(pending-epic): Socket Mode; webhook path exists but needs public URL (`config/communications.py:22` empty default) |
| 1 | Agent replies | ✅ | ✅ | planned(pending-epic); depends #17894/#17895 |
| 2 | Multi-turn persistent sessions | ✅ per-channel + `:thread:<ts>`; DMs → main session | ✅ per-chat; per-user in shared channels; survive restarts | planned(pending-epic): DM per-sender, channel per-chat, thread per-thread |
| 3 | Access gate | ✅ `dmPolicy` pairing default, `allowFrom` U-ids | ✅ `SLACK_ALLOWED_USERS`, deny-by-default, pairing | planned(pending-epic): `allow_from` owner-only; pairing backlog |
| 4 | Groups: mention-gated, per-chat session | ✅ `requireMention` default, `groupPolicy`, mentionPatterns | ✅ `require_mention`, `strict_mention`, free-response channels | planned(pending-epic); no `app_mention` parsing today (`slack.py:289-381`) |
| 5 | Media inbound (files/images/audio STT) | ✅ images→vision, audio→STT, PDFs, 20MB/8-file caps | ✅ files+images, STT default on | planned(pending-epic); today all uploads dropped (`slack.py:324-331`) |
| 6 | Media outbound | ✅ files, charts/tables, buttons | ✅ files, TTS audio bubbles | partial: file upload works (`slack.py:206-271`); images validate, TTS backlog |
| 7 | Working indicator | ✅ ack+typing reactions; assistant-thread status | ✅ assistant status ("briefly disables compose box"), heartbeats | planned(pending-epic): ack reaction 👀 + early-post; native assistant status backlog (no bot typing API — honest fallback) |
| 8 | Streaming | ✅ `off/partial/block/progress`, native transport | ✅ (edit-in-place tool progress; token-level UNVERIFIED) | planned(pending-epic): streaming-by-edit `chat.update` ~1.5s throttle |
| 9 | Formatting + chunking | ✅ mrkdwn+Block Kit; chunk default 8000 (config ref says 4000 — UNVERIFIED) | ✅ mrkdwn; Block Kit opt-in; limits undocumented | partial: blocks/mrkdwn sends exist (`slack.py:146-204`), 3000 chunking (`slack.py:47-49`); md→mrkdwn conversion planned |
| 10 | Commands | ~25 via native or `/openclaw` | ~19 native + `!cmd` in threads | planned(pending-epic): `/new /reset /stop /status /help` + `!cmd` alias; native registration backlog |
| 11 | Reliability | ✅ socket default, LB-able HTTP mode | ✅ socket-only, restart auto-resume | planned(pending-epic): reconnect+envelope ack; dedup #17890; init_error #17893 |
| — | Reply destination resolution | ✅ | ✅ | gap today: no `conversation_reference` emitted (`slack.py:289-381`; only Teams does, `teams.py:285`); planned(pending-epic) |
| — | Reactions inbound | ✅ | ✅ (table; specifics UNVERIFIED) | ✅ parsed today (`slack.py:352-381`) |
| — | Live validation pass | — | — | planned(pending-epic) |

**Slack backlog (post-epic):** native slash commands, interactive Block Kit, assistant threads + native status, TTS replies, pairing flow, multi-workspace, charts/tables, custom username/icon, home-channel cron delivery, edit/delete event ingestion, Enterprise Grid.

**UNVERIFIED (research notes):** OpenClaw textChunkLimit default (8000 vs 4000 conflict); OpenClaw explicit restart-persistence statement; Hermes token-level answer streaming on Slack; Hermes chunking + media size limits; Hermes reaction-handling specifics.

## Discord / Teams / Email / SMS

To be filled by their channel-parity efforts (run the `channel-parity` skill per channel; suggested order: Discord next). Known pre-existing findings to re-verify during each gap code-trace:

- ~~Slack~~/Teams/SMS adapters resolve fixed global secret names — **re-verified for Slack 2026-07-11: stale.** Slack resolves channel-scoped `$secret:` refs (`adapters/slack.py:66-72`, `lifecycle.py:95-108,162-176`); only misleading error strings remain (`slack.py:76,80`). Teams/SMS still to re-verify.
- Discord outbound uses `platform_thread_id`/`channel_id` rather than the `conversation_reference` contract.
- Email has the clearest generic outbound path (`default_destination`) but no webhook inbound (IMAP polling only).
- SMS signature verification requires the exact webhook URL.

## Behavioral parity notes (cross-channel)

- Both competitors auto-respond by default; Gobby's responder (#17894/#17895) is the equivalent and is shared across channels — per-channel work is capability methods + config only.
- Both support voice in (STT) broadly; Gobby Telegram also ships optional TTS voice-note replies with text fallback.
- Both competitors offer pairing flows. Gobby Telegram uses numeric allowlists plus exact private `/start` first-owner binding; it has no pairing-code flow.
- Neither competitor fronts a task system, persistent memory, worktree isolation, or multi-provider CLI agents — that is Gobby's "+".

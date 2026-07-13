# Channel Parity Matrix — OpenClaw vs Hermes Agent vs Gobby

Maintained by agents running the `channel-parity` skill; update rows whenever a channel effort lands or competitor docs change. Status values: `done` (validated live), `planned(#task)`, `backlog(#task)`, `gap`, `n/a`.

Competitor sources (fetched 2026-07-11): OpenClaw docs.openclaw.ai/channels; Hermes Agent hermes-agent.nousresearch.com/docs/user-guide/messaging/. Unverified competitor claims are marked UNVERIFIED in the research notes attached to epic #17888.

## Channel Coverage

| Channel | OpenClaw | Hermes | Gobby |
|---------|----------|--------|-------|
| Telegram | ✅ | ✅ | adapter shipped; parity effort in flight (#17888) |
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

| # | Feature | OpenClaw | Hermes | Gobby status |
|---|---------|----------|--------|--------------|
| 1 | Agent replies (inbound → agent turn → reply) | ✅ | ✅ | planned(#17894, #17895) |
| 2 | Multi-turn persistent sessions | ✅ per-chat | ✅ per-chat/thread, survives restarts | planned(#17895); per-sender comms sessions exist today |
| 3 | Access gate (allowlist) | ✅ `allowFrom`, dmPolicy default pairing | ✅ `TELEGRAM_ALLOWED_USERS`, pairing | planned(#17894) |
| 4 | Groups: mention-gated, per-chat session | ✅ requireMention, groupPolicy | ✅ require_mention, wake words, guest mode | planned(#17894, #17896) |
| 5 | Media inbound (photo/doc/voice STT/video/captions) | ✅ incl. sticker vision | ✅ incl. STT (whisper) | planned(#17891, #17898); today non-text is dropped |
| 6 | Media outbound (documents, photos) | ✅ + voice notes, stickers | ✅ + TTS voice bubbles | planned(#17897); documents work today |
| 7 | Typing indicator | ✅ incl. forum topics | ✅ | planned(#17897) |
| 8 | Streaming-by-edit | ✅ off/partial/block/progress | ✅ auto/draft/edit/off | planned(#17895, #17897) |
| 9 | Formatting + chunking | ✅ HTML, 4000-char chunks | ✅ Bot API rich messages, MarkdownV2 fallback | planned(#17897); chunking exists |
| 10 | Commands | ~55 cross-channel + menu | ~60 auto-registered + menu | planned(#17894): `/new /reset /stop /status /help`; full menu backlog |
| 11 | Reliability (dedup, offset persistence, init errors) | ✅ durable ingress queue | ✅ sessions auto-resume | planned(#17890, #17893) |
| — | Reply destination resolution (session-scoped sends) | ✅ | ✅ | planned(#17889); broken today |
| — | HTTP/CLI send surface | n/a | n/a | planned(#17892) |
| — | Live validation pass | — | — | planned(#17903) |

**Telegram backlog (post-#17888, tasks to be filed at wrap-up per #17903):** pairing-code flow, reactions (read/send/ack emoji), stickers (incl. vision description), forum topics + private-chat topics multi-session, inline keyboards/clarify buttons, TTS voice replies, Mini App dashboard, full command menu via `setMyCommands`, cron home-channel delivery, passive group observation, link-preview control, proxy support.

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
- Both support voice in (STT) broadly; TTS out is backlog for Gobby.
- Both offer pairing flows in addition to allowlists; Gobby ships allowlist first, pairing backlog.
- Neither competitor fronts a task system, persistent memory, worktree isolation, or multi-provider CLI agents — that is Gobby's "+".

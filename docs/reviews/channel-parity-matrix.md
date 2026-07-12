# Channel Parity Matrix — OpenClaw vs Hermes Agent vs Gobby

Maintained by agents running the `channel-parity` skill; update rows whenever a channel effort lands or competitor docs change. Status values: `done` (validated live), `planned(#task)`, `backlog(#task)`, `gap`, `n/a`.

Competitor sources (fetched 2026-07-11): OpenClaw docs.openclaw.ai/channels; Hermes Agent hermes-agent.nousresearch.com/docs/user-guide/messaging/. Unverified competitor claims are marked UNVERIFIED in the research notes attached to epic #17888.

## Channel Coverage

| Channel | OpenClaw | Hermes | Gobby |
|---------|----------|--------|-------|
| Telegram | ✅ | ✅ | adapter shipped; parity effort in flight (#17888) |
| Slack | ✅ (plugin) | ✅ | adapter shipped; untested |
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

## Slack / Discord / Teams / Email / SMS

To be filled by their channel-parity efforts (run the `channel-parity` skill per channel; suggested order: Discord next). Known pre-existing findings to re-verify during each gap code-trace:

- Slack/Teams/SMS adapters resolve fixed global secret names rather than channel-scoped `$secret:` refs.
- Discord outbound uses `platform_thread_id`/`channel_id` rather than the `conversation_reference` contract.
- Email has the clearest generic outbound path (`default_destination`) but no webhook inbound (IMAP polling only).
- SMS signature verification requires the exact webhook URL.

## Behavioral parity notes (cross-channel)

- Both competitors auto-respond by default; Gobby's responder (#17894/#17895) is the equivalent and is shared across channels — per-channel work is capability methods + config only.
- Both support voice in (STT) broadly; TTS out is backlog for Gobby.
- Both offer pairing flows in addition to allowlists; Gobby ships allowlist first, pairing backlog.
- Neither competitor fronts a task system, persistent memory, worktree isolation, or multi-provider CLI agents — that is Gobby's "+".

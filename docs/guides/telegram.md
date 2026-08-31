# Telegram

This guide is the operator reference for running a Telegram bot through Gobby.
It covers secure setup, direct-message and group authorization, transport,
responder behavior, shipped capabilities, limits, and recovery.

For cross-channel architecture and generic communications APIs, see
[Communications Integrations](comm-integrations.md). Telegram platform behavior
is documented by Telegram's official [bot features guide][telegram-features]
and [bot FAQ][telegram-faq].

## Quick path: a private DM bot

This path produces a polling bot that enrolls its owner only after receiving
the exact private `/start` command.

### 1. Enable communications

Set communications in the daemon configuration and restart Gobby:

```yaml
communications:
  enabled: true
  webhook_base_url: ""
  auto_create_sessions: true
```

An empty `webhook_base_url` selects Telegram polling. The standard WebSocket
server must also be enabled because it supplies the persistent `ChatSession`
runtime used by the responder.

### 2. Create and protect the bot token

Open a private chat with [@BotFather](https://t.me/BotFather), run `/newbot`,
and follow the prompts. Treat the returned token as a password: anyone holding
it controls the bot.

- Enter the token only at Gobby's hidden CLI prompt or through the channel
  `secrets` object.
- Keep it out of shell history, configuration files, chat, screenshots, and
  version control.
- If it is exposed, use BotFather's `/token` command to generate a replacement,
  update the channel secret, and verify that the adapter becomes active again.

### 3. Create the channel

```bash
gobby comms channels add telegram personal-telegram
```

Enter the Bot Token at the hidden prompt. Leave Chat ID empty for an inbound
DM-only setup; supplying it stores a `default_destination` for proactive sends.

Verify startup:

```bash
gobby comms status
gobby comms channels list
```

The channel must report `active: true` with no `init_error`. Initialization
validates the token with `getMe`, synchronizes the command menu with
`setMyCommands`, and selects polling or webhook transport.

### 4. Enable the responder safely

Channel updates replace the complete non-secret configuration. Use the
authenticated read-modify-write procedure in
[Safe channel updates](#safe-channel-updates) to add:

```json
{
  "responder": {
    "enabled": true
  }
}
```

Keep `allow_from` absent or empty only while enrolling the owner.

### 5. Claim ownership from Telegram

From the intended owner's private chat with the bot, send the exact command
`/start`. A command addressed to the bot as `/start@your_bot_username` is also
accepted. Arguments, surrounding text, other commands, and group messages do
not claim ownership.

When `allow_from` is empty, the first exact private `/start` atomically stores
that sender's numeric Telegram user ID in `allow_from`. This is a first-contact
race: whichever trusted user sends the qualifying command first becomes the
owner. Complete enrollment immediately after enabling the responder.

After binding, messages from other direct-message senders are silently denied
before identity creation, attachment download, message persistence, session
creation, or responder dispatch. Clearing `allow_from` explicitly reopens
first-owner enrollment.

Send a normal DM after `/start`, then use `/status` to confirm the responder's
provider and model binding.

## Transport

### Polling

Polling is the local-first default:

```yaml
communications:
  enabled: true
  webhook_base_url: ""
```

During adapter initialization Gobby calls `deleteWebhook`, then starts
`getUpdates`. The channel's `poll_interval` overrides the daemon
`channel_defaults.poll_interval_seconds`. Successfully acknowledged updates
advance and persist `poll_offset`, so restarts continue after the last completed
update.

### Webhooks

Set a public HTTPS origin that Telegram can reach:

```yaml
communications:
  enabled: true
  webhook_base_url: "https://gobby.example.com"
```

Store a strong `webhook_secret` in the channel's `secrets` object. Gobby
registers:

```text
https://gobby.example.com/api/comms/webhooks/<channel-name>
```

The adapter verifies `x-telegram-bot-api-secret-token` with a constant-time
comparison. Telegram polling and webhooks are mutually exclusive while a
webhook is registered. Changing `webhook_base_url` requires a daemon restart so
the adapter can call `setWebhook` or `deleteWebhook`.

### Proxies

Telegram accepts `http`, `socks5`, and `socks5h` proxy URLs. All Bot API
traffic uses the configured proxy, including token validation, polling,
uploads, downloads, typing, edits, and webhook administration.

Unauthenticated proxies may be stored as channel config:

```json
{
  "proxy_url": "socks5h://127.0.0.1:1080"
}
```

Proxy URLs containing credentials must be supplied through the `secrets`
object:

```json
{
  "secrets": {
    "proxy_url": "socks5h://user:password@proxy.example:1080"
  }
}
```

Plaintext authenticated proxy URLs in non-secret config are rejected.

## Direct-message authorization

`allow_from` contains Telegram's numeric user IDs as strings. Usernames and
display names are mutable and are not authorization identifiers.

Preconfigure one owner:

```json
{
  "responder": {"enabled": true},
  "allow_from": ["123456789"]
}
```

Allow several trusted users:

```json
{
  "responder": {"enabled": true},
  "allow_from": ["123456789", "987654321"]
}
```

Allow every DM sender:

```json
{
  "responder": {"enabled": true},
  "allow_from": ["*"]
}
```

The wildcard creates a public bot. Use it only when the provider, model, agent
permissions, spend, and data exposure are appropriate for untrusted users.

When `allow_from` is empty, only first-owner binding through the exact private
`/start` path is accepted. Unknown senders are denied before persistence both
during and after enrollment.

## Group authorization

Group chat IDs and user IDs are numeric strings. Telegram supergroup chat IDs
usually begin with `-100`. Gobby has no in-chat group-administration command;
an operator changes the channel configuration through the authenticated daemon
API.

Group authorization uses four fields:

| Field | Behavior |
|-------|----------|
| `groups` | Exact chat-ID entries and optional `"*"` defaults. If non-empty, only matching chats are accepted. |
| `group_policy` | `allowlist` by default; `open` accepts any sender in an accepted group. Any other value disables group responses. |
| `allow_from` | Channel user allowlist. A group's own `allow_from` replaces the channel value for that group. `"*"` allows every sender. |
| `require_mention` | `true` by default. Authorized messages wake the responder only when they mention the bot. |

An exact chat entry overrides the `"*"` group entry. Group-level
`group_policy`, `allow_from`, `require_mention`, and `responder` values override
their channel defaults.

### Owner-only, mention-gated

```json
{
  "responder": {"enabled": true},
  "allow_from": ["123456789"],
  "group_policy": "allowlist",
  "require_mention": true,
  "groups": {
    "-100111222333": {}
  }
}
```

Only user `123456789` can wake the bot, and the message must mention it.

### Owner plus authorized group users

This complete example keeps DMs owner-only, permits two additional users in
one group, requires mentions by default, and lets a second group respond
without mentions:

```json
{
  "responder": {
    "enabled": true,
    "provider": "codex",
    "model": "<model-id>"
  },
  "allow_from": ["123456789"],
  "group_policy": "allowlist",
  "require_mention": true,
  "groups": {
    "*": {
      "allow_from": ["123456789"]
    },
    "-100111222333": {
      "allow_from": ["123456789", "222333444", "555666777"]
    },
    "-100444555666": {
      "allow_from": ["123456789", "222333444"],
      "require_mention": false
    }
  }
}
```

### Open group

```json
{
  "responder": {"enabled": true},
  "group_policy": "open",
  "require_mention": true,
  "groups": {
    "-100111222333": {}
  }
}
```

Any member of the configured group may wake the bot by mentioning it. Use
`"groups": {"*": {}}` to accept every group the bot joins.

### Passive observation

Keep `require_mention: true` and authorize the group and users. Messages from
authorized users that do not wake the bot are stored as passive context. The
next mention-gated turn receives up to the latest 20 messages and 8,000
characters. Messages that fail group or sender authorization are excluded.

Passive observation also depends on Telegram delivering those messages. See
[Privacy mode](#privacy-mode).

## Privacy mode

Telegram privacy mode determines which group messages reach the bot. Gobby's
authorization rules apply after Telegram delivery.

| Operation | Telegram setup | Gobby setup |
|-----------|----------------|-------------|
| Mention-only | Privacy enabled is sufficient. Telegram delivers commands, replies, and messages addressed to the bot. | Keep `require_mention: true`. |
| Passive observation | Disable privacy mode with BotFather `/setprivacy`, or make the bot a group administrator. | Authorize the group and users, and keep `require_mention: true` so non-waking messages become passive context. |
| Respond to every authorized message | Privacy disabled or bot-administrator access. | Set `require_mention: false`. |

After changing privacy mode, remove the bot from each group and add it again;
Telegram applies the new mode when the bot rejoins. Telegram's
[bot features guide][telegram-features] and [FAQ][telegram-faq] are the
platform authority for privacy behavior.

## Safe channel updates

`PUT /api/comms/channels/{id}` replaces the entire non-secret `config_json`.
Omitted non-secret fields are removed. Existing omitted `$secret:` references
are preserved, and values supplied in `secrets` are updated separately.

Use daemon bearer authentication and read-modify-write:

```bash
GOBBY_API="http://127.0.0.1:60887"
GOBBY_TOKEN="$(tr -d '\r\n' < "${GOBBY_HOME:-$HOME/.gobby}/local_cli_token")"
CHANNEL_NAME="personal-telegram"

CHANNEL_JSON="$(
  curl -fsS "$GOBBY_API/api/comms/channels" \
    -H "Authorization: Bearer $GOBBY_TOKEN" |
    jq -ce --arg name "$CHANNEL_NAME" '.[] | select(.name == $name)'
)"
CHANNEL_ID="$(jq -r '.id' <<<"$CHANNEL_JSON")"
CURRENT_CONFIG="$(jq -c '.config_json' <<<"$CHANNEL_JSON")"
UPDATED_CONFIG="$(
  jq -c '.responder = ((.responder // {}) + {"enabled": true})' \
    <<<"$CURRENT_CONFIG"
)"

jq -cn --argjson config "$UPDATED_CONFIG" '{"config": $config}' |
  curl -fsS -X PUT "$GOBBY_API/api/comms/channels/$CHANNEL_ID" \
    -H "Authorization: Bearer $GOBBY_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary @-

curl -fsS "$GOBBY_API/api/comms/channels/$CHANNEL_ID/status" \
  -H "Authorization: Bearer $GOBBY_TOKEN" |
  jq '{active, polling, init_error, capabilities}'
```

The update deactivates and reinitializes the live adapter. Require
`active: true` and `init_error: null` after every configuration or secret
change.

To rotate an exposed bot token, generate a replacement through BotFather
`/token`, perform the same read-modify-write, and include the replacement only
in the request's `secrets` object:

```json
{
  "config": {
    "...": "the complete current non-secret configuration"
  },
  "secrets": {
    "bot_token": "<replacement-token>"
  }
}
```

Recheck channel status immediately. The old token stops working once Telegram
rotates it.

## Responder, projects, and models

Telegram uses the shared communications responder:

- The default agent is the restricted `comms-agent` in normal chat mode.
- The default project is Gobby's Personal project.
- `responder.provider` and `responder.model` override the daemon's provider and
  model selection.
- `responder.tts_enabled: true` requests Telegram voice-note replies.
- `set_channel_project` persists a channel project by exact name or UUID. The
  next responder turn switches to it.
- The responder handles operational questions directly and delegates
  implementation, debugging, and validation work to stronger tool-enabled
  agents.

Example responder configuration:

```json
{
  "responder": {
    "enabled": true,
    "provider": "codex",
    "model": "<model-id>",
    "tts_enabled": false
  }
}
```

The registered responder commands are:

| Command | Behavior |
|---------|----------|
| `/new` | Start a new persistent chat session. |
| `/reset` | Reset the current conversation session. |
| `/stop` | Cancel the active turn for this conversation. |
| `/status` | Report active or idle state plus the resolved provider and model. |
| `/subscriptions` | Manage event subscriptions attached to this Telegram channel. |
| `/help` | Show the command summary. |

Telegram initialization synchronizes these commands into the bot menu.
`/start` shows help after authorization; it also performs first-owner binding
when the DM allowlist is empty.

Set the project through the `gobby-communications` MCP server:

```json
{
  "tool": "set_channel_project",
  "arguments": {
    "channel": "personal-telegram",
    "project": "My Project"
  }
}
```

Project selection applies to future turns and survives daemon restarts.

## Actionable session notifications

Subscriptions for session pause and expiry events produce concise lifecycle
messages such as `#42 - Index docs - Paused` and
`#42 - Index docs - Expired`. Provisional titles contain only the provider
label, so the session reference appears once. The message
includes the complete last visible assistant response while omitting provider
details, session UUIDs, compaction summaries, continuation prompts, and injected
handoff context. Long responses use normal Telegram HTML conversion and
4,096-character chunking.

Paused notifications are actionable:

- A single structured question gets one button per declared option.
- Multiple structured questions keep their complete prompts and options in the
  message for a native Telegram reply.
- A live native plan menu gets its exact provider choices. Claude, Codex,
  Droid, Grok, Qwen, and AGY choices are revalidated against the current pane
  before provider-specific keystrokes are sent. AGY opens the artifact review
  with `ctrl+r`, then sends `y` to approve or `n` to request changes.
- Other pauses get a **Continue** button.
- A native reply to any chunk of a paused notification forwards the reply text
  verbatim. Every paused message explicitly tells the user that replies are
  accepted.

Buttons and replies are checked against the current Telegram access policy,
originating channel and chat, persisted notification metadata, and live paused
session state. For Continue and native replies, **Sent.** means the mailbox
message reached a live wake channel; **Queued for delivery.** means the answer
is durably stored for the session and will be available when it can resume.
For a native plan button, **Sent.** means the revalidated provider keystroke was
delivered to the live pane.

A pause caused only by compaction machinery is held for up to 600 seconds. Gobby
re-reads the session and transcript at the deadline: real agent output produces
the normal paused message, a still-paused session with no real activity produces
`#<session-ref> - <title> - Compaction failed`, and a resumed or superseded
session produces no notification. Pending evaluations recover after daemon
restart.

`/subscriptions` works only in an allowlisted private chat. It shows enabled and
disabled subscriptions attached to the current Telegram channel, six per page.
Each row changes to an explicit desired state; **Enable all** and **Disable all**
apply that state to every subscription on the channel. Every mutation returns a
fresh menu snapshot. Callbacks are scoped to their originating channel and chat.
Starting a new Telegram conversation with `/new` does not change channel
subscriptions. A subscription explicitly scoped to the previous session simply
does not match events from the new session.

## MCP sends

`send_message` supports Telegram inline keyboards and link-preview overrides:

```json
{
  "tool": "send_message",
  "arguments": {
    "channel": "personal-telegram",
    "session_id": "#9574",
    "content": "Deploy this version?",
    "inline_keyboard": [
      [
        {"text": "Deploy", "value": "deploy"},
        {"text": "Cancel", "value": "cancel"}
      ]
    ],
    "callback_ttl_seconds": 300,
    "link_preview_options": {
      "is_disabled": true
    }
  }
}
```

Button selections return their configured value to the supplied session.
Callback tokens are opaque, single-use, scoped to their chat, topic, and
session, and stored in memory until they expire.

Send an existing local file with `send_attachment`:

```json
{
  "tool": "send_attachment",
  "arguments": {
    "channel": "personal-telegram",
    "file_path": "/absolute/path/report.pdf",
    "caption": "Nightly report",
    "session_id": "#9574"
  }
}
```

Images use Telegram's photo endpoint, Ogg Opus voice replies use the voice
endpoint, and other files use the document endpoint.

## Shipped capabilities

| Capability | Current behavior |
|------------|------------------|
| Commands and menu | `/new`, `/reset`, `/stop`, `/status`, `/subscriptions`, and `/help`; menu synchronized at adapter initialization. |
| Typing | Sends `sendChatAction` while a responder turn is active. Very fast replies may finish before the indicator is visually noticeable. |
| Streaming | Sends an initial response and edits it as text arrives; final content is persisted. |
| Formatting and chunking | Converts Markdown to Telegram HTML and safely splits output into 4,096-character messages. |
| Inbound media | Downloads photos, documents, video, voice notes, captions, and stickers before responder dispatch. |
| Outbound media | Sends photos, documents, and generated Ogg Opus voice notes; `send_attachment` exposes local-file delivery. |
| Voice | Shared speech-to-text transcribes inbound voice when available. With `responder.tts_enabled`, the existing shared TTS provider supplies PCM and `ffmpeg` packages it as a Telegram voice reply; failure falls back to text. |
| Stickers and vision | Static and animated sticker media is normalized for vision when the vision service supports it; sticker emoji and metadata remain as fallback context. |
| Reactions | Normalizes Telegram reaction updates and can add one standard emoji reaction or clear the bot's reaction. |
| Topics | Preserves `message_thread_id` for forum topics and private topics, with per-topic sessions and replies. |
| Inline keyboards | Sends callback buttons, resolves opaque single-use values, acknowledges callback queries, and routes selections to the originating session. |
| Session lifecycle actions | Pause notifications accept option/Continue buttons and native replies; live wakes report sent and durable mailbox fallback reports queued. |
| Subscription controls | Authorized private chats can list, page, enable, and disable the current channel's event subscriptions with `/subscriptions`. |
| Link previews | Channel `link_preview_options` provides defaults; a `send_message` override controls one message and remains consistent during streaming edits. |
| Scheduled delivery | Cron and routing-rule delivery can target a Telegram channel's `default_destination`; delivery is idempotent across job retries. |
| Passive context | Stores authorized non-waking group messages and prepends bounded recent context to the next waking turn. |
| Polling continuity | Persists acknowledged `poll_offset` values after processed batches. |
| Deduplication | Platform message IDs prevent repeated inbound processing. |
| Restart continuity | Channel configuration, identities, sessions, passive messages, and acknowledged polling position survive daemon restarts. |
| Network proxy | Routes all Bot API requests through an optional HTTP or SOCKS proxy. |

## Limits and dependencies

| Limit or dependency | Operator impact |
|---------------------|-----------------|
| Numeric-ID administration | `allow_from` and `groups` use stable numeric Telegram IDs. Gobby does not authorize by username. |
| Group administration | There is no in-chat group-admin command. Use the authenticated daemon API and complete config replacement. |
| Attachment size | Gobby accepts Telegram attachments up to 50 MiB. Provider-side limits can differ by operation. |
| Captions | Outbound attachment captions use the first 1,024-character formatted chunk. |
| Text messages | Formatted output is split into 4,096-character messages. |
| Passive context | At most 20 messages and 8,000 characters are inserted into a waking group turn. |
| Inline keyboard shape | Maximum 8 rows, 8 buttons per row, 32 total buttons, 64 characters of button text, and 1,024 bytes per button value. |
| Callback lifetime | `callback_ttl_seconds` is clamped to 1–3,600 seconds; callbacks are in-memory, single-use, and unavailable after daemon restart. |
| Compaction grace | Compaction-only pauses wait up to 600 seconds before a failure notification; pending evaluations recover after daemon restart. |
| Voice STT | Requires the shared speech-to-text service; without it, the stored voice attachment and available message text remain. |
| Voice TTS | Requires a configured TTS provider and `ffmpeg`; synthesis, encoding, or send failure falls back to the text response. |
| Sticker vision | Requires an available vision extraction service and supported sticker media; emoji and metadata provide fallback context. |
| Typing visibility | Fast replies can complete before Telegram visibly renders the typing action. |
| Telegram rate limits | Telegram can throttle bursts. Its FAQ advises roughly one message per second per chat, 20 per minute in a group, and about 30 per second for broadcasts unless paid broadcasts are enabled. |
| Mini App dashboard | A Telegram Mini App dashboard remains deferred under #18498. |

The 50 MiB figure is Gobby's attachment-ingestion cap. Telegram's current
platform limits and rate guidance remain governed by the
[Bot API documentation](https://core.telegram.org/bots/api) and
[FAQ][telegram-faq].

## Troubleshooting

| Symptom | Checks |
|---------|--------|
| Channel is inactive | Run `gobby comms status`, list channels, then call authenticated `GET /api/comms/channels/{id}/status`. Read `init_error`. |
| Token validation fails | Confirm the BotFather token was copied completely. Rotate it if exposed, update it through `secrets.bot_token`, and verify live reinitialization. Error reporting redacts the token. |
| Polling never starts | Leave top-level `communications.webhook_base_url` empty. Initialization must successfully call `deleteWebhook`; Telegram rejects `getUpdates` while a webhook remains registered. |
| Webhook receives no updates | Use a publicly reachable HTTPS origin, configure `webhook_secret`, restart the daemon, and verify `setWebhook` succeeds. Check reverse-proxy forwarding of `x-telegram-bot-api-secret-token`. |
| Bot sees commands but not ordinary group messages | Privacy mode is limiting Telegram delivery. Disable it with BotFather `/setprivacy` or make the bot an administrator, then remove and re-add it. |
| DM is ignored | Confirm `responder.enabled`, numeric `allow_from`, and `auto_create_sessions`. With an empty allowlist, only the exact private `/start` enrollment path is accepted. |
| Group is ignored | Check the numeric chat ID, `groups`, effective `group_policy`, effective `allow_from`, `require_mention`, and Telegram privacy delivery. |
| Wrong provider or model | Inspect channel and per-group `responder.provider` and `responder.model`; send `/status` to see the resolved binding. |
| Wrong project | Call `set_channel_project` with an exact project name or UUID. The change applies on the next message. |
| Voice is stored but not transcribed | Check shared speech-to-text service availability and logs. The attachment remains available when transcription cannot run. |
| TTS responds with text | Confirm `responder.tts_enabled`, TTS provider health, `ffmpeg` availability, attachment storage, and Telegram send status. Text is the intended failure fallback. |
| Sticker lacks a visual description | Check vision-service availability and sticker media support. Emoji and sticker metadata remain the fallback. |
| Proxy fails | Validate the `http`, `socks5`, or `socks5h` URL, DNS mode, credentials, and reachability. Put authenticated URLs in `secrets.proxy_url`. |
| Updated config lost a field | Restore it from the previous configuration and repeat read-modify-write. `PUT` replaces all non-secret config. |

[telegram-features]: https://core.telegram.org/bots/features
[telegram-faq]: https://core.telegram.org/bots/faq

_Last verified: 2026-08-30_

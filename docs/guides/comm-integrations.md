# Communications Integrations Guide

Gobby's communications framework connects external channels to the daemon
through adapters, HTTP webhooks, polling loops, and the
`gobby-communications` MCP server. It is disabled by default.

For secure BotFather setup, DM and group authorization, privacy mode, responder
operation, shipped features, and Telegram-specific troubleshooting, use the
[Telegram operator guide](telegram.md).

## Enable communications

Set `communications.enabled` in daemon config and restart the daemon:

```yaml
communications:
  enabled: true
  webhook_base_url: ""
  auto_create_sessions: true
```

Set `webhook_base_url` to the public HTTPS origin that providers can reach,
without the `/api/comms/...` path. Leave it empty to use Telegram polling.
Email uses polling in either mode because it has no webhook transport.

Defaults:

| Field | Default |
|-------|---------|
| `communications.enabled` | `false` |
| `communications.webhook_base_url` | `""` |
| `communications.inbound_enabled` | `true` |
| `communications.outbound_enabled` | `true` |
| `communications.auto_create_sessions` | `true` |
| `communications.channel_defaults.rate_limit_per_minute` | `30` |
| `communications.channel_defaults.burst` | `5` |
| `communications.channel_defaults.retry_count` | `3` |
| `communications.channel_defaults.poll_interval_seconds` | `30` |
| `communications.channel_defaults.retention_days` | `90` |

`communications.enabled` controls manager, route, adapter, polling, cleanup,
and MCP registration. The current runtime parses `inbound_enabled` and
`outbound_enabled` but does not use them as traffic gates.

An enabled `gobby_chat` channel is created automatically when the adapter is
registered and no such channel exists.

## Supported adapters

| Type | Inbound | Outbound | Polling | Capabilities and destination |
|------|---------|----------|---------|------------------------------|
| `slack` | Events API webhook | Web API | No | Threads, reactions, files, Markdown; Slack channel ID. |
| `telegram` | Bot API webhook or `getUpdates` | Bot API | Yes | Topics, reactions, photos, documents, voice, stickers, STT/TTS, HTML chunking, typing, streaming edits, inline keyboards, link previews, proxies, and persistent polling offsets; Telegram chat/topic ID. |
| `discord` | Gateway plus interaction webhooks | REST API | No | Threads, reactions, files, Markdown; Discord channel or thread ID. |
| `teams` | Bot Framework webhook | Bot Framework API | No | Threads, Adaptive Cards, files; conversation ID plus service URL. |
| `email` | IMAP inbox | SMTP | Yes | Threads, HTML email, files; recipient address. |
| `sms` | Twilio webhook | Twilio REST API | No | SMS/MMS; destination phone number. |
| `gobby_chat` | WebSocket chat | WebSocket broadcast | No | Internal channel with threads, files, and Markdown; no external credentials. |

Unknown `channel_type` values can be stored, but activation records an
initialization error because no adapter is registered.

## Manage channels

The CLI surface is:

```bash
gobby comms status
gobby comms send CHANNEL_NAME MESSAGE
gobby comms channels list
gobby comms channels add CHANNEL_TYPE NAME
gobby comms channels remove NAME
```

`channels add` prompts for adapter-specific credentials and configuration.
The optional Slack channel ID, Telegram chat ID, and Discord channel ID are
stored as `default_destination`, so `gobby comms send` can use them directly.
Other adapters need `default_destination` or a previously linked session as
described below.

### HTTP API

The router is mounted at `/api/comms` when communications are enabled.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/comms/webhooks/{channel_name}` | Receive and verify an inbound webhook. |
| `GET` | `/api/comms/webhooks/{channel_name}` | Echo `validationToken` or `challenge` during provider setup. |
| `POST` | `/api/comms/send` | Send through a named active channel. |
| `GET` | `/api/comms/channels` | List channels with `active` and `init_error` state. |
| `POST` | `/api/comms/channels` | Create and initialize a channel. |
| `PUT` | `/api/comms/channels/{channel_id}` | Replace non-secret config, update secrets, and/or change `enabled`. |
| `DELETE` | `/api/comms/channels/{channel_id}` | Remove a channel by UUID. |
| `GET` | `/api/comms/channels/{channel_id}/status` | Get active, polling, capability, and initialization state. |
| `GET` | `/api/comms/messages` | List stored messages with filters. |
| `POST` | `/api/comms/subscriptions` | Create an event subscription. |
| `GET` | `/api/comms/subscriptions` | List event subscriptions. |
| `GET` | `/api/comms/subscriptions/{id}` | Get an event subscription. |
| `PATCH` | `/api/comms/subscriptions/{id}` | Partially update an event subscription. |
| `DELETE` | `/api/comms/subscriptions/{id}` | Delete an event subscription. |

`POST /api/comms/send` accepts `channel_name`, `content`, optional
`session_id`, and optional `metadata`. It returns the stored message on
success, `404` for an unknown or inactive channel, and `502` when the adapter
reports a delivery failure.

Example with an explicit destination:

```bash
GOBBY_TOKEN="$(tr -d '\r\n' < "${GOBBY_HOME:-$HOME/.gobby}/local_cli_token")"

curl -X POST http://127.0.0.1:60887/api/comms/send \
  -H "Authorization: Bearer $GOBBY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "channel_name": "team-telegram",
    "content": "Build completed.",
    "metadata": {"platform_destination": "123456789"}
  }'
```

`PUT /api/comms/channels/{channel_id}` replaces the channel's non-secret
configuration wholesale. Include every non-secret field that must remain;
omitted non-secret fields are removed. Existing `$secret:` fields omitted from
`config` are preserved, and supplied `secrets` are updated separately. A
successful update deactivates and reinitializes the live adapter. Verify
`active` and `init_error` through the channel status endpoint after each
change. The [Telegram safe-update procedure](telegram.md#safe-channel-updates)
shows authenticated read-modify-write.

`GET /api/comms/messages` accepts `channel_id`, `session_id`, `direction`
(`inbound` or `outbound`), `limit` (1–1000, default 50), and `offset`.

### MCP tools

When the manager is available, the proxy registers `gobby-communications`:

| Tool | Arguments |
|------|-----------|
| `send_message` | `channel`, `content`; optional `session_id`, `thread_id`, `content_type="text"`, `inline_keyboard`, `callback_ttl_seconds=300`, `link_preview_options` |
| `send_attachment` | `channel`, `file_path`; optional `caption`, `session_id`, `filename`, `content_type`, `metadata` |
| `set_channel_project` | `channel`, `project` exact name or UUID |
| `list_channels` | None |
| `get_messages` | Optional `channel`, `session_id`, `direction`, `limit=50` |
| `add_channel` | `channel_type`, `name`, `config`, optional `secrets` |
| `remove_channel` | `name` |
| `send_proactive_message` | `channel`, `conversation_id`, `content`, `content_type="text"` |
| `link_identity` | `channel`, `external_user_id`, `session_id` |
| `list_identities` | Optional `session_id`, optional `channel` |
| `unlink_identity` | `identity_id` |
| `create_event_subscription` | `name`, `channel`, `event_pattern`; optional project/global/session scope, priority, enabled |
| `list_event_subscriptions` | Optional channel, project/global scope, enabled, event pattern filters |
| `get_event_subscription` | `subscription_id` |
| `update_event_subscription` | `subscription_id` and changed fields |
| `delete_event_subscription` | `subscription_id` |

`send_message` does not expose arbitrary metadata. Supply a channel
`default_destination`, pass a linked `session_id`, or use the HTTP endpoint
when adapter-specific metadata is required. `thread_id` is copied to the
platform reply/thread field and overrides a thread remembered for the session.

For Telegram, `inline_keyboard` accepts rows of `{text, value}` buttons and
requires `session_id` so selected values can return to the originating
session. `callback_ttl_seconds` controls the bounded callback lifetime.
`link_preview_options` overrides channel defaults for one text message.
Supported Telegram preview fields are `is_disabled`, `url`,
`prefer_small_media`, `prefer_large_media`, and `show_above_text`.
See [Telegram MCP sends](telegram.md#mcp-sends) for examples and limits.

## Configuration and secrets

Channel creation accepts separate `config` and `secrets` objects. Each
non-empty secret is stored in `SecretStore` with category `integration` and a
generated name:

```text
COMMS_<CHANNEL_TYPE>_<SECRET_KEY>_<CHANNEL_NAME>
```

Non-alphanumeric characters become underscores and the name is uppercased.
The channel config stores `$secret:NAME` references. `webhook_secret` is kept
in the channel's dedicated secret field, omitted from API responses, resolved
before adapter initialization, and resolved again for inbound verification.
Plaintext webhook secrets from older rows are migrated into `SecretStore` at
startup.

Use the `secrets` object for credentials:

```json
{
  "name": "team-telegram",
  "channel_type": "telegram",
  "config": {
    "default_destination": "123456789"
  },
  "secrets": {
    "bot_token": "<telegram-bot-token>",
    "webhook_secret": "<telegram-webhook-secret>"
  }
}
```

Adapter fields:

| Type | Secrets | Main configuration |
|------|---------|--------------------|
| `slack` | `bot_token`; `signing_secret` for webhooks; optional `webhook_secret` | `default_destination` |
| `telegram` | `bot_token`; `webhook_secret` for webhook mode; authenticated `proxy_url` | `default_destination`, optional `poll_interval` and unauthenticated `proxy_url`; `poll_offset` is maintained by the adapter |
| `discord` | `bot_token`; `webhook_secret` holds the interaction public key | `default_destination`, `enable_gateway` (default `true`) |
| `teams` | `app_id`, `app_password` | `default_destination`; outbound also needs `service_url` or a stored conversation reference |
| `email` | `password`, or OAuth values `oauth2_client_id`, `oauth2_client_secret`, `oauth2_refresh_token` | `smtp_host`, `smtp_port`, `imap_host`, `imap_port`, `from_address`, `auth_method`; optional `default_destination`, `to_address`, `default_recipient`, `oauth2_token_url`, `allow_plaintext_credentials` |
| `sms` | `auth_token` | `account_sid`, `from_number` or `messaging_service_sid`, `default_destination`, optional `webhook_url` |
| `gobby_chat` | None | None |

Slack uses `signing_secret` when verifying Events API requests. The dedicated
`webhook_secret` field is the verification source passed to every adapter, so
provider-specific setups may put the same verification value there as well.

Telegram accepts an optional `proxy_url` using `http`, `socks5`, or `socks5h`.
All Bot API requests—including polling, attachment downloads, and sends—use
that proxy. Configure URLs containing proxy credentials through the `secrets`
map so channel reads expose only a `$secret:` reference.

## Outbound destination resolution

The manager builds outbound metadata in this order:

1. Caller metadata `platform_destination`.
2. Channel config `default_destination`.
3. A linked identity's `conversation_reference.conversation_id` when
   `session_id` is supplied.

Conversation references can also inject `service_url`, which Teams requires.
Every external adapter reads the resulting `platform_destination`; the
internal channel UUID is only a storage key.

Practical patterns:

- Configure `default_destination` for a channel that always targets one
  Slack channel, Telegram chat, Discord channel, email address, or phone
  number.
- Reply with the inbound message's `session_id` to reuse its external
  conversation. This also restores Teams conversation reference data.
- Use HTTP `metadata.platform_destination` for one-off destinations.
- Use HTTP metadata `service_url` with Teams when no linked inbound identity
  exists.

Inbound messages also populate a session-to-thread map. A later send with the
same `session_id` replies in that platform thread. An explicit `thread_id`
takes precedence.

## Automatic responder

The responder turns approved inbound messages into persistent `ChatSession`
turns and streams the result back through the originating channel. It is
configured per channel inside `config`.

Telegram adds first-`/start` owner binding, passive group context, a restricted
default agent, persistent channel project selection, STT/TTS, and platform
commands. Those operator-facing details are authoritative in
[Telegram responder, projects, and models](telegram.md#responder-projects-and-models).

Minimal direct-message configuration:

```json
{
  "responder": {
    "enabled": true
  },
  "allow_from": ["123456789"]
}
```

Optional `responder.provider` and `responder.model` select the chat backend.
Omitting them uses daemon defaults. The standard WebSocket server must be
enabled because it provides the `ChatSession` runtime.

Access rules:

- Direct messages require the sender's platform user ID in `allow_from`.
  `"*"` allows every sender.
- Telegram alone can atomically populate an empty `allow_from` from the first
  exact private `/start`; unknown senders are denied before persistence.
- Group policy defaults to `allowlist`. Under this policy, the group must
  appear in `groups` (or match `"*"`) and the sender must pass `allow_from`.
- `group_policy: "open"` accepts any sender. When `groups` is non-empty, only
  configured groups are accepted.
- Any other group policy value disables group responses.
- Group messages require a bot mention by default. Set
  `require_mention: false` globally or for one group to respond without it.
- An exact group entry overrides the `"*"` entry. Group `allow_from`,
  `group_policy`, `require_mention`, and `responder` settings override the
  channel-level values.

Example:

```json
{
  "responder": {
    "enabled": true
  },
  "allow_from": ["42"],
  "group_policy": "allowlist",
  "require_mention": true,
  "groups": {
    "-100123456789": {
      "allow_from": ["42", "84"],
      "require_mention": false,
      "responder": {
        "provider": "codex"
      }
    }
  }
}
```

Keep `auto_create_sessions` enabled for responder traffic. Direct messages
receive per-identity sessions. Group messages receive per-channel,
per-group-chat sessions, so multiple senders in one group share context while
different groups stay isolated. Turns for one conversation are serialized;
different conversations can run concurrently.

Supported commands are `/new`, `/reset`, `/stop`, `/status`, and `/help`.
Telegram shows a typing indicator during a turn and edits the first response
message as text streams. Other adapters receive the finalized response unless
they implement message editing.

Telegram attachment messages are downloaded before responder dispatch.
Voice notes are transcribed with the shared speech-to-text service when it is
available, and the transcript becomes the responder input.

## Webhooks and polling

Provider webhook URL:

```text
https://<your-gobby-host>/api/comms/webhooks/<channel-name>
```

Include `:60887` when the public endpoint exposes the daemon port directly.
The POST route passes the raw body, parsed JSON when applicable, and
lower-cased headers to the adapter. Verification runs before parsing.

| Type | Verification |
|------|--------------|
| `slack` | HMAC SHA-256 over Slack's timestamp and raw request body. |
| `telegram` | Constant-time comparison with `x-telegram-bot-api-secret-token`. |
| `discord` | Ed25519 verification using Discord signature headers and the configured public key. |
| `teams` | Bot Framework bearer JWT validation against Microsoft JWKS and the app ID. |
| `sms` | Twilio signature validation against the exact webhook URL and form values. |
| `email` | No webhook support. |
| `gobby_chat` | No webhook support. |

Telegram inbound mode is selected from top-level
`communications.webhook_base_url`:

- With a base URL, initialization calls `setWebhook` with
  `{base}/api/comms/webhooks/{channel-name}` and the resolved webhook secret.
- Without a base URL, initialization calls `deleteWebhook` and starts
  `getUpdates` polling.

Email always polls because it does not support webhooks. Polling uses channel
config `poll_interval` when set, otherwise
`channel_defaults.poll_interval_seconds`. Telegram persists acknowledged
`poll_offset` values so restarts do not replay completed updates.

Discord Gateway reception is enabled by default. Set `enable_gateway: false`
when using only interaction webhooks.

## Event subscriptions

Public interfaces call these records **event subscriptions**. Internal storage
keeps the `CommsRoutingRule` and `comms_routing_rules` names.

Create a subscription for the project resolved from the CLI's current working
directory:

```bash
gobby comms subscriptions create gobby-telegram-agent-paused \
  --channel gobby-telegram \
  --event session.agent.paused
```

`--project <name-or-uuid>` overrides cwd inference. Global scope is always
explicit:

```bash
gobby comms subscriptions create global-agent-pauses \
  --channel operations \
  --event 'session.agent.*' \
  --global
```

The remaining CLI operations are:

```bash
gobby comms subscriptions list --project gobby
gobby comms subscriptions get <subscription-id>
gobby comms subscriptions update <subscription-id> --priority 10 --disabled
gobby comms subscriptions delete <subscription-id>
```

HTTP creation requires exactly one of `project_id` or `global_scope=true`:

```bash
curl -X POST http://127.0.0.1:60887/api/comms/subscriptions \
  -H "Authorization: Bearer $GOBBY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "gobby-telegram-agent-expired",
    "channel": "gobby-telegram",
    "event_pattern": "session.agent.expired",
    "project_id": "<project-uuid>",
    "priority": 0,
    "enabled": true
  }'
```

MCP creation infers project scope from the calling session:

```python
call_tool("gobby-communications", "create_event_subscription", {
    "name": "gobby-telegram-agent-paused",
    "channel": "gobby-telegram",
    "event_pattern": "session.agent.paused",
})

call_tool("gobby-communications", "list_event_subscriptions", {
    "project": "gobby",
    "enabled": True,
})
call_tool("gobby-communications", "get_event_subscription", {
    "subscription_id": "<subscription-id>",
})
call_tool("gobby-communications", "update_event_subscription", {
    "subscription_id": "<subscription-id>",
    "priority": 10,
})
call_tool("gobby-communications", "delete_event_subscription", {
    "subscription_id": "<subscription-id>",
})
```

A Telegram responder session inherits the responder project configured on its
channel. That binding controls MCP caller inference and responder work. Outbound
event delivery is controlled separately by each subscription's project,
global, and optional session scope.

Scope rules:

- Project subscriptions match events from one project.
- Session scope further restricts a project subscription to one session in
  that project.
- Global subscriptions use `project_id=None` internally and cannot carry a
  session ID.
- CLI creation fails when cwd has no project unless `--project` or `--global`
  is supplied. MCP creation fails without a calling-session project unless
  `global_scope=true` is supplied.

Event patterns use glob matching. A pattern without wildcard characters is an
exact match; patterns such as `session.agent.*` match multiple event names.
Every matching channel receives the event. Lower priority values route first.
Disabled subscriptions remain visible to administrative lists and do not
deliver. Deterministic source event IDs suppress replayed delivery once per
channel.

Session lifecycle events are:

- `session.agent.paused`
- `session.agent.expired`
- `session.interactive.paused`
- `session.interactive.expired`

Subscription timestamps are stored in UTC and presented to users as local ISO
timestamps.

## Inbound identity and session behavior

Inbound messages are verified, normalized, deduplicated by platform message
ID, identity-linked, attachment-processed, stored, and emitted as
`comms.message_received`.

With `auto_create_sessions: true`, direct-message sessions use:

```text
external_id = comms:<channel_id>:<external_user_id>
machine_id = comms
source = comms
```

Group sessions use:

```text
external_id = comms:<channel_id>:group:<chat_id>
machine_id = comms
source = comms
```

Identity records keep sender attribution. Group context belongs to the group
session instead of one sender's identity.

## Troubleshooting

| Issue | Check |
|-------|-------|
| Communications routes return 404 | Set `communications.enabled: true` and restart the daemon. |
| Channel is inactive | Run `gobby comms status` or `GET /api/comms/channels/{id}/status`; inspect `init_error`. |
| CLI or HTTP send returns a delivery error | Configure `default_destination`, use a linked `session_id`, or pass HTTP destination metadata. |
| MCP `send_message` has no destination | MCP has no arbitrary metadata field; configure `default_destination` or supply a linked `session_id`. |
| Teams send reports missing `service_url` | Reply through a linked inbound session or pass `service_url` through HTTP metadata. |
| Telegram webhook receives no events | Set a reachable HTTPS `webhook_base_url`, configure `webhook_secret`, restart, and confirm Telegram `setWebhook` succeeds. |
| Telegram polling does not start | Leave top-level `webhook_base_url` empty and confirm the channel is active. |
| Email polling does not start | Confirm IMAP settings, credentials, channel state, and `poll_interval`. |
| Slack webhook verification fails | Configure `signing_secret` and preserve the raw request body through any reverse proxy. |
| Twilio verification fails | Set `webhook_url` or forward the exact original URL in `x-original-url` or `x-gobby-webhook-url`. |
| Responder ignores a direct message | Enable `config.responder.enabled`, add the platform user ID to `allow_from`, and keep auto-session creation enabled. |
| Responder ignores a group message | Check `group_policy`, `groups`, `allow_from`, `require_mention`, and the adapter's extracted chat ID. |
| Responder has no backend | Enable the daemon WebSocket server so the communications `ChatSession` backend is installed. |

Telegram-specific token, privacy, proxy, media-service, authorization, and
provider/model diagnostics live in
[Telegram troubleshooting](telegram.md#troubleshooting).

_Last verified: 2026-07-25_

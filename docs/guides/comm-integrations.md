# Communications Integrations Guide

Gobby's communications framework connects external message channels to the
daemon through channel adapters, HTTP webhooks, polling loops, and MCP tools.
The framework is disabled by default.

## Enable the framework

Communications routes, adapters, storage cleanup, and MCP tools are only wired
when `communications.enabled` is true in daemon config. The current defaults
are:

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

The manager auto-creates an enabled `gobby_chat` channel on startup when the
adapter is registered and no `gobby_chat` channel exists.

## Supported Adapters

| Channel type | Inbound | Outbound | Polling | Files | Notes |
|--------------|---------|----------|---------|-------|-------|
| `slack` | Events API webhook | Slack Web API | No | Yes | Handles message events and reactions. |
| `telegram` | Bot API webhook | Bot API | Yes | Yes | Polling fallback uses `getUpdates`. |
| `discord` | Interaction/webhook payloads | Discord REST API | No | Yes | Gateway connection code exists, but the HTTP webhook path is the daemon entrypoint. |
| `teams` | Bot Framework webhook | Bot Framework API | No | Capability says yes | Proactive sends require a prior inbound conversation reference. |
| `email` | No webhooks | SMTP | Yes | Yes | IMAP polling reads unseen inbox messages. |
| `sms` | Twilio webhook | Twilio REST API | No | MMS URL only | Signature verification needs the exact webhook URL. |
| `gobby_chat` | WebSocket chat handler | WebSocket broadcast | No | Capability says yes | Internal bridge, no external credentials. |

Adapters are registered from `src/gobby/communications/adapters/`. Unknown
`channel_type` values can be stored, but they cannot initialize because no
adapter is registered for them.

The outbound column means the adapter implements `send_message`; it does not
mean every adapter has a complete daemon-level destination path. See
"Outbound destination behavior" below before relying on generic sends.

## CLI Surface

The CLI group is `gobby comms`:

```bash
gobby comms status
gobby comms send CHANNEL_NAME MESSAGE
gobby comms channels list
gobby comms channels add CHANNEL_TYPE NAME
gobby comms channels remove NAME
```

`channels add` prompts for type-specific fields and sends this HTTP request to
the daemon:

```http
POST /api/comms/channels
```

with a JSON body containing `name`, `channel_type`, `config`, and optional
`secrets`.

Current CLI limitations:

- `gobby comms send` posts to `/api/comms/send`, but the HTTP router does not
  define that route in the current codebase. Use the MCP `send_message` tool
  for outbound sends until a route exists.
- `gobby comms status` calls `/api/comms/channels?status=true` and expects a
  wrapped `{"channels": [...]}` response. The current HTTP route returns a bare
  list and ignores the `status` query parameter.
- There is no `gobby comms routes` command. Routing rules are implemented in
  storage and `MessageRouter`, but this guide should not claim CLI route
  management.

## HTTP Routes

The communications router is mounted at `/api/comms` when the framework is
enabled.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/comms/webhooks/{channel_name}` | Receive an inbound webhook for an active channel. |
| `GET` | `/api/comms/webhooks/{channel_name}` | Echo `validationToken` or `challenge` query parameters for webhook verification. |
| `GET` | `/api/comms/channels` | Return all channels as a JSON list. |
| `POST` | `/api/comms/channels` | Create and initialize a channel. |
| `PUT` | `/api/comms/channels/{channel_id}` | Update channel `config` and/or `enabled`. |
| `DELETE` | `/api/comms/channels/{channel_id}` | Remove a channel by UUID. |
| `GET` | `/api/comms/channels/{channel_id}/status` | Return active/inactive status for one channel. |
| `GET` | `/api/comms/messages` | List messages with optional filters. |

`GET /api/comms/messages` accepts:

| Query parameter | Type |
|-----------------|------|
| `channel_id` | string |
| `session_id` | string |
| `direction` | `inbound` or `outbound` |
| `limit` | integer, 1 through 1000, default 50 |
| `offset` | integer, default 0 |

There is no HTTP send route in the current router.

## MCP Tools

When a `CommunicationsManager` is available, the MCP proxy registers an
internal registry named `gobby-communications`.

| Tool | Signature |
|------|-----------|
| `send_message` | `channel`, `content`, optional `session_id`, optional `thread_id`, `content_type="text"` |
| `list_channels` | no arguments |
| `get_messages` | optional `channel`, optional `session_id`, optional `direction`, `limit=50` |
| `add_channel` | `channel_type`, `name`, `config`, optional `secrets` |
| `remove_channel` | `name` |
| `send_proactive_message` | `channel`, `conversation_id`, `content`, `content_type="text"` |
| `link_identity` | `channel`, `external_user_id`, `session_id` |
| `list_identities` | optional `session_id`, optional `channel` |
| `unlink_identity` | `identity_id` |

Example outbound send:

```python
call_tool(
    "gobby-communications",
    "send_message",
    {
        "channel": "team-slack",
        "content": "Build completed successfully.",
        "session_id": "#1234",
    },
)
```

Current MCP send limitation: `send_message` does not accept arbitrary metadata.
It can pass `thread_id` and `content_type`, but not adapter-specific fields such
as Telegram `chat_id`, Teams `service_url`, Email `to_address`, or a generic
`platform_destination`.

## Secret and Config Behavior

`add_channel` stores each supplied secret in the daemon `SecretStore` and writes
a `$secret:NAME` reference into the channel config, except `webhook_secret`,
which is stored on the channel record for signature verification.

Current implementation limitations:

- `CommunicationsManager.add_channel` writes secrets with category `comms`, but
  `SecretStore` currently allows `general`, `llm`, `mcp_server`, `memory`, and
  `integration`. Secret-bearing channel creation can fail until that category
  mismatch is fixed.
- Slack, Teams, and SMS adapters resolve fixed secret names
  (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `TEAMS_APP_ID`,
  `TEAMS_APP_PASSWORD`, `TWILIO_AUTH_TOKEN`) instead of the channel-scoped
  secret references produced by `add_channel`.
- Telegram, Discord, and Email adapters read token/password/OAuth references
  from `channel.config_json` and can resolve `$secret:` references there.

Adapter-specific config fields:

| Channel type | Required or recognized fields |
|--------------|-------------------------------|
| `slack` | Secrets `SLACK_BOT_TOKEN`; optional `SLACK_SIGNING_SECRET`. |
| `telegram` | `bot_token`; outbound messages need `chat_id` in message metadata. |
| `discord` | `bot_token`; optional `enable_gateway`; outbound adapter uses message destination fields, not channel config. |
| `teams` | Secrets `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`; outbound needs `service_url` metadata or a stored conversation reference. |
| `email` | `smtp_host`, `smtp_port`, `imap_host`, `imap_port`, `from_address`, optional `to_address` or `default_destination`, `auth_method`, OAuth2 fields. |
| `sms` | Secret `TWILIO_AUTH_TOKEN`, `account_sid`, plus `from_number` or `messaging_service_sid`; optional `webhook_url`. |
| `gobby_chat` | No external fields. |

## Outbound Destination Behavior

`CommunicationsManager.send_message` creates a `CommsMessage` with
`channel_id` set to the internal Gobby channel UUID. It also copies
`default_destination` from channel config into metadata key
`platform_destination` when present.

Current adapter behavior differs:

| Channel type | Destination used by adapter |
|--------------|-----------------------------|
| `slack` | `message.channel_id`, which is currently the internal channel UUID. |
| `discord` | `message.platform_thread_id` or `message.channel_id`, so generic sends also use the internal channel UUID unless a thread is tracked. |
| `teams` | `message.channel_id` as conversation ID plus `service_url` metadata. |
| `sms` | `message.channel_id` as the Twilio `To` number. |
| `telegram` | `message.metadata_json["chat_id"]`; generic MCP sends cannot supply it. |
| `email` | `platform_destination`, `to_address`, or configured `default_destination`. |
| `gobby_chat` | Broadcasts to WebSocket clients; no external destination. |

For now, Email and Gobby Chat have the clearest generic outbound path. Other
channels may need a prior inbound thread/identity or a code change that maps
configured destinations into the adapter's expected field.

## Webhooks and Polling

External webhook providers should target:

```text
https://<your-gobby-host>:60887/api/comms/webhooks/<channel-name>
```

The POST route passes the raw body, parsed JSON payload when applicable, and
headers to `CommunicationsManager.handle_inbound`.

Webhook signature verification only runs when the channel has
`webhook_secret` set. Verification behavior is adapter-specific:

| Channel type | Verification |
|--------------|--------------|
| `slack` | HMAC SHA-256 using `x-slack-request-timestamp` and `x-slack-signature`. |
| `telegram` | Compares `x-telegram-bot-api-secret-token` with `webhook_secret`. |
| `discord` | Ed25519 signature verification using Discord signature headers and `webhook_secret` as the public key. |
| `teams` | Validates Bot Framework bearer JWT against Microsoft JWKS and the app ID. |
| `sms` | Validates `x-twilio-signature` against the configured webhook URL and payload parameters. |
| `email` | No webhook support. |
| `gobby_chat` | No webhook support. |

Polling starts for adapters where `supports_polling` is true and the top-level
`communications.webhook_base_url` is empty. Telegram and Email currently poll.
Polling interval is read from channel config key `poll_interval`; otherwise the
polling manager uses 30 seconds.

Current Telegram webhook caveat: `TelegramAdapter.initialize` looks for
`webhook_base_url` in channel config and registers
`/v1/comms/webhooks/{config.id}` with Telegram. The current HTTP router exposes
`/api/comms/webhooks/{channel_name}`, so automatic Telegram webhook
registration does not match the daemon route.

## Inbound Routing and Sessions

Inbound messages are parsed by the adapter, identity-linked, stored, and emitted
through the communications event callback as `comms.message_received`.

When `communications.auto_create_sessions` is true, a previously unseen
external identity creates a Gobby session with:

```text
external_id = comms:<channel_id>:<external_user_id>
machine_id = comms
source = comms
```

Routing rules are stored in `comms_routing_rules` and matched by
`MessageRouter` using glob patterns such as `task.*`. Matching can be scoped by
`project_id` or `session_id`, and the rule cache is refreshed every 30 seconds.

## Troubleshooting

| Issue | Check |
|-------|-------|
| Communications routes return 404 | Confirm `communications.enabled` is true and the daemon was restarted after config changes. |
| CLI `send` fails | The CLI targets `/api/comms/send`, which does not exist in the current HTTP router. Use MCP `send_message`. |
| CLI `status` errors or shows no status | The CLI expects a wrapped response, while the route returns a bare list. Use `GET /api/comms/channels/{channel_id}/status` for per-channel status. |
| MCP `send_message` fails for Slack, Discord, Teams, Telegram, or SMS | Check the outbound destination behavior above; generic sends do not currently provide every adapter's required destination metadata. |
| Channel creation with secrets fails | Check for the current `comms` SecretStore category mismatch. |
| Slack, Teams, or SMS adapter cannot resolve credentials | Confirm the fixed secret names expected by those adapters exist. |
| Telegram webhook does not receive events | Manually point Telegram at `/api/comms/webhooks/<channel-name>`; the adapter's automatic registration path currently differs. |
| Polling does not start | Polling only starts for polling-capable adapters when top-level `communications.webhook_base_url` is empty. |
| Twilio verification fails | Set channel config `webhook_url` or provide `x-original-url`/`x-gobby-webhook-url` so the signature base URL is exact. |

_Last verified: 2026-05-07_

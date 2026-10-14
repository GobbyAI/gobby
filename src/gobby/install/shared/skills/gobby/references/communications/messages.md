# Messages

Load before external message retrieval or delivery. Fetch schemas for
`get_messages`, `send_message`, or `send_proactive_message` as needed.

1. Discover and verify the named channel. Load `routing.md` to confirm the
   effective destination and thread before sending authorized content.
2. `get_messages` filters by channel, session, and direction. Its default
   limit is 50; it has no offset or cursor. A bounded result is not a complete
   transcript. Unknown channels fail rather than widening the query.
3. `send_message` accepts text plus optional session/thread and supported
   keyboard/preview fields. It does not expose arbitrary metadata.
4. Check `success`, `message_id`, and `error`. A returned ID alone is not proof
   of delivery. Investigate failure before retrying to avoid duplicate sends.

Telegram inline keyboards require an originating session and use opaque,
single-use callbacks. Load the [Telegram send instructions](../../../../../../../../docs/guides/telegram.md#mcp-sends)
before building buttons; do not present a button as approval already granted.
Proactive delivery depends on adapter support; Teams requires a previously
received conversation reference. Unsupported proactive delivery fails.

For an authorized operator update, fetch
`gobby-communications:send_operator_message` and supply `channel` and Markdown
`content`. The caller's installed agent definition must explicitly grant
`operator_messaging`. The tool targets the enrolled operator's Telegram DM,
accepts no destination, and preserves the conversation's attached session.
Check `success`, `message_id`, and `error` before retrying.

Operator `gobby comms send CHANNEL_NAME MESSAGE` and authenticated HTTP
`POST /api/comms/send` provide delivery interfaces. HTTP accepts adapter
metadata, while `GET /api/comms/messages` supports offset pagination.
See [MCP tools and HTTP](../../../../../../../../docs/guides/comm-integrations.md#manage-channels).

_Last verified: 2026-10-13_

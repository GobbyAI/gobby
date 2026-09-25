# Communications

Load when working with external communication channels, their messages,
attachments, identity mappings, or responder projects.
`$gobby communications references` lists topics without executing operations.

Discover `gobby-communications` tools with `list_tools`; fetch the schema of
each needed tool before calling it. The service requires an enabled, available
communications manager. Connecting a channel does not authorize sending messages.
Use `gobby-agents:send_message` for coordination between coding sessions.

1. Load `channels.md` for channel discovery, setup, status, and removal.
2. Load `messages.md` before sending or reading external messages.
3. Load `attachments.md` before delivering files.
4. Load `identities.md` for external-user/session mappings.
5. Load `routing.md` for destinations and responder project selection.

Confirm the intended channel and destination before an authorized send.
Inspect `success` and delivery errors; successful channel creation alone does
not establish adapter health. Menus and examples do not send messages.

Operator setup, authentication, webhooks, polling, and adapter diagnostics
live in the [communications guide](../../../../../../../../docs/guides/comm-integrations.md).
Telegram access and responder procedures live in the
[Telegram guide](../../../../../../../../docs/guides/telegram.md).

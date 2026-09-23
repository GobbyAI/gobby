# Routing and projects

Load before choosing a delivery destination or changing a responder project.
Discover channels, then fetch the needed communications tool schema.

Outbound destination precedence is caller metadata `platform_destination`,
the sending session's attached conversation, channel `default_destination`,
then a linked session identity's stored conversation reference. A configured
default can therefore override a linked session's destination. Teams also needs
`service_url` or a stored reference.

`attach_conversation(channel, conversation_id)` binds the calling live,
non-comms session to one Telegram conversation: `dm:<chat_id>`,
`group:<chat_id>`, or `topic:<chat_id>:<thread_id>`. Plain inbound messages
there route to that session instead of the responder, and its sends default to
that conversation. A conversation has one holder and a session holds one
conversation; attaching elsewhere fails until you detach. The binding is
in-process: it drops when the session stops being live or the daemon restarts.
`detach_conversation` with the same arguments releases it, holder only.
`send_message` exposes no arbitrary metadata; HTTP send and MCP attachment
delivery accept metadata. A session's remembered thread is restored for
replies; explicit message `thread_id` takes precedence.

Use `set_channel_project(channel, project)` for an authorized persistent
responder project change. Resolve the exact project name or UUID first. It
preserves other channel configuration and affects future responder turns.
The result may have `project_path: null` when a local checkout cannot be
resolved; successful project binding alone does not establish execution access.

Responder project selection and event-subscription scope are separate.
Changing one does not retarget the other. Load `subscriptions.md` before
changing event delivery. Unknown project/channel or unavailable project
storage requires fixing discovery or service health, not inventing a path.

See [destination resolution](../../../../../../../../docs/guides/comm-integrations.md#outbound-destination-resolution)
and [responder projects](../../../../../../../../docs/guides/telegram.md#responder-projects-and-models).

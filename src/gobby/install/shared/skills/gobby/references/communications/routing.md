# Routing and projects

Load before choosing a delivery destination or changing a responder project.
Discover channels, then fetch the needed communications tool schema.

## Register with Telegram

When the operator asks a live agent session to "register with Telegram":

1. Discover the active Telegram channel with `list_channels`, then call
   `resolve_operator_conversation(channel)` to resolve the enrolled operator's
   linked DM. Never search `list_identities` or invent a chat ID. A missing DM
   needs the operator's `/start`; an ambiguous allowlist needs operator selection
   through channel configuration. Report the returned error and stop registration.
2. Call `attach_conversation(channel, conversation_id)` using that result.
   An existing live holder requires it to detach before you can attach. Do not
   displace it or rebind the identity.
3. Type the confirmation normally. The daemon mirrors completed assistant text
   to the attached DM automatically; no explicit `send_message` is needed.

While registered, type operator-facing replies normally. Completed assistant text
is mirrored to the live Telegram attachment, including the final reply at the
provider's turn-end hook. Tool calls, thinking and partial renders are excluded.
Do not send the same reply explicitly or relay it through the Assistant. Inbound
DM messages arrive in your durable mailbox. When unattached, reply on the channel
the operator last used; an explicit Telegram send is then the reply.
Everything typed is mirrored; decisions alone still go out explicitly as button
messages. Send each completed reply whole. Telegram splits replies only when its
length limit requires multiple messages; it never truncates them.
On an explicit unregister/detach request, call `detach_conversation` with the
same channel and conversation ID. DM/topic selections survive daemon restarts.
An operator-owned terminal session retains its selection while expired and
receives messages again once resumed with the same session ID. Expired sessions
receive no live delivery. A clear continuation (staged or consumed), or a managed
agent run ending, releases the selection and may return the DM to a live Assistant.
`list_conversation_attachments()` lists current live holders for coordination;
an attached agent needs no Assistant relay.

## Destination precedence

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
conversation; attaching elsewhere fails until you detach. Group bindings are
in-process; DM/topic bindings persist until detach, replacement, or permanent end.
In an authorized private Telegram chat, `/agent` can select a live root agent.
That selection persists across daemon restarts. When its session permanently ends, the chat
targets a live Assistant if one is available; otherwise the bot clears the
selection and prompts for `/agent`.
`detach_conversation` with the same arguments releases it, holder only.
`send_message` exposes no arbitrary metadata; HTTP send and MCP attachment
delivery accept metadata. A session's remembered thread is restored for
replies; explicit message `thread_id` takes precedence.

Use `set_channel_project(channel, project)` for an authorized persistent
responder project change. Resolve the exact project name or UUID first. It
preserves other channel configuration and affects future responder turns.
The result may have `project_path: null` when a local checkout cannot be
resolved; successful project binding alone does not establish execution access.

Unknown project/channel or unavailable project storage requires fixing
discovery or service health, not inventing a path.

See [destination resolution](../../../../../../../../docs/guides/comm-integrations.md#outbound-destination-resolution)
and [responder projects](../../../../../../../../docs/guides/telegram.md#responder-projects-and-models).

_Last verified: 2026-10-10_

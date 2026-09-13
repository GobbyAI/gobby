# Identities

Load when inspecting or changing an external identity's session link.
Fetch `list_identities`, `link_identity`, or `unlink_identity` schemas from
`gobby-communications` as needed.

1. List mappings with the intended channel and optional session filter.
   Retain identity UUID, external user ID, and current session.
2. `link_identity` requires an existing identity on the named channel; it
   updates the session link rather than creating a new external identity.
3. `unlink_identity` clears the session association. It does not delete the
   external identity or change the channel's access policy.
4. List again to verify the requested association.

Unknown channel or missing identity errors require correcting the selection.
An identity mapping is distinct from sender authorization and conversation
routing. Groups and topics can share conversation context without combining
sender identities. Do not use a mapping change to bypass Telegram allowlists.

See [automatic responder access](../../../../../../../../docs/guides/comm-integrations.md#automatic-responder)
and the [Telegram operator guide](../../../../../../../../docs/guides/telegram.md).

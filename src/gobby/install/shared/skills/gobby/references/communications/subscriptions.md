# Event subscriptions

Load before inspecting or changing automatic event delivery. Fetch schemas
for the needed `gobby-communications` subscription CRUD tools.

1. List subscriptions for the intended channel/project. Listing includes
   disabled rows by default and does not infer the caller's project filter.
2. Inspect a subscription by UUID before changing it.
3. Creation requires name, channel, and event pattern. Omitted project scope
   resolves from the calling session; global scope must be explicit. Project
   and global scope are mutually exclusive; a global subscription cannot
   carry a session restriction.
4. Update only intended fields. Use `clear_session` to remove a session
   restriction. Supply an explicit project to select project scope; do not
   rely on `global_scope: false` alone as a scope change.
5. Read back scope, pattern, priority, and enabled state. Delete by UUID only
   when removal is intended.

Patterns use glob matching. Delivery considers enabled matching subscriptions,
project and optional session restrictions, orders highest priority first, and deduplicates
channels. Enabling a subscription authorizes future event delivery within its
scope; a menu or example is not a request to enable it.

Missing caller project context requires explicit project or global selection.
Empty updates fail. Check channel activation and destination before diagnosing
a matching subscription as a delivery failure. Channel responder project
selection is independent of subscription scope.

Operator CLI entrypoints are `gobby comms subscriptions create`, `list`, `get`,
`update`, and `delete`; HTTP exposes corresponding `/api/comms/subscriptions`
routes. See [scope, examples, and delivery](../../../../../../../../docs/guides/comm-integrations.md#event-subscriptions).

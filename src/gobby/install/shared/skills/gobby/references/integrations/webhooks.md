# Webhooks

Load before configuring a receiver, diagnosing delivery or enabling blocking.
Identify the surface: hook extensions send normalized provider events; pipeline
webhooks notify approval/completion/failure. Channel webhooks belong to the
communications capability. Gobby does not accept GitHub issue triage webhooks.

1. For hook endpoints, inspect operator `gobby webhooks list --json` and active
   `hook_extensions.webhooks` configuration. Both global and endpoint enabled
   flags matter; empty event filters mean all normalized events.
2. Use the config capability for scoped, revision-checked changes. Hook URL and
   header expansion differs from pipelines, whose notifier passes values
   literally. Never assume a secret placeholder will resolve on another surface.
3. Start with observer behavior unless the authorized policy requires blocking.
   `can_block` accepts a successful JSON block/deny decision; `fail_closed` also
   blocks delivery failure. The shared blocking deadline bounds the whole effect.
4. Runtime delivery pins public destinations, forbids redirects/environment
   proxies, limits response size and bounds attempts by a total deadline.
   Pipeline notifications use one attempt and failures do not undo transitions.
5. Operator `gobby webhooks test NAME` makes one real outbound call on a simpler
   HTTP path. It does not validate runtime filtering, retries, address policy or
   blocking. Verify those behaviors using isolated runtime tests.

External registration and configuration are operator tasks; examples grant no
authority to send messages or close issues.

See [webhook operation](../../../../../../../../docs/guides/webhooks-and-plugins.md)
and [transport/model boundary](../../../../../../../../docs/guides/webhook-action-schema.md).

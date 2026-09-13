# Read configuration values

Load when checking defaults, scope, effective behavior or a completed mutation.
After schema discovery, call `gobby-config:get_config_values` without arguments.
Read `revision`, `desired`, `active`, `secret_set`, `pending_restart_keys` and
`failed_live_keys` together. Each response represents one coherent snapshot.

Desired values include registry defaults plus persisted machine overrides.
Active values describe the daemon's activated projection. A default appearing
in the response does not prove that an override row exists. Neither projection
is a project's session-variable store. Bootstrap topology, project settings and
build defaults have their own sources and precedence.

Public reads mask secret payloads; use desired/active `secret_set` flags to check
presence. Do not export secrets into the conversation. Operator/client
`GET /api/config/effective` requires the local runtime token and resolves the
active machine projection; `/api/runtime/config` additionally validates a runtime
grant. These projections are not substitutes for public config discovery.

Operator/client `GET /api/config/ui-settings` and
`GET /api/config/tool-approvals/global` read supplemental namespaces. Changes to
these settings use the revisioned values surface; there are no dedicated save
or reset routes. Tool-approval configuration does not itself grant authority
to perform the operations being configured.

For unexpected behavior, compare desired and active, then inspect the key's
activation policy and failure metadata. A stale revision requires a fresh read
before a newly composed write. See
[sources](../../../../../../../../docs/guides/configuration.md#configuration-sources) and
[runtime overrides](../../../../../../../../docs/guides/configuration.md#runtime-overrides).

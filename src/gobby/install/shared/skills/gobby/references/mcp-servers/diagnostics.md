# Diagnose proxy and connection state

Load when discovery, connection, schema, or tool execution fails. Start with the
returned typed error and `list_mcp_servers` state; operator
`gobby mcp-proxy status --json` provides registry diagnostics. A pending lazy
instance has not necessarily failed. Disabled templates/instances need an
intentional enabled-state decision, not a retry loop.

Check the intended name, project/global scope, installed instance UUID, transport,
and credential requirements. A same-named project instance shadows global scope.
Use the authorized context rather than switching project identity to bypass an
access error. Unknown tool: refresh inventory. Bad arguments: use the returned
schema. Connection/authentication error: repair the named dependency first.

Operator `gobby mcp-proxy refresh --server INSTANCE` reloads the connection,
updates cached schemas and hashes, and embeds changed tools where configured.
`--force` treats tools as new for indexing. It is not an enabled-state or access
bypass. Filtered refresh can adopt a row synced after daemon startup. Always
inspect per-server errors and counts: a top-level successful refresh may contain
failed instances. Unfiltered refresh can affect multiple visible servers.

`search-tools`/`recommend-tools` rely on their configured search/model services.
Missing embeddings need configuration or explicit simpler discovery through
`list_tools`; repeated semantic queries do not repair configuration. HTTP clients
can explicitly request tool embedding with `/api/mcp/tools/embed`.

`read_mcp_resource` exists on daemon MCP but not the stdio carrier. Do not infer
that an external template's resources or prompts are forwarded merely because
its tools work. For OpenAPI, inspect strict/repair/off output-validation settings;
repair reports `schema_deviations`, which must not be hidden as original data.

Daemon-level health/authentication and installation belong to operator lifecycle
procedures. Coordinate any shared restart with active sessions; do not restart
from an unmerged worktree to repair a connection that supports scoped refresh.

Guide: [Diagnostics](../../../../../../../../docs/guides/mcp-tools.md#diagnostics).

_Last verified: 2026-09-12_

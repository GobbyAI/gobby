# Manage instances

Load before adding, importing, modifying, or removing a downstream MCP server.
Use top-level `add_mcp_server`, `remove_mcp_server`, or `import_mcp_server` where
exposed. Inspect their actual native schemas; do not route those tools through
an invented `gobby` internal registry.

Prefer an installed template plus `name`, `values`, and explicit project/global
`scope`. A new instance normally defaults enabled. Manual instances require
transport and its URL or command. A successful add can persist the row while
connection fails; inspect `connected`, `needs_configuration`, `missing_secrets`,
and recovery commands before attempting a duplicate add.

Name resolution for use is project first, then global; mutation targets the
exact selected scope. Same-scope duplicates are refused. Never remove/recreate a
connection merely to repair missing credentials: its UUID is part of OAuth
identity. Fix credentials then refresh the same instance.

Declarative instances use `.gobby/mcp/servers/NAME.yaml` or
`~/.gobby/mcp/servers/NAME.yaml` with template and values; name defaults to the
file stem. Commit references, never secret values. Sync upserts rows without
pruning removed instance files. To retire a declarative instance, remove its
source declaration and explicitly remove the matching installed row.

Import requires a source: project, GitHub repository, or natural-language query.
The optional `servers` list narrows project import; it is not a standalone source.
Use one source per request and inspect imported/skipped/failed results. Import
can execute downstream programs or connect to services; it needs task authority.

Operator/client surfaces include `gobby mcp-proxy add-server`, `remove-server`,
and `import-server`; authenticated HTTP PUT/PATCH update the exact scoped row.
Names cannot be changed by update. Templated instances reject template-owned
runtime-field edits; change the supported parameter values or the user template.
There is no native MCP update tool to invent. HTTP parameters and tool schemas
remain authoritative. Agent operations such as task lifecycle still use their
MCP tools, never a raw CLI/HTTP call to bypass workflow gates.

Guide: [Manage instances](../../../../../../../../docs/guides/mcp-tools.md#manage-instances).

_Last verified: 2026-09-12_

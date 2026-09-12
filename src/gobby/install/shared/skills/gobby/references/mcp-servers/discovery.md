# Discover and call tools

Load when selecting or invoking a proxy tool. The native proxy's registered
schema is authoritative; stdio and direct daemon MCP expose different native
signatures. Discover available surfaces before assuming an HTTP-only helper
exists in a CLI provider.

1. Known leased tool: call `call_tool` directly.
2. Known unleased tool: fetch `get_tool_schema`, then call.
3. Unknown tool on a known server: `list_tools`, then fetch the selected schema.
4. Unknown server or explicit inventory: `list_mcp_servers`.

Do each discovery step as its own top-level tool. A schema lease survives an
ordinary resume/restart; clear, compact, and reconstructed context reset leases.
Inventory observations survive those resets. Parameter validation errors return
the current schema and retain its lease; repair the arguments before retrying.
The skill bootstrap tools `get_skill`, `list_skills`, and `search_skills` bypass
the schema gate. Reference files do not: load their schema and every cursor page.

Pass wrapper `session_id` outside `arguments` for caller context. A target tool's
session argument is for a different target session. Prefer structured `arguments`
over the `args` alias. Use wrapper `project_id` for an authorized cross-project
call; local task/session shorthand resolves in the caller's project. Use full
UUID or project-qualified session references for foreign targets.

`recommend_tools` accepts a task description with llm, semantic, or hybrid
selection; `search_tools` searches indexed tool metadata and can filter by
server. Their results identify candidates, not schema leases, installed skills,
or authority to execute. Fetch the chosen schema. Missing semantic configuration
needs diagnostics, not an invented tool name or an eager all-schema download.

`list_mcp_servers` includes internal registries and visible external instances,
connection state, and the template catalog. Pending lazy connections are not
proof of failure. A template entry is not an instance. Discovery of guarded
internal tools may depend on the caller's current role, as with Ask stages.

The daemon MCP surface additionally exposes `read_mcp_resource`; the stdio
carrier does not register it. Downstream resource availability is separate from
tool discovery. Stdio `init_project` only returns a CLI-required error: project
initialization is `gobby init`, an operator procedure.

Guide: [Progressive discovery](../../../../../../../../docs/guides/mcp-tools.md#progressive-discovery-pattern).

_Last verified: 2026-09-12_

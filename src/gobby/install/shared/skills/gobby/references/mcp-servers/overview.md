# MCP servers

Load when discovering proxy tools or managing downstream MCP connections.
Templates describe servers; instances are installed hub rows with their own
identity, scope, configuration, and connection state. A connected server is
not an installed skill. Gobby installation lists templates and does not
instantiate them automatically.

Use top-level `list_mcp_servers`, `list_tools`, `get_tool_schema`, and `call_tool`
for progressive discovery. These are separate calls, never discovery steps
nested inside `call_tool`. Tool schemas own parameters. Menus and listings do
not execute operations or load instruction bodies.

| Topic | Load when |
| --- | --- |
| [discovery](discovery.md) | Selecting tools, leases, recommendations, or proxy calls |
| [templates](templates.md) | Inspecting or authoring parameterized definitions |
| [instances](instances.md) | Adding, importing, updating, or removing connections |
| [authentication](authentication.md) | Configuring or rotating secret references |
| [oauth](oauth.md) | Signing in or recovering browser authorization |
| [diagnostics](diagnostics.md) | Investigating unavailable tools or refreshing state |
| [results](results.md) | Retrieving oversized tool output |

Name lookup uses the caller's project then global scope. Exact mutations must
select the intended scope; a project name can shadow a global instance. Check
installed state rather than inferring enabled/connected from bundled YAML.
Operating instructions grant no additional authority to connect, execute,
import, or remove a server. Use isolated state for verification mutations.

Guide: [MCP tools](../../../../../../../../docs/guides/mcp-tools.md).

_Last verified: 2026-09-12_

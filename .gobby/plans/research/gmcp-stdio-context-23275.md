# #23275 working context (W4 gobby#15469): research for `.gobby/plans/gmcp-stdio.md`

Session-scoped working notes. The plan file, once written, is the authority.

## Orchestrator ruling (gobby#14972, 2026-10-05 11:19 CT)

1. Option (a). `crates/gmcp` (package `gobby-mcp`, binary `gmcp`) may join the workspace
   now. The crates/AGENTS.md timing rule (lines 80-88) covers RouteFamily crates only, and
   gmcp is a client binary like ghook. Name `rmcp` as a new dependency; Josh approves it
   with the plan.
2. Every D3 obligation (gdaemon-api-keys-nodes.md:1048-1066, plan of record
   gdaemon-front-door.md:1659-1691) is either planned in P1 or a typed deferral with an
   owner.
   - Native streamable HTTP and restart-stateless HTTP serving defer to #21570 (MCP
     front door flip).
   - gdaemon OAuth 2.1 AS (metadata, DCR or CIMD, authorize, token, client-registry
     table), the second bearer kind, and the users/api_keys binding defer to a
     TBD-at-expansion task gated on #23519 (Shared-token cutover).
   - Keep the D2 node-relay wait (#23274).
3. CIMD vs DCR: write it as an open product question in the OAuth deferral; the
   Orchestrator flags it to Josh at approval.
4. D2.5 (the hub-node pair test) maps to #23274; W1 gobby#15434 covers it there. The
   coverage table points there.

Adv4 gobby#15471: P1 must preserve the stdio lifecycle from plan of record :1671-1674
(survives gdaemon/backend restarts, no session state, dials per request, typed
unavailable while down). Parity cases go beyond tool names: request headers, 401
refresh, 409 SESSION_REQUIRED, DAEMON_UNAVAILABLE vs REQUEST_TIMEOUT, wait guards,
heartbeats, capped-timeout metadata, and bridge-ready after tools/list.

## External facts

- MCP authorization spec version 2026-07-28: DCR is MAY and deprecated (kept for
  backward compatibility); Client ID Metadata Documents are SHOULD. The spec also
  requires PRM (RFC 9728), AS metadata (RFC 8414 or OIDC discovery), PKCE, RFC 8707
  resource indicators with audience validation, RFC 9207 `iss`, and no token
  passthrough. STDIO transports SHOULD NOT use OAuth; they take credentials from the
  environment.
- rmcp 3.5.0 (crates.io, 2026-09-28), Apache-2.0, rust_version 1.88 (the workspace is
  1.88). Features: server, macros, transport-io (stdio server), and others. Releases
  are weekly. Its optional reqwest is ^0.13, but gcore uses reqwest 0.12; a stdio
  server needs no reqwest. ghook deliberately uses ureq 2, never reqwest.

## Python wrapper inventory (`gobby mcp-server`, to be replaced)

- Entry: `src/gobby/cli/mcp.py::mcp_server`. Modules: `src/gobby/mcp_proxy/stdio.py`,
  `stdio_server.py`, `stdio_proxy.py` (DaemonProxy 110-619), `stdio_tools.py`
  (register_proxy_tools 66-365), `stdio_daemon.py`, `stdio_results.py`,
  `daemon_control.py` (consumed only by the stdio modules).
- 12 tools: list_mcp_servers, list_tools, get_tool_schema, call_tool, recommend_tools,
  search_tools, add_mcp_server, remove_mcp_server, import_mcp_server, init_project,
  set_variable, get_variable. `_StdioMCPServer.list_tools` strips None from input
  schemas and sets `tools_listed`. `_report_ready_once_listed` then calls
  POST /api/mcp/bridge/ready (`servers/routes/mcp/tools.py:58`; rule
  memory-lifecycle/layered-memory-guidance.yaml reads `_mcp_proxy_ready`).
- Instructions: `instructions.py::build_gobby_instructions` reads bundled
  `prompts/mcp/progressive-discovery.md` with its front matter stripped, falling back
  to `_FALLBACK_INSTRUCTIONS`. Initialize does no daemon or hub I/O (#21705, #22032,
  #23343; memory 728c2ca0).
- REST routes used: /api/mcp/{server}/tools, /api/mcp/{srv}/tools/{tool} or
  /api/mcp/tools/call, /api/mcp/tools/schema, /api/mcp/servers (GET/POST),
  /api/mcp/servers/{name}, /api/mcp/servers/import, /api/mcp/tools/recommend,
  /api/mcp/tools/search, /api/sessions/{id}/variables/set|get, /api/admin/status,
  /api/mcp/bridge/ready.
- `DaemonProxy._request` sends these headers:
  - X-Gobby-MCP-Wrapper-Protocol-Version: 1 (checked server-side in
    `servers/routes/mcp/endpoints/request_context.py`; wait tools without it get
    GOBBY_MCP_WRAPPER_STALE).
  - X-Gobby-Project-Id is the target; X-Gobby-Caller-Project-Id is from cwd
    .gobby/project.json and is re-read when absent.
  - X-Gobby-Session-Id comes from env GOBBY_SESSION_ID first, else the body
    session_id unless a tmux identity exists. Otherwise the terminal context header
    is sent.
  - AGENT_RUN_ID header from GOBBY_AGENT_RUN_ID.
  - Auth comes from `utils/local_token.py::daemon_auth_headers`, which prefers
    GOBBY_AGENT_API_TOKEN plus the agent identity env headers and otherwise reads the
    local token. A 401 re-reads auth and retries once.
  - A 409 with detail.error_code SESSION_REQUIRED returns the detail. Non-200
    responses become {success:false, error:"HTTP n: body"}. ConnectError gives
    DAEMON_UNAVAILABLE and timeout gives REQUEST_TIMEOUT (stdio_results.py).
  - The preflight health check is cached for 5 s.
- call_tool canonicalizes (`_call_tool_wrapper.py`), then `wait_tools.prepare_client_guard`
  (clamps wait tool timeouts; EXTENDED_TIMEOUT_TOOL_NAMES get 300 s), then
  `call_with_wait_heartbeat` (progress notification every 15 s for HEARTBEAT_TOOL_NAMES),
  and adds capped-timeout metadata. Tool timeouts come from config
  `mcp_client_proxy.tool_timeouts`, read on first call (hub access).
  `wait_tools.py` STAYS: server.py, execution.py, request_context.py,
  agents_query_tools.py, and workspaces/registry.py import it.
- Daemon auto-start is DEAD. `daemon_control.start_daemon_process` runs
  `python -m gobby.cli.app daemon start`, and `find_spec('gobby.cli.app')` is None.
  This was reported to the Orchestrator as found work. Decision: gmcp does not
  auto-start; it returns DAEMON_UNAVAILABLE with `gobby start` guidance.
- The HTTP `/mcp` (`server.py::create_mcp_server`, mounted by
  `servers/app_factory.py::_register_mcp_http_route`) has a different tool set (status,
  read_mcp_resource, no init_project) and different identity resolution. Bridging stdio
  to `/mcp` is rejected.

## Rust reuse

- `crates/gcore/src/daemon_url.rs::daemon_url`, `local_token.rs` (ureq bearer helpers,
  GOBBY_AGENT_API_TOKEN preferred; #23519 rewrites it to the bootstrap key, so gmcp
  works in either order), `project.rs::find_project_root` and `read_project_id`.
- Terminal context: `crates/ghook/src/terminal_context.rs::capture` is ghook-private.
  Python `mcp_proxy/terminal_context.py` (TERMINAL_CONTEXT_KEYS, current_terminal_context,
  serialize_terminal_context). Decide whether to move the capture into gcore for sharing,
  or reimplement. Compare key sets first.
- The workspace (root Cargo.toml) has members gcode, gcore, gclient, gdaemon, ghook,
  gterminal, and gterminals. There is no MCP code in Rust.

## Launch-site sweep (`gcode grep -F "mcp-server" src tests crates`)

src/gobby/cli/installers/mcp_config_shared.py:18 (_GOBBY_MCP_ARGS), mcp_config_toml.py:108,
mcp_config_json.py:277 (repair of stale `uv run` entries), agents/spawn_executor_support.py:533
(_codex_mcp_config_overrides uses `uv run --no-sync --project <path> gobby mcp-server`),
agents/isolation_repair.py:214-304, agents/sandbox_policy.py:359-386, agents/srt_runtime.py:520
(comment on loopback egress), agents/constants.py:96-109 (MCP_CONNECT_TIMEOUT_MS and
MCP_TIMEOUT rationale; cold wrapper 25s and >30s), servers/chat_session_helpers.py:145-149,
cli/__init__.py:17-28 (lazy map), cli/mcp.py:25, tasks/criterion_commands.py:69,
utils/local_token.py:57 (docstring), install/shared/skills/gobby/references/admin/clients.md:26,
tests/agents/spawners/test_command_builder.py:203-212. Memory ce6ece28: `uv run --project
<repo>` is intentional for isolated worktrees. Revisit why when switching to gmcp.

## Install surfaces for a new managed binary

`src/gobby/install/bin_set_coherence.py` (the stamped set is gcode/gdaemon/ghook only);
`cli/install_components.py` (76, 108, 193, 216, 222, 291), `cli/install_setup.py`
(MANAGED_NATIVE_BINARY_NAMES 280, labels 285, installers 311), `cli/install_setup_gclient.py`
(the per-binary installer pattern), `install/bin_freshness_promotion.py:208`,
`install/distribution.py:18-23` (HOMEBREW_HELPERS, package names),
`install/version_pins.py:11-14` (pins, UNPUBLISHED_MANAGED_BINS). The `gobby cutover` path
should be checked as well.

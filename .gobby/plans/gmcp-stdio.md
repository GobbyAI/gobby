Plan artifact: `.gobby/plans/gmcp-stdio.md`

# `gmcp`: a native stdio MCP client replaces `gobby mcp-server`

**Plan ID:** gmcp-stdio

## Overview
`kind: framing`

This plan specifies #23275 (`gobby-mcp` crate: native MCP transports with OAuth
and DCR). That task is deferred section D3 of
`.gobby/plans/gdaemon-api-keys-nodes.md` (#21555, API keys and node
registration), which carries deferred section D1 of the plan of record
`.gobby/plans/gdaemon-front-door.md`. Lane 7 plans it in place under the
Orchestrator's 11:19 CT ruling of 2026-10-05. The parent texts stay unchanged.

**The problem.** Every CLI config Gobby writes, and every agent it spawns,
launches `gobby mcp-server`. That is a Python stdio wrapper
(`src/gobby/mcp_proxy/stdio*.py`) that forwards twelve proxy tools to the
daemon's REST API. It is slow to start: `src/gobby/agents/constants.py` records
a cold wrapper taking 25 s beside concurrent spawns, with two siblings past 30 s,
which left those agents without Gobby tools. It needs a Python environment,
which a thin node will not have. Its daemon auto-start is dead code: it runs a
module that does not exist (#23525, MCP stdio wrapper's daemon auto-start runs a
module that does not exist).

**When the leaves close:**
- The workspace crate `crates/gmcp` (package `gobby-mcp`, binary `gmcp`) serves
  the wrapper's tool surface over stdio. It holds no session state, dials the
  daemon per request, and survives daemon and backend restarts (1.1 to 1.3).
- `gobby install` builds and promotes `gmcp`, and a release workflow can publish
  it (1.4).
- The e2e and server tests that reached through the wrapper drive `gmcp` or the
  routes directly (1.5).
- Every launch site writes `~/.gobby/bin/gmcp` (1.6).
- The Python wrapper, `daemon_control.py`, and the
  `mcp_client_proxy.tool_timeouts` setting are gone (1.7).
- The agent sandbox drops the wrapper's main-repo read grant and its
  `gobby mcp-server` text (1.8).

Live activation, the streamable HTTP transport, OAuth, thin-node use,
publishing, and the post-switch smoke proof are deferred sections D1 to D6, each
with an owner.

## Decision Record
`kind: framing`

Orchestrator rulings (gobby#14972, 2026-10-05). The Orchestrator flags 1 and 3
to Josh at approval.

1. **Option (a).** `crates/gmcp` joins the workspace now. The timing rule in
   `crates/AGENTS.md` covers `RouteFamily` crates only, and `gmcp` is a client
   binary like `ghook`. The new dependency is **rmcp 3.5.0** (crates.io,
   2026-09-28, Apache-2.0, `rust_version` 1.88, matching the workspace). `gmcp`
   uses its `server` and `transport-io` features only, so rmcp's optional
   reqwest 0.13 is not pulled in. The crates.io name `gobby-mcp` is free
   (checked 2026-10-05).
2. **Every D3 obligation is planned here or is a typed deferral with an owner.**
   Native streamable HTTP and restart-stateless HTTP serving go to #21570 (MCP
   front door flip). The OAuth 2.1 authorization server, the second bearer kind,
   and the `users`/`api_keys` binding go to a task gated on #23519 (Shared-token
   cutover). The node-relay wait (#23274) stays.
3. **CIMD versus DCR is an open product question** inside the OAuth deferral
   (D3).
4. **D2.5 (the hub-node pair test) belongs to #23274** (Node channel, relay
   backend, and `/api/machines`).
5. **No daemon auto-start** (11:24 CT). `gmcp` returns the typed
   `DAEMON_UNAVAILABLE` result with `gobby start` guidance.
   `src/gobby/mcp_proxy/daemon_control.py` retires with the wrapper.
6. **Bridge readiness for agent-token sessions** (11:48 CT). `gmcp` keeps the
   wrapper's wire call `POST /api/mcp/bridge/ready`. Capability-token sessions
   are refused on that route today, and #23531 (Spawned agents cannot report MCP
   bridge readiness: agent capability matrix lacks POST /api/mcp/bridge/ready)
   fixes the server. Operator sessions report readiness without #23531. Agent
   sessions report it once #23531 lands. No leaf here waits on it.

Writer decisions:

7. **Port the REST wrapper; do not bridge to Python `/mcp`.** The HTTP `/mcp`
   server (`src/gobby/mcp_proxy/server.py::create_mcp_server`) has a different
   tool set (it has `status` and `read_mcp_resource` and lacks `init_project`)
   and resolves identity differently. Bridging would change what every agent
   sees.
8. **Exact tool-surface parity comes from a captured fixture.** `gmcp` serves
   `tools/list` from `crates/gmcp/contracts/tools_list.v1.json`. That file holds
   the Python wrapper's `tools/list` output after `_strip_none`, captured once
   in 1.2. The same capture writes the `initialize` result and the call-result
   envelopes to `crates/gmcp/contracts/wire_cases.v1.json`. A Python test
   proves both fixtures equal to the wrapper until 1.7 retires the wrapper.
   Rust subprocess tests compare `gmcp`'s wire output against both fixtures, and
   they outlive the wrapper. Generating schemas from Rust types would drift from
   what clients see today.
9. **Server-coupled constants live in one JSON contract.** This contract is
   `crates/gmcp/contracts/mcp_wrapper.v1.json`. It holds the protocol version
   (`"1"`, unchanged), the wait-tool and extended-timeout tool names with their
   ceilings, the heartbeat interval, the terminal-context keys, and the
   agent-identity environment headers. It follows the
   `crates/gcore/contracts/tool_loop_limits.v1.json` precedent. `gmcp` embeds it
   with `include_str!`. A permanent Python test asserts it equals the surviving
   Python constants. The `call_tool` canonicalization cases follow the same
   pattern (`call_tool_wrapper_cases.v1.json`, checked against
   `src/gobby/mcp_proxy/_call_tool_wrapper.py`, which stays).
10. **The instructions are an embedded copy.** `crates/gmcp/assets/progressive-discovery.md`
    is a byte copy of the bundled
    `src/gobby/install/shared/prompts/mcp/progressive-discovery.md`, kept equal
    by a Python test. `cargo package` cannot include files outside the crate,
    and `initialize` must do no I/O (memory 728c2ca0).
11. **Terminal context is captured inside `gmcp`.** The header key set is
    `TERMINAL_CONTEXT_KEYS` (12 keys, which include
    `tmux_server_pid`/`tmux_server_start_time` from a tmux generation query).
    `crates/ghook/src/terminal_context.rs::capture` emits a different set for
    `SESSION_START` (it has `gobby_*` identity keys and no generation), so
    moving it into gcore would serve neither caller exactly.
12. **Retire `mcp_client_proxy.tool_timeouts`.** Its only reader is the
    wrapper (`DaemonProxy._get_tool_timeouts`). `gmcp` has no hub access, and
    `/api/config/values` is outside the agent capability matrix, so keeping the
    setting would need a new route and a lazy fetch. It defaults to empty.
    Ordinary calls keep the 30 s client timeout; the wait and extended tools
    keep their contract timeouts.
13. **A front-door typed 503 is `DAEMON_UNAVAILABLE`.** While Python is down,
    gdaemon answers proxied routes with
    `{"status":"unavailable","backend":{...}}` and 503
    (`crates/gdaemon/src/front_door/health.rs::unavailable`). The wrapper turned
    that into `HTTP 503: …` whenever preflight was off. `gmcp` types it, which
    is what the plan of record's restart-survival clause asks for.
14. **Dropped wrapper-only shims.** The client-side `TOOL_REMOVED` stub for
    `gobby-workflows.wait_for_completion` is not ported; the daemon answers that
    name. `init_project` is ported as listed: it returns its static "use
    `gobby init`" error with no daemon call.
15. **`gmcp` is a managed client binary, not a stamped-set member.** It embeds
    no schema identity and opens no database, so `gobby cutover` and
    `promote_workspace_binary_set` are unchanged. It follows `gclient`:
    `gobby install` builds and promotes it, `gobby install gmcp` re-promotes it,
    and freshness tracks the `gmcp` and `gcore` crates.
16. **A separate leaf, 1.8, removes the wrapper's sandbox read grant and its
    stale text** (Orchestrator, 12:12 CT).
    - `mcp_config_read_exceptions` grants the main repo as a read root, because
      the isolated entry runs `uv run --project <main repo>`. The `gmcp` entry
      has no arguments, so after 1.6 the function grants nothing.
    - The files involved are `src/gobby/agents/sandbox_policy.py` (935 lines)
      and `src/gobby/agents/srt_runtime.py` (867 lines). `production-size-growth`
      requires any deliverable that targets them to move code into a new file.
    - 1.8 moves the two functions that hold the stale text into new modules and
      deletes the dead grant.
    - #23273 (Hub-side key validation, front-door identity, and shared-token
      cutover) splits `sandbox_policy.py` too, moving `_credential_roots` and
      `_gcode_runtime_root` to `sandbox_credentials.py`. The only overlap is
      import lines, and whichever change lands second rebases them.

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never runs the full suite. No leaf restarts the running daemon, touches
  port 60891, or reads `~/.gobby/local_cli_token` or `~/.gobby/bootstrap.yaml`.
  Tests build their own homes.
- **Rust.** Load the `rust` skill before editing `crates/`, where
  `crates/CLAUDE.md` governs. `cargo build`, `clippy`, and `nextest` are heavy
  work under `.gobby/roles/_common.md`.
- **No secrets in output.** `gmcp` never logs a bearer, a key, or a header
  value. Its diagnostics go to stderr, because stdout carries the protocol.
- **No backward compatibility.** 0.5.0 has not shipped. `gobby mcp-server` and
  the setting leave without aliases. The installer's repair (1.6) rewrites
  existing wrapper entries to `gmcp`.
- **Consumer sweeps** ran read-only on `0.5.0` at `6f5d0c8733` (2026-10-05):
  - `gcode grep -F` for `mcp-server`, `mcp_proxy.stdio`, `daemon_control`,
    `current_terminal_context`, `serialize_terminal_context`, `tool_timeouts`,
    and `_call_tool_wrapper`;
  - `gcode grep -w` for `_resolved_gobby_mcp_command`,
    `_is_repairable_stale_gobby_mcp_server_config`, and
    `mcp_config_read_exceptions`.
  - `docs/evidence/**`, `docs/plans/**`, `docs/audits/**`, `docs/archive/**`,
    `tests/fixtures/provider_contracts/**`, and `CHANGELOG.md` hold dated history
    and are excluded.
- **Landing order.** D1 (pre-switch promotion) must be complete before the
  Merge Manager lands 1.6. The daemon restart that follows the 1.6 landing makes
  spawns launch `~/.gobby/bin/gmcp`. D6 (post-switch smoke proof) runs after
  that restart.

## Parent D3 Mapping
`kind: framing`

| Parent obligation | Here | Note |
| --- | --- | --- |
| stdio transport replacing `gobby mcp-server` in every config the installer writes (plan of record D1, Transports) | 1.1, 1.2, 1.3, 1.6, 1.7 | |
| Restart survival: no session state, per-request dial, typed unavailable while down (plan of record D1, Restart survival) | 1.1.5, 1.5.2 | Also the typed 503 (Decision 13). |
| Streamable HTTP for remote clients as the `mcp` route family | D2 | #21570 |
| OAuth 2.1: protected-resource metadata, AS metadata, client registration, authorize, token, client-registry table, `users`/`api_keys` binding, second bearer kind | D3 | Gated on #23519; CIMD versus DCR stays open. |
| Waits on the node relay (api-keys D2) | D4 | #23274 |
| D2.5 (plan of record 4.4.5), hub-node pair test | D4 | Owned by #23274 (W1 gobby#15434). |
| Ownership S2.10/S2.12 | D2, D3 | |

## P1: `gmcp` replaces the Python stdio wrapper
`kind: framing`

**Goal**: every local MCP client reaches Gobby through a native binary that
starts at once, matches the wrapper's tool surface and wire behavior exactly,
and outlives daemon restarts.

### 1.1 `gobby-mcp` crate and its daemon dialer [category: code]
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gmcp/Cargo.toml`
- `crates/gmcp/README.md`
- `crates/gmcp/src/lib.rs`
- `crates/gmcp/src/contract.rs`
- `crates/gmcp/src/daemon.rs`
- `crates/gmcp/src/terminal_context.rs`
- `crates/gmcp/contracts/mcp_wrapper.v1.json`
- `crates/gmcp/tests/stub_daemon/mod.rs`
- `crates/gmcp/tests/dialer.rs`
- `tests/mcp_proxy/test_gmcp_contracts.py`

**Research context:**
- The wrapper's dialer is `src/gobby/mcp_proxy/stdio_proxy.py::DaemonProxy._request`:
  - Every request carries `X-Gobby-MCP-Wrapper-Protocol-Version: 1`.
    `src/gobby/servers/routes/mcp/endpoints/request_context.py` answers wait
    tools without it with `GOBBY_MCP_WRAPPER_STALE`.
  - `X-Gobby-Project-Id` is the call's `project_id`, else the caller project.
    `X-Gobby-Caller-Project-Id` is the cwd `.gobby/project.json` id. It is
    re-read while absent, because a proxy can outlive `gobby init`.
  - `X-Gobby-Session-Id` is `GOBBY_SESSION_ID` when set. Otherwise it is the
    body `session_id`, but only when there is no `tmux_pane`. Otherwise
    `X-Gobby-Terminal-Context` carries the compact terminal-context JSON.
    `X-Gobby-Agent-Run-Id` is `GOBBY_AGENT_RUN_ID` when set.
  - Auth comes from `src/gobby/utils/local_token.py::daemon_auth_headers`. A
    non-empty `GOBBY_AGENT_API_TOKEN` gives a Bearer plus the
    `_AGENT_IDENTITY_ENV_HEADERS` pairs (session, caller project, agent run,
    managed execution). Otherwise the local credential gives a Bearer. Auth
    headers are merged last, so they win.
  - A 401 re-reads auth and retries once. A 200 returns the JSON body. A 409
    whose `detail.error_code` is `SESSION_REQUIRED` returns `detail`. Any other
    status gives `{"success": false, "error": "HTTP <n>: <body>"}`. A connect
    error gives `DAEMON_UNAVAILABLE`. A timeout gives `REQUEST_TIMEOUT`, with a
    retry hint for `/tools/spawn_agent`
    (`src/gobby/mcp_proxy/stdio_results.py`).
  - Preflight is `GET /api/health` with a 2 s timeout, cached for 5 s after a
    success. The default timeout is 30 s.
- The front door's native `/api/health` answers the typed 503 while Python is
  down (`crates/gdaemon/src/front_door/health.rs::HealthFamily`). Proxied routes
  answer the same body (`unavailable`).
- gcore reuse:
  - `crates/gcore/src/daemon_url.rs::daemon_url` is the shared resolver:
    `GOBBY_DAEMON_URL`, then `GOBBY_PORT`, then `bootstrap.yaml`.
  - `crates/gcore/src/local_token.rs::read_local_cli_token` prefers
    `GOBBY_AGENT_API_TOKEN`. #23519 rewrites it to the bootstrap key, and `gmcp`
    follows without edits.
  - `crates/gcore/src/project.rs::find_project_root` and `read_project_id`.
  - gcore's default features exclude postgres. `ureq` 2 is already its HTTP
    client, and `ghook` uses ureq deliberately so that tiny binaries stay off
    reqwest.
- Sandboxed agents cannot read `bootstrap.yaml`, which is a credential root.
  Spawns export `GOBBY_DAEMON_URL` (`src/gobby/agents/constants.py`, and the
  Codex `env_vars` forwarding in `spawn_executor_support.py`), which `daemon_url`
  reads first.
- `src/gobby/mcp_proxy/terminal_context.py::current_terminal_context` collects:
  - `parent_pid`, `tmux_pane`, and the `TMUX` socket path (before the first
    comma);
  - `query_tmux_generation` (`#{pid}\t#{start_time}\t#{window_id}\t#{session_name}`),
    falling back to `query_tmux_identity`
    (`src/gobby/sessions/tmux_context.py`);
  - then, only for keys not yet set, the environment fallbacks `TMUX_SESSION`,
    `TTY`, `TERM_PROGRAM`, `TERM_SESSION_ID`, `GOBBY_TERMINAL_ID`, and
    `GOBBY_PANE_REF`.
  - `serialize_terminal_context` keeps `TERMINAL_CONTEXT_KEYS` and drops nulls.
    The server parses the JSON, so key order is not load-bearing.
- `src/gobby/mcp_proxy/wait_tools.py` defines the server-coupled constants:
  protocol version and header, `WAIT_TOOL_NAMES`, `EXTENDED_TIMEOUT_TOOL_NAMES`,
  the 300 s ceilings, the 30 s HTTP buffer, the 5 s grace, and the 15 s
  heartbeat. The module stays, because server modules import it.
- The workspace is edition 2024 with `rust-version` 1.88. Its members are listed
  in the root `Cargo.toml`. `crates/ghook/Cargo.toml` is the manifest template
  (license `FSL-1.1-ALv2`, authors, repository).

**Implementation:**
- Root `Cargo.toml` adds `crates/gmcp` to `members`. `crates/gmcp/Cargo.toml`
  declares package `gobby-mcp` 0.1.0 and a `[lib]`. Its dependencies are
  `anyhow`, `gobby-core` (path, version 0.10.0, default features), `serde`,
  `serde_json`, and `ureq` 2 with `json`. `README.md` states what `gmcp` is and
  how CLIs launch it.
- `contract.rs` exposes `WrapperContract`, deserialized once
  (`std::sync::LazyLock`) from
  `include_str!("../contracts/mcp_wrapper.v1.json")`. The JSON holds every
  value Decision 9 lists, with the exact Python values.
- `terminal_context.rs`:
  - `capture() -> serde_json::Map` mirrors `current_terminal_context`.
    `parent_pid` comes from `std::os::unix::process::parent_id` and is absent
    off Unix.
  - The two tmux queries run `tmux -S <socket> display-message -p -t <pane> …`
    with the 500 ms timeout ghook uses, and validate fields as `tmux_context.py`
    does.
  - `serialize()` keeps contract keys and drops nulls.
  - The tmux runner is a function parameter, so unit tests inject outputs.
- `daemon.rs` exposes `Dialer`, which is constructed once per process:
  - It holds the cached auth headers, the cached caller project id, the
    `GOBBY_SESSION_ID` value, the serialized terminal context, a
    `has_tmux_pane` flag, and the last preflight success time.
  - `Dialer::request(method, path, body, RequestOptions) -> serde_json::Value`
    implements every rule in the research context, in the same order. The base
    URL is resolved with `daemon_url()` on every request, so a port change
    across a restart is followed.
  - A connection failure gives `DAEMON_UNAVAILABLE` with detail
    `connection failed`. A 503 whose JSON body has `status == "unavailable"`
    gives `DAEMON_UNAVAILABLE` with detail `backend <state>` (Decision 13).
  - The error text is
    `Gobby daemon HTTP control plane is unavailable at <base URL>: <detail>. Start it with `gobby start`, or check `gobby status`.`
  - A read timeout gives `REQUEST_TIMEOUT` with the wrapper's text and the
    spawn-agent hint.
  - `Dialer` never starts a daemon.
  - Calls block on ureq. 1.2 runs them on tokio's blocking pool.
  - Request identity and cached headers are copied under a short lock. No
    `Dialer` lock is held across preflight or HTTP I/O. The Python wrapper
    has no global request lock either.
- `tests/stub_daemon/mod.rs` is a `std::net::TcpListener` stub. It records each
  request's method, path, headers, and body, replays scripted responses, and can
  close and re-bind the same port. `crates/gclient/tests/mock_daemon/mod.rs` is
  the pattern.
- `tests/mcp_proxy/test_gmcp_contracts.py` loads the contract JSON and asserts
  equality with `wait_tools` constants, `TERMINAL_CONTEXT_KEYS`, and
  `local_token._AGENT_IDENTITY_ENV_HEADERS`.

**Granularity:** one leaf. Twelve new files make up one lifecycle owner, the
dialer, and its tests. Seven acceptance items each cover a rule of that one
request path.

**Focused verification (planned):** `cargo nextest run -p gobby-mcp` and
`cargo clippy -p gobby-mcp --all-targets -- -D warnings` (heavy work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_gmcp_contracts.py -q`.

**Acceptance:**

- 1.1.1 - Requests carry the protocol header `1`, and the target and caller project headers follow the wrapper's rules. The session header comes from `GOBBY_SESSION_ID` before the body, the body value is used only without a tmux pane, and otherwise the terminal-context header is sent. With an agent token, the identity headers override. test: `crates/gmcp/tests/dialer.rs::request_headers_match_python_wrapper`.
- 1.1.2 - A 401 re-reads the credential and retries exactly once. test: `crates/gmcp/tests/dialer.rs::unauthorized_rereads_credential_and_retries_once`.
- 1.1.3 - A 409 `SESSION_REQUIRED` returns its `detail`. Other non-200 answers return `HTTP <n>: <body>`. test: `crates/gmcp/tests/dialer.rs::conflict_and_error_statuses_match_wrapper`.
- 1.1.4 - A refused connection and the front door's typed 503 both return `DAEMON_UNAVAILABLE` naming `gobby start`. A read timeout returns `REQUEST_TIMEOUT` with the spawn-agent hint on `/tools/spawn_agent`. test: `crates/gmcp/tests/dialer.rs::unavailable_and_timeout_results_are_typed`.
- 1.1.5 - One `Dialer` returns `DAEMON_UNAVAILABLE` while the stub is down and succeeds after the stub re-binds the same port, with no new `Dialer`. Preflight is cached for 5 s only after a success. test: `crates/gmcp/tests/dialer.rs::dialer_survives_daemon_restart`.
- 1.1.6 - Terminal context prefers the generation query, falls back to window identity, applies the environment fallbacks only to unset keys, and serializes only the twelve contract keys without nulls. test: `crates/gmcp/src/terminal_context.rs::capture_matches_python_wrapper_rules`.
- 1.1.7 - The wrapper contract equals the Python constants it mirrors. test: `tests/mcp_proxy/test_gmcp_contracts.py::test_wrapper_contract_matches_python_constants`.

### 1.2 `gmcp` serves the wrapper's tool surface over stdio [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gmcp/Cargo.toml`
- `crates/gmcp/src/lib.rs`
- `crates/gmcp/src/main.rs`
- `crates/gmcp/src/server.rs`
- `crates/gmcp/src/tools.rs`
- `crates/gmcp/assets/progressive-discovery.md`
- `crates/gmcp/contracts/tools_list.v1.json`
- `crates/gmcp/contracts/wire_cases.v1.json`
- `crates/gmcp/tests/server.rs`
- `tests/mcp_proxy/test_gmcp_parity.py`
- `tests/mcp_proxy/test_gmcp_contracts.py`

**Research context:**
- `src/gobby/mcp_proxy/stdio_server.py::create_stdio_mcp_server` builds
  `_StdioMCPServer` with `build_gobby_instructions()`, and construction does no
  daemon or hub I/O (memory 728c2ca0).
  - `_StdioMCPServer.list_tools` strips `None` from input schemas and sets
    `tools_listed`.
  - `_report_ready_once_listed` then runs `DaemonProxy.report_bridge_ready`
    once. That posts `/api/mcp/bridge/ready` after delays of 0, 0.5, 1, 2, 4, 8,
    15, 30, and 30 s. It retries on `SESSION_REQUIRED`, `DAEMON_UNAVAILABLE`,
    and `REQUEST_TIMEOUT`. A final `SESSION_REQUIRED` logs at debug, and other
    failures log a warning.
  - Agent-token sessions are refused on that route until #23531 lands
    (Decision 6).
- `src/gobby/mcp_proxy/stdio_tools.py::register_proxy_tools` defines twelve
  tools. Apart from `call_tool` (1.3), each maps to one `DaemonProxy` method and
  one route:
  - `list_mcp_servers`: `GET /api/mcp/servers`.
  - `list_tools`: `GET /api/mcp/{server}/tools`, with an `intent` query capped
    at 1,024 characters.
  - `get_tool_schema`: `POST /api/mcp/tools/schema`.
  - `recommend_tools` and `search_tools`: `POST /api/mcp/tools/recommend` and
    `POST /api/mcp/tools/search`.
  - `add_mcp_server`, `remove_mcp_server`, and `import_mcp_server`:
    `POST /api/mcp/servers`, `DELETE /api/mcp/servers/{name}`, and
    `POST /api/mcp/servers/import`.
  - `set_variable` and `get_variable`:
    `POST /api/sessions/{id}/variables/set|get`.
  - `init_project`: a static error with no request.
  - Each method's body, query, and `preflight` flag are in `stdio_proxy.py`
    (lines 335 to 619); the executor copies them exactly.
- `src/gobby/mcp_proxy/instructions.py::build_gobby_instructions` reads the
  bundled `progressive-discovery.md` with its front matter stripped. It stays,
  because `server.py` uses it.
- The wrapper returns each tool's dict through the Python MCP SDK. The
  call-result envelope (content blocks and structured content) is part of what
  clients see, so the parity capture includes it.
- rmcp 3.5.0 provides `ServerHandler`, the `transport-io` stdio transport, and
  progress notifications through the request context's peer. tokio 1.50 is
  already in `Cargo.lock`.

**Implementation:**
- `Cargo.toml` adds `rmcp = { version = "3.5", default-features = false, features = ["server", "transport-io"] }`,
  `tokio` (`rt`, `macros`, `io-std`, `time`, `sync`), and `clap` (derive, as
  `ghook` uses). It also declares `[[bin]] name = "gmcp"`.
- `main.rs` handles `--version` (`gmcp <version>`) and `--help`. Otherwise it
  builds a current-thread tokio runtime that it owns, builds one `Dialer`,
  serves `GobbyServer` over stdio, and exits 0 when stdin closes.
  - When stdin closes, it stops the service and the readiness and heartbeat
    tasks, then calls `Runtime::shutdown_background()`. Dropping a tokio
    runtime waits for `spawn_blocking` work, and a guarded wait request can
    run for 330 s.
  - While stdin stays open, 1.3's background-request behavior is unchanged.
- `server.rs` implements `GobbyServer` as rmcp `ServerHandler`:
  - `get_info` reports the Python wrapper's server name, the crate version, the
    tools capability, and the instructions: the embedded asset with its front
    matter stripped as `instructions.py` does.
  - `list_tools` returns the tools deserialized from the fixture. After the
    first answer it starts the bridge-ready reporter once per process, with the
    same schedule, retry codes, and stderr logging.
  - `call_tool` dispatches by name to `tools.rs`. An unknown name returns an MCP
    error.
- `tools.rs` builds each request exactly as the matching `DaemonProxy` method
  does, runs `Dialer::request` on `tokio::task::spawn_blocking`, and wraps the
  JSON result in the envelope the capture recorded.
- Capture: `tests/mcp_proxy/test_gmcp_parity.py` holds `_capture()`. It drives
  the Python wrapper in process (`create_stdio_mcp_server` plus
  `register_proxy_tools` over a scripted `httpx.MockTransport`).
  - It writes the exact stripped `tools/list` result to `tools_list.v1.json`.
  - It writes the `initialize` result and the call-result envelopes for one
    success, one `DAEMON_UNAVAILABLE`, and `init_project` to
    `wire_cases.v1.json`.
  - The executor writes both fixtures once from `_capture()`, and the test
    asserts the wrapper still equals them.
- Rust subprocess tests in `crates/gmcp/tests/server.rs` compare `gmcp`'s MCP
  wire responses against both fixtures. They ignore JSON object order and
  normalize only the server version. Content blocks, `structuredContent`,
  `isError`, capabilities, instructions, and schema fields must match. 1.7
  deletes the Python capture test; both fixtures and the Rust assertions
  stay.

**Granularity:** one leaf. The MCP server and its eleven REST tools share one
dispatcher and one fixture, so they are one testable unit.

**Focused verification (planned):** `cargo nextest run -p gobby-mcp` (heavy
work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_gmcp_parity.py tests/mcp_proxy/test_gmcp_contracts.py -q`.

**Acceptance:**

- 1.2.1 - `initialize` succeeds with the stub daemon closed and its listener never touched, and returns the embedded instructions. test: `crates/gmcp/tests/server.rs::initialize_does_no_io`.
- 1.2.2 - `gmcp`'s `tools/list` equals `tools_list.v1.json`, and both fixtures equal the Python wrapper's capture. test: `tests/mcp_proxy/test_gmcp_parity.py::test_fixture_matches_python_wrapper` and `crates/gmcp/tests/server.rs::tools_list_matches_fixture`.
- 1.2.3 - Each of the eleven non-`call_tool` tools sends the wrapper's method, path, query, and body. `gmcp`'s `initialize` result and call-result envelopes equal `wire_cases.v1.json`. `init_project` makes no request. test: `crates/gmcp/tests/server.rs::tools_forward_like_python_wrapper` and `crates/gmcp/tests/server.rs::wire_results_match_fixture`.
- 1.2.4 - After the first `tools/list`, readiness is posted once. `SESSION_REQUIRED`, `DAEMON_UNAVAILABLE`, and `REQUEST_TIMEOUT` are retried on the wrapper's schedule, and other failures are not. test: `crates/gmcp/tests/server.rs::bridge_ready_follows_wrapper_schedule`.
- 1.2.5 - The embedded instructions equal the bundled prompt byte for byte. test: `tests/mcp_proxy/test_gmcp_contracts.py::test_instructions_copy_matches_bundled_prompt`.
- 1.2.6 - `gmcp --version` prints the crate version. Closing stdin exits 0 within a 2 s deadline, both when idle and while the stub holds a wait request's response open. The child exits before the held response is released. test: `crates/gmcp/tests/server.rs::binary_version_and_clean_exit` and `crates/gmcp/tests/server.rs::stdin_close_exits_with_outstanding_wait`.

### 1.3 `call_tool` keeps the wrapper's canonicalization, wait guard, and heartbeat [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gmcp/src/lib.rs`
- `crates/gmcp/src/server.rs`
- `crates/gmcp/src/canonical.rs`
- `crates/gmcp/src/call_tool.rs`
- `crates/gmcp/contracts/call_tool_wrapper_cases.v1.json`
- `crates/gmcp/tests/call_tool.rs`
- `tests/mcp_proxy/test_gmcp_contracts.py`

**Research context:**
- `stdio_tools.py`'s `call_tool` runs these steps in order:
  1. `_call_tool_wrapper.py::canonicalize_call_tool_wrapper`. This unwraps
     nested wrappers, decodes JSON-string `arguments`/`args`, rejects ambiguity
     with `CallToolWrapperInputError`, and returns
     `{"success": false, "error": str(exc)}`.
  2. Missing `server_name` or `tool_name` gives
     `Missing required parameters: server_name, tool_name`.
  3. `wait_tools.prepare_client_guard` clamps a wait tool's `timeout` or
     `timeout_seconds` to 300 s. Extended tools get 300 s.
  4. `call_with_wait_heartbeat` sends a progress notification every 15 s for
     heartbeat tools: progress is the elapsed time capped at the timeout, total
     is the timeout, and the message is `<tool> still waiting for daemon result`.
     It is sent only when the client supplied a progress token.
  5. `_await_with_guard` returns `_wrapper_timeout_result` after timeout + 5 s,
     and the background call continues.
  6. When capped, it adds `requested_timeout_seconds`,
     `effective_timeout_seconds`, and `wait_timeout_capped_by_mcp_wrapper`.
- `DaemonProxy.call_tool`:
  - It uses 30 s by default, 300 s for extended tools, and for wait tools the
    argument timeout plus 30 s. A non-finite or non-positive wait timeout
    returns `Invalid wait timeout: <raw>`.
  - Wait tools post `{server_name, tool_name, arguments, intent?}` to
    `/api/mcp/tools/call`. Others post the arguments to
    `/api/mcp/{server}/tools/{tool}`, with `intent` as a query capped at 1,024
    characters.
  - Preflight follows `preflight_enabled`.
- `_call_tool_wrapper.py` stays: `server.py`, `hooks/_normalization_mcp.py`,
  `workflows/engine/*`, and `sessions/transcripts/tool_activity.py` import it.
  `tests/mcp_proxy/test_call_tool_wrapper.py` holds its cases.

**Implementation:**
- `canonical.rs` ports `canonicalize_call_tool_wrapper` and its error texts.
  `call_tool_wrapper_cases.v1.json` lists input/output cases, including every
  case in `test_call_tool_wrapper.py`. A Rust test and a Python test each run
  all of them.
- `call_tool.rs` runs the six steps above, with constants from
  `WrapperContract`. The heartbeat is a tokio interval that runs beside the
  blocking request and stops when the request finishes. The guard timeout
  returns the wrapper-timeout result and lets the blocking request finish in
  the background.
- `server.rs` routes `call_tool` here.

**Focused verification (planned):** `cargo nextest run -p gobby-mcp` (heavy
work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_gmcp_contracts.py tests/mcp_proxy/test_call_tool_wrapper.py -q`.

**Acceptance:**

- 1.3.1 - Every shared canonicalization case gives the same result in Rust and in Python. test: `crates/gmcp/tests/call_tool.rs::canonicalization_matches_shared_cases` and `tests/mcp_proxy/test_gmcp_contracts.py::test_call_tool_cases_match_python_canonicalizer`.
- 1.3.2 - A wait tool asking for 900 s is sent with 300 s, through `/api/mcp/tools/call`, with an HTTP timeout of 330 s, and the result carries the three capped-timeout fields. test: `crates/gmcp/tests/call_tool.rs::wait_tool_timeout_is_capped_with_metadata`.
- 1.3.3 - A heartbeat tool that blocks past 15 s emits progress with the wrapper's fields when a progress token is present, and none without one. test: `crates/gmcp/tests/call_tool.rs::heartbeat_reports_progress_while_waiting`.
- 1.3.4 - Past timeout + 5 s, the call returns the wrapper-timeout result while the request continues. test: `crates/gmcp/tests/call_tool.rs::guard_timeout_returns_background_result`.
- 1.3.5 - Ordinary tools post to `/api/mcp/{server}/tools/{tool}` with the intent query and a 30 s timeout, and extended tools use 300 s. test: `crates/gmcp/tests/call_tool.rs::ordinary_and_extended_tools_use_wrapper_routes`.
- 1.3.6 - While the stub holds a wait request's response, one `gmcp` process completes a concurrent ordinary call with the correct request id and identity. Releasing the held response then completes the wait. test: `crates/gmcp/tests/call_tool.rs::ordinary_call_completes_during_wait`.

### 1.4 `gobby install` builds, promotes, and tracks `gmcp` [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_setup_gmcp.py`
- `src/gobby/cli/install_setup.py::*` — scope-reason: `MANAGED_NATIVE_BINARY_NAMES`, its descriptions, `_run_managed_native_binary_installs`, and the `_install_gmcp*` delegates beside the gclient ones
- `src/gobby/cli/install_components.py::*` — scope-reason: the `gmcp` component, its label, `_workspace_client_version`, `promote_client_binary`, and the `run_install_components` branch
- `src/gobby/cli/install.py::*` — scope-reason: the components list in the install help text
- `src/gobby/install/bin_freshness_promotion.py::*` — scope-reason: `WORKSPACE_BINARY_CRATES` gains `gmcp` with `gcore`
- `src/gobby/install/version_pins.py::*` — scope-reason: pin `gmcp` 0.1.0 and list it in `UNPUBLISHED_MANAGED_BINS`
- `.github/workflows/release-gmcp.yml`
- `.github/workflows/rust-ci.yml`
- `tests/cli/test_install_setup_gmcp.py`
- `tests/cli/test_install_setup.py::*` — scope-reason: the managed-binary install cases patch `_install_gmcp` beside `_install_gclient`
- `tests/cli/test_install_components.py::*` — scope-reason: adds the `gmcp` component promotion case
- `tests/install/test_bin_freshness.py::*` — scope-reason: adds the `gmcp` crate-set case
- `tests/install/test_version_pins.py::*` — scope-reason: asserts the `gmcp` pin and unpublished status

**Research context:**
- `src/gobby/cli/install_setup_gclient.py` (341 lines) is the per-binary
  installer pattern:
  - it builds from the checkout (`install_gclient_from_submodule`), takes the
    native-bin lock, replaces the binary through a new inode, and signs it;
  - it writes a source hash and a version stamp;
  - outside a checkout, an unpublished binary raises
    `ManagedBinaryReleaseMissing`.
- `install_setup.py` (615 lines) runs every `MANAGED_NATIVE_BINARY_NAMES` entry
  on a default `gobby install`. `install_components.py` (367 lines) lets
  `gobby install gclient` re-promote one binary without claiming the daemon
  singleton.
- `bin_freshness_promotion.py::WORKSPACE_BINARY_CRATES` drives the
  "installed binary predates source" warning. `version_pins.py` holds the pins
  and `UNPUBLISHED_MANAGED_BINS`.
- `promote_workspace_binary_set` and `gobby cutover`
  (`src/gobby/cli/cutover.py::_BINARY_NAMES`) cover only the stamped set gcode,
  gdaemon, and ghook (Decision 15).
- `.github/workflows/release-gclient.yml` checks the tag against the crate
  version, runs clippy and nextest, asserts the packaged file list, publishes to
  crates.io, and uploads macOS/Linux tarballs. `rust-ci.yml` has per-crate
  clippy and nextest steps, and path filters that name each release workflow.
- `distribution.py::HOMEBREW_HELPERS` needs a tap formula outside this
  repository, which is deferred to D5.

**Implementation:**
- `install_setup_gmcp.py` mirrors `install_setup_gclient.py` for crate dir
  `gmcp`, package `gobby-mcp`, binary `gmcp`, and stamp `.gmcp-version`. It
  provides the workspace build path and the unpublished guard. The
  GitHub-release and cargo paths follow gclient's shape, so D5 only flips the
  published flag.
- `install_setup.py` adds `gmcp` ("MCP stdio client") to
  `MANAGED_NATIVE_BINARY_NAMES` and its installer map, with `_install_gmcp`
  delegates.
- `install_components.py` adds the `gmcp` component, promoted through
  `promote_client_binary`. `install.py`'s help lists it.
- `WORKSPACE_BINARY_CRATES["gmcp"] = ("gmcp", "gcore")`, and `version_pins.py`
  pins `gmcp` 0.1.0 as unpublished.
- `release-gmcp.yml` mirrors `release-gclient.yml` with `gmcp-v*` tags and
  requires `contracts/*.json` and `assets/progressive-discovery.md` in the
  package list. `rust-ci.yml` gains the `gobby-mcp` clippy and nextest steps
  and the workflow path filter.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_setup_gmcp.py tests/cli/test_install_setup.py tests/cli/test_install_components.py tests/install/test_bin_freshness.py tests/install/test_version_pins.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

**Acceptance:**

- 1.4.1 - In a checkout, the gmcp installer builds `gobby-mcp`, promotes `gmcp` through a new inode with the lock held, and writes the version stamp and source hash. Outside a checkout it raises `ManagedBinaryReleaseMissing`. test: `tests/cli/test_install_setup_gmcp.py::test_submodule_install_promotes_and_stamps`.
- 1.4.2 - A default install runs the `gmcp` installer with the other managed binaries. test: `tests/cli/test_install_setup.py::test_managed_native_binaries_include_gmcp`.
- 1.4.3 - `gobby install gmcp` promotes only `gmcp` and does not claim the daemon singleton. test: `tests/cli/test_install_components.py::test_gmcp_component_promotes_client_binary`.
- 1.4.4 - Freshness treats a `gcore` or `gmcp` source change as making `gmcp` stale, and the pins mark it unpublished at 0.1.0. test: `tests/install/test_bin_freshness.py::test_gmcp_tracks_gmcp_and_gcore`.
- 1.4.5 - The release workflow publishes on `gmcp-v*` tags and fails when the package list lacks the contract or asset files. behavior: "gmcp-v" in `.github/workflows/release-gmcp.yml`.

### 1.5 E2e and server tests drive `gmcp` or the routes, not the wrapper [category: test] (depends: 1.3)
`kind: deliverable`

Targets:
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: `_new_proxy` and the four proxy tests drive a `gmcp` subprocess through the MCP SDK stdio client instead of `DaemonProxy`
- `tests/e2e/gmcp_client.py`
- `tests/servers/routes/mcp_endpoints/test_registry_routes.py::*` — scope-reason: `test_stdio_server_listing_preserves_scoped_template_catalog` asserts through the route instead of `DaemonProxy`
- `tests/servers/routes/mcp_endpoints/test_template_routes.py::*` — scope-reason: the disabled-template case asserts the route's answer instead of `DaemonProxy` and `_capture_stdio_tools`
- `tests/servers/test_mcp_execution_context.py::*` — scope-reason: the stdio half sends the wrapper's headers with the test client instead of `DaemonProxy`
- `tests/skills/reference_library_helpers.py::*` — scope-reason: the stdio tool inventory comes from `crates/gmcp/contracts/tools_list.v1.json` instead of `register_proxy_tools`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: `test_scrubbed_child_env_reaches_daemon_proxy_identity` asserts the scrubbed child environment carries the session, daemon URL, and capability that `gmcp` reads, without `DaemonProxy`

**Research context:**
- `tests/e2e/test_stateless_ambient_session.py` (548 lines) covers, through
  `DaemonProxy` against an isolated daemon: the ambient session following
  `/clear`, the schema lease bound to the child identity, hookless terminals
  requiring a session, and the same proxy resolving its session after a daemon
  restart. These are the server half of the restart-survival contract, and
  they must keep running.
- `tests/e2e/test_runtime_boundary.py::_native_bin` resolves `target/debug/<name>`,
  then `resolve_native_bin`, and otherwise fails. It never skips. The Python
  `mcp` SDK, which the repository already depends on, provides
  `mcp.client.stdio.stdio_client`.
- Four server tests and the reference inventory import `stdio_proxy` or
  `stdio_tools`, but they test routes and inventories, not the wrapper.

**Implementation:**
- `tests/e2e/gmcp_client.py` resolves `gmcp` as `_native_bin` does and opens an
  MCP stdio session with an explicit environment: the isolated daemon URL, the
  test home, and the session or terminal identity under test.
- The e2e file's proxy helper becomes that session, and assertions read
  `call_tool` results. The restart test keeps one `gmcp` process across the
  isolated daemon's stop and start. It asserts `DAEMON_UNAVAILABLE` while the
  daemon is down and success afterwards.
- The route tests assert the same outcomes through the ASGI test client. The
  reference helper reads tool names from the fixture.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_stateless_ambient_session.py tests/servers/routes/mcp_endpoints/test_registry_routes.py tests/servers/routes/mcp_endpoints/test_template_routes.py tests/servers/test_mcp_execution_context.py tests/skills/ tests/agents/test_spawn_executor.py -q`,
with `gmcp` built by `cargo build -p gobby-mcp` (heavy work).

**Acceptance:**

- 1.5.1 - Through `gmcp`, the ambient session follows `/clear`, the schema lease binds to the child identity, and a hookless terminal gets `SESSION_REQUIRED`. test: `tests/e2e/test_stateless_ambient_session.py::test_ambient_proxy_follows_clear_and_attributes_schema_lease`.
- 1.5.2 - One `gmcp` process answers `DAEMON_UNAVAILABLE` while the isolated daemon is stopped, and resolves the same session after it restarts. test: `tests/e2e/test_stateless_ambient_session.py::test_same_proxy_resolves_existing_session_after_daemon_restart`.
- 1.5.3 - The route tests, the execution-context test, and the reference inventory pass with no import of `gobby.mcp_proxy.stdio*`. test: `tests/servers/test_mcp_execution_context.py::test_stdio_and_http_share_trusted_agent_identity`.
- 1.5.4 - The scrubbed Codex child environment carries `GOBBY_SESSION_ID`, `GOBBY_DAEMON_URL`, and the capability token. test: `tests/agents/test_spawn_executor.py::test_scrubbed_child_env_reaches_daemon_proxy_identity`.

### 1.6 Every launch site starts `~/.gobby/bin/gmcp` [category: code] (depends: 1.3, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/mcp_config_shared.py::*` — scope-reason: `_GOBBY_MCP_COMMAND`/`_GOBBY_MCP_ARGS` become `gmcp` with no arguments, `_resolved_gobby_mcp_command` returns the managed `gmcp` path, and the stale-entry repair also rewrites `gobby mcp-server` entries
- `src/gobby/cli/installers/mcp_config_toml.py::*` — scope-reason: the Codex block writes `args = []`
- `src/gobby/cli/installers/mcp_config_json.py::*` — scope-reason: the repair and fresh-entry paths write `gmcp` with no arguments
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: `_codex_mcp_config_overrides` sets the managed `gmcp` command with empty arguments instead of `uv run`
- `src/gobby/agents/isolation_repair.py::*` — scope-reason: `_patch_mcp_config_for_isolation` writes the `gmcp` entry, and `provider_mcp_config_error` accepts exactly that entry
- `src/gobby/servers/chat_session_helpers.py::*` — scope-reason: `_build_gobby_mcp_entry` returns the managed `gmcp` path with no arguments
- `src/gobby/agents/constants.py::*` — scope-reason: the `MCP_CONNECT_TIMEOUT_MS` and `MCP_TIMEOUT` comments describe `gmcp`; the values stay
- `src/gobby/utils/local_token.py::*` — scope-reason: the docstring that names `gobby mcp-server` names `gmcp`
- `src/gobby/install/shared/skills/gobby/references/admin/clients.md`
- `tests/e2e/composer_proof_setup.py::*` — scope-reason: `main` points the proof's MCP entry at the checkout's built `gmcp` and refuses when it is absent
- `tests/cli/installers/test_cli_installers_mcp_config.py::*` — scope-reason: installer and repair expectations move to `gmcp`
- `tests/cli/installers/test_codex_installer.py::*` — scope-reason: TOML expectations move to `gmcp`
- `tests/cli/installers/test_shared.py::*` — scope-reason: settings expectations move to `gmcp`
- `tests/cli/installers/test_cli_installers_agy.py::*` — scope-reason: the args expectation becomes empty
- `tests/cli/installers/test_cli_installers_droid.py::*` — scope-reason: the args expectation becomes empty
- `tests/agents/spawners/test_command_builder.py::*` — scope-reason: the Codex override expectations move to `gmcp`
- `tests/agents/test_spawn_executor_support.py::*` — scope-reason: the override expectation moves to `gmcp`
- `tests/agents/test_isolation.py::*` — scope-reason: the isolation config and preflight expectations move to `gmcp`
- `tests/agents/test_local_context_setup.py::*` — scope-reason: the mocked entry becomes `gmcp`

**Research context:**
- `mcp_config_shared.py` (221 lines) resolves `gobby` next to `sys.executable`,
  then `which`. `_is_repairable_stale_gobby_mcp_server_config` repairs only
  `uv run [--directory X] gobby mcp-server`. The TOML (203 lines) and JSON (420
  lines) writers share those helpers. Codex keeps `required = true`, the 120 s
  startup, the 360 s tool timeout, and `default_tools_approval_mode`.
- `spawn_executor_support.py::_codex_mcp_config_overrides` (599-line file)
  runs `uv run --no-sync --project <main repo> gobby mcp-server`. That way an
  isolated worktree's old code never serves the proxy (memory ce6ece28). A
  native client has no per-worktree code, so the managed binary serves every
  workspace. The overrides keep `required`, the timeouts, the env forwarding,
  and the approval seeding.
- `isolation_repair.py` (325 lines) writes `.mcp.json` for isolated agents.
  `provider_mcp_config_error` currently requires `command == "uv"` and
  `mcp-server` in `args`.
- `chat_session_helpers.py::_build_gobby_mcp_entry` builds the chat SDK's
  stdio entry.
- The managed path is `get_gobby_home() / "bin" / native_bin_name("gmcp")`
  (`src/gobby/utils/native_bin.py`). Sandboxed CLIs already execute `ghook`
  from that directory.
- The sandbox read grant for the `uv run --project` entry stays until 1.8
  (Decision 16).
- Test literals that are sample commands rather than launch-site expectations
  stay: the memory watchdog command line, the `.mcp.json` samples in the
  resume and spawn timeout tests, and `npx` server names.

**Implementation:**
- One helper, `gmcp_command()`, in `mcp_config_shared.py` returns the managed
  path. Every site above writes `{"command": gmcp_command(), "args": []}` (or
  the TOML and `-c` equivalents).
- The stale-entry repair rewrites two shapes to that entry: today's
  `uv run [--directory X] gobby mcp-server`, and a `gobby` command, bare or at
  any path, with `["mcp-server"]`.
- `provider_mcp_config_error` requires the `gmcp` entry.
- `clients.md` describes `gmcp` as the stdio transport. `gmcp` does not start
  the daemon; it reports `DAEMON_UNAVAILABLE` with `gobby start` guidance.
- **Landing gate.** The Merge Manager lands this leaf only after D1 confirms
  that `~/.gobby/bin/gmcp` is promoted on the hub.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/ tests/agents/spawners/test_command_builder.py tests/agents/test_spawn_executor_support.py tests/agents/test_isolation.py tests/agents/test_local_context_setup.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

Consumers unchanged:
- `tests/agents/test_memory_watchdog.py` — no-edit-reason: its `uv run gobby mcp-server` command line is sample process data.
- `tests/agents/test_resume_executor.py` — no-edit-reason: its `.mcp.json` sample only feeds the timeout environment assertions.

**Acceptance:**

- 1.6.1 - Fresh JSON and TOML installs write the managed `gmcp` path with no arguments, and Codex keeps `required`, both timeouts, and the approval mode. test: `tests/cli/installers/test_cli_installers_mcp_config.py::test_fresh_install_writes_gmcp_entry`.
- 1.6.2 - The repair rewrites `uv run [--directory X] gobby mcp-server` entries and `gobby mcp-server` entries (bare or at any path) to `gmcp`, and leaves other servers alone. test: `tests/cli/installers/test_cli_installers_mcp_config.py::test_repair_rewrites_wrapper_entries_to_gmcp`.
- 1.6.3 - Spawned Codex agents get `gmcp` through `-c` overrides with the same timeouts, environment forwarding, and approvals. test: `tests/agents/test_spawn_executor_support.py::test_codex_overrides_launch_gmcp`.
- 1.6.4 - Isolated agents get a `gmcp` `.mcp.json`, and the preflight accepts it and rejects a `uv` entry. test: `tests/agents/test_isolation.py::test_isolation_writes_and_accepts_gmcp_entry`.
- 1.6.5 - The chat SDK entry and the clients reference name `gmcp`. behavior: "gmcp" in `src/gobby/install/shared/skills/gobby/references/admin/clients.md`.

### 1.7 The Python wrapper and its setting are gone [category: code] (depends: 1.5, 1.6)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/stdio.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_server.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_proxy.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_tools.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_daemon.py::*` — operation: delete — scope-reason: replaced by `gmcp`, which never starts the daemon
- `src/gobby/mcp_proxy/stdio_results.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/daemon_control.py::*` — operation: delete — scope-reason: only the stdio modules consumed it (Decision 5)
- `src/gobby/cli/mcp.py::*` — operation: delete — scope-reason: `gobby mcp-server` is removed
- `src/gobby/mcp_proxy/terminal_context.py::*` — scope-reason: `current_terminal_context` and `serialize_terminal_context` leave; `TERMINAL_CONTEXT_KEYS` stays for `request_context.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: the `mcp-server` lazy-command entry leaves, and the lazy-map comment stops citing it
- `src/gobby/tasks/criterion_commands.py::*` — scope-reason: `mcp-server` leaves the CLI command-name list
- `src/gobby/config/servers.py::*` — scope-reason: `MCPClientProxyConfig.tool_timeouts` leaves (Decision 12)
- `src/gobby/config/registry.py::*` — scope-reason: the `mcp_client_proxy.tool_timeouts` mapping pattern leaves
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated without the `tool_timeouts` pattern
- `web/src/api/runtimeConfigCodecVectors.gen.ts::*` — scope-reason: regenerated with the contract
- `tests/contracts/http/config_schema.json::*` — scope-reason: the `/api/config/schema` snapshot drops `tool_timeouts`
- `docs/reference-audit/admin.json::*` — scope-reason: the `gobby mcp-server` row becomes a `gmcp` row
- `web/src/components/settings/sections/McpToolsSection.tsx::*` — scope-reason: the `tool_timeouts` path and its Per-tool timeouts editor leave
- `web/src/components/settings/sections/configFields.tsx::*` — scope-reason: `NumberMapConfigField` leaves; the `tool_timeouts` editor was its only consumer
- `docs/guides/configuration.md`
- `docs/guides/mcp-tools.md`
- `docs/guides/cli-commands.md`
- `docs/architecture/development-guide.md`
- `README.md`
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — operation: delete — scope-reason: tests the retired wrapper
- `tests/mcp_proxy/test_mcp_proxy_stdio_session_context.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.1.1 covers the header rules
- `tests/mcp_proxy/test_stateless_stdio_proxy.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.1 and 1.5 cover it
- `tests/mcp_proxy/test_stdio_config_runtime.py::*` — operation: delete — scope-reason: tests the retired wrapper
- `tests/mcp_proxy/test_stdio_proxy.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.2 and 1.3 cover the tools
- `tests/mcp_proxy/test_stdio_results.py::*` — operation: delete — scope-reason: tests the retired wrapper
- `tests/mcp_proxy/test_stdio_startup.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.2.1 covers no-I/O initialize
- `tests/mcp_proxy/test_gmcp_parity.py` — operation: delete
- `tests/mcp_proxy/test_mcp_server_factory.py::*` — scope-reason: the `create_stdio_mcp_server` case leaves; the HTTP factory cases stay
- `tests/mcp_proxy/test_mcp_proxy_terminal_context.py::*` — scope-reason: the cases for the removed functions leave; the key-set case stays
- `tests/cli/test_cli.py::*` — scope-reason: the `mcp-server` command and help assertions leave
- `tests/config/test_servers.py::*` — scope-reason: the `tool_timeouts` cases leave
- `tests/config/test_config_registry.py::*` — scope-reason: the `tool_timeouts` mapping pattern leaves the expected set
- `web/src/components/settings/sections/__tests__/McpToolsSection.test.tsx::*` — scope-reason: the `tool_timeouts` fixture and editor case leave, and a case asserts the editor is absent
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: the settings fixture drops `tool_timeouts`

**Research context:**
- Only `cli/mcp.py`, the stdio modules, and tests import the stdio modules
  and `daemon_control.py`. After 1.5, no server or e2e test does.
- `request_context.py` imports `TERMINAL_CONTEXT_KEYS`.
  `current_terminal_context` and `serialize_terminal_context` have no
  non-wrapper caller.
- `tool_timeouts` is read only by `DaemonProxy._get_tool_timeouts`.
- The web settings page edits it.
  `web/src/components/settings/sections/McpToolsSection.tsx` lists
  `mcp_client_proxy.tool_timeouts` in `PROXY_PATHS` and renders the "Per-tool
  timeouts (seconds)" `NumberMapConfigField`. That editor is the only consumer
  of `configFields.tsx::NumberMapConfigField`. `McpToolsSection.test.tsx`
  edits the field, and `web/tests/style-surfaces.spec.ts` seeds it.
  `scripts/generate_runtime_config_contract.py` emits both the gcore contract
  JSON and `web/src/api/runtimeConfigCodecVectors.gen.ts` from the registry.
  `tests/config/test_runtime_config_contract.py` checks both.
  `tests/contracts/http/config_schema.json` is the HTTP corpus snapshot of
  `/api/config/schema`. Semantic lint requires the gcore contract as a carrier
  of any `src/gobby/config/` change.
- `docs/reference-audit/admin.json` has the `gobby mcp-server` row
  (implementation `src/gobby/cli/mcp.py::mcp_server`), and native binaries have
  rows (`ghook --version`, `gclient …`). `tests/skills/` checks them.
- `src/gobby/mcp_proxy/wait_tools.py`, `instructions.py`, and
  `_call_tool_wrapper.py` stay (server importers listed in 1.1 and 1.3).

**Implementation:**
- Delete the eight modules and the seven wrapper test files. Remove
  `mcp-server` from `cli/__init__.py`'s lazy map and from `criterion_commands.py`.
  Trim `terminal_context.py` to `TERMINAL_CONTEXT_KEYS`.
- Remove `tool_timeouts` from `MCPClientProxyConfig` and from `registry.py`'s
  mapping patterns, then regenerate with
  `uv run python scripts/generate_runtime_config_contract.py` and refresh the
  HTTP config-schema snapshot.
- The MCP & Tools settings section drops the `tool_timeouts` path and editor,
  and `configFields.tsx` drops `NumberMapConfigField`. The unit test replaces
  the editor case with `omitsRetiredPerToolTimeoutsEditor`, and both web
  fixtures drop the key.
- `admin.json` replaces the `gobby mcp-server` row with a `gmcp` row:
  reference `clients.md`, implementation `crates/gmcp/src/main.rs::main`, and
  verification `public-cli-inventory`.
- The guides, the development guide, and `README.md` describe `gmcp`. The
  `configuration.md` example drops `tool_timeouts`.

**Granularity:** one leaf. The deletions, the setting, and the references are
one retirement. Splitting them would leave the wrapper importable after the
inventory forgets it, or the setting documented after its reader is gone.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/ tests/cli/test_cli.py tests/config/ tests/skills/ tests/contracts/test_http_corpus.py -q`,
then `npx vitest run src/components/settings/sections/__tests__/McpToolsSection.test.tsx`
and `npx tsc --noEmit` in `web/`, `uv run ruff check src/`, `uv run mypy src/`,
and `gcode grep -F "mcp_proxy.stdio" src tests` and
`gcode grep -F "tool_timeouts" src tests web` with no hits.

**Acceptance:**

- 1.7.1 - No module under `src/` or `tests/` imports the stdio modules or `daemon_control`, and the eight files are gone. test: `tests/cli/test_cli.py::test_mcp_server_command_is_absent`.
- 1.7.2 - `TERMINAL_CONTEXT_KEYS` still serves `request_context.py`, and the contract test still passes. test: `tests/mcp_proxy/test_gmcp_contracts.py::test_wrapper_contract_matches_python_constants`.
- 1.7.3 - `mcp_client_proxy.tool_timeouts` is absent from the config model, the registry, the gcore contract, the web vectors, and the HTTP schema snapshot. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.7.4 - The reference audit carries a `gmcp` row and no `gobby mcp-server` row. behavior: "gmcp" in `docs/reference-audit/admin.json`.
- 1.7.5 - The guides describe `gmcp` as the stdio transport. behavior: "gmcp" in `docs/guides/mcp-tools.md`.
- 1.7.6 - The MCP & Tools settings section renders no Per-tool timeouts editor, and `NumberMapConfigField` no longer exists. test: `web/src/components/settings/sections/__tests__/McpToolsSection.test.tsx::omitsRetiredPerToolTimeoutsEditor`.

### 1.8 The sandbox drops the wrapper's read grant [category: code] (depends: 1.6)
`kind: deliverable`

Targets:
- `src/gobby/agents/srt_settings.py`
- `src/gobby/agents/sandbox_read_roots.py`
- `src/gobby/agents/srt_runtime.py::*` — scope-reason: `render_srt_settings` moves to `srt_settings.py`, and `prepare_sandbox_launch` imports it from there
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: `gobby_read_exceptions` moves to `sandbox_read_roots.py`, and `mcp_config_read_exceptions` is deleted
- `src/gobby/agents/sandbox.py::*` — scope-reason: imports `gobby_read_exceptions` from `sandbox_read_roots.py` and drops the `mcp_config_read_exceptions` read root
- `tests/agents/test_srt_runtime.py::*` — scope-reason: imports `render_srt_settings` from `srt_settings`
- `tests/agents/test_srt_filesystem_integration.py::*` — scope-reason: imports `render_srt_settings` from `srt_settings`
- `tests/agents/test_external_write_grants.py::*` — scope-reason: imports `render_srt_settings` from `srt_settings`
- `tests/agents/test_sandbox.py::*` — scope-reason: the `uv run --project` read-root case becomes the `gmcp` no-read-root case

**Research context:**
- `src/gobby/agents/sandbox_policy.py::mcp_config_read_exceptions` reads the
  workspace `.mcp.json` and grants each absolute directory argument of the
  `gobby` entry as a read root, so that `uv run --project <main repo>` can open
  the main repo's `pyproject.toml` (#19097). `sandbox.py::compute_sandbox_paths`
  adds it to the read paths, and
  `tests/agents/test_sandbox.py::test_isolated_mcp_project_root_is_readable_but_not_writable`
  covers it. The grant must outlive the `uv run` entries, so this leaf follows
  1.6.
- `src/gobby/agents/srt_runtime.py::render_srt_settings` (lines 504 to 567)
  holds the loopback-egress comment that names `gobby mcp-server`. Its
  consumers are `prepare_sandbox_launch` and the three test files above.
- `src/gobby/agents/sandbox_policy.py::gobby_read_exceptions` (lines 335 to
  367) holds the `uv` comments about the wrapper subprocess. Its only consumer
  is `sandbox.py`. Its `uv` cache and binary read roots still serve agent shell
  `uv run` commands.
- `sandbox_policy.py` has 935 lines and `srt_runtime.py` has 867, so
  `production-size-growth` requires the moves (Decision 16). #23273 moves other
  symbols out of `sandbox_policy.py`; only import lines overlap.

**Implementation:**
- Move `render_srt_settings` from `srt_runtime.py` to the new
  `src/gobby/agents/srt_settings.py` unchanged, except that the loopback-egress
  comment names `ghook` and `gmcp`. `srt_runtime.py` imports it from there.
- Move `gobby_read_exceptions` from `sandbox_policy.py` to the new
  `src/gobby/agents/sandbox_read_roots.py`, importing `canonical_paths` from
  `sandbox_policy.py`. Its read roots stay. Its comments describe `uv run` agent
  shell commands instead of the wrapper subprocess.
- Delete `mcp_config_read_exceptions`. `sandbox.py` drops its import and read
  root, and imports `gobby_read_exceptions` from `sandbox_read_roots.py`. No
  re-exports remain.
- The tests import from the new modules. The `uv run --project` case becomes
  `test_isolated_gmcp_entry_adds_no_project_read_root`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_sandbox.py tests/agents/test_sandbox_policy.py tests/agents/test_srt_runtime.py tests/agents/test_srt_filesystem_integration.py tests/agents/test_external_write_grants.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

**Acceptance:**

- 1.8.1 - An isolated workspace whose `.mcp.json` holds the `gmcp` entry gets no main-repo read root, and `mcp_config_read_exceptions` no longer exists. test: `tests/agents/test_sandbox.py::test_isolated_gmcp_entry_adds_no_project_read_root`.
- 1.8.2 - The rendered SRT settings are unchanged by the move. test: `tests/agents/test_srt_runtime.py::test_render_settings_uses_srt_credential_schema`.
- 1.8.3 - The sandbox read paths keep the `uv` cache roots after the move. test: `tests/agents/test_sandbox.py::test_package_installs_use_explicit_per_run_cache_paths`.
- 1.8.4 - The loopback-egress comment names `gmcp`. behavior: "gmcp" in `src/gobby/agents/srt_settings.py`.

## D1 Pre-switch promotion: `gmcp` on the hub before launch sites switch (depends: 1.3, 1.4)
`kind: deferred`

- D1.1: after 1.1 to 1.4 land, the PD runs `gobby install gmcp` from the main
  checkout and confirms that `~/.gobby/bin/gmcp --version` answers. This needs
  no daemon restart.
- D1.2: the PD tells the Merge Manager that 1.6 may land.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Promoting a binary into the operator's ~/.gobby/bin and gating a landing are PD operations outside any leaf. Expansion manifests reject the manual category, so this expands as a planning task held needs-planning with blocked-by edges to 1.1 to 1.4; the Orchestrator retypes it to manual."
  owner: "program-director"
  original_acceptance_items:
    - D1.1
    - D1.2
```

## D2 Streamable HTTP transport as the `mcp` route family
`kind: deferred`

- D2.1: remote MCP clients reach the same tool surface over streamable HTTP,
  served through the front door as the `mcp` route family.
- D2.2: the HTTP server holds no state that a gdaemon or backend restart loses.

```yaml
deferral:
  task_ref: "#21570"
  reason: "The HTTP transport is the MCP front door flip itself (S2.12); the Orchestrator ruled it moves there."
  owner: "gobby-1.0 stage 2 (S2.12, #21570)"
  original_acceptance_items:
    - D2.1
    - D2.2
```

## D3 OAuth 2.1 authorization for the HTTP transport
`kind: deferred`

These obligations follow the MCP authorization specification of 2026-07-28.

- D3.1: the HTTP transport publishes Protected Resource Metadata (RFC 9728),
  and its 401 `WWW-Authenticate` carries `resource_metadata` and `scope`.
- D3.2: gdaemon hosts authorization-server metadata (RFC 8414 or OIDC
  discovery), `authorize` with PKCE, and `token` with RFC 8707 resource
  indicators, audience validation, and RFC 9207 `iss`. There is no token
  passthrough.
- D3.3: a client-registry table and client registration.
- D3.4: access tokens are bound to the `users` and `api_keys` identity, and the
  front door validates them as the second bearer kind beside `gobby_` keys.
- D3.5: open product question for Josh. The 2026-07-28 specification makes
  Client ID Metadata Documents SHOULD, and Dynamic Client Registration (RFC
  7591) MAY and deprecated. Which does Gobby support: CIMD, DCR, or both?
- stdio stays credential-from-environment, as the specification recommends.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Access tokens become a second bearer kind at the dispatch seam #23519 (Shared-token cutover) builds, so this is planned after #23519 lands; CIMD versus DCR needs Josh's decision first."
  owner: "gobby-1.0 stage 2 (S2.12, #21570)"
  original_acceptance_items:
    - D3.1
    - D3.2
    - D3.3
    - D3.4
    - D3.5
```

## D4 `gmcp` on thin nodes through the relay
`kind: deferred`

- D4.1: on a node, `gmcp` reaches the hub's tool surface through the node
  relay.
- D4.2: the api-keys D2.5 hub-node pair test (plan of record 4.4.5) passes.

```yaml
deferral:
  task_ref: "#23274"
  reason: "The node channel and relay backend are #23274 (Node channel, relay backend, and /api/machines); the Orchestrator ruled D2.5 belongs there."
  owner: "Lane 7 (W1 gobby#15434)"
  original_acceptance_items:
    - D4.1
    - D4.2
```

## D5 Publish `gmcp`
`kind: deferred`

- D5.1: the first `gmcp-v0.1.0` tag publishes `gobby-mcp` to crates.io and
  attaches the release tarballs.
- D5.2: `version_pins.py` drops `gmcp` from `UNPUBLISHED_MANAGED_BINS`.
- D5.3: the Homebrew tap gains a `gobby-mcp` formula, and
  `distribution.py::HOMEBREW_HELPERS` lists `gmcp`.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Tagging, crates.io publishing, and the external Homebrew tap are release operations Josh authorizes; until then gmcp installs from the checkout like gclient."
  owner: "program-director (release)"
  original_acceptance_items:
    - D5.1
    - D5.2
    - D5.3
```

## D6 Post-switch smoke proof (depends: 1.6)
`kind: deferred`

- D6.1: after 1.6 lands and its daemon restart completes, a newly spawned
  Claude agent and a newly spawned Codex agent each list Gobby tools within
  their first turn.
- D6.2: during a later daemon restart, a live `gmcp` answers
  `DAEMON_UNAVAILABLE` and recovers afterwards without relaunch.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "The proof needs the 1.6 landing, a live daemon restart, and fresh spawns on the operator's hub, which are PD operations outside any leaf. It expands as a planning task held needs-planning with a blocked-by edge to 1.6; the Orchestrator retypes it to manual."
  owner: "program-director"
  original_acceptance_items:
    - D6.1
    - D6.2
```

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15469 under the
  Orchestrator rulings of 11:19, 11:24, and 11:48 CT. Targets and consumers were
  swept read-only on `0.5.0` at `6f5d0c8733`. The draft is narrative only, with
  no M1.

## V2: Verification
`kind: verification`

After every leaf has passed:

1. Run the focused suites of 1.1 to 1.8 together against the test hub, plus
   `cargo nextest run -p gobby-mcp` and
   `cargo clippy -p gobby-mcp --all-targets -- -D warnings` (heavy work).
2. Run `tests/e2e/test_stateless_ambient_session.py` against an isolated
   daemon with the checkout's `gmcp`.
3. Run `uv run gobby plans validate .gobby/plans/gmcp-stdio.md -p <root>` and
   `gcode grep -F "gobby mcp-server" src`. Hits remain only in the 1.6
   repair.
4. D6 records the post-switch smoke proof.

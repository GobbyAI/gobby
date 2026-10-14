Plan artifact: `.gobby/plans/gmcp-stdio.md`

# `gmcp`: a native stdio MCP client replaces `gobby mcp-server`

**Plan ID:** gmcp-stdio

## Overview
`kind: framing`

This plan specifies #23275 (`gobby-mcp` crate: native MCP transports with OAuth
and CIMD). That task is deferred section D3 of
`.gobby/plans/gdaemon-api-keys-nodes.md` (#21555, API keys and node
registration), which carries deferred section D1 of the plan of record
`.gobby/plans/gdaemon-front-door.md`. Lane 7 first planned it in place under
the Orchestrator's 11:19 CT ruling of 2026-10-05. On 2026-10-13 #23275 moved
under Stage 2 (#21544, strangler absorption of the Python daemon behind the
front door) in Lane 2, so its leaves are implemented only after Stage 1
(#21543, the front door) closes. The parent texts stay unchanged.

**The problem.** Every CLI config Gobby writes, and every agent it spawns,
launches `gobby mcp-server`. That is a Python stdio wrapper
(`src/gobby/mcp_proxy/stdio*.py`) that forwards twelve proxy tools to the
daemon's REST API. It is slow to start: `src/gobby/agents/constants.py` records
a cold wrapper taking 25 s beside concurrent spawns, with two siblings past 30 s,
which left those agents without Gobby tools. It needs a Python environment,
which a thin node will not have. Its daemon auto-start ran a module that did not
exist until #23525 (MCP stdio wrapper's daemon auto-start runs a module that
does not exist) was fixed on 2026-10-06. `gmcp` ports that auto-start through a
start command that `gobby install` records (Decision 5).

**When the leaves close:**
- The workspace crate `crates/gmcp` (package `gobby-mcp`, binary `gmcp`) serves
  the wrapper's tool surface over stdio. It holds no session state, dials the
  daemon per request, and survives daemon and backend restarts (1.1 to 1.3).
- `gobby install` builds and promotes `gmcp`, and a release workflow can publish
  it (1.4).
- The e2e and server tests that reached through the wrapper drive `gmcp` or the
  routes directly, and `gmcp` on an enrolled node reaches the hub through the
  node relay (1.5).
- Every launch site writes `~/.gobby/bin/gmcp` (1.6).
- The Python wrapper, `daemon_control.py`, and the
  `mcp_client_proxy.tool_timeouts` setting are gone (1.7).
- The agent sandbox drops the wrapper's main-repo read grant and its
  `gobby mcp-server` text (1.8).
- `gmcp` starts a stopped local daemon at launch, as the wrapper does, by
  running the start command `gobby install` records (1.4, 1.9).
- After Josh authorizes its release, `gmcp` installs as a published binary,
  and Homebrew installs require its formula (1.10).

**Internal servers.** Internal Python servers such as `gobby-tasks` keep
running in the daemon until they are ported, and their proxy tool calls route
through `gmcp` the way they route through the Python wrapper today: the agent
calls `gmcp` over stdio, `gmcp` posts to the daemon's REST route
(`/api/mcp/{server}/tools/{tool}`, or `/api/mcp/tools/call` for wait tools;
1.3) through the `gdaemon` front door, and the daemon dispatches the call to
the internal server. `gmcp` replaces the REST wrapper only (Decision 7). When a
server is later ported to Rust behind the same REST route, `gmcp` does not
change.

The pre-switch promotion, the post-switch smoke proof, and the release are
operations in the Activation section, each with an owner and the leaves it
waits for. The streamable HTTP transport and OAuth are deferred sections D2
and D3. D4 (thin nodes) was withdrawn on 2026-10-13, when item 1.5.5 took over
its proof (ruling 4).

## Decision Record
`kind: framing`

Orchestrator rulings (gobby#14972, 2026-10-05). Josh bounced the plan on
2026-10-13 for open questions and ruled on them the same day: he confirmed 1,
decided 3, reversed 5, and kept publishing for his release authorization
(1.10 and Activation step 3).

1. **Option (a).** `crates/gmcp` joins the workspace now. The timing rule in
   `crates/AGENTS.md` covers `RouteFamily` crates only, and `gmcp` is a client
   binary like `ghook`. The new dependency is **rmcp 3.5** (crates.io,
   Apache-2.0, `rust_version` 1.88, matching the workspace). 3.5.0 shipped on
   2026-09-28, and 3.5.1 was the latest release at the 2026-10-13 recheck, which
   `version = "3.5"` resolves to. `gmcp` uses its `server` and `transport-io`
   features only, so rmcp's optional reqwest is not pulled in. The crates.io
   name `gobby-mcp` is free (checked 2026-10-05, 2026-10-09, and 2026-10-13).
   - Josh confirmed on 2026-10-13: add `crates/gmcp` now on rmcp 3.5, with
     only the `server` feature and the stdio transport feature `transport-io`.
2. **Every D3 obligation is planned here or is a typed deferral with an owner.**
   Native streamable HTTP and restart-stateless HTTP serving go to #21570 (MCP
   front door flip). The OAuth 2.1 authorization server, the second bearer kind,
   and the `users`/`api_keys` binding go to a task gated on #23519 (Shared-token
   cutover). The node-relay wait (#23274) stays; ruling 4's revision moves its
   proof into 1.5.5.
3. **Client ID Metadata Documents only** (Josh, 2026-10-13). Gobby's own HTTP
   MCP endpoint identifies OAuth clients by Client ID Metadata Documents (CIMD)
   and offers no Dynamic Client Registration (DCR, RFC 7591). The 2026-07-28
   specification makes CIMD SHOULD, and DCR MAY and deprecated. The work sits
   in the OAuth deferral (D3.3, D3.5).
4. **D2.5 (the hub-node pair test) belongs to #23274** (Node channel, relay
   backend, and `/api/machines`).
   - Revised by the Orchestrator gobby#15977 on 2026-10-13: #24092 (Hub-node
     pair test over self-signed TLS) delivered D2.5 under #23274, and item 1.5.5
     proves `gmcp` on the same fixtures. D4 is withdrawn. #23274 sits in Stage
     1, so appending a `gmcp` obligation to it would make Stage 1 wait on this
     Stage 2 plan.
5. **`gmcp` ports the daemon auto-start** (Josh, 2026-10-13, reversing the
   Orchestrator's 11:24 CT ruling of 2026-10-05, which dropped it).
   - When the ruling was made, the wrapper's auto-start could not work
     (#23525). `f3d55136b7` (#23531, 2026-10-06) fixed it to run
     `python -m gobby.cli start`, and
     `tests/e2e/test_daemon_lifecycle.py::TestBridgeAutoStart` proves it.
     Retiring it without a successor would remove a working path.
   - The wrapper runs `sys.executable -m gobby.cli start`, so it always finds
     its CLI. `gmcp` has no Python, so `gobby install` records that argument
     list in `<GOBBY_HOME>/gmcp-daemon-start.json` whenever it promotes `gmcp`
     (1.4), and `gmcp` runs it (1.9). A home without the record, such as a thin
     node with no Python, never starts a daemon.
   - The rules follow `stdio_daemon.py::ensure_daemon_running` and
     `daemon_control.py::start_daemon_process`, except for one: the wrapper
     scans processes for `gobby.runner` before starting, and `gmcp` starts only
     after its health probe fails to connect. `gobby start` admits a start
     through `src/gobby/cli/daemon_singleton.py::admit_direct_start`, which
     refuses while a daemon, a restart or cutover, or a live service
     reservation holds the singleton.
   - `src/gobby/mcp_proxy/daemon_control.py` still retires with the wrapper in
     1.7. 1.9 rewrites `TestBridgeAutoStart` to drive `gmcp`.
6. **Bridge readiness for agent-token sessions** (11:48 CT). `gmcp` keeps the
   wrapper's wire call `POST /api/mcp/bridge/ready`. #23531 (Spawned agents
   cannot report MCP bridge readiness: agent capability matrix lacks POST
   /api/mcp/bridge/ready) landed at `f3d55136b7` on 2026-10-06.
   `src/gobby/servers/auth_service.py` now admits agent tokens on that route,
   bound to their own identity, so a token marks only its own session ready.
   Operator and agent sessions both report readiness. No leaf here waits on it.

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
12. **Retire `mcp_client_proxy.tool_timeouts`.** The setting defaults to
    empty, and its only reader is the wrapper (`DaemonProxy._get_tool_timeouts`).
    - Since #23680 (Close-orchestration test times out on its stdio MCP read
      under machine load; `f7ebca019a`, 2026-10-06), the wrapper no longer loads
      the map from the hub. It fetches it once per process from
      `GET /api/mcp/bridge/tool-timeouts`, a route agent tokens may read.
    - Keeping the setting would mean porting that fetch, its cache, and its
      retry into `gmcp`, for a map that ships empty.
    - Retiring it removes the setting, its web editor, the route, the route's
      agent-matrix entry, and the fetch together, in 1.7.
    - Ordinary calls keep the 30 s client timeout; the wait and extended tools
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
    - The files involved are `src/gobby/agents/sandbox_policy.py` (958 lines)
      and `src/gobby/agents/srt_runtime.py` (956 lines). `production-size-growth`
      requires any deliverable that targets them to move code into a new file.
    - 1.8 moves the two functions that hold the stale text into new modules and
      deletes the dead grant.
    - `23328c7392` (#23519, Shared-token cutover) has already moved the
      credential helpers out of `sandbox_policy.py` into
      `sandbox_credentials.py` (`credential_roots`, `gcode_runtime_root`). 1.8
      starts from that tree.

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never runs the full suite. No leaf restarts the running daemon, touches
  port 60891, or reads `~/.gobby/local_cli_token` or `~/.gobby/bootstrap.yaml`.
  Tests build their own homes. Every test that launches `gmcp` sets
  `GOBBY_HOME` to its own temporary home, so `gmcp` never finds the operator's
  recorded start command (Decision 5).
- **Rust.** Load the `rust` skill before editing `crates/`, where
  `crates/CLAUDE.md` governs. `cargo build`, `clippy`, and `nextest` are heavy
  work under the project operating policy: each run takes a heavy-run slot from
  the Release Manager and sets `CARGO_BUILD_JOBS=3`.
- **No secrets in output.** `gmcp` never logs a bearer, a key, or a header
  value. Its diagnostics go to stderr, because stdout carries the protocol.
- **No backward compatibility.** 0.5.0 has not shipped. `gobby mcp-server` and
  the setting leave without aliases. The installer's repair (1.6) rewrites
  existing wrapper entries to `gmcp`.
- **Consumer sweeps** ran read-only on `0.5.0` at `6f5d0c8733` (2026-10-05),
  at `ba4aa371f8` (2026-10-09), and at `97b1bbe055` (2026-10-13), where every
  file with a new hit was checked:
  - `gcode grep -F` for `mcp-server`, `mcp_proxy.stdio`, `mcp_proxy/stdio`
    (2026-10-13 only), `daemon_control`, `current_terminal_context`,
    `serialize_terminal_context`, `tool_timeouts`, `tool-timeouts` (from
    2026-10-09), and `_call_tool_wrapper`;
  - `gcode grep -w` for `_resolved_gobby_mcp_command`,
    `_is_repairable_stale_gobby_mcp_server_config`, `render_srt_settings`,
    `gobby_read_exceptions`, and `mcp_config_read_exceptions`;
  - `rg -l` with the same literals over `scripts/` and `.github/`, which the
    code index does not cover. On 2026-10-13 a code-index refresh under machine
    load made `gcode grep` time out, so `rg -l` ran over `src`, `tests`, and
    `docs` too.
  - `docs/evidence/**`, `docs/plans/**`, `docs/audits/**`, `docs/archive/**`,
    `tests/fixtures/provider_contracts/**`, and `CHANGELOG.md` hold dated history
    and are excluded.
- **Landing order.** Activation step 1 (pre-switch promotion) must be
  complete before 1.6 lands, and 1.6's Lane Manager holds that landing until
  step 1 confirms. The daemon restart that follows the 1.6 landing makes
  spawns launch `~/.gobby/bin/gmcp`. Activation step 2 (post-switch smoke
  proof) runs after that restart. 1.10 lands only after Activation step 3
  publishes the release.

## Parent D3 Mapping
`kind: framing`

| Parent obligation | Here | Note |
| --- | --- | --- |
| stdio transport replacing `gobby mcp-server` in every config the installer writes (plan of record D1, Transports) | 1.1, 1.2, 1.3, 1.6, 1.7 | |
| Restart survival: no session state, per-request dial, typed unavailable while down (plan of record D1, Restart survival) | 1.1.5, 1.5.2 | Also the typed 503 (Decision 13). |
| Streamable HTTP for remote clients as the `mcp` route family | D2 | #21570 |
| OAuth 2.1: protected-resource metadata, AS metadata, client registration, authorize, token, client-registry table, `users`/`api_keys` binding, second bearer kind | D3 | #23519 has landed. Clients are identified by CIMD only (Decision 3). |
| Waits on the node relay (api-keys D2) | 1.5.5 | #24089 (Node relay: a node's front door forwards to its hub over the pinned connection) has landed. |
| D2.5 (plan of record 4.4.5), hub-node pair test | 1.5.5 | #24092 (Hub-node pair test over self-signed TLS) delivered it under #23274; 1.5.5 adds `gmcp` to the same fixtures (ruling 4). |
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
  - Auth comes from `src/gobby/utils/local_token.py::daemon_auth_headers`:
    - An agent token gives a Bearer plus the `_AGENT_IDENTITY_ENV_HEADERS`
      pairs (session, caller project, agent run, managed execution). It comes
      from the private grant envelope that `GOBBY_MANAGED_EXECUTION_BOOTSTRAP`
      names, or else from a non-empty `GOBBY_AGENT_API_TOKEN`.
    - With `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` set and no agent token, it
      refuses. Otherwise the local credential gives a Bearer.
    - Auth headers are merged last, so they win.
  - While `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` is set, `_request` re-reads auth
    before every request. That picks up a capability the daemon renews in the
    envelope, from #23865 (Standing seats lose their managed capability after
    24 hours, so their hooks spool until a resume). Otherwise auth is read
    once and cached.
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
  - `crates/gcore/src/local_token.rs::read_api_key` (`read_api_key_for` with an
    explicit home) resolves the credential in the wrapper's order:
    1. the managed envelope;
    2. `GOBBY_AGENT_API_TOKEN`;
    3. a refusal while `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` is set;
    4. the bootstrap `api_key`.
    An agent token is in use exactly when `GOBBY_MANAGED_EXECUTION_BOOTSTRAP`
    is set or `GOBBY_AGENT_API_TOKEN` is non-empty, and that decides the
    identity headers. `gmcp` needs no gcore change.
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
  - It holds the auth headers, the cached caller project id, the
    `GOBBY_SESSION_ID` value, the serialized terminal context, a
    `has_tmux_pane` flag, and the last preflight success time.
  - Auth headers come from `read_api_key`. They are cached, unless
    `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` is set, in which case they are re-read
    before every request.
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
dialer, and its tests. Eight acceptance items each cover a rule of that one
request path.

**Focused verification (planned):** `cargo nextest run -p gobby-mcp` and
`cargo clippy -p gobby-mcp --all-targets -- -D warnings` (heavy work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_gmcp_contracts.py -q`.

**Acceptance:**

- 1.1.1 - Requests carry the protocol header `1`, and the target and caller project headers follow the wrapper's rules. The session header comes from `GOBBY_SESSION_ID` before the body, the body value is used only without a tmux pane, and otherwise the terminal-context header is sent. With an agent token, from the managed envelope or `GOBBY_AGENT_API_TOKEN`, the identity headers override. test: `crates/gmcp/tests/dialer.rs::request_headers_match_python_wrapper`.
- 1.1.2 - A 401 re-reads the credential and retries exactly once. test: `crates/gmcp/tests/dialer.rs::unauthorized_rereads_credential_and_retries_once`.
- 1.1.3 - A 409 `SESSION_REQUIRED` returns its `detail`. Other non-200 answers return `HTTP <n>: <body>`. test: `crates/gmcp/tests/dialer.rs::conflict_and_error_statuses_match_wrapper`.
- 1.1.4 - A refused connection and the front door's typed 503 both return `DAEMON_UNAVAILABLE` naming `gobby start`. A read timeout returns `REQUEST_TIMEOUT` with the spawn-agent hint on `/tools/spawn_agent`. test: `crates/gmcp/tests/dialer.rs::unavailable_and_timeout_results_are_typed`.
- 1.1.5 - One `Dialer` returns `DAEMON_UNAVAILABLE` while the stub is down and succeeds after the stub re-binds the same port, with no new `Dialer`. Preflight is cached for 5 s only after a success. test: `crates/gmcp/tests/dialer.rs::dialer_survives_daemon_restart`.
- 1.1.6 - Terminal context prefers the generation query, falls back to window identity, applies the environment fallbacks only to unset keys, and serializes only the twelve contract keys without nulls. test: `crates/gmcp/src/terminal_context.rs::capture_matches_python_wrapper_rules`.
- 1.1.7 - The wrapper contract equals the Python constants it mirrors. test: `tests/mcp_proxy/test_gmcp_contracts.py::test_wrapper_contract_matches_python_constants`.
- 1.1.8 - While `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` is set, every request re-reads the credential, so a capability renewed in the envelope between two requests is sent on the second. Without it, the credential is read once and re-read only after a 401. test: `crates/gmcp/tests/dialer.rs::managed_envelope_credential_is_reread_per_request`.

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
  - Agent-token sessions may post it since #23531 landed (Decision 6).
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
  - Each method's body, query, and `preflight` flag are in its `DaemonProxy`
    method in `stdio_proxy.py`; the executor copies them exactly.
    `set_variable` sends `{name, value, scope}`, where `scope` is `session`
    (the default) or `step`.
- `src/gobby/mcp_proxy/instructions.py::build_gobby_instructions` reads the
  bundled `progressive-discovery.md` with its front matter stripped. It stays,
  because `server.py` uses it.
- The wrapper returns each tool's dict through the Python MCP SDK. The
  call-result envelope (content blocks and structured content) is part of what
  clients see, so the parity capture includes it.
- rmcp 3.5 provides `ServerHandler`, the `transport-io` stdio transport, and
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
  JSON result in the envelope the capture recorded. Its `init_project`
  function returns the static error without a request; 1.7 points the
  reference-audit row at it.
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
- `src/gobby/cli/install_components.py::*` — scope-reason: the `gmcp` component, its label, `_workspace_client_version`, `promote_client_binary` (which also records the daemon start command after a `gmcp` promotion), and the `run_install_components` branch
- `src/gobby/cli/install.py::*` — scope-reason: the components list in the install help text
- `src/gobby/install/bin_freshness_promotion.py::*` — scope-reason: `WORKSPACE_BINARY_CRATES` gains `gmcp` with `gcore`
- `src/gobby/install/version_pins.py::*` — scope-reason: pin `gmcp` 0.1.0 and list it in `UNPUBLISHED_MANAGED_BINS`
- `.github/workflows/release-gmcp.yml`
- `.github/workflows/rust-ci.yml`
- `docs/guides/release-guide.md`
- `tests/cli/test_install_setup_gmcp.py`
- `tests/cli/test_install_setup.py::*` — scope-reason: the managed-binary install cases patch `_install_gmcp` beside `_install_gclient`
- `tests/cli/test_install_components.py::*` — scope-reason: adds the `gmcp` component promotion case
- `tests/install/test_bin_freshness.py::*` — scope-reason: adds the `gmcp` crate-set case
- `tests/install/test_version_pins.py::*` — scope-reason: asserts the `gmcp` pin and unpublished status

**Research context:**
- `src/gobby/cli/install_setup_gclient.py` (342 lines) is the per-binary
  installer pattern:
  - it builds from the checkout (`install_gclient_from_submodule`), takes the
    native-bin lock, replaces the binary through a new inode, and signs it;
  - it writes a source hash and a version stamp;
  - outside a checkout, an unpublished binary raises
    `ManagedBinaryReleaseMissing`.
- `install_setup.py` (570 lines) runs every `MANAGED_NATIVE_BINARY_NAMES` entry
  on a default `gobby install`. `install_components.py` (361 lines) lets
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
- `docs/guides/release-guide.md` lists every crate that has a release workflow
  in its Current Release Set table, with `gobby-client` at `gclient-v0.1.0`
  although `gclient` is still unpublished. Its Version Rules list every tag
  prefix.
- `distribution.py::HOMEBREW_HELPERS` needs a formula in the
  `GobbyAI/homebrew-tap` repository. Activation step 3 adds the formula, and
  1.10 lists `gmcp`.

**Implementation:**
- `install_setup_gmcp.py` mirrors `install_setup_gclient.py` for crate dir
  `gmcp`, package `gobby-mcp`, binary `gmcp`, and stamp `.gmcp-version`. It
  provides the workspace build path and the unpublished guard. The
  GitHub-release and cargo paths follow gclient's shape, so 1.10 only flips the
  published flag.
- `install_setup_gmcp.py` also provides
  `write_daemon_start_command(gobby_home, argv)`. It writes `argv` as a JSON
  array to `<gobby_home>/gmcp-daemon-start.json` through a temporary file in
  the same directory and `os.replace`, with mode 0644, because it holds no
  secret (Decision 5). After every `gmcp` promotion, the default install and
  the `gmcp` component both call it with `get_gobby_home()` and
  `[sys.executable, "-m", "gobby.cli", "start"]`, the command the wrapper runs.
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
  and the workflow path filter. The release guide adds a `gobby-mcp` row
  (`gmcp`, `0.1.0`, `gmcp-v0.1.0`, crates.io + GitHub binaries) and `gmcp-v*`
  to the tag prefixes.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_setup_gmcp.py tests/cli/test_install_setup.py tests/cli/test_install_components.py tests/install/test_bin_freshness.py tests/install/test_version_pins.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

**Acceptance:**

- 1.4.1 - In a checkout, the gmcp installer builds `gobby-mcp`, promotes `gmcp` through a new inode with the lock held, and writes the version stamp and source hash. Outside a checkout it raises `ManagedBinaryReleaseMissing`. test: `tests/cli/test_install_setup_gmcp.py::test_submodule_install_promotes_and_stamps`.
- 1.4.2 - A default install runs the `gmcp` installer with the other managed binaries. test: `tests/cli/test_install_setup.py::test_managed_native_binaries_include_gmcp`.
- 1.4.3 - `gobby install gmcp` promotes only `gmcp` and does not claim the daemon singleton. test: `tests/cli/test_install_components.py::test_gmcp_component_promotes_client_binary`.
- 1.4.4 - Freshness treats a `gcore` or `gmcp` source change as making `gmcp` stale, and the pins mark it unpublished at 0.1.0. test: `tests/install/test_bin_freshness.py::test_gmcp_tracks_gmcp_and_gcore`.
- 1.4.5 - The release workflow publishes on `gmcp-v*` tags and fails when the package list lacks the contract or asset files, and the release guide lists the `gmcp-v*` prefix. behavior: "gmcp-v" in `.github/workflows/release-gmcp.yml` and `docs/guides/release-guide.md`.
- 1.4.6 - After a `gmcp` promotion by the default install or by `gobby install gmcp`, `<GOBBY_HOME>/gmcp-daemon-start.json` holds the JSON array `[<installer's sys.executable>, "-m", "gobby.cli", "start"]` with mode 0644, and a second promotion replaces it whole. test: `tests/cli/test_install_setup_gmcp.py::test_promotion_records_daemon_start_command` and `tests/cli/test_install_components.py::test_gmcp_component_records_daemon_start_command`.

### 1.5 E2e and server tests drive `gmcp` or the routes, not the wrapper [category: test] (depends: 1.3)
`kind: deliverable`

Targets:
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: `_new_proxy` and the four proxy tests drive a `gmcp` subprocess through the MCP SDK stdio client instead of `DaemonProxy`
- `tests/e2e/gmcp_client.py`
- `tests/servers/routes/mcp_endpoints/test_registry_routes.py::*` — scope-reason: `test_stdio_server_listing_preserves_scoped_template_catalog` asserts through the route instead of `DaemonProxy`
- `tests/servers/routes/mcp_endpoints/test_template_routes.py::*` — scope-reason: the disabled-template case asserts the route's answer instead of `DaemonProxy` and `_capture_stdio_tools`
- `tests/servers/test_mcp_execution_context.py::*` — scope-reason: the stdio half of `test_run_identity_transport_chain` sends the wrapper's headers with the test client instead of `DaemonProxy`
- `tests/servers/routes/mcp_endpoints/test_terminal_session_revival.py::*` — scope-reason: the `mcp_after_hook` case of `test_expired_owner_recovers_only_from_live_session_traffic` builds the wrapper's request headers directly instead of using `_proxy_with_response` and `_success_response` from the wrapper test file 1.7 deletes, and drops its `stdio_proxy` patches
- `tests/skills/reference_library_helpers.py::*` — scope-reason: the stdio tool inventory comes from `crates/gmcp/contracts/tools_list.v1.json` instead of `register_proxy_tools`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: `test_scrubbed_child_env_reaches_daemon_proxy_identity` keeps its child-environment assertions and resolves the envelope's credential with `daemon_auth_headers()` instead of `DaemonProxy`; its `tool_timeouts` response mock leaves with `DaemonProxy`
- `tests/e2e/test_hub_node_pair.py::*` — scope-reason: adds `test_gmcp_on_node_reaches_hub_through_relay` on the `pair_hub` and `pair_node` fixtures

**Research context:**
- `tests/e2e/test_stateless_ambient_session.py` (546 lines) covers, through
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
  - `tests/servers/test_mcp_execution_context.py::test_run_identity_transport_chain`
    imports `DaemonProxy` and `DaemonProxyDependencies` for its stdio half.
  - Since #24044 (Live operator-pane session stays expired after an outage:
    revival suppressed on a stale recorded CLI PID),
    `tests/servers/routes/mcp_endpoints/test_terminal_session_revival.py`
    imports `_proxy_with_response` and `_success_response` from
    `tests/mcp_proxy/test_stateless_stdio_proxy.py`, which 1.7 deletes. Its
    `mcp_after_hook` case patches `gobby.mcp_proxy.stdio_proxy.current_terminal_context`
    and `daemon_auth_headers`, sends one `DaemonProxy._request`, and passes the
    captured headers to `_set_context_for_request`. It tests the server half:
    terminal-context headers without a session header revive the session.
- Since #23923 (Sandboxed agents can read every same-user process's
  environment, including agent tokens and DATABASE_URL), managed tokens travel
  only in the private envelope.
  `tests/agents/test_spawn_executor.py::test_scrubbed_child_env_reaches_daemon_proxy_identity`
  asserts that the Codex child environment holds `GOBBY_SESSION_ID`,
  `GOBBY_HOME`, `GOBBY_DAEMON_URL`, and `GOBBY_MANAGED_EXECUTION_BOOTSTRAP`, and
  that no variable or override holds the token. Its second half builds a
  `DaemonProxy` in that environment and checks the Bearer and identity headers.
  `src/gobby/utils/local_token.py::daemon_auth_headers` resolves the same
  credential and identity headers without the wrapper.
- #24092 (Hub-node pair test over self-signed TLS) added
  `tests/e2e/test_hub_node_pair.py` under #23274 (Node channel, relay backend,
  and `/api/machines`):
  - `pair_hub` spawns a hub with self-signed TLS. `pair_node` enrolls a node
    home with `gobby auth login` and runs a bare `gdaemon serve` there.
  - The node's `bootstrap.yaml` holds `bind_host`, `daemon_port`, and the
    enrolled `api_key`. So `gmcp` with `GOBBY_HOME` set to the node home
    resolves the node's front door through gcore's `daemon_url` and its key
    through `read_api_key`.
  - `test_node_enrolls_and_relays` bounds readiness with `_wait_listener` and
    `_wait_connection`, the latter through an observer key minted on the hub.
  - `src/gobby/servers/routes/mcp/endpoints/server.py::list_mcp_servers`
    resolves no wrapper caller, so it needs no session.

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
- The revival test's `mcp_after_hook` case builds the wrapper's request
  headers directly: the protocol header, the project headers, and
  `X-Gobby-Terminal-Context` as the recovered context's JSON filtered to
  `TERMINAL_CONTEXT_KEYS`, with no session header. The rest of the case is
  unchanged.
- The spawn test keeps its child-environment assertions. Its second half
  calls `daemon_auth_headers()` in the child environment instead of building a
  `DaemonProxy`, and checks that the Bearer is the envelope's capability and
  that the session and run headers are the child's.
- `test_gmcp_on_node_reaches_hub_through_relay` waits for the node's listener
  and channel as `test_node_enrolls_and_relays` does. It then opens a `gmcp`
  session through `gmcp_client.py` with `GOBBY_HOME` set to the node home and
  neither `GOBBY_DAEMON_URL` nor `GOBBY_PORT`, and calls `list_mcp_servers`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_stateless_ambient_session.py tests/e2e/test_hub_node_pair.py tests/servers/routes/mcp_endpoints/test_registry_routes.py tests/servers/routes/mcp_endpoints/test_template_routes.py tests/servers/routes/mcp_endpoints/test_terminal_session_revival.py tests/servers/test_mcp_execution_context.py tests/skills/ tests/agents/test_spawn_executor.py -q`,
with `gmcp` and `gdaemon` built by `cargo build -p gobby-mcp -p gobby-daemon`
(heavy work).

**Acceptance:**

- 1.5.1 - Through `gmcp`, the ambient session follows `/clear`, the schema lease binds to the child identity, and a hookless terminal gets `SESSION_REQUIRED`. test: `tests/e2e/test_stateless_ambient_session.py::test_ambient_proxy_follows_clear_and_attributes_schema_lease`.
- 1.5.2 - One `gmcp` process answers `DAEMON_UNAVAILABLE` while the isolated daemon is stopped, and resolves the same session after it restarts. test: `tests/e2e/test_stateless_ambient_session.py::test_same_proxy_resolves_existing_session_after_daemon_restart`.
- 1.5.3 - The route tests, the revival test, the execution-context test, and the reference inventory pass with no import of `gobby.mcp_proxy.stdio*` or of the wrapper's test helpers. test: `tests/servers/test_mcp_execution_context.py::test_run_identity_transport_chain`.
- 1.5.4 - The scrubbed Codex child environment carries `GOBBY_SESSION_ID`, `GOBBY_HOME`, `GOBBY_DAEMON_URL`, and `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` naming the private grant envelope, and no variable or override carries the capability. In that environment, `daemon_auth_headers()` returns the envelope's capability as the Bearer with the child's session and run headers. test: `tests/agents/test_spawn_executor.py::test_scrubbed_child_env_reaches_daemon_proxy_identity`.
- 1.5.5 - On an enrolled node, `gmcp` given only the node home resolves the node's front door and key from the node's `bootstrap.yaml`, and `list_mcp_servers` returns the hub's internal servers, `gobby-tasks` among them, through the relay. test: `tests/e2e/test_hub_node_pair.py::test_gmcp_on_node_reaches_hub_through_relay`.

### 1.6 Every launch site starts `~/.gobby/bin/gmcp` [category: code] (depends: 1.3, 1.4, 1.5, 1.9)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/mcp_config_shared.py::*` — scope-reason: `_GOBBY_MCP_COMMAND`/`_GOBBY_MCP_ARGS` become `gmcp` with no arguments, `_resolved_gobby_mcp_command` becomes the public `gmcp_command()` and returns the managed `gmcp` path, and the stale-entry repair also rewrites `gobby mcp-server` entries
- `src/gobby/cli/installers/mcp_config_toml.py::*` — scope-reason: the Codex block writes `args = []`
- `src/gobby/cli/installers/mcp_config_json.py::*` — scope-reason: the repair and fresh-entry paths write `gmcp` with no arguments
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: `_codex_mcp_config_overrides` sets the managed `gmcp` command with empty arguments instead of `uv run`
- `src/gobby/agents/isolation_repair.py::*` — scope-reason: `_patch_mcp_config_for_isolation` writes the `gmcp` entry, and `provider_mcp_config_error` accepts exactly that entry
- `src/gobby/servers/chat_session_helpers.py::*` — scope-reason: `_build_gobby_mcp_entry` returns the managed `gmcp` path with no arguments
- `src/gobby/agents/constants.py::*` — scope-reason: the `MCP_CONNECT_TIMEOUT_MS` and `MCP_TIMEOUT` comments describe `gmcp`; the values stay
- `src/gobby/agents/grok_sandbox_home.py::*` — scope-reason: `_declare_gobby_mcp_server` writes the `gmcp` entry with no arguments into the managed Grok `config.toml`
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
- `tests/agents/test_grok_sandbox_home.py::*` — scope-reason: the two Gobby-entry cases expect `gmcp_command()` with no arguments
- `tests/agents/test_spawn_executor.py::*` — scope-reason: `test_codex_terminal_direct_spawn` expects the `gmcp` override instead of `uv run --no-sync --project /main/repo gobby mcp-server`; the `.mcp.json` samples that feed the timeout tests stay

**Research context:**
- `mcp_config_shared.py` (221 lines) resolves `gobby` next to `sys.executable`,
  then `which`. `_is_repairable_stale_gobby_mcp_server_config` repairs only
  `uv run [--directory X] gobby mcp-server`. Two other wrapper shapes exist:
  - `_patch_mcp_config_for_isolation` persists
    `uv run --project <main repo> gobby mcp-server` in each isolated
    workspace's `.mcp.json` (`isolation_repair.py`, lines 230 to 292). For
    Claude it also writes that entry into the isolated path's project scope in
    `~/.claude.json` through `src/gobby/claude_config.py::update_claude_config`,
    the locked writer #24225 (Gobby writers of the host Claude config race live
    Claude saves: no shared lock) added;
  - the Codex overrides run `uv run --no-sync --project <main repo> gobby mcp-server`.
  After 1.7 deletes the command and 1.8 drops the main-repo grant, any
  surviving entry of these shapes fails to start. The TOML (203 lines) and JSON
  (189 lines) writers share those helpers; since #24225 the JSON writer's
  Claude writes also go through `update_claude_config`, which builds no entry
  itself. Codex keeps `required = true`, the 120 s startup, the 360 s tool
  timeout, and `default_tools_approval_mode`.
- `spawn_executor_support.py::_codex_mcp_config_overrides` (643-line file)
  runs `uv run --no-sync --project <main repo> gobby mcp-server`. That way an
  isolated worktree's old code never serves the proxy (memory ce6ece28). A
  native client has no per-worktree code, so the managed binary serves every
  workspace. The overrides keep `required`, the timeouts, the env forwarding,
  and the approval seeding.
- `isolation_repair.py` (331 lines) writes `.mcp.json` for isolated agents.
  `provider_mcp_config_error` currently requires `command == "uv"` and
  `mcp-server` in `args`.
- `chat_session_helpers.py::_build_gobby_mcp_entry` builds the chat SDK's
  stdio entry.
- `src/gobby/agents/grok_sandbox_home.py::_declare_gobby_mcp_server` came from
  #23554 (Harden managed sandbox policy: protect hook config and trust state,
  deny credential-store reads; `6f56a8ec50`).
  - It writes `{"command": _resolved_gobby_mcp_command(), "args": ["mcp-server"]}`
    as `mcp_servers.gobby` in each managed Grok run's `config.toml`, replacing
    any operator entry.
  - `tests/agents/test_grok_sandbox_home.py` asserts that entry in
    `test_managed_config_declares_gobby_mcp_server` and
    `test_operator_gobby_mcp_server_is_replaced_and_others_kept`.
  - Every prepare rewrites the entry, so it needs no repair.
- `tests/agents/test_spawn_executor.py::TestExecuteSpawn.test_codex_terminal_direct_spawn`
  asserts the Codex `-c` override
  `args=["run","--no-sync","--project","/main/repo","gobby","mcp-server"]`.
  1.5 also edits that file, so this leaf depends on 1.5.
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
- The stale-entry repair, which the JSON and TOML writers share, rewrites
  every wrapper entry to that entry:
  - a `uv` command whose arguments start with `run` and end with
    `gobby mcp-server`, whatever flags lie between (none, `--directory X`,
    `--project X`, or `--no-sync --project X`);
  - a `gobby` command, bare or at any path, with `["mcp-server"]`.
  The rewrite keeps the entry's `env`, timeouts, and other keys, and leaves
  other servers alone.
- `_patch_mcp_config_for_isolation` replaces the whole `gobby` entry in
  `.mcp.json` and in the Claude project scope, so re-patching a workspace that
  holds the `--project` shape writes `gmcp` to both.
- `_declare_gobby_mcp_server` writes the same entry into the managed Grok
  `config.toml`.
- `provider_mcp_config_error` requires the `gmcp` entry.
- `clients.md` describes `gmcp` as the stdio transport. It keeps the
  wrapper's claim that the transport can start the daemon, which 1.9 ports,
  in this sentence: "`gmcp` starts a stopped local daemon with the start
  command that `gobby install` records, and otherwise reports
  `DAEMON_UNAVAILABLE` with `gobby start` guidance."
- **Landing gate.** This leaf's Lane Manager holds its landing until
  Activation step 1 confirms that `~/.gobby/bin/gmcp` is promoted on the hub.

**Granularity:** one leaf. Every production target writes, repairs, or
describes the one `gobby` MCP entry. If the sites switched separately, some
spawns would launch `gmcp` while repairs and preflights still demanded the `uv`
entry, so they land together.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/ tests/agents/spawners/test_command_builder.py tests/agents/test_spawn_executor_support.py tests/agents/test_spawn_executor.py tests/agents/test_isolation.py tests/agents/test_local_context_setup.py tests/agents/test_grok_sandbox_home.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

Consumers unchanged:
- `tests/agents/test_memory_watchdog.py` — no-edit-reason: its `uv run gobby mcp-server` command line is sample process data.
- `tests/agents/test_resume_executor.py` — no-edit-reason: its `.mcp.json` sample only feeds the timeout environment assertions.

**Acceptance:**

- 1.6.1 - Fresh JSON and TOML installs write the managed `gmcp` path with no arguments, and Codex keeps `required`, both timeouts, and the approval mode. test: `tests/cli/installers/test_cli_installers_mcp_config.py::test_fresh_install_writes_gmcp_entry`.
- 1.6.2 - In JSON and TOML configs, the repair rewrites `uv run gobby mcp-server`, `uv run --directory X gobby mcp-server`, `uv run --project X gobby mcp-server`, `uv run --no-sync --project X gobby mcp-server`, and `gobby mcp-server` entries (bare or at any path) to `gmcp`. It keeps each entry's `env` and timeouts and leaves other servers alone. test: `tests/cli/installers/test_cli_installers_mcp_config.py::test_repair_rewrites_wrapper_entries_to_gmcp`, parameterized over every shape and both formats.
- 1.6.3 - Spawned Codex agents get `gmcp` through `-c` overrides with the same timeouts, environment forwarding, and approvals. test: `tests/agents/test_spawn_executor_support.py::test_codex_overrides_launch_gmcp`.
- 1.6.4 - Isolated agents get a `gmcp` `.mcp.json`, re-patching a workspace that holds the `uv run --project` entry replaces it, and the preflight accepts the `gmcp` entry and rejects a `uv` entry. test: `tests/agents/test_isolation.py::test_isolation_writes_and_accepts_gmcp_entry`.
- 1.6.5 - The chat SDK entry and the clients reference name `gmcp`, and the clients reference states the auto-start sentence, naming both the recorded start command and the `DAEMON_UNAVAILABLE` guidance. behavior: "`gmcp` starts a stopped local daemon with the start command that `gobby install` records, and otherwise reports `DAEMON_UNAVAILABLE` with `gobby start` guidance" in `src/gobby/install/shared/skills/gobby/references/admin/clients.md`.
- 1.6.6 - A managed Grok run's `config.toml` declares `gobby` as the managed `gmcp` path with no arguments, replacing an operator `gobby` entry and keeping other servers. test: `tests/agents/test_grok_sandbox_home.py::test_operator_gobby_mcp_server_is_replaced_and_others_kept`.

### 1.7 The Python wrapper and its setting are gone [category: code] (depends: 1.5, 1.6, 1.9)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/stdio.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_server.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_proxy.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_tools.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/stdio_daemon.py::*` — operation: delete — scope-reason: replaced by `gmcp`, whose startup task 1.9 ports from it
- `src/gobby/mcp_proxy/stdio_results.py::*` — operation: delete — scope-reason: replaced by `gmcp`
- `src/gobby/mcp_proxy/daemon_control.py::*` — operation: delete — scope-reason: only the stdio modules consumed it; 1.9 ports its start path into `gmcp` (Decision 5)
- `src/gobby/cli/mcp.py::*` — operation: delete — scope-reason: `gobby mcp-server` is removed
- `src/gobby/mcp_proxy/terminal_context.py::*` — scope-reason: `current_terminal_context` and `serialize_terminal_context` leave; `TERMINAL_CONTEXT_KEYS` stays for `request_context.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: the `mcp-server` lazy-command entry leaves, and the lazy-map comment stops citing it
- `src/gobby/tasks/criterion_commands.py::*` — scope-reason: `mcp-server` leaves the CLI command-name list
- `src/gobby/config/servers.py::*` — scope-reason: `MCPClientProxyConfig.tool_timeouts` leaves (Decision 12)
- `src/gobby/config/registry.py::*` — scope-reason: the `mcp_client_proxy.tool_timeouts` mapping pattern leaves
- `src/gobby/servers/routes/mcp/endpoints/bridge.py::*` — scope-reason: `get_bridge_tool_timeouts` leaves (Decision 12); `report_bridge_ready` stays
- `src/gobby/servers/routes/mcp/tools.py::*` — scope-reason: the `/bridge/tool-timeouts` registration and its import leave
- `src/gobby/servers/auth_service.py::*` — scope-reason: only the `GET /api/mcp/bridge/tool-timeouts` agent route leaves; `POST /api/mcp/bridge/ready` stays
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated without the `tool_timeouts` pattern
- `tests/contracts/http/config_schema.json::*` — scope-reason: the `/api/config/schema` snapshot drops `tool_timeouts`
- `docs/reference-audit/admin.json::*` — scope-reason: the `gobby mcp-server` row becomes a `gmcp` row
- `docs/reference-audit/mcp-servers.json::*` — scope-reason: the `gobby` `init_project` row's implementation becomes `crates/gmcp/src/tools.rs::init_project`
- `scripts/generate_parity_ledger.py`
- `docs/contracts/parity-ledger-status.d/remainder.json`
- `docs/contracts/parity-ledger.md`
- `web/src/components/settings/sections/McpToolsSection.tsx::*` — scope-reason: the `tool_timeouts` path and its Per-tool timeouts editor leave
- `web/src/components/settings/sections/configFields.tsx::*` — scope-reason: `NumberMapConfigField` leaves; the `tool_timeouts` editor was its only consumer
- `docs/guides/configuration.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/mcp-tools.md`
- `docs/guides/cli-commands.md`
- `docs/architecture/development-guide.md`
- `docs/guides/ghook-development-guide.md`
- `README.md`
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — operation: delete — scope-reason: tests the retired wrapper
- `tests/mcp_proxy/test_mcp_proxy_stdio_session_context.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.1.1 covers the header rules
- `tests/mcp_proxy/test_stateless_stdio_proxy.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.1 and 1.5 cover it
- `tests/mcp_proxy/test_stdio_config_runtime.py::*` — operation: delete — scope-reason: tests the retired wrapper
- `tests/mcp_proxy/test_stdio_proxy.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.2 and 1.3 cover the tools
- `tests/mcp_proxy/test_stdio_results.py::*` — operation: delete — scope-reason: tests the retired wrapper
- `tests/mcp_proxy/test_stdio_startup.py::*` — operation: delete — scope-reason: tests the retired wrapper; 1.2.1 covers no-I/O initialize and 1.9 the auto-start rules
- `tests/mcp_proxy/test_gmcp_parity.py` — operation: delete
- `tests/mcp_proxy/test_mcp_server_factory.py::*` — scope-reason: the `create_stdio_mcp_server` case leaves; the HTTP factory cases stay
- `tests/mcp_proxy/test_mcp_proxy_terminal_context.py::*` — scope-reason: the cases for the removed functions leave; the key-set case stays
- `tests/cli/test_cli.py::*` — scope-reason: the `mcp-server` command and help assertions leave
- `tests/config/test_servers.py::*` — scope-reason: the `tool_timeouts` cases leave
- `tests/config/test_config_registry.py::*` — scope-reason: the `tool_timeouts` mapping pattern leaves the expected set
- `tests/servers/routes/mcp_endpoints/test_bridge.py::*` — scope-reason: the three tool-timeouts tests leave, and `test_router_registers_bridge_routes` asserts that route is absent from the router and the agent route matrix
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py::*` — scope-reason: the config stub drops `tool_timeouts={}`
- `tests/utils/test_posix_spawn.py::*` — scope-reason: `_NOT_DAEMON` drops its `mcp_proxy/daemon_control.py` and `mcp_proxy/stdio` entries
- `tests/workflows/test_active_progressive_discovery_guidance.py::*` — scope-reason: `ACTIVE_GUIDANCE_FILES` swaps `src/gobby/mcp_proxy/stdio_tools.py` for `crates/gmcp/contracts/tools_list.v1.json`, which now holds the tool descriptions
- `tests/terminals/test_no_direct_tmux_consumers.py::*` — scope-reason: `_OWNED_CONSUMER_PATHS` drops the deleted `mcp_proxy/stdio.py` and `mcp_proxy/stdio_server.py`
- `web/src/components/settings/sections/__tests__/McpToolsSection.test.tsx::*` — scope-reason: the `tool_timeouts` fixture and editor case leave, and a case asserts the editor is absent
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: the settings fixture drops `tool_timeouts`

**Research context:**
- Only `cli/mcp.py`, the stdio modules, and tests import the stdio modules
  and `daemon_control.py`. After 1.5 no server test does, and after 1.9 no e2e
  test does, because 1.9 replaces the e2e auto-start script with a `gmcp`
  launch.
- `request_context.py` imports `TERMINAL_CONTEXT_KEYS`.
  `current_terminal_context` and `serialize_terminal_context` have no
  non-wrapper caller.
- `tool_timeouts` is read only by `DaemonProxy._get_tool_timeouts`, through
  the route #23680 added (Decision 12):
  - the handler is `src/gobby/servers/routes/mcp/endpoints/bridge.py::get_bridge_tool_timeouts`,
    registered in `src/gobby/servers/routes/mcp/tools.py`;
  - `src/gobby/servers/auth_service.py` admits agent tokens on it;
  - the route has a row in `docs/guides/http-endpoints.md`;
  - `tests/servers/routes/mcp_endpoints/test_bridge.py` tests it with
    `test_bridge_tool_timeouts_serve_the_active_config`,
    `test_bridge_tool_timeouts_wait_for_daemon_config`, and
    `test_agent_tokens_may_read_bridge_tool_timeouts`, and
    `test_router_registers_bridge_routes` lists it.
- Two other test files name the setting. The close-orchestration test's config
  stub sets `tool_timeouts={}`. `tests/agents/test_spawn_executor.py` mocks
  the route's answer, and that mock leaves in 1.5.
- `tests/utils/test_posix_spawn.py::_NOT_DAEMON` exempts
  `mcp_proxy/daemon_control.py` and `mcp_proxy/stdio` from the daemon spawn
  lint.
- The web settings page edits it.
  `web/src/components/settings/sections/McpToolsSection.tsx` lists
  `mcp_client_proxy.tool_timeouts` in `PROXY_PATHS` and renders the "Per-tool
  timeouts (seconds)" `NumberMapConfigField`. That editor is the only consumer
  of `configFields.tsx::NumberMapConfigField`. `McpToolsSection.test.tsx`
  edits the field, and `web/tests/style-surfaces.spec.ts` seeds it.
  `scripts/generate_runtime_config_contract.py` emits both the gcore contract
  JSON and `web/src/api/runtimeConfigCodecVectors.gen.ts` from the registry.
  `tests/config/test_runtime_config_contract.py` checks both. The web vectors
  hold no `tool_timeouts` case and have not changed since 2026-09-05, so
  regeneration leaves them unchanged.
  `tests/contracts/http/config_schema.json` is the HTTP corpus snapshot of
  `/api/config/schema`. Semantic lint requires the gcore contract as a carrier
  of any `src/gobby/config/` change.
- `docs/reference-audit/admin.json` has the `gobby mcp-server` row
  (implementation `src/gobby/cli/mcp.py::mcp_server`), and native binaries have
  rows (`ghook --version`, `gclient …`). `tests/skills/` checks them.
- `src/gobby/mcp_proxy/wait_tools.py`, `instructions.py`, and
  `_call_tool_wrapper.py` stay (server importers listed in 1.1 and 1.3).
- #24315 (Parity ledger generator, contract doc and drift test) added
  `scripts/generate_parity_ledger.py` on 2026-10-13. The code index covers
  neither the script nor the status mapping, so both are bare-path targets.
  - `collect_surfaces` imports `register_proxy_tools` to list one
    `MCP stdio gobby:<tool>` row per wrapper tool. It also inventories
    `CLI gobby mcp-server` and `HTTP GET /api/mcp/bridge/tool-timeouts`,
    which this leaf retires.
  - `load_statuses` accepts `proxied`, `delegated`, or
    `dropped by decision #N` from the JSON shards in
    `docs/contracts/parity-ledger-status.d/`, one per classification group,
    merged in filename order. A key in two shards fails generation.
    `render_ledger` raises on a mapped key whose surface is gone unless its
    status is a drop, and keeps dropped rows as evidence.
  - `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`
    fails when the committed `docs/contracts/parity-ledger.md` differs from a
    regeneration, and `test_inventory_contains_all_four_protocols_and_terminal_flip`
    expects `MCP stdio gobby:init_project`.
  - At `97b1bbe055` the twelve stdio rows and both retired surfaces are
    unclassified. Ten classification leaves under #21572 (Parity ledger) are
    queued, so a retired surface may carry a non-drop status before this leaf
    lands, and generation would then fail.
  - #24323 (Parity ledger: shard the status mapping into one file per
    classification group) landed at `c4566fdd76` and replaced the single
    mapping file with those shards. No shard holds the fourteen keys this leaf
    sets, and no rule assigns a key to a group.
- `tests/workflows/test_active_progressive_discovery_guidance.py::ACTIVE_GUIDANCE_FILES`
  lists `stdio_tools.py`, and the test reads every listed file, so deleting
  the module fails it. The tool descriptions it scans move into
  `tools_list.v1.json` (1.2).
- `tests/terminals/test_no_direct_tmux_consumers.py::_OWNED_CONSUMER_PATHS`
  lists `stdio.py` and `stdio_server.py`. A missing path scans nothing, so
  those entries would go stale silently.
- `docs/reference-audit/mcp-servers.json` points the `gobby` `init_project`
  row at `src/gobby/mcp_proxy/stdio_tools.py` symbol
  `register_proxy_tools.init_project`. `tests/skills/reference_library_helpers.py`
  fails a row whose implementation file is missing, and resolves symbols only
  in `.py` files.
- `docs/guides/ghook-development-guide.md` names the stdio proxy's
  `current_terminal_context` as the second terminal-context producer.

**Implementation:**
- Delete the eight modules and the seven wrapper test files. Remove
  `mcp-server` from `cli/__init__.py`'s lazy map and from `criterion_commands.py`.
  Trim `terminal_context.py` to `TERMINAL_CONTEXT_KEYS`.
- Drop the two `_NOT_DAEMON` entries.
- Remove `tool_timeouts` from `MCPClientProxyConfig` and from `registry.py`'s
  mapping patterns, then regenerate with
  `uv run python scripts/generate_runtime_config_contract.py` and refresh the
  HTTP config-schema snapshot. The close-orchestration stub drops the key.
- Delete `get_bridge_tool_timeouts`, its registration, its agent route, its
  `http-endpoints.md` row, and its three tests.
  `test_router_registers_bridge_routes` asserts that
  `GET /api/mcp/bridge/tool-timeouts` is absent from both the router and the
  agent route matrix, and that `/bridge/ready` is still registered.
- The MCP & Tools settings section drops the `tool_timeouts` path and editor,
  and `configFields.tsx` drops `NumberMapConfigField`. The unit test replaces
  the editor case with `omitsRetiredPerToolTimeoutsEditor`, and both web
  fixtures drop the key.
- `admin.json` replaces the `gobby mcp-server` row with a `gmcp` row:
  reference `clients.md`, implementation `crates/gmcp/src/main.rs::main`, and
  verification `public-cli-inventory`.
- `mcp-servers.json` points the `init_project` row at
  `crates/gmcp/src/tools.rs::init_project` (path and symbol).
- `collect_surfaces` lists `MCP stdio gobby:<name>` for each tool in
  `crates/gmcp/contracts/tools_list.v1.json`, and the ledger's sources sentence
  names the `gmcp` stdio client and that fixture. The `remainder.json` shard
  marks the twelve stdio rows `delegated` and records `CLI gobby mcp-server`
  and `HTTP GET /api/mcp/bridge/tool-timeouts` as
  `dropped by decision #23275`. A key that a classification leaf has already
  placed in another shard is updated in that shard instead, because the
  generator rejects duplicate keys.
  Regenerate `docs/contracts/parity-ledger.md` with
  `uv run python scripts/generate_parity_ledger.py`; never edit it by hand.
- The guidance test lists `tools_list.v1.json` in place of `stdio_tools.py`,
  and the tmux test drops the two deleted paths.
- The guides, the development guide, and `README.md` describe `gmcp`. The
  `configuration.md` example drops `tool_timeouts`. The ghook guide names
  `gmcp`'s `crates/gmcp/src/terminal_context.rs::capture` as the second
  producer.

**Granularity:** one leaf. The deletions, the setting, its route, the
references, and the inventories that list them (the parity ledger, the
reference audit, and the guidance scan) are one retirement. Splitting them
would leave the wrapper importable after the inventory forgets it, an
inventory naming deleted files, or the setting and its route served after
their reader is gone.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/ tests/cli/test_cli.py tests/config/ tests/skills/ tests/contracts/test_http_corpus.py tests/servers/routes/mcp_endpoints/test_bridge.py tests/utils/test_posix_spawn.py tests/test_parity_ledger.py tests/workflows/test_active_progressive_discovery_guidance.py tests/terminals/test_no_direct_tmux_consumers.py -q`
and `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_daemon_lifecycle.py --collect-only -q`,
then `npx vitest run src/components/settings/sections/__tests__/McpToolsSection.test.tsx`
and `npx tsc --noEmit` in `web/`, `uv run ruff check src/ scripts/generate_parity_ledger.py`,
and `uv run mypy src/`. The parity-ledger test imports the generator, which
the code index does not cover, so a stale import fails that run.
Then run these five searches:
- `gcode grep -F "mcp_proxy.stdio" src tests`
- `gcode grep -F "mcp_proxy/stdio" src tests`
- `gcode grep -w "daemon_control" src tests`
- `gcode grep -F "tool_timeouts" src tests web`
- `gcode grep -F "tool-timeouts" src tests web docs/guides`

They must find no consumer in production code, docs, the settings editor, or
tests of the retired wrapper. The only hits allowed are negative retirement
assertions in the tests that 1.7.1, 1.7.6, and 1.7.7 name.

Consumers unchanged:
- `tests/test_parity_ledger.py` — no-edit-reason: its `MCP stdio gobby:init_project` expectation holds, because the stdio rows now come from `gmcp`'s fixture, and its drift test checks the regenerated ledger.

**Acceptance:**

- 1.7.1 - No module under `src/` or `tests/` imports the stdio modules or `daemon_control`, and the eight files are gone. test: `tests/cli/test_cli.py::test_mcp_server_command_is_absent`.
- 1.7.2 - `TERMINAL_CONTEXT_KEYS` still serves `request_context.py`, and the contract test still passes. test: `tests/mcp_proxy/test_gmcp_contracts.py::test_wrapper_contract_matches_python_constants`.
- 1.7.3 - `mcp_client_proxy.tool_timeouts` is absent from the config model, the registry, the gcore contract, and the HTTP schema snapshot. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.7.4 - The reference audit carries a `gmcp` row and no `gobby mcp-server` row. behavior: "gmcp" in `docs/reference-audit/admin.json`.
- 1.7.5 - The guides describe `gmcp` as the stdio transport. behavior: "gmcp" in `docs/guides/mcp-tools.md`.
- 1.7.6 - The MCP & Tools settings section renders no Per-tool timeouts editor, and `NumberMapConfigField` no longer exists. test: `web/src/components/settings/sections/__tests__/McpToolsSection.test.tsx::omitsRetiredPerToolTimeoutsEditor`.
- 1.7.7 - `GET /api/mcp/bridge/tool-timeouts` is neither registered nor in the agent route matrix, and `POST /api/mcp/bridge/ready` is still registered. test: `tests/servers/routes/mcp_endpoints/test_bridge.py::test_router_registers_bridge_routes`.
- 1.7.8 - The parity ledger lists the twelve stdio tools from `gmcp`'s fixture as `delegated`, records `CLI gobby mcp-server` and `HTTP GET /api/mcp/bridge/tool-timeouts` as dropped by decision #23275, and the committed ledger equals a regeneration. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.
- 1.7.9 - The reference audit covers every stdio tool in `gmcp`'s fixture, and the `gobby` `init_project` row points at an existing `gmcp` implementation file. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

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
- `tests/agents/test_sandbox_policy.py::*` — scope-reason: imports `render_srt_settings` from `srt_settings`
- `tests/agents/test_archivist_sandbox.py::*` — scope-reason: imports `render_srt_settings` from `srt_settings`; `SandboxLaunch` stays imported from `srt_runtime`
- `tests/agents/test_spawn_executor_providers.py::*` — scope-reason: imports `render_srt_settings` from `srt_settings`; `SandboxLaunch` and `SrtRuntimeError` stay imported from `srt_runtime`
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
- `src/gobby/agents/srt_runtime.py::render_srt_settings` (lines 547 to 610)
  holds the loopback-egress comment that names `gobby mcp-server`. Its
  consumers are `prepare_sandbox_launch` (lines 628 to 885) and the six test
  files above.
- `src/gobby/agents/sandbox_policy.py::gobby_read_exceptions` (lines 367 to
  399) holds the `uv` comments about the wrapper subprocess. Its only code
  consumer is `sandbox.py`;
  `docs/architecture/sandbox-hub-credential-isolation.md` names it without a
  module path, so that doc needs no edit. Its `uv` cache and binary read roots
  still serve agent shell `uv run` commands.
  `mcp_config_read_exceptions` is lines 414 to 442.
- `sandbox_policy.py` has 958 lines and `srt_runtime.py` has 956, so
  `production-size-growth` requires the moves (Decision 16). The credential
  helpers already live in `sandbox_credentials.py` (`23328c7392`).

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
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_sandbox.py tests/agents/test_sandbox_policy.py tests/agents/test_srt_runtime.py tests/agents/test_srt_filesystem_integration.py tests/agents/test_external_write_grants.py tests/agents/test_archivist_sandbox.py tests/agents/test_spawn_executor_providers.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

**Acceptance:**

- 1.8.1 - An isolated workspace whose `.mcp.json` holds the `gmcp` entry gets no main-repo read root, and `mcp_config_read_exceptions` no longer exists. test: `tests/agents/test_sandbox.py::test_isolated_gmcp_entry_adds_no_project_read_root`.
- 1.8.2 - The rendered SRT settings are unchanged by the move. test: `tests/agents/test_srt_runtime.py::test_render_settings_uses_srt_credential_schema`.
- 1.8.3 - The sandbox read paths keep the `uv` cache roots after the move. test: `tests/agents/test_sandbox.py::test_package_installs_use_explicit_per_run_cache_paths`.
- 1.8.4 - The loopback-egress comment names `gmcp`. behavior: "gmcp" in `src/gobby/agents/srt_settings.py`.

### 1.9 `gmcp` starts a stopped local daemon at launch [category: code] (depends: 1.4, 1.5)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gmcp/Cargo.toml`
- `crates/gmcp/src/lib.rs`
- `crates/gmcp/src/main.rs`
- `crates/gmcp/src/daemon.rs`
- `crates/gmcp/src/daemon_start.rs`
- `crates/gmcp/tests/daemon_start.rs`
- `tests/e2e/test_daemon_lifecycle.py::*` — scope-reason: `TestBridgeAutoStart` opens a `gmcp` session instead of running `_BRIDGE_AUTO_START`, which leaves; the helpers `TestFrontDoor` shares stay

**Research context:**
- `src/gobby/mcp_proxy/stdio_daemon.py::ensure_daemon_running` runs as a task
  beside stdio, so `initialize` never waits on it (memory 728c2ca0). In order:
  - A dial target whose host is not `localhost`, `127.0.0.1`, or `::1` gets one
    health probe and an error log if it fails. It is never started.
  - A running daemon gets one probe and a warning if it fails, and stdio is
    served anyway.
  - With `GOBBY_AGENT_RUN_ID` non-empty, it logs an error and refuses to start
    a daemon from an agent process.
  - Otherwise it starts the daemon, then probes `/api/health` up to
    `src/gobby/mcp_proxy/stdio_results.py::DAEMON_HEALTH_ATTEMPTS` (30) times,
    with a 2 s timeout and a 1 s delay, and logs an error if no probe succeeds.
- `src/gobby/mcp_proxy/daemon_control.py::start_daemon_process` runs
  `sys.executable -m gobby.cli start` with stdin from `/dev/null` and both
  output streams to one temporary file. A pipe would stay open while a
  grandchild holds it, and the caller's stdio carries the MCP protocol. It
  waits up to 600 s, above `gobby start`'s own 120 s health and 300 s readiness
  waits. On timeout it terminates the child, waits 5 s, and kills it. A
  failure logs the exit code or the timeout and the last 2,000 characters of
  output.
- `gobby start` refuses with exit 1 while the singleton is held
  (`src/gobby/cli/daemon_singleton.py::admit_direct_start`), so a start that
  races a running, restarting, or reserved daemon fails cleanly.
- `crates/gcore/src/lib.rs::gobby_home` resolves `GOBBY_HOME`, then
  `~/.gobby`. `tempfile` 3, `libc`, and tokio's `process` feature are already in
  `Cargo.lock`.
- 1.7 deletes `stdio_results.py`, so the timing values become Rust constants
  in `daemon_start.rs`. They are not server-coupled, so they stay out of the
  wrapper contract (Decision 9).

**Implementation:**
- `Cargo.toml` adds tokio's `process` feature, `tempfile` 3, and `libc` for
  Unix.
- `daemon.rs` adds `Dialer::probe_health()`. It sends the preflight request
  (`GET /api/health`, 2 s) to the current `daemon_url()` and returns
  `Healthy`, `Answered(status)` for any other HTTP answer, the typed 503
  included, or `Unreachable` when the connection fails. `Dialer` still never
  starts a daemon.
- `daemon_start.rs` provides `run_startup(dialer)`, which follows the wrapper's
  rules in this order:
  1. A non-local dial target gets one probe, an error log if it is not
     `Healthy`, and no start.
  2. One probe. `Healthy` returns. `Answered` logs a warning that names the
     status and returns, because a daemon is running.
  3. `Unreachable` with `GOBBY_AGENT_RUN_ID` non-empty logs the refusal and
     returns.
  4. It reads `<gobby_home()>/gmcp-daemon-start.json`, which 1.4 writes. A
     missing file logs that no start command is recorded, with `gobby start`
     guidance. Anything other than a non-empty JSON array of strings logs an
     error. Both return.
  5. It spawns the array as a `tokio::process::Command` with stdin null, both
     output streams on one `tempfile::tempfile()`, and the inherited
     environment and working directory. It waits up to 600 s. On timeout it
     sends `SIGTERM`, waits 5 s, then kills.
  6. Exit 0 starts the 30-probe poll with a 1 s delay. It logs at info when the
     daemon is healthy and at error, with the attempt count, when it is not.
     Any other outcome logs the exit code or the timeout and the output's last
     2,000 characters.
  The wait and the tail length are parameters of an inner function, so unit
  tests can use a short timeout. Logs go to stderr. The output tail is
  `gobby start`'s own console text, and no bearer, key, or header value is
  logged.
- `main.rs` spawns `run_startup` on its runtime after building the `Dialer` and
  before serving stdio, and never awaits it. On stdin close, the existing
  `shutdown_background()` path drops the task. `kill_on_drop` stays false, so
  a running `gobby start` finishes on its own.
- `tests/daemon_start.rs` runs `gmcp` against the stub daemon with a temporary
  `GOBBY_HOME`. Each test writes a record whose array runs a shell script the
  test creates, which writes a marker file and its stdin state.
- `TestBridgeAutoStart::test_gmcp_starts_a_stopped_daemon` builds
  `_real_cli_env` without `GOBBY_AGENT_RUN_ID` or `GOBBY_DAEMON_URL`. It writes
  the home's record with 1.4's `write_daemon_start_command` and the array
  `[sys.executable, "-c", _CLI_WITHOUT_MANAGED_SERVICES, "start"]`, opens a
  session with 1.5's e2e `gmcp` helper in that environment, and waits up to
  300 s for `/api/health` to answer 200. It terminates the daemon's process
  tree from the PID file afterwards.

**Focused verification (planned):** `cargo nextest run -p gobby-mcp` and
`cargo clippy -p gobby-mcp --all-targets -- -D warnings` (heavy work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_daemon_lifecycle.py -q`,
with `gmcp` and `gdaemon` built by `cargo build -p gobby-mcp -p gobby-daemon`
(heavy work).

**Acceptance:**

- 1.9.1 - With a local dial target that refuses connections, no `GOBBY_AGENT_RUN_ID`, and a record in `GOBBY_HOME`, `gmcp` runs the recorded command once at launch with stdin at end of file and output to a file, and logs the daemon healthy after the stub binds. `initialize` answers before the command exits. test: `crates/gmcp/tests/daemon_start.rs::recorded_command_starts_unreachable_local_daemon`.
- 1.9.2 - No command runs when `GOBBY_AGENT_RUN_ID` is set, when the dial target is not local, when the stub answers `/api/health` with 200 or the typed 503, or when the home has no record. The missing-record case logs `gobby start` guidance. test: `crates/gmcp/tests/daemon_start.rs::no_start_without_cause`.
- 1.9.3 - A command that exits non-zero logs its exit code and only the last 2,000 characters of its output. A command that outlives the start timeout gets `SIGTERM`, then a kill 5 s later. test: `crates/gmcp/src/daemon_start.rs::failed_start_logs_tail_and_timeout_terminates`.
- 1.9.4 - Closing stdin while the command runs exits 0 within 2 s, and the command still runs to completion. test: `crates/gmcp/tests/daemon_start.rs::stdin_close_leaves_start_running`.
- 1.9.5 - With an isolated daemon stopped and its home's record written by 1.4's writer, opening a `gmcp` session brings the daemon's `/api/health` to 200. test: `tests/e2e/test_daemon_lifecycle.py::TestBridgeAutoStart::test_gmcp_starts_a_stopped_daemon`.

### 1.10 `gmcp` is a published managed binary [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/install/version_pins.py::*` — scope-reason: `UNPUBLISHED_MANAGED_BINS` drops `gmcp`; 1.4 targets the same file with this scope
- `src/gobby/install/distribution.py::HOMEBREW_HELPERS`
- `src/gobby/install/distribution.py::HOMEBREW_HELPER_FORMULAE`
- `tests/install/test_version_pins.py::*` — scope-reason: `test_published_state_is_explicit_for_managed_binaries` asserts `gmcp` is published; 1.4 targets the same file with this scope
- `tests/install/test_distribution.py::test_homebrew_helper_detection_fails_with_brew_guidance_when_missing`

**Research context:**
- Josh ruled on 2026-10-13 that the tag, crates.io publishing, and the
  Homebrew tap wait for his release authorization. Until then `gmcp` installs
  from the checkout, like `gclient` and `gterm`, which
  `version_pins.py::UNPUBLISHED_MANAGED_BINS` still lists.
- 1.4 pins `gmcp` 0.1.0, lists it as unpublished, and adds
  `release-gmcp.yml`, which publishes `gobby-mcp` to crates.io and uploads
  tarballs on a `gmcp-v*` tag. `install_setup_gmcp.py` follows
  `src/gobby/cli/install_setup_gclient.py`, where `is_published("gclient")`
  chooses the release and cargo paths over the checkout build. Removing `gmcp`
  from the unpublished set therefore switches the installer by itself.
- `distribution.py::verify_homebrew_managed_bins` checks each
  `HOMEBREW_HELPERS` name through its `HOMEBREW_HELPER_FORMULAE` formula on a
  Homebrew-managed install. Listing `gmcp` before `GobbyAI/homebrew-tap` has a
  `gobby-mcp` formula would fail every Homebrew install.
- `test_published_state_is_explicit_for_managed_binaries` asserts the
  unpublished set, and
  `test_homebrew_helper_detection_fails_with_brew_guidance_when_missing`
  asserts the helper tuple and the formula map. The other tests in
  `tests/install/test_distribution.py` iterate `HOMEBREW_HELPERS`, so they
  cover `gmcp` without edits.
- This leaf lands only after Activation step 3 has published `gmcp-v0.1.0`
  and added the formula. Activation step 4 holds it until then.

**Implementation:**
- Remove `gmcp` from `UNPUBLISHED_MANAGED_BINS`.
- Append `gmcp` to `HOMEBREW_HELPERS`, and map it to `gobby-mcp` in
  `HOMEBREW_HELPER_FORMULAE`.
- Update the two tests' expected values.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_version_pins.py tests/install/test_distribution.py tests/cli/test_install_setup_gmcp.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

**Acceptance:**

- 1.10.1 - `is_published("gmcp")` is true, and `gterm` and `gclient` stay unpublished. test: `tests/install/test_version_pins.py::test_published_state_is_explicit_for_managed_binaries`.
- 1.10.2 - A Homebrew-managed install without the `gobby-mcp` formula's `gmcp` fails with brew guidance naming `gobby-mcp`. test: `tests/install/test_distribution.py::test_homebrew_helper_detection_fails_with_brew_guidance_when_missing`.

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
- D3.3: clients are identified by Client ID Metadata Documents. The
  authorization server fetches the HTTPS `client_id` URL, checks that the
  document's `client_id` equals that URL and that the request's redirect URI is
  one the document lists, and caches the document in a client table.
- D3.4: access tokens are bound to the `users` and `api_keys` identity, and the
  front door validates them as the second bearer kind beside `gobby_` keys.
- D3.5: CIMD only (Josh, 2026-10-13; Decision 3). The authorization-server
  metadata advertises Client ID Metadata Document support and has no
  `registration_endpoint`, and no Dynamic Client Registration route exists.
- stdio stays credential-from-environment, as the specification recommends.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "The OAuth authorization server serves the HTTP transport, which is the MCP front door flip (S2.12). Its access tokens become a second bearer kind at the dispatch seam #23519 (Shared-token cutover) built, which closed on 2026-10-09. The task expands held needs-planning, because an authorization server needs its own planning pass before it can be specified."
  owner: "gobby-1.0 stage 2 (S2.12, #21570)"
  original_acceptance_items:
    - D3.1
    - D3.2
    - D3.3
    - D3.4
    - D3.5
```

## Activation
`kind: framing`

These steps are operations. Each one changes the operator's hub or an
external repository, which no implementation leaf may do, so expansion
creates no task for them. Each names its owner and the leaves it waits for.

1. **Pre-switch promotion.** Owner: the Release Manager. It runs after the
   1.1 to 1.4 and 1.9 leaves land, and needs no daemon restart.
   - Run `gobby install gmcp` from the main checkout.
   - Confirm that `~/.gobby/bin/gmcp --version` answers, and that
     `~/.gobby/gmcp-daemon-start.json` names the main checkout's Python.
   - Tell 1.6's Lane Manager that 1.6 may land.
2. **Post-switch smoke proof.** Owner: the Orchestrator. It runs after the
   1.6 leaf lands and its daemon restart completes.
   - A newly spawned Claude agent and a newly spawned Codex agent each list
     Gobby tools within their first turn.
   - During the next daemon restart, a live `gmcp` answers
     `DAEMON_UNAVAILABLE` and recovers afterwards without relaunch. 1.1.5
     and 1.5.2 prove this behavior in tests; this step observes it on the
     hub.
   - The Orchestrator records both observations on #23275.
3. **Release.** Owner: the Orchestrator, on Josh's release authorization. It
   runs after the 1.4 leaf lands.
   - Push the first `gmcp-v0.1.0` tag. `release-gmcp.yml` then publishes
     `gobby-mcp` to crates.io and attaches the release tarballs.
   - Add a `gobby-mcp` formula for `gmcp` to `GobbyAI/homebrew-tap`.
   - Remove the hold that step 4 places on 1.10.
4. **Hold on 1.10.** Owner: the Orchestrator. When expansion creates the
   1.10 leaf, the Orchestrator labels it `needs-decision`, which automated
   dispatch never selects. 1.10 would otherwise land before a release exists.

## Deferral Finalization
`kind: framing`

After Josh approves and expansion applies, the Orchestrator runs these steps
(accepted by the Orchestrator at 12:26 CT on Adv4's finding F3). The plan's
root is #23275. Drafting creates no tasks, labels, or edges.

1. D2, #21570 (MCP front door flip):
   - add `deferred-from:gmcp-stdio:D2`;
   - set its null validation criteria to D2.1 and D2.2 as written here;
   - add `cited-parent:#21544` to it, and `out-of-scope-for:#23275` to #21544
     (Stage 2: strangler absorption of the Python daemon behind the front
     door).
   Both now sit under #21544. A #23275 blocked-by edge on #21570 would make
   this plan wait for the whole MCP front door flip, which no leaf here needs.
   The cited-parent route validates without that edge
   (`src/gobby/plans/deferral.py::_has_valid_cited_parent`), because #21544 is
   open and is not in #23275's dependency closure.
2. D3: expansion creates the task with `deferred-from:gmcp-stdio:D3`, criteria
   D3.1 to D3.5, and `needs-planning` for its own planning pass. D3.5 already
   records Josh's CIMD-only ruling. #23519 (Shared-token cutover: gdaemon
   validates keys and Python trusts only the front door) closed on 2026-10-09,
   so the task needs no edge on it.
3. Run `gobby-tasks:check_dependency_cycles`. Then confirm that the D2 and D3
   deferrals validate against #23275 before releasing any leaf.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15469 under the
  Orchestrator rulings of 11:19, 11:24, and 11:48 CT. Targets and consumers were
  swept read-only on `0.5.0` at `6f5d0c8733`. The draft is narrative only, with
  no M1.
- 2026-10-05: Consensus between the Plan Writer gobby#15469 and the Plan
  Adversary gobby#15471 at `fcc970aa76`.
  - The enhancer pass (run `ee783f28`) proposed E1 to E4. The Orchestrator
    gobby#14972 disposed them at 12:08 CT: E1 accepted as modified (1.4 depends
    on 1.2, D1 on 1.3 and 1.4), and E2, E3, and E4 accepted.
  - The Orchestrator's first Decision 16 ruling put the sandbox edits in 1.6,
    which the `production-size-growth` lint rejects. At 12:12 CT it ruled for a
    new leaf 1.8 that moves code out of the two large files.
  - Adversary findings, all resolved:
    - F1: D1 waited on 1.6 while 1.6 waited on D1. The post-switch proof moved
      to D6.
    - F2: the web Per-tool timeouts editor and `NumberMapConfigField` retire
      in 1.7.
    - F3: the Deferral Finalization ledger was added, accepted by the
      Orchestrator at 12:26 CT. D2 uses the cited-parent route because a
      blocked-by edge would form a cycle.
    - F4: the repair covers every `uv run … gobby mcp-server` shape.
    - N1: 1.6 records its Granularity decision.
  - No disagreement went to the Orchestrator.
- 2026-10-09: Evidence refresh by the Lane 7 Plan Writer gobby#15860, under
  LM7 gobby#15389's assignment, for delta review by the Plan Adversary
  gobby#15401. Josh had not yet approved the plan, and it had no tasks. The
  consumers were re-swept at `ba4aa371f8`.
  - Auth (1.1): gcore's `read_api_key` replaces the removed
    `read_local_cli_token`. The managed grant envelope is now a token source.
    New item 1.1.8 re-reads it per request, as the wrapper does since #23865.
  - Decision 5 is unchanged, but its basis moved. #23525 was fixed at
    `f3d55136b7`, so retiring `daemon_control.py` now removes a working
    auto-start. 1.7 deletes its e2e test and the spawn-lint exemption.
  - Decision 6: #23531 has landed.
  - Decision 12 keeps the approved retirement with an updated rationale.
    1.7 now also retires #23680's `GET /api/mcp/bridge/tool-timeouts`: the
    route, its registration, its agent route, its tests, and its docs row
    (new item 1.7.7). That leaves no reader of `tool_timeouts`.
  - 1.6 gains the managed Grok launch site from #23554 (new item 1.6.6) and
    `test_codex_terminal_direct_spawn`. Because 1.5 shares that test file,
    1.6 now depends on 1.5. The stale `local_token.py` docstring target is
    dropped.
  - Decision 1 rechecks rmcp (3.5.1 resolves from `version = "3.5"`).
    `set_variable` gains `scope`. File sizes and line ranges are re-measured.
    Decision 16 and 1.8 record that `sandbox_credentials.py` has already
    landed. V2 is worded as completion gates.
  - New acceptance items left the M1 manifest stale. Following the PD's
    withdrawal route, the whole `## M1 Task Manifest` block is withdrawn; its
    bytes stay in Git history at `e4e60efccd`. The Adversary derives a fresh
    M1 after delta review.
  - Delta finding F1 (blocking): 1.7's no-hit searches conflicted with 1.7.7,
    whose router test must name the retired route. The searches now allow
    only negative retirement assertions in the tests that 1.7.1, 1.7.6, and
    1.7.7 name. The collect-only pytest run uses the isolated test hub.
- 2026-10-09: Renewed consensus between the Plan Writer gobby#15860 and the
  Plan Adversary gobby#15401 on the refresh, after F1's repair at
  `d2c0bf751b`.
  - The Adversary found no further blocking findings. Its nit N1, the
    `TestExecuteSpawn` class missing from the 1.6 test locator, is fixed in
    this entry's commit.
  - The Adversary verified the following:
    - auth reuse and renewal, including the fail-closed refusal;
    - the three new acceptance items;
    - the Grok entry replacement and preservation;
    - the 1.6 dependency on 1.5 for the shared file;
    - the literal consumers.
  - The leaf routing from the previous M1 is kept, with the new edge from 1.6
    to 1.5. The Adversary derives and applies a fresh M1 from this commit.
  - No disagreement went to the Orchestrator.
- 2026-10-13: Evidence refresh by the Lane 2 Plan Writer gobby#16770, under
  the Orchestrator gobby#15977's assignment, for delta review by the Plan
  Adversary gobby#16771. #23275 had moved under Stage 2 (#21544) that day.
  Josh had not yet approved the plan, and it had no tasks. The consumers were
  re-swept at `97b1bbe055`.
  - Orchestrator rulings of 2026-10-13:
    - D4 is withdrawn, and new item 1.5.5 drives `gmcp` on the `pair_hub` and
      `pair_node` fixtures (ruling 4 revised). Appending D4 to #23274, a Stage
      1 task, would have made Stage 1 wait on this Stage 2 plan.
    - The Program Director and Merge Manager roles no longer exist. D1 belongs
      to the Release Manager, 1.6's Lane Manager holds its landing until D1
      confirms, and D5 and D6 belong to the Orchestrator.
  - New consumers of the retired wrapper join their leaves:
    - in 1.7, #24315's parity ledger generator, status mapping, and ledger
      (new item 1.7.8), the reference-audit `init_project` row (new item
      1.7.9), the guidance and tmux consumer tests, and the ghook guide;
    - in 1.5, #24044's revival test;
    - in 1.8, three more test files that import `render_srt_settings`.
  - 1.5.3 names the renamed `test_run_identity_transport_chain`. 1.5.4 follows
    #23923: the capability travels only in the envelope.
  - D3 waits only on the D3.5 decision, because #23519 has closed. D2's
    Finalization rationale no longer claims a cycle.
  - 1.7 drops the web vectors target, which holds no `tool_timeouts`. The
    heavy-work constraint cites the project operating policy in place of the
    deleted `.gobby/roles/_common.md`. Decision 1 rechecks rmcp (3.5.1 is still
    the latest) and the crate name. Line counts and ranges are re-measured.
  - #24323 (Parity ledger: shard the status mapping into one file per
    classification group) landed at `c4566fdd76`, so 1.7 targets its
    `remainder.json` shard (Orchestrator gobby#15977).
  - New acceptance items left the M1 manifest stale. The whole
    `## M1 Task Manifest` block is withdrawn; its bytes stay in Git history at
    `208d6318fe`. The Adversary derives a fresh M1 after delta review.
- 2026-10-13: Renewed consensus between the Plan Writer gobby#16770 and the
  Plan Adversary gobby#16491 on the refresh at `86ab1cd19d`.
  - The Orchestrator gobby#15977 moved the delta review to gobby#16491,
    because gobby#16771 could not be resumed.
  - The Adversary raised no findings. It verified the following:
    - the D4 withdrawal, and 1.5.5's node fixtures and credential resolution;
    - the new revival, envelope, reference-audit, parity-ledger, guidance,
      tmux, and sandbox consumers;
    - shared-file ordering;
    - the deferral owners and D2's Finalization;
    - every leaf's acceptance, atomicity, and proportionality.
  - The leaf routing from the previous M1 is kept. The Adversary derives and
    applies a fresh M1 from this commit.
  - No disagreement went to the Orchestrator.
- 2026-10-13: Josh bounced the stamped plan at `34e7c28cee` because it held
  open questions. The Plan Writer gobby#16770 listed four, the Orchestrator
  gobby#15977 put them to Josh, and he answered all four that evening:
  - Q1: CIMD only. Decision 3, D3.3, D3.5, the Parent D3 Mapping row, the D3
    deferral reason, and Finalization step 2 record it. D3 keeps
    `needs-planning` for its own planning pass.
  - Q2: add `crates/gmcp` now on rmcp 3.5, with the `server` and stdio
    (`transport-io`) features only. Decision 1 records it.
  - Q3: port the daemon auto-start. Decision 5 is reversed. 1.4 records the
    start command (new item 1.4.6). New leaf 1.9 runs it from `gmcp` and moves
    `TestBridgeAutoStart` to `gmcp` instead of deleting it. 1.7 now depends on
    1.9 and no longer edits `test_daemon_lifecycle.py`. D1 waits for 1.9, so
    the promoted `gmcp` can start the daemon before 1.6 switches launch sites.
    The Isolation constraint gives every `gmcp` test its own `GOBBY_HOME`.
  - Q4: publish on Josh's release authorization. The D5 reason records it.
  - New items left the M1 manifest stale. The whole `## M1 Task Manifest`
    block is withdrawn; its bytes stay in Git history at `34e7c28cee`. Plan
    Adversary gobby#16791 derives a fresh M1 after review.
- 2026-10-13: Renewed consensus between the Plan Writer gobby#16770 and the
  Plan Adversary gobby#16791 on `9caa4d2608`.
  - The Adversary confirmed that the body carries Q1 to Q4.
  - It raised one finding: the V2 close gate stopped at leaf 1.8. V2 now
    covers 1.1 to 1.9, and gate 2 adds
    `tests/e2e/test_daemon_lifecycle.py::TestBridgeAutoStart`.
  - The Adversary derives and applies a fresh M1 from this commit.
  - No disagreement went to the Orchestrator.
- 2026-10-13: Before the plan went to Josh, the Orchestrator gobby#15977
  returned it at `17b17064bc` under his rule against deferred sections. The
  M1 derived from those bytes was not committed.
  - D1 and D6 become Activation steps 1 and 2, operations that name an owner
    and the leaves they wait for. 1.1.5 and 1.5.2 still prove D6.2's behavior.
  - D5 becomes leaf 1.10 for the in-repo publish flags, plus Activation steps
    3 and 4: the tag, the crates.io publish, the tap formula, and the hold on
    1.10 until Josh authorizes the release.
  - Deferral Finalization keeps only the D2 and D3 steps.
  - The sweep also found two stale spots. 1.4 now updates the release guide.
    1.6 now depends on 1.9, and its `clients.md` text keeps the auto-start
    claim that 1.9 ports.
  - D2 and D3 went to the Orchestrator for Josh, with the reasons they cannot
    be planned as leaves here. They stay deferred until he rules.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation,
none has run yet. Each runs after the 1.1 to 1.10 leaves land, and each must
hold before #23275 closes.

1. The focused suites of 1.1 to 1.10 must pass together against the test hub,
   with `cargo nextest run -p gobby-mcp` and
   `cargo clippy -p gobby-mcp --all-targets -- -D warnings` (heavy work).
2. `tests/e2e/test_stateless_ambient_session.py` and
   `tests/e2e/test_daemon_lifecycle.py::TestBridgeAutoStart` must pass against
   an isolated daemon with the checkout's `gmcp`.
3. `uv run gobby plans validate .gobby/plans/gmcp-stdio.md -p <root>` must
   pass, and `gcode grep -F "gobby mcp-server" src` must hit only the 1.6
   repair.
4. Activation step 2 must have recorded the post-switch smoke proof on
   #23275, and Activation step 3 must have published `gmcp-v0.1.0`.

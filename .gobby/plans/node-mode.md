Plan artifact: `.gobby/plans/node-mode.md`

# Node mode: the per-machine daemon that runs only local duties

**Plan ID:** node-mode

## Overview
`kind: framing`

Task #17436 (Node mode: the per-machine daemon that holds no datastore
credentials and runs only local duties) is the root of this plan. A node is a
`gdaemon serve` whose bootstrap says `datastore_mode: remote`. It holds a
machine-bound API key and no database credential. It answers hook ingress,
runs agents and terminals locally, and relays every other request to its hub.
Other plans already own most of that:

- node-install-enrollment (#24657) installs, enrolls, starts and updates a node;
- node-halves (#24150, #24148) owns hook ingress, agents, the terminal host and
  node reports;
- remote-terminal-attach (#20202) owns terminals across machines.

This plan delivers what remains of #17436 in two phases:

- P1 (under #17436): the node tells the hub its platform and capabilities
  (1.1), keeps its own channel state and serves it locally (1.2), and
  `gobby node status` reports node health apart from hub health (1.3).
- P2 (under #21579, Rust node duties): the operator CLI a Python-free node
  needs. It moves client-operator-verbs 1.1 to 1.5 here (2.1 to 2.4 and 2.7),
  adds `gobby node start|stop|restart` (2.5), stops workspace and pane verbs from
  acting on the hub host's panes from a node (2.6), names the Windows
  task in the refusal text (2.8, 2.9) and proves the slice from a Python-free
  node (2.10).
- D1 defers the Rust-only Windows node to #25249.

**Moved sections (R2).** Sections 2.1, 2.2, 2.3, 2.4 and 2.7 are
client-operator-verbs sections 1.1 to 1.5, moved verbatim except for these
changes:

- section and acceptance IDs, cross-references and Decision Record numbers
  (now O1 to O10) are renumbered;
- 2.1 depends on 1.3, because both edit `command.rs` and `help.rs`;
- the token reader is now `gobby_core::local_token::read_api_key()`, and line
  counts are re-read at `babdb0d558` (As-Is Facts);
- each crate-changing section gains the `gclient` patch bump targets and an
  acceptance item;
- the 2.7 inventory is re-walked at `9b148b644c`. It now has 279 commands
  in 65 rows and 107 helper modules in 25 rows. Each row gains a `node` field
  (R7), a `client` array lists the Rust-only verbs, and a drift rule covers
  changes before the executor's starting tip.

The wave-4 #21571 plan (Operator verbs on the client) drops its sections 1.1
to 1.5. It keeps 1.6 (client docs), 1.7 (parity gate) and D1 (hub lifecycle),
which now depend on this plan's 2.7 leaf. This plan has no edge to #21571.

**#25247 (Node operator CLI parity before Python retirement).** The
Orchestrator closes it as superseded at expansion (R3). Its four criteria
map to these sections:

1. "A committed inventory lists every current user-facing operator verb with
   its node-side equivalent and status": 2.7.
2. "Each verb in the inventory runs from a Python-free node on macOS and
   Linux against an isolated hub": 2.10 proves the `carried` rows, and 2.6
   refuses the `#20202` rows. Under R1, every other row reads "not on node
   until #N" and names its owner.
3. "#21571 cross-links this leaf and no longer needs full #21544 completion
   for the hub-node subset": the moved-sections note above. 2.1 to 2.4 depend
   on no #21544 work.
4. "Focused tests pass on the isolated hub": V2.

Josh's rulings (memory 83a9445f) bound the work: the hub-node matrix is macOS
and Linux; a Windows node is a 1.0 obligation after hub-node and is never
silently passed; latency is measured, never gated. No backward compatibility
is kept.

## Ownership Map
`kind: framing`

Rows from `.gobby/plans/research/hub-node-live-task-list-25236.md`:

- F7 (install and enrollment): node-install-enrollment, #24657.
- F8 (node operator CLI, #25247): P2 here.
- F9.1 (attach survives a hub restart): #25217.
- F9.2 (a node restart keeps its terminals): #24872. 2.5's restart reuses
  #24872's restart step.
- F9.3 (hub health and node health are reported apart): 1.3 here, moved off
  #24872 (R5).
- F9.4 and F9.5 (typed unavailability across hub restart and cutover; backup
  and restore): #24875.
- F11.1 (Rust-only Windows node): #25249, deferred as D1 (R4).
- F11.2 (Windows reports "not yet supported" and names the task): 2.8 and 2.9.
- Node duties (hook ingress, agents, terminal host, reports): node-halves.
- Workspace and pane verbs on a node: #20202 (remote terminal attach). 2.6
  refuses them until then (R6).
- Hub lifecycle (`gobby start|stop|restart` on the hub): #21571's wave-4 plan.
- Readiness packet (N1) with the macOS and Linux PASS rows: #25246.
  Latency measurement: #25245, never a gate.

## Decision Record
`kind: framing`

**Rulings.**

Asked by Plan Writer gobby#18264 as D1 to D7. The Orchestrator gobby#17281 ruled
through Triage gobby#17480 on 2026-10-28.

- **R1 CLI slice: option A.** The node slice is:
  - the `node` family: `install`, `enroll`, `list` and `update` from
    node-install-enrollment, plus `node status` (1.3) and
    `node start|stop|restart` (2.5) for the local service;
  - the read and config verbs: `status`, `projects list|show`,
    `tasks list|show|ready|blocked|search` and `config get|set`, all `gclient`
    only;
  - gmcp.

  The slice is proven from a Python-free node on macOS and Linux against an
  isolated hub. Every other inventory row reads "not on node until #N".
  Rejected: B (no read verbs), which leaves a node operator without
  `status`; and C (adding the daily writes), which duplicates the family
  owners' carriers.
- **R2 Sourcing: option (ii).** Move client-operator-verbs 1.1 to 1.5 here
  verbatim, re-verifying the facts. Node-mode owns
  `docs/contracts/cli-inventory.toml` and its per-row `node` field. A typed
  note records that the wave-4 #21571 plan drops 1.1 to 1.5, and there is no
  edge to #21571. Rejected: (i) expanding client-operator-verbs now and
  depending on its leaves, which ties this plan to #21571's
  root; `(depends: #21571)` would form a cycle.
- **R3 #25247.** The CLI phase sits under #21579 through the `(#21579)` phase
  marker. The Orchestrator closes #25247 as superseded at expansion, and its
  four criteria ride on the new leaves (Overview).
- **R4 Windows.** The Orchestrator filed #25249 (Windows Rust-only node, after
  hub-node), an epic under #21542 that depends on #17436 and carries F11.1.
  This plan names it in D1 and delivers F11.2: the `gdaemon serve`
  refusal and the node docs' platform matrix name #25249. F11.1 leaves
  #21579's criteria at expansion.
- **R5 F9.3.** It moves off #24872, and `gobby node status` (1.3) owns it.
- **R6 Workspace and pane verbs on a node: option (a).** These verbs reach the
  daemon WebSocket. On a node, the front door splices that socket to the hub,
  so the verbs would act on the hub host's panes; hub-mode 3.3 records that "the
  hub cannot capture a node's pane". Their inventory rows read "not on node
  until #20202", and the node proof leaves them out. On a node they refuse
  and name #20202 (2.6). The refusal is a small guard: one bootstrap read
  and one check in `dispatch_inner`, plus one method on `verbs::Action`.
- **R7 Inventory node field.** The `node` field takes `carried`, `#N` or
  `hub`. A `hub` row may name its node counterpart in `node_carrier`. The
  re-walk corrections are accepted: 280 commands, six new ones, `mcp-server`
  carried by gmcp, and lifecycle refs on #21571. Since the ruling, #25233
  retired `release batch` (`18b3e49ba1`), so the tree at `9b148b644c` has 279
  commands and five new ones.

**Writer's calls.**

- **W1 Capabilities come from the install.** A node probes them once per
  process, when `run_client` starts. `agents` is available when the node's SRT
  verifier passes, and `terminals` when `<home>/bin/gterm` is executable. They
  ride the hello into the hub's in-memory registry and the machine rows. A
  re-install restarts the service (#24872), so a changed install is
  re-probed.
  - Rejected: a database column. Capabilities belong to a connection and go
    stale when it drops, and a column needs a schema change.
  - Rejected: capability frames during a connection. Nothing changes the
    install without restarting the service.
  - Rejected: a provider-CLI list. Provider hooks are install-time state owned
    by node-install-enrollment 3.4 to 3.8.
- **W2 Registry keeps `register`.** `Registry::register(machine)` keeps its
  signature, because `channel.rs` and many tests call it (`nodes.rs`,
  `node_internal_route.rs`). The hello is stored through a new
  `Registry::set_hello` and read through `Registry::hello`.
- **W3 Hello `capabilities` defaults to empty.** An older node's hello has no
  `capabilities` key. It must still register, so that #24873's typed skew
  refusal reaches it rather than a 1008 close. A malformed or oversized list
  is a bad hello and closes with 1008.
- **W4 Node status is served by the node.** `GET /api/node/status` is
  answered by the node's own front door and authenticated by the node key,
  through hook ingress's `has_node_key`. Rejected: reading node health from
  the hub's machine rows. That cannot answer while the hub is down, and F9.3
  exists for exactly that case.
- **W5 Node restart reuses the install's restart step**, so F9.2 (#24872)
  holds for `gobby node restart` with no second code path.
- **W6 Linux evidence.** CI has no e2e job. The Linux run of 2.10 is recorded
  in #25246's macOS and Linux readiness packet, which the Orchestrator links
  to 2.10's leaf at expansion.

**Moved operator-verb decisions.**

These are client-operator-verbs Decision Record items 2 to 8 and 10 to 12,
renumbered O1 to O10. Item 1 (that plan's rulings) and item 9 (`lease
handoff`; the lease commands no longer exist) do not move.

- **O1 Carrier: `gclient` command mode.** The verbs extend the existing
  hand-rolled parser in `crates/gclient/src/command.rs`, not a second parser.
  Global `VALUE_FLAGS` and `SWITCH_FLAGS`, plus `Parsed::finish` rejecting
  leftovers, already give every verb one exit contract. Rejected: clap. It
  would mean two parsers or a rewrite of the tested exit contract
  (`crates/gclient/tests/command_mode.rs`).
- **O2 One credential path.** Every command-mode verb resolves its token the
  way the workspace verbs do today: `--token-file PATH`, else
  `gobby_core::local_token::read_api_key()`. That reader prefers a managed
  capability, then `GOBBY_AGENT_API_TOKEN`, then the `api_key` in
  `<GOBBY_HOME>/bootstrap.yaml`; on a node that key is the node's machine-bound
  key. 2.1 moves those lines out of `dispatch_inner` into one function (new:
  `command::resolve_token`).
  - Command mode is operator-only in practice, and the daemon enforces it.
    An agent token gets 401 `route_not_permitted` on `/api/config`,
    `/api/projects` and `/api/admin/status`, which are not in
    `_AGENT_CAPABILITY_MATRIX`. It gets 401 `identity_mismatch` on
    `POST /api/mcp/*/tools/*`, because that row binds caller identity: the
    caller must also send its project, session and agent-run headers, and the
    client sends none. Under O4 these exit 3, and the client prints the code.
  - Rejected (client-operator-verbs E1): parsing the agent capability token to
    send those identity headers. Agents already reach the task tools through
    the MCP proxy, and the task asks for operator verbs.
  - Rejected: reading only the bootstrap key with `read_api_key_at`. The doc
    comment on `read_api_key_for` says callers must go through it, because
    sandboxed agent runs are denied the operator file. Bypassing the agent
    capability would also lift an agent run to operator authority.
- **O3 Lazy token for `status`.** `status` probes `GET /api/health` with no
  credential first, and reads the token only for `GET /api/admin/status`.
  A stopped daemon reports "not running" even when no token is readable.
- **O4 Exit codes stay the documented contract.** 0 success. 1 daemon refusal
  or error: a non-2xx reply other than 401, a tool `error` or
  `success: false`, and for `status` a degraded daemon, a typed 503 or a 502.
  2 usage, decided before any token read or request. 3 connection refused or
  timed out, token missing, 401, or a reply that is not JSON. Errors print as
  `gclient {verb}: ...` on stderr.
- **O5 Error bodies are kept, and each verb sees the status first.** A new
  helper, `crates/gclient/src/command/api.rs` (its request function from 1.3,
  its fallback from 2.1), returns the HTTP status with
  the body, parsed when it is JSON. Each verb handles its own documented
  statuses first: `status` reads the typed 503, and `config set` reads 409
  `revision_conflict` and 500 `persistence_indeterminate`. Everything else
  goes to one shared fallback. That fallback renders the config envelope as
  `code: message (path a.b)`, else FastAPI's `detail`, else the 401 `code`.
  It exits 3 for 401 and for a body that is not JSON, and 1 otherwise. The
  TUI's `status_error` in `crates/gclient/src/daemon/rest.rs` maps 401/403
  and 5xx to fixed variants and drops the body, so the helper does not reuse
  it. For the same reason `tasks --project NAME` does not reuse
  `verbs::resolve_project`, which reads the projects through that client;
  it resolves the name through 2.2's operator lookup (Adv3 F2).
- **O6 `tasks` goes through the MCP tool route, not REST.** It calls
  `POST /api/mcp/gobby-tasks/tools/{tool}` with the tool arguments as the
  body. The tool call lives in `tasks.rs`, its only caller (Adv3 F3). The
  REST `/api/tasks` route resolves `#N` against the daemon's own project and
  has no ready, blocked or search view. Project context comes from
  `--project NAME|ID` or the current directory's `.gobby/project.json` and is
  sent both as `X-Gobby-Project-Id` and as the tools' `project` argument. An
  unregistered id then fails loudly instead of listing every project. Writes
  (`create`, `update`, `close`, `reopen` and the rest) require Rust client
  commands owned by S2.4 #21560 (Tasks family), using its write path #21561.
  They remain uncarried until that owner proves CLI parity, which must
  precede the S3.2 switch.
- **O7 `config` uses `/api/config/values`, the service the MCP config tools
  use.** `set` reads the revision, then sends a compare-and-set `PATCH`. On a
  409 `revision_conflict` it re-reads and retries once, as the Python
  `config_writes.py` helper does. The server is the type authority: VALUE is
  sent as a JSON literal when it parses as one, otherwise as a string.
  Rejected: the MCP `patch_config_values` tool, because it fronts the same
  service with a second envelope to decode. `config get|set` is a new verb;
  the Python CLI has no `config` command to retire.
- **O8 Not carried by `status`.** Today's `gobby status` also prints a
  three-way schema-heads line, a lock probe and process metrics.
  - Schema heads: `gdaemon schema verify` checks the live identity receipts,
    and `gdaemon schema version` prints the embedded pin. Neither prints the
    checkout/installed/live line, so `gdaemon schema verify` is the
    operator's schema check until S3.2 #21573 (naming switch with Python
    removed; deletion child #21574) collapses the heads.
  - The lock probe and pid claim belong to the hub lifecycle (#21571).
  - Process metrics are in `/api/admin/status` and print with `--json`.
- **O9 The inventory is a contract file with rows, guarded by tests.** One
  TOML file lists every command row and helper-module row, and each row lists
  the exact commands or modules it decides. Its rows are written in 2.7
  below, so this plan's reviewers review the decisions themselves. The
  tests fail when a click command or a `gobby.cli` helper module is no
  row's member or a member of two rows, or when a member names nothing
  live. A new subcommand inside a known family, such as a future `tasks`
  subcommand, therefore fails too. The change that adds it places it in a
  row on purpose, and the failure message names the file. S3.3 #21572
  (Parity ledger) reads the file.
  - Rejected (Adv3 F1): family rows matched by prefix. Under that rule a
    new descendant silently inherits its family's disposition, and every
    check still passes.
- **O10 Group decisions.**
  - Carried by this plan: `status` (absorbs `health`), `projects list|show`,
    `tasks list|show|ready|blocked|search`, the new `config get|set`, and on a
    node `node status` and `node start|stop|restart`. `node install|enroll`
    (#24860) and `node list|update` (#24877) are carried by
    node-install-enrollment.
  - Hub lifecycle: `start`, `stop` and `restart` are carried by #21571's
    wave-4 plan. On a node, `gobby node start|stop|restart` manage the node's
    own service instead.
  - Absorbed by existing tools: the `panes` verbs that `gclient` already has
    (`close`, `read`, `rename`, `send`, `split`, `wait`), `workspaces show`
    into `gclient list --workspace`, and `schema apply` into
    `gdaemon schema apply`. `mcp-server` is absorbed into `gmcp`, which every
    launch site starts (#24355, closed).
  - Dropped: `test-quality` and `test-types`, because S3.2 #21573 removes
    Python through deletion child #21574. `auth token` and the `lease`,
    `build`, `hub-maintenance`, `profiles` and `stages` commands no longer
    exist in the click tree, so they have no rows.
  - Every other kept row names a planned Rust `gclient` carrier and the
    owning family task as `ref`. The owner must carry all row members
    before S3.2.

## As-Is Facts
`kind: framing`

**Node facts (read at `babdb0d558`).**

- **Channel.** `crates/gdaemon/src/nodes/channel.rs` (439 lines):
  - `Hello` has `kind`, `node_version` and `platform`.
  - `hello_version` returns only the version, so the hub drops `platform`.
  - `hub_channel` closes 4401 for a revoked key, 4409 when the connection is
    replaced, 1008 for a bad hello and 1011 for an internal error. #24873
    adds a refuse frame with close 4426.
  - `connect_client` reads the key with `read_api_key_at(home)`, sends
    `{type, node_version, platform}`, and expects an ack carrying `machine_id`.
  - `run_client` (`#[cfg(unix)]`) retries `connect_client` with backoff from
    1 s to 60 s, records no state, and prints only the first failure.
- **Registry.** `crates/gdaemon/src/nodes/registry.rs` (282 lines):
  - `Registry::register(machine)` returns the connection id, close receiver and
    command receiver. `channel.rs` and the tests in `nodes.rs` and
    `node_internal_route.rs` call it.
  - `Slot` is private and holds the close and command senders.
- **Machine rows.** `crates/gdaemon/src/nodes/machines_api.rs` (198 lines):
  `machine(row, registry)` renders a row and adds `connected` from the
  registry.
- **Node front door.** `crates/gdaemon/src/front_door/mod.rs` (312 lines):
  `FrontDoor::handle`'s `Door::Node` arm answers hook ingress
  (`POST /api/hooks/execute`) and the challenge locally and relays or
  splices everything else to the hub. The relay answers a connect failure
  with a typed 503 whose backend state is down. On a node, therefore,
  `/api/health`, `/api/admin/status`, `/api/config`, `/api/projects` and the
  MCP tool route reach the hub. The hub's front door authenticates the
  node's machine-bound key and stamps the user, machine and key ids.
- **Node services.** `crates/gdaemon/src/state.rs::NodeServices` holds
  `bootstrap_path`, `relay`, `hook_ledger` and `agents`.
  `crates/gdaemon/src/nodes/hooks/ingress.rs::has_node_key` (private, async)
  compares a bearer with `read_api_key_at` in constant time.
- **Install-derived duties.**
  `crates/gdaemon/src/nodes/agents/sandbox/verify.rs::verify_srt_installation`
  returns the verified SRT or a `SandboxError`.
  `crates/gdaemon/src/nodes/agents/host.rs::node_host_options` sets the host
  binary to `<home>/bin/gterm`.
- **Serve.** `crates/gdaemon/src/serve.rs` is 818 lines, near the 850-line
  lint. Its `#[cfg(not(unix))]` `run` bails with "gdaemon serve is
  unsupported on Windows; launch the Python daemon with gobby start".
  `gdaemon` is a library crate with `pub mod serve`.
- **Node detection in gclient.** `gobby_core::bootstrap::read_files_home_view()`
  returns a `FilesHomeView` whose `datastore_mode` is `DatastoreMode::Remote`
  on a node, with the node's `hub_daemon_url`.
- **Not yet on disk.** These files are created by node-install-enrollment
  leaves that this plan depends on: `crates/gclient/src/command/node.rs`,
  `crates/gclient/tests/node_verbs.rs` (#24860);
  `crates/gdaemon/src/node_setup/` with `mod.rs` and `service.rs`,
  `crates/gdaemon/tests/node_setup.rs` and
  `crates/gdaemon/tests/node_setup_support/mod.rs` (#24859, #24872);
  `docs/guides/node-setup.md` (#24878). #24860 routes `node install|enroll` in
  `dispatch_inner` to `node::exec_gdaemon` before the URL or token is read,
  and exits 2 on Windows with "gobby node runs on macOS and Linux".
- **e2e.** `tests/e2e/test_hub_node_pair.py` has the `pair_hub` and `pair_node`
  fixtures. `pair_node` runs a bare `gdaemon serve` with a temporary
  `GOBBY_HOME` whose bootstrap has `datastore_mode: remote`, `hub_daemon_url`
  and `api_key`. It drops `DATABASE_URL` and resolves binaries through
  `gobby.utils.native_bin`. It enrolls through Python `_login` until #24863.
  `test_gmcp_on_node_reaches_hub_through_relay` proves gmcp on a node. CI has
  no e2e job.
- **Release.** `gclient` is 0.1.89 and is in `UNPUBLISHED_MANAGED_BINS`;
  `gdaemon` is 0.4.113. Release source identity
  (`src/gobby/install/bin_release_identity.py::workspace_source_identity`)
  skips `tests` directories and `tests.rs` files, so a test-only change needs
  no bump.

**Moved operator-verb facts.**

Client-operator-verbs read this evidence through `gcode evidence` at
`dfec42f1b2`, and its post-review citations at `c3a4fe3763`. Hashes are the
first 16 hex characters. At `babdb0d558` the cited symbols were re-read for
`command.rs`, `help.rs`, `verbs.rs` and `local_token.rs`; the click tree was
re-walked at `9b148b644c`. Drift: `command.rs` is 227 lines with `dispatch_inner` at
169-223 and the token read at 198-205; `help.rs` is 127 lines (`VERBS` 12-89,
`COMMON` 91-99); `verbs.rs` is 628 lines with `resolve_project` at 597;
`rest.rs` is 337 lines; `mock_daemon/mod.rs` is 1,490 lines. Older line
ranges below are kept as cited.

- **Command mode.** `crates/gclient/src/startup.rs:674-700`
  (`39513405fc71c9b7`) sends any first argument without a leading dash to
  `command::dispatch`. `crates/gclient/src/command.rs:86-160`
  (`d01d1ae775e67c31`) holds `VALUE_FLAGS`, `SWITCH_FLAGS` and `parse`, which
  refuses an unknown verb through `help::is_verb` and an unknown option, and
  reads any argument starting with `-` as an option. `dispatch_inner` calls
  `verbs::Action::parse`, then reads the token before running any verb, then
  prints `output.result` for `--json` or `output.plain`.
- **Help.** `crates/gclient/src/command/help.rs`: `VERBS` is the verb table,
  and `COMMON` begins "Every verb takes --workspace REF, --json, --daemon-url
  URL and --token-file PATH."
- **Shared output.** `crates/gclient/src/command/verbs.rs`:
  `CommandOutput { result, plain, status }` is `pub(super)`.
  `resolve_project` passes a UUID through, matches a name against
  `GET /api/projects`, and fails with exit 2 for an unknown name. Its one
  caller is `Action::run`.
- **Token.** `crates/gcore/src/local_token.rs`: `read_api_key()` calls
  `read_api_key_for(gobby_home())`, which returns a managed capability, else
  `GOBBY_AGENT_API_TOKEN`, else `read_api_key_at`, the `api_key` in
  `<home>/bootstrap.yaml`. `crates/gcore/src/lib.rs:28-40`
  (`52666a46b4fe1cfc`): `gobby_home` honours `GOBBY_HOME`.
- **Agent capability.** `src/gobby/servers/auth_service.py`:
  `_AGENT_CAPABILITY_MATRIX` includes `POST /api/mcp/*/tools/*` with
  `bind_identity` true and excludes `/api/config`, `/api/projects` and
  `/api/admin/status`. A bound row needs the caller project and session
  headers plus a matching agent-run or managed-execution header. Rejections
  are `route_not_permitted` or `identity_mismatch`, rendered as a 401 body
  code.
- **TUI REST errors.** `crates/gclient/src/daemon/rest.rs:300-337`
  (`252bda97334e4dfe`): `status_error` drops the body for 401, 403 and 5xx.
- **Health.** `src/gobby/servers/routes/admin/_health.py:370-400`
  (`e5403f42ed1136f2`): `/api/health` returns `status` (`ok` or `degraded`),
  `mode`, `degraded_services` and `hook_runtime`.
  `crates/gdaemon/src/front_door/health.rs:30-100` (`1389354389956129`): while
  the backend is not serving, gdaemon answers 503
  `{"status":"unavailable","backend":{"state","target"}}` with `Retry-After: 1`,
  and 502 `bad_gateway` when the proxy fails. `_health.py:730-774`
  (`3446a47a31f50b4d`): `/api/admin/status` returns `status`,
  `degraded_services`, `server.uptime_seconds`, `process` and counts. Today's
  Python command is `src/gobby/cli/daemon_health.py:1-132`
  (`ca2e7360b02319ba`).
- **Schema heads.** `src/gobby/storage/schema_divergence.py:41-72`
  (`52b365196e77f558`) renders the three-way line, and `203-234`
  (`c30a711f6bdcef8c`) reads it straight from the database.
  `crates/gdaemon/src/main.rs` has `schema apply`, `schema verify` and
  `schema version`. `src/gobby/cli/schema.py` is the Python `schema apply`.
- **Projects.** `src/gobby/servers/routes/projects.py:271-334`
  (`664500e858764cfe`): `GET /api/projects` lists every project except
  `_orphaned`, `_migrated` and `_global`. `projects.py:195-250`
  (`3970b3e7c95d790b`): each row is the project plus `display_name`,
  `session_count`, `last_activity_at`, `open_task_count` and
  `checkout {machine_id, root_path}` or null.
- **MCP tool route.** `src/gobby/servers/routes/mcp/tools.py:40-56`
  (`5b37f447feebf47e`) mounts `mcp_proxy` at
  `POST /api/mcp/{server_name}/tools/{tool_name}`.
  `src/gobby/servers/routes/mcp/endpoints/execution.py:761-900`
  (`982064498ef61875`): the body is the arguments object; success returns
  `{"success": true, "result": <tool dict>}`, and a failure is HTTP 200 with a
  flat `success: false`. `execution.py:47-196` (`a4d1f079d36f8b8c`) enforces
  workflow rules only for a wrapper or agent principal, so an operator call
  needs no session.
  `src/gobby/servers/routes/mcp/endpoints/request_context.py:150-330`
  (`f260734abe5e052f`, `875a2f33f7b36c6d`, `72328bdb25e17651`) seeds the
  project from request headers. `src/gobby/utils/session_context.py:187-216`
  (`003498d8d6cf89bc`) accepts a UUID or a name in the project header and
  leaves no project context when it cannot resolve one.
- **Task tools.** `src/gobby/mcp_proxy/tools/tasks/_crud.py:795-851`
  (`19bd4443846dfdd8`): `list_tasks(current_stage_state, priority, task_type,
  label, closed, parent_task_id, title_like, limit=50, all_projects, project)`.
  `_crud.py:489-525` (`66cb790b5c78e074`): `get_task(task_id, brief=true)`,
  which returns `found: false` for an unknown ref.
  `src/gobby/mcp_proxy/tools/task_readiness.py:433-560`
  (`c08fadad39c51568`): `list_ready_tasks(priority, task_type,
  parent_task_id, limit=10, all_projects, project)` and
  `list_blocked_tasks(parent_task_id, limit=20, all_projects, project)`.
  `src/gobby/mcp_proxy/tools/tasks/_search.py:29-60` (`f6fc87bc7a72aa7a`) and
  `60-115` (`e1c8362afd2eadec`): `search_tasks(query, ...)`. All four list
  tools return `{tasks, count}` and report failure as an `error` key.
  `src/gobby/mcp_proxy/tools/tasks/_context.py:268-295` (`360a6e6592dacfc3`)
  fails an unregistered `project` argument. `_resolution.py:12-55`
  (`1bc093cea91da3a0`) cannot resolve `#N` without project context.
- **Config.** `src/gobby/servers/routes/configuration.py:45-55`
  (`500b02233a125fa6`) mounts the router at `/api/config`.
  `src/gobby/servers/routes/configuration_values.py:1-58`
  (`60856eaa1de87479`): `GET /values` and `PATCH /values`.
  `src/gobby/config/values.py`:
  - `582-600` (`ed4f6a567a50a157`): the read returns `revision`, nested
    `desired` and `active`, `secret_set`, `pending_restart_keys` and
    `failed_live_keys`, with secrets masked and non-public keys left out.
  - `342-380` (`26e41737ef7300f0`): the patch flattens dotted keys.
  - `428-442` (`cb7944fb4b6c7215`): a stale revision is 409
    `revision_conflict`.
  - `532-546` (`8e02ed6129bbba4a`): 422 `managed_activation_required`
    carries an `action`.
  - `638-653` (`dbf685d0a801a4c6`): success returns `apply_status`
    (`applied`, `pending_restart` or `failed_live`) and `changed_keys`.
  - `72-109` (`8af385b18691c0c5`): `ConfigValuesError.public_body` is the
    error envelope.
  `src/gobby/cli/config_writes.py:23-54` (`4b8ae8622155fbc6`) retries a
  conflict once.
- **Tests.** `crates/gclient/tests/command_mode.rs` lists 11 verbs in
  `VERBS`, asserts the `COMMON` wording in
  `help_documents_one_workspace_contract_for_every_verb`, and has its own
  `invoke` helper. `crates/gclient/tests/mock_daemon/mod.rs`:
  `RequestRecord { method, target, authorization, body }`;
  `enqueue(method, path_prefix, status, body)` queues replies first-match by
  method and prefix; a request without the bearer token gets 401 before it
  is recorded; any status code can be written.
- **Click tree.** Walking `gobby.cli.cli` at `9b148b644c` gives 279 checked
  commands: every non-group command plus each group that runs without a
  subcommand (`hub-backup`, `memory dream` and `auth key`). The `gobby.cli`
  package has 107 helper modules, which are no command's callback module.
  `gobby.test_quality.cli` and `gobby.test_types.cli` are the only callback
  modules outside the package.

## Constraints
`kind: framing`

- Rust work loads the `rust` skill and follows `crates/CLAUDE.md`. Each gclient
  deliverable passes
  `cargo clippy -p gobby-client --all-targets -- -D warnings` and
  `crates/gclient/tests/source_size.rs`; each gdaemon deliverable passes
  `cargo clippy -p gobby-daemon --all-targets -- -D warnings`. Run nextest
  with `CARGO_BUILD_JOBS=3` and `-p`. No production file reaches 1,000 lines.
- `command.rs` (227) and `help.rs` (127) are far below the 850-line lint, and
  each verb lives in its own new module. 2.1 to 2.4 do not edit `verbs.rs`;
  2.6 adds one method to it (628 lines).
- `serve.rs` is 818 lines. 2.8 adds one constant and changes one statement.
  No other section here edits it.
- Unit tests live in `<module>/tests.rs`, declared with `#[path]`, so they do
  not count toward a production file.
- No new crate dependency.
- Release chain: each section that changes `gclient` or `gdaemon` source
  bumps that binary one patch version in `crates/<crate>/Cargo.toml`,
  `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, proven by
  `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
  Test-only and docs-only sections need no bump.
- Consumer sweeps at `babdb0d558`:
  - `gcode grep -w 'dispatch_inner|VALUE_FLAGS|SWITCH_FLAGS' crates/gclient`
    finds only `command.rs`.
  - `gcode grep -w 'COMMON|VERBS' crates/gclient` finds `help.rs` and the test
    file's own `VERBS` in `command_mode.rs`.
  - `Registry::register` callers stay unchanged (W2).
  - `has_node_key` has one caller in `ingress.rs`; 1.2 adds the second.
- `command.rs` is targeted as `::*` in every section, because the validator
  takes one scope form per file. 2.1 to 2.4 each add a module declaration,
  which is no indexed symbol, beside its flags and its dispatch arm, as in
  the moved plan. 1.3, 2.5 and 2.6 change only `dispatch_inner`.
- Files created by upstream leaves are targeted as bare paths, because they
  do not exist at drafting (As-Is Facts, Not yet on disk). If one exists
  when the manifest is derived, the writer converts its targets to exact
  symbols or a scoped `::*` first.
- Shared targets inside this plan are ordered by one linear chain:
  1.1, 1.2, 1.3, 2.1 to 2.10.
- The client sends no credential to `/api/health`. Only the node-status
  paths (1.3, 2.5) and the pane guard (2.6) read `~/.gobby/bootstrap.yaml`
  directly, through `gobby_core::bootstrap`.

## Cross-Plan Edges
`kind: framing`

- **1.1 depends on #24875, #24376 and #25210.**
  - #24875 ends node-install-enrollment's handshake chain (#24873, #24874,
    #24875). That chain edits `Hello`, `connect_client`, `run_client`, the
    registry's refusal map and `machine`.
  - #24376 (node-halves 2.7) routes `command` frames in `run_client`.
  - #25210 ends remote-terminal-attach's chain (#25207 to #25210), which edits
    `registry.rs`, `connect_client` (extracting `dial_hub`) and
    `FrontDoor::handle`. These are the same symbols that 1.1 and 1.2 edit.
    That chain is already expanded on the 1.0 critical path and needs nothing
    from this plan.
- **1.2 and #25213.** Remote-terminal-attach 3.2 (#25213) edits
  `state.rs::RuntimeState`, and 1.2 edits `state.rs::NodeServices`.
  #25213's chain (#24578, #24580, #25212) is deeper than 1.2's, so the
  Orchestrator adds "#25213 blocked by the 1.2 leaf" at expansion. Gating 1.2
  on #25213 would hold node status behind terminal-family work.
- **1.3 depends on #24877.** That puts the `node` verb, `node.rs` and
  `node list|update` (#24860, #24877) in place first.
- **2.5 depends on #24872.** That puts the node service files and the
  install's restart step in place first.
- **2.8 depends on #24875**, the last node-install-enrollment edit to
  `serve.rs`. Node-halves 1.6, 2.2, 2.3, 2.8, 2.14 and 2.15 also edit
  `serve.rs`'s unix `run`. 2.8 edits only the module constants and the
  non-unix `run`, and the Orchestrator orders it after them at expansion.
- **2.9 depends on #24878**, which creates `docs/guides/node-setup.md`.
- **2.10 depends on #24863**, after which `pair_node` enrolls with
  `gobby node enroll`. The Orchestrator adds "#25246 blocked by the 2.10 leaf"
  at expansion, so the readiness packet records 2.10's macOS and Linux runs
  (W6).
- **#21571.** Its wave-4 plan drops 1.1 to 1.5, and its 1.6 and 1.7 depend
  on this plan's 2.7 leaf. Node-mode has no edge to #21571.
- **Expansion actions for the Orchestrator:**
  - close #25247 as superseded (R3);
  - label #25249 `deferred-from:node-mode:D1`;
  - remove F11.1 from #21579's criteria (R4);
  - make #25213 blocked by the 1.2 leaf;
  - make #25246 blocked by the 2.10 leaf.

## P1: Node registration and status
`kind: framing`

**Goal:** the hub sees each node's platform and capabilities, and the node
reports its own channel state and capabilities without asking the hub.

### 1.1 Node hello carries platform and capabilities [category: code] (depends: #24875, #24376, #25210)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/channel.rs::Hello`
- `crates/gdaemon/src/nodes/channel.rs::hello_version`
- `crates/gdaemon/src/nodes/channel.rs::hub_channel`
- `crates/gdaemon/src/nodes/channel.rs::connect_client`
- `crates/gdaemon/src/nodes/channel.rs::run_client`
- `crates/gdaemon/src/nodes/registry.rs::*` — scope-reason: the private `Slot` gains the stored hello, and `Registry` gains `set_hello` and `hello`
- `crates/gdaemon/src/nodes/machines_api.rs::machine`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the `capabilities` module
- `crates/gdaemon/src/nodes/capabilities.rs`
- `crates/gdaemon/src/nodes/capabilities/tests.rs`
- `crates/gdaemon/tests/node_capabilities.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`crates/gdaemon/src/nodes/capabilities.rs` is new:

- `Capability { name: String, available: bool, reason: Option<String> }`,
  serialized as `{name, available, reason}`.
- `NodeHello { platform: String, capabilities: Vec<Capability> }`.
- `probe(home) -> Vec<Capability>` returns two entries in this order.
  - `agents` is available when
    `nodes::agents::sandbox::verify::verify_srt_installation(home)` returns
    `Ok`; otherwise `reason` is the error's display text.
  - `terminals` is available when `<home>/bin/gterm` is an executable file,
    the path that `nodes::agents::host::node_host_options` gives the host;
    otherwise `reason` names the path and says it is missing or not
    executable.
- `validate(&[Capability])` refuses more than 8 entries, a duplicate name, a
  `name` longer than 32 bytes or a `reason` longer than 512 bytes.

Node side: `run_client` calls `probe` once through
`tokio::task::spawn_blocking` before its first attempt and passes the list to
every `connect_client` attempt, so reconnects send the same list.
`connect_client` adds `capabilities` to the hello it sends today.

Hub side:

- `Hello` gains `#[serde(default)] capabilities: Vec<Capability>` (W3).
- `hello_version` becomes a parse that returns the version and a `NodeHello`
  for `hub_channel`. A list that fails `validate` is a bad hello and closes
  1008, as an unparsable hello does today, with no registration.
- After `Registry::register`, `hub_channel` calls
  `Registry::set_hello(machine, connection, hello)` before it sends the ack.
- `set_hello` stores the hello on the slot only when `connection` is still the
  slot's connection. `Registry::hello(machine)` returns a clone while the
  machine is connected, else `None`.
- `machines_api.rs::machine` adds `platform` and `capabilities` from
  `Registry::hello`, both null when the machine is not connected.

**Research context:**
- Observed: the hub already parses `platform` and drops it (`hello_version`).
  The probe functions and the gterm path are landed (As-Is Facts, Node facts).
- Observed: `Registry::register` has many test callers, so its signature stays
  (W2).
- The hub needs no schema change: capabilities live with the connection (W1).
- Planned checks:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_capabilities) | test(capabilities)'`
  and `cargo clippy -p gobby-daemon --all-targets -- -D warnings`.

**Granularity:** seven acceptance items across five production files and the
release files. One outcome: the hello carries the node's capabilities to the
hub's rows. The probe has no consumer without the hello, and the hub fields
have no source without the probe, so neither half can land alone.

**Acceptance:**

- 1.1.1 - `probe` reports `agents` available when the SRT verifier passes,
  and unavailable with the verifier's error text when it fails. test:
  `crates/gdaemon/src/nodes/capabilities/tests.rs::probe_reports_agents_from_srt_verifier`.
- 1.1.2 - `probe` reports `terminals` available for an executable
  `<home>/bin/gterm`, a path equal to `node_host_options`' binary, and
  unavailable naming the path when it is missing or not executable. test:
  `crates/gdaemon/src/nodes/capabilities/tests.rs::probe_reports_terminals_from_installed_gterm`.
- 1.1.3 - A hello with 9 entries, a duplicate name, a 33-byte name or a
  513-byte reason closes 1008 and registers nothing. test:
  `crates/gdaemon/tests/node_capabilities.rs::hub_closes_invalid_capabilities_with_1008`.
- 1.1.4 - A connected node's `GET /api/machines` row shows the platform and
  capabilities from its hello. After the channel drops, the row shows
  `connected: false` and null for both. test:
  `crates/gdaemon/tests/node_capabilities.rs::machine_rows_show_hello_platform_and_capabilities`.
- 1.1.5 - A hello with no `capabilities` key registers with an empty list.
  test:
  `crates/gdaemon/tests/node_capabilities.rs::hello_without_capabilities_registers_empty_list`.
- 1.1.6 - A node's hello carries the probed list, and a reconnect sends the
  same list without probing again. test:
  `crates/gdaemon/tests/node_capabilities.rs::node_hello_carries_capabilities_probed_once`.
- 1.1.7 - `gdaemon` is one patch version above the executor's starting tip in
  `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.2 Node channel state and `GET /api/node/status` [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/channel_status.rs`
- `crates/gdaemon/src/nodes/channel_status/tests.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the `channel_status` module
- `crates/gdaemon/src/nodes/channel.rs::connect_client`
- `crates/gdaemon/src/nodes/channel.rs::run_client`
- `crates/gdaemon/src/state.rs::NodeServices`
- `crates/gdaemon/src/state.rs::NodeServices::prepare`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/src/nodes/hooks/ingress.rs::has_node_key`
- `crates/gdaemon/tests/node_status.rs`
- `crates/gdaemon/tests/node_support/fake_hub.rs::*` — scope-reason: the fake hub answers the channel upgrade with an ack, a 401, a 4401 close, a refuse frame or a drop after the ack, chosen per test
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`crates/gdaemon/src/nodes/channel_status.rs` is new. `ChannelStatus` holds
the capability list and a `ChannelState` behind a mutex, each state with
`since` (UTC):

- `Connecting`: set by `NodeServices::prepare` and kept until the first
  outcome.
- `Connected`: the ack arrived.
- `HubUnavailable { error }`: TCP, TLS or timeout failure, or an upgrade
  answered with a status other than 101 or 401.
- `AuthFailed { error }`: an upgrade answered 401, or a 4401 close.
- `Refused { refusal }`: #24873's refuse frame, holding its refusal object.
- `Disconnected { error }`: the channel closed after the ack (4409, 1011, an
  idle timeout or a dropped socket), with the close code and reason.

A failure state stays through the backoff until the next outcome, so status
shows why the node is not connected. `run_client` records the probed list
once (1.1) and each outcome of `connect_client`. `NodeServices` gains
`channel: Arc<ChannelStatus>`.

The `Door::Node` arm of `FrontDoor::handle` answers `GET /api/node/status`
locally, before the relay. The bearer must pass `has_node_key`, which becomes
`pub(crate)`. Otherwise the reply is 401 `{"code": "node_key_required"}` and
nothing reaches the hub. The 200 body is:

```json
{
  "machine_id": "<gobby_core::machine::read_machine_id_from_home or null>",
  "node_version": "<CARGO_PKG_VERSION>",
  "platform": "<the platform string the hello sends>",
  "hub": "<bootstrap hub_daemon_url>",
  "channel": {"state": "connected", "since": "<RFC 3339>", "error": null, "refusal": null},
  "capabilities": [{"name": "agents", "available": true, "reason": null}]
}
```

`state` is one of `connecting`, `connected`, `hub_unavailable`,
`auth_failed`, `refused` and `disconnected`. `capabilities` is empty until
the probe finishes. `machine_id` has one source in every state, the home's
`machine_id` file, because enrollment sends that id and the hub acks the same
one; `connect_client` keeps its existing ack check.

**Research context:**
- Observed: `run_client` records no state today, and the node front door
  relays every path except hook ingress and the challenge (As-Is Facts).
- The route is local so that it answers while the hub is down (W4, F9.3).
- `has_node_key` is the existing constant-time check against the node's
  bootstrap key, so the route adds no second credential path.
- node-install-enrollment 2.3 (#24860) creates `<gobby_home>/machine_id`,
  sends it to the hub and checks that the reply's `machine_id` is ours. The
  hub acks the channel with the machine row the key belongs to, so the ack's
  id and the file's id are the same, and `ChannelState` stores neither.
- Planned checks:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_status) | test(channel_status)'`.

**Granularity:** eight acceptance items across five production files and the
release files. One outcome: the node keeps its channel state and serves it
locally. Recording state that nothing serves fails clippy's `dead_code`, and
serving state that is never recorded reports nothing. Each failure class has
its own item, because each comes from a different reply.

**Acceptance:**

- 1.2.1 - A new `ChannelStatus` reads `connecting`, and a failure state
  persists until the next outcome replaces it. test:
  `crates/gdaemon/src/nodes/channel_status/tests.rs::status_starts_connecting_and_keeps_last_failure`.
- 1.2.2 - With the fake hub acking, `GET /api/node/status` with the node key
  returns `connected` with the home's `machine_id`, plus the node version,
  platform, hub URL and probed capabilities. test:
  `crates/gdaemon/tests/node_status.rs::status_reports_connected_channel`.
- 1.2.3 - With nothing listening at the hub URL, the route answers 200 with
  `hub_unavailable` and the connect error. test:
  `crates/gdaemon/tests/node_status.rs::status_reports_unreachable_hub`.
- 1.2.4 - An upgrade answered 401 and a 4401 close each report
  `auth_failed`. test:
  `crates/gdaemon/tests/node_status.rs::status_reports_auth_failure`.
- 1.2.5 - A refuse frame reports `refused` with the hub's refusal object.
  test: `crates/gdaemon/tests/node_status.rs::status_reports_hub_refusal`.
- 1.2.6 - A channel closed after the ack reports `disconnected` with the
  close code. test:
  `crates/gdaemon/tests/node_status.rs::status_reports_dropped_channel`.
- 1.2.7 - A request with no bearer or a wrong one gets 401
  `node_key_required`, and the fake hub records no request. test:
  `crates/gdaemon/tests/node_status.rs::status_requires_node_key`.
- 1.2.8 - `gdaemon` is one patch version above the executor's starting tip in
  `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.3 `gobby node status` [category: code] (depends: 1.2, #24877)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `api` module and `dispatch_inner` routes `node status` beside #24860's node routing; the plan scopes this file one way
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/api.rs`
- `crates/gclient/src/command/node.rs`
- `crates/gclient/tests/node_status_verb.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`gclient node status [--json] [--daemon-url URL] [--token-file PATH]` runs in
`gclient` itself; it does not exec `gdaemon`. `dispatch_inner` sends
`node status` to `node::status` beside #24860's `install|enroll` routing,
before the shared URL and token resolution. Its steps, in order:

1. `Parsed::finish` rejects any other position or option, a workspace or pane
   flag included, with exit 2 before anything is read.
2. `gobby_core::bootstrap::read_files_home_view()`. `DatastoreMode::Local`
   exits 1 with `this machine runs the hub, not a node; use gobby status`. A
   read error exits 3 with `not enrolled: <error>; run gobby node install`.
3. The token comes from `--token-file`, else
   `gobby_core::local_token::read_api_key()`. Missing or empty exits 3 with
   `not enrolled; run gobby node install`.
4. `GET <url>/api/node/status` with the token, through the `api.rs` request
   function below. The URL is `--daemon-url`, else
   `gobby_core::daemon_url::daemon_url()`. If the request function reports
   the daemon unreachable, the verb exits 3. The message is
   `node service not installed; run gobby node install` when the service file
   is absent, and `node daemon not running (service <path>)` when it exists.
   The service file is `~/Library/LaunchAgents/com.gobby.node.plist` on macOS
   and `~/.config/systemd/user/gobby-node.service` on Linux.
5. A 401 exits 3 with `node key rejected by the local daemon`, and a body that
   is not JSON exits 3.
6. The first line of a 200 reply is the node's state:
   - `healthy`: `connected` with every capability available; exit 0;
   - `capability unavailable: <name>: <reason>`: `connected` with any
     unavailable capability; exit 1;
   - `hub unavailable: <error>`, `auth failed: <error>; re-enroll with gobby node enroll`,
     `refused: <refusal>`, `disconnected: <error>` and `connecting`; exit 1.

   The next lines are `machine`, `hub`, `version`, `platform`, `since` and one
   line per capability. `--json` prints the body as returned and keeps the
   same exit code.

On Windows the verb exits 2 with #24860's node-family message; 2.8 changes
that text. `help.rs::VERBS` adds `status` to the `node` row's usage.

`crates/gclient/src/command/api.rs` is new and holds the operator HTTP
helper's request function (O5); 2.1 adds the fallback. The function sends a
request with an optional JSON body and an optional bearer token under
`crate::daemon::REQUEST_DEADLINE` (5 s) as both connect and total timeout. It
returns the HTTP status and the body, parsed when it is JSON and raw
otherwise, or an unreachable outcome for a refused connection or a timeout.
Each caller maps that outcome to its own exit-3 message: the service state
here, `not running` in 2.1's `status`.

**Research context:**
- The 5 s deadline is the one the TUI's REST client uses
  (`daemon/live.rs:25`, `acc5e1644333ded9`, re-exported at
  `daemon/mod.rs:12-16`, `09e0961fb67e4d80`; `daemon/rest.rs:30-35`,
  `3484fdc92bcd278d`). `node status` is the request function's first caller,
  so it lands here; `status`, `projects`, `tasks` and `config` reuse it.
- F9.3 (R5): `gobby status` on a node reports the hub, because the node relays
  `/api/health` and `/api/admin/status` (As-Is Facts). `gobby node status`
  reports the node from the node's own route (1.2), so it answers while the
  hub is down. 2.10 proves both live.
- The exit codes follow O4: 1 for a refusal or degraded state, 2 for usage, 3
  for connection, missing token, 401 or non-JSON.
- The hub-machine check comes before the token read, so a hub operator gets
  the pointer rather than a token error.
- Planned checks:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client -E 'binary(node_status_verb)'`.

**Granularity:** nine acceptance items, four production files (`node.rs`,
`api.rs`, the routing in `command.rs` and the `help.rs` row) plus the
release files. One outcome: an operator reads node health from the node.
Each item is one branch of one verb's documented contract. The request
function cannot land alone, because unused Rust code fails clippy's
`dead_code` lint, so it lands with its first caller.

**Acceptance:**

- 1.3.1 - A connected node with every capability available prints `healthy`
  and its identity lines, and exits 0. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_healthy_exits_zero`.
- 1.3.2 - A connected node with an unavailable capability prints
  `capability unavailable` and exits 1. Each of `hub_unavailable`,
  `auth_failed`, `refused`, `disconnected` and `connecting` prints its line
  and exits 1. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_unhealthy_states_exit_one`.
- 1.3.3 - `--json` prints the route's body unchanged with the same exit code.
  test: `crates/gclient/tests/node_status_verb.rs::node_status_json_prints_body`.
- 1.3.4 - With a local-mode bootstrap the verb exits 1, points to
  `gobby status` and sends no request. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_on_hub_points_to_status`.
- 1.3.5 - With a remote-mode bootstrap but no `api_key`, the verb exits 3
  with `not enrolled` and sends no request. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_unenrolled_exits_three`.
- 1.3.6 - With nothing listening, the verb exits 3 with `not installed` when
  the service file is absent, and `not running` naming the file when it
  exists. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_unreachable_names_service_state`.
- 1.3.7 - A 401 and a non-JSON body each exit 3. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_rejected_key_exits_three`.
- 1.3.8 - `node status extra`, `node status --workspace 0:1` and
  `node status --bogus` each exit 2 with an empty `GOBBY_HOME` and send no
  request, and `gclient help node` lists `status`. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_usage_errors_exit_two`.
- 1.3.9 - `gclient` is one patch version above the executor's starting tip in
  `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P2: Node operator CLI (#21579)
`kind: framing`

**Goal:** an operator on a Python-free node runs `status`, `projects`,
`tasks` reads, `config get|set` and the `node` family against the hub. Every
Python command has a recorded decision and a recorded node status. Workspace
and pane verbs refuse on a node instead of acting on the hub host's panes.

### 2.1 Operator HTTP helper and `status` [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `status` module, extracts token resolution into `resolve_token`, and routes `status` before the workspace verbs' eager token read
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/help.rs::COMMON`
- `crates/gclient/src/command/api.rs`
- `crates/gclient/src/command/status.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/command_mode.rs::help_documents_one_workspace_contract_for_every_verb`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: serves and records `GET /api/health` without the bearer check
- `crates/gclient/tests/operator_support/mod.rs`
- `crates/gclient/tests/status_verb.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`crates/gclient/src/command/api.rs` holds the operator verbs' HTTP helper
(O5). 1.3 created its request function; 2.1 adds the fallback.

- The request function, from 1.3, sends a request with an optional bearer
  token under the 5 s deadline and returns the status and the body, or an
  unreachable outcome. `status` maps that outcome to `not running` with the
  URL and exit 3.
- One fallback function turns any reply a verb did not handle into a
  `CommandError`. A 401 exits 3 and prints its body `code` (for example
  `route_not_permitted`); a non-JSON body exits 3. Any other non-2xx exits 1
  with `code: message (path a.b)` from the config envelope, else `detail`.
- The helper has no MCP tool function. The tool call lives in 2.3's
  `tasks.rs`, its only caller, so 2.1 adds no code that `status` leaves
  unused.

`crates/gclient/src/command/status.rs` is new: `gclient status [--json]`.

- It calls `Parsed::finish` first. Any leftover argument or option, a
  workspace or pane flag included, exits 2 before any token read or request.
- It sends `GET /api/health` with no token. A 503 whose body has `backend`
  and a 502 are handled here; any other non-200 goes to the fallback.
- On 200 it reads the token through `resolve_token` and sends
  `GET /api/admin/status`.
- The daemon is healthy only when `health.status` is `ok` and the admin
  `status` is `healthy`. The admin view can be degraded while the public
  probe says `ok`, because it also checks event collection and PostgreSQL.
- Text output: the first line is `healthy`, `degraded: <services>` (the
  union of both bodies' `degraded_services`, or `degraded: admin status`
  when that union is empty) or `backend <state>`. Then come `mode`, `uptime`
  and, when degraded, the hook runtime state.
- `--json` prints `{"health": ..., "status": ...}`, with `status` null when
  the admin call was not made.
- Exit 0 when healthy. Exit 1 when either body is degraded, on the typed 503
  (any `backend.state`, including S1.3's `standby`) or on 502. Exit 3 when
  the daemon is not reachable.

`command.rs` gains `resolve_token(token_file)` (new), holding the lines
`dispatch_inner` runs today. The workspace verbs keep calling it before they
run, and `dispatch_inner` sends `status` to `status.rs` before
`verbs::Action::parse`. `help.rs` gains the `status` row. `COMMON` now says
that every verb takes `--json`, `--daemon-url URL` and `--token-file PATH`,
that the workspace verbs take `--workspace REF`, and that the operator verbs
take none of the workspace or pane options. The rest of its text is unchanged.
`command_mode.rs` adds `status` to `VERBS`, and its help test asserts the new
sentence.

`crates/gclient/tests/operator_support/mod.rs` is new: an `invoke` helper that
runs `gclient` with `--daemon-url`, an optional `--token-file`, a chosen
working directory and `GOBBY_HOME`, and with `GOBBY_AGENT_API_TOKEN`,
`GOBBY_PANE_REF`, `GOBBY_WORKSPACE_ID` and `GOBBY_TAB_ID` removed. The mock
daemon lets `GET /api/health` through without the bearer check and records it.

**Research context:**
- Today `dispatch_inner` reads the token before any verb runs
  (`command.rs:198-205` at `babdb0d558`), which would make a stopped daemon
  with no token report a token error. Hence `resolve_token` and the routing
  before `Action::parse`.
- The typed 503 and the 502 come from gdaemon's front door; the healthy and
  degraded bodies come from Python's `/api/health`. The verb prints
  `backend.state` without a fixed list, so S1.3's `standby` needs no change.
- On a node, the node's front door relays both requests to the hub, so
  `status` reports the hub. When the hub is unreachable, the relay's typed
  503 prints `backend down` and exits 1. Node health is `gobby node status`
  (1.3).
- `/api/admin/status` is operator-only (O2). An agent token
  gets 401 `route_not_permitted`, which exits 3 with that code printed.
- Public health is `ok` or `degraded` (`_health.py:380-392`,
  `c669c899bfe3115d`). Admin status is `healthy` only when the server is
  running, with no degraded hook runtime or services, complete event
  collection and healthy PostgreSQL stores (`_health.py:718-736`,
  `2ff8e1a3e17ba2a8`).
- Not carried: the schema-heads line, the lock probe and the pid claim
  (O8).
- Verification commands (not yet run):
  `cargo nextest run -p gobby-client --test status_verb --test command_mode`,
  then `cargo clippy -p gobby-client --all-targets -- -D warnings`.

**Granularity:** twelve acceptance items, four production files plus the
release files, one outcome: `gclient status` reports the daemon with the
documented exit codes. The fallback cannot land alone, because unused
Rust code fails clippy's `dead_code` lint, so `status` is its first caller.
Each later verb is its own deliverable.

**Acceptance:**

- 2.1.1 - A healthy daemon prints `healthy` with mode and uptime and exits 0.
  test: `crates/gclient/tests/status_verb.rs::status_prints_healthy_summary`.
- 2.1.2 - A degraded daemon prints `degraded:` with its services and exits 1.
  test: `crates/gclient/tests/status_verb.rs::status_degraded_exits_one`.
- 2.1.3 - A typed 503 prints `backend <state>` (tested with `starting` and
  `standby`) and exits 1, with no admin request and no token read. test:
  `crates/gclient/tests/status_verb.rs::status_typed_503_prints_backend_state`.
- 2.1.4 - A refused connection prints `not running` with the URL and exits 3.
  test: `crates/gclient/tests/status_verb.rs::status_not_running_exits_three`.
- 2.1.5 - With no token file, an empty `GOBBY_HOME` and no
  `GOBBY_AGENT_API_TOKEN`, a stopped daemon still reports `not running` and
  no token error. test:
  `crates/gclient/tests/status_verb.rs::status_not_running_needs_no_token`.
- 2.1.6 - A healthy probe with no readable token exits 3 and names the
  missing token; a 401 from `/api/admin/status` also exits 3. test:
  `crates/gclient/tests/status_verb.rs::status_missing_token_after_health_exits_three`.
- 2.1.7 - `--json` prints one object holding both bodies verbatim. test:
  `crates/gclient/tests/status_verb.rs::status_json_prints_health_and_status`.
- 2.1.8 - A 403 or 500 from `/api/admin/status` prints the daemon's code and
  message and exits 1. test:
  `crates/gclient/tests/status_verb.rs::status_error_keeps_body`.
- 2.1.9 - `gclient help` lists `status`, and its common text separates the
  workspace options from the options every verb takes. test:
  `crates/gclient/tests/command_mode.rs::help_documents_one_workspace_contract_for_every_verb`.
- 2.1.10 - A public probe of `ok` with an admin status of `degraded` and no
  degraded services prints `degraded: admin status` and exits 1. test:
  `crates/gclient/tests/status_verb.rs::status_admin_degraded_exits_one`.
- 2.1.11 - `status extra`, `status --workspace 0:1` and `status --bogus`
  each exit 2 with an empty `GOBBY_HOME` and send no request. test:
  `crates/gclient/tests/status_verb.rs::status_usage_errors_exit_two`.
- 2.1.12 - `gclient` is one patch version above the executor's starting tip
  in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.2 `projects list|show` [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `projects` module and routes the `projects` verb
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/projects.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/projects_verb.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`crates/gclient/src/command/projects.rs` is new:
`gclient projects list [--json]` and `gclient projects show NAME|ID [--json]`.
Both resolve the token and send one `GET /api/projects`.

- Two `pub(super)` functions carry that work. `rows` sends the request
  through the 2.1 request function and hands any reply other than 200 to
  the 2.1 fallback. `find` matches an argument in three tiers, in order:
  `id`, then `name`, then `display_name`. The first tier with any match
  wins. One match returns that row; two or more in the winning tier return
  the ambiguous candidates; no match in any tier returns none. `list` and
  `show` call them, and 2.3 reuses them for `tasks --project NAME`.
- `list` prints one row per project: name, id, open task count, and the
  checkout root or `-`.
- `show` prints the row `find` picks, one field per line. No match exits 1
  with `no project NAME`. An ambiguous match exits 2 with
  `ambiguous project NAME:` followed by each candidate's name and id.
- `--json` prints the rows, or the one row, as returned.
- `gclient projects` with no subcommand, an unknown subcommand, the wrong
  number of arguments or a leftover option exits 2. `Parsed::finish` runs
  before the token read, so a usage error sends nothing.

**Research context:**
- The route hides `_orphaned`, `_migrated` and `_global` itself, so the client
  filters nothing.
- `show` reads the list instead of `GET /api/projects/{id}` so that a name
  works with one request.
- The route is operator-only (O2).
- `display_name` is free text, so two projects can share one. The tier
  order makes a lookup deterministic, and the ambiguity exit keeps a
  collision from silently picking whichever row the route returns first.
- Verification (not yet run): `cargo nextest run -p gobby-client --test projects_verb --test command_mode`.

**Granularity:** seven acceptance items, three production files
(`projects.rs` and the `command.rs` and `help.rs` rows) plus the release
files, one outcome: an operator reads projects by name or id. `rows` and
`find` have no caller without `list` and `show`.

**Acceptance:**

- 2.2.1 - `projects list` prints one row per project with name, id, open
  tasks and checkout root. test:
  `crates/gclient/tests/projects_verb.rs::projects_list_prints_rows`.
- 2.2.2 - `projects show` finds a project by id, by name and by display name.
  test: `crates/gclient/tests/projects_verb.rs::projects_show_matches_name_or_id`.
- 2.2.3 - `projects show` with an unknown argument exits 1 and names it.
  test: `crates/gclient/tests/projects_verb.rs::projects_show_unknown_exits_one`.
- 2.2.4 - An argument equal to one project's `name` and another's
  `display_name` picks the `name` match. Two projects sharing a
  `display_name` make `projects show` exit 2 and list both names and ids.
  test: `crates/gclient/tests/projects_verb.rs::projects_show_tier_order_and_ambiguity`.
- 2.2.5 - `projects list --json` prints the route's rows unchanged. test:
  `crates/gclient/tests/projects_verb.rs::projects_list_json_prints_rows`.
- 2.2.6 - `projects`, `projects bogus`, `projects show` with no argument and
  `projects list --workspace 0:1` each exit 2 with an empty `GOBBY_HOME` and
  send no request. test:
  `crates/gclient/tests/projects_verb.rs::projects_usage_errors_exit_two`.
- 2.2.7 - `gclient` is one patch version above the executor's starting tip
  in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.3 `tasks list|show|ready|blocked|search` [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `tasks` module, routes the `tasks` verb, and adds its value flags and switches
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/tasks.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: records the `X-Gobby-Project-Id` header on each request
- `crates/gclient/tests/tasks_verb.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`crates/gclient/src/command/tasks.rs` is new. Its private tool call sends
`POST /api/mcp/gobby-tasks/tools/{tool}` through the 2.1 request function,
with the arguments as the body and an optional `X-Gobby-Project-Id` header.
It returns the unwrapped `result`, and hands any reply it does not handle to
the 2.1 fallback. Each subcommand calls one `gobby-tasks` tool this way.
Flags map to tool arguments as
`--state` → `current_stage_state`, `--priority` → `priority`, `--type` →
`task_type`, `--label` → `label`, `--parent` → `parent_task_id` and
`--limit` → `limit`; an absent flag sends no argument.

- `list [--state S] [--priority N] [--type T] [--label L] [--parent REF]
  [--closed] [--limit N]` calls `list_tasks`, with `closed: false` unless
  `--closed` is given.
- `show REF` calls `get_task` with `task_id: REF` and `brief: true`.
- `ready [--priority N] [--type T] [--parent REF] [--limit N]` calls
  `list_ready_tasks`.
- `blocked [--parent REF] [--limit N]` calls `list_blocked_tasks`.
- `search QUERY [--state S] [--priority N] [--type T] [--parent REF]
  [--limit N]` calls `search_tasks` with `query: QUERY`.

Project context:

- `--project ID` with a UUID is used as given, with no request.
- `--project NAME` reads the token and resolves through 2.2's
  `projects::rows` and `projects::find`, so the lookup keeps the operator
  error contract (O4 and O5). A 401 exits 3 with its body
  `code`. A 403 or 500 exits 1 with the daemon's code and message, and a
  body that is not JSON exits 3. An unknown name exits 2 with
  `unknown project: NAME`, and an ambiguous one exits 2 with `find`'s
  candidate list, as `projects show` prints it. After a failed lookup no
  task tool is called.
- Without `--project`, the client reads the project id from the current directory's
  `.gobby/project.json`, using `gobby_core::project::find_project_root` and
  `read_project_id`.
- With neither, and no `--all-projects`, the verb exits 2 with
  `no project: run inside a Gobby project or pass --project NAME|ID
  (--all-projects for listings)` before sending anything.
- The id goes out as `X-Gobby-Project-Id`, and the list tools also get it as
  `project`. `--all-projects` (list, ready, blocked, search) sends
  `all_projects: true` with no `project` argument and no header. `show`
  refuses `--all-projects` with exit 2, because `#N` needs one project.

Output reads the tools' nested shape:

- The list tools return `{tasks, count}`. Each row prints `ref`, a stage
  (`state.current_stage` when set, else `closed` when `state.is_closed`,
  else `open`), `priority` and `title`.
- `show` prints `ref` and `title`, the same stage, `priority`, `labels`,
  `parent_task_id`, each `dependencies.blocked_by[*].ref` and `description`.
- `--json` prints the tool result as returned.
- A result carrying `error`, `success: false` or `found: false` exits 1 with
  the message.

Usage, checked by `Parsed::finish` and the subcommand's own argument rules
before the project lookup, the token read or any request, exits 2:

- a missing or unknown subcommand;
- the wrong number of positions (`show` and `search` take one);
- a flag the subcommand does not take (for example `--label` on `ready`, or
  `--closed` on `search`);
- a non-integer `--priority` or `--limit`;
- `--all-projects` on `show`.

`command.rs` adds `--state`, `--priority`, `--type`, `--label`, `--parent`
and `--limit` to `VALUE_FLAGS`, and `--all-projects` and `--closed` to
`SWITCH_FLAGS`. The mock daemon's `RequestRecord` gains
`project_id: Option<String>`, recorded from `X-Gobby-Project-Id`.

**Research context:**
- The MCP route seeds project context from the header, and an unresolvable
  header leaves none (As-Is Facts, MCP tool route), which is why the list
  tools also get `project`: `resolve_project_filter_standalone` then fails an
  unregistered id instead of listing every project.
- `#N` refs resolve server-side in that context (`_resolution.py:12-55`), so
  the client never parses refs.
- `verbs::resolve_project` is not reused. It reads the projects through the
  TUI's `RestClient`, whose `status_error` drops the body, and maps every
  error other than a workspace refusal to exit 3 (`verbs.rs:564-595`,
  `700c3b72cb43d8e6`; `daemon/rest.rs:1-40`, `14c8971692876786`, and
  `77-80`, `4bd5ed8fb5ff7063`). A 401 would lose its `code`, and a 403 or
  500 would exit 3 instead of 1. It stays with the workspace verbs,
  unchanged.
- `list` shows open tasks by default (`closed: false`) and closed tasks only
  with `--closed` (`closed: true` selects rows with `closed_at` set). A mixed
  open and closed listing is additional Rust CLI parity owned by #21560,
  outside this read-only slice; it must be carried before S3.2.
- The row and card shapes come from `src/gobby/mcp_proxy/tools/tasks/_formatters.py:71-162`
  (`6ac386355be2a12b`): `state` nests `current_stage` and `is_closed`, and
  `task_summary_payload` carries `parent_task_id` and `dependencies`.
  `get_task` with `brief` fills `dependencies.blocked_by` and `blocking`
  (`_crud.py:507-517`, `46b7563b6d8553a5`). Test fixtures copy this shape,
  covering a staged open task, a closed task and a card with a parent and
  blockers.
- Writes are out of scope (O6).
- Verification (not yet run): `cargo nextest run -p gobby-client --test tasks_verb --test command_mode`.

**Granularity:** ten acceptance items, two production files (`tasks.rs`
and the routing in `command.rs`) plus the release files, one outcome: task
reads in project context. The five subcommands share one
project rule and one tool call path, so splitting them would repeat the
same context tests.

**Acceptance:**

- 2.3.1 - `tasks list` inside a project directory calls `list_tasks` with the
  project id as both header and `project` argument and `closed: false`, and
  prints the returned staged open row with its `state.current_stage`.
  `tasks list --closed` sends `closed: true` and prints the returned closed
  row with stage `closed`. The server filters on `closed`, so the client
  prints every row it gets. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_list_sends_project_header_and_arg`.
- 2.3.2 - `tasks show #N` calls `get_task` with `task_id` `#N` and the project
  header, and prints the card with its `parent_task_id` and each
  `dependencies.blocked_by` ref. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_show_resolves_ref_in_project_context`.
- 2.3.3 - `ready`, `blocked` and `search` call their tools with the flag
  mapping above, `search` taking QUERY as its position. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_ready_blocked_search_map_flags`.
- 2.3.4 - A tool result with `error`, a `success: false` reply and a
  `found: false` card each exit 1 with the message. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_tool_error_exits_one`.
- 2.3.5 - Outside a project with no `--project`, the verb exits 2 and sends
  no request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_without_project_exits_two`.
- 2.3.6 - `--all-projects` sends `all_projects: true`, no `project` argument
  and no header; `show --all-projects` exits 2. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_all_projects_omits_project`.
- 2.3.7 - `--project NAME` resolves through `GET /api/projects` to its id; an
  unknown name exits 2; a `display_name` two projects share exits 2, lists
  both candidates and calls no task tool; a UUID sends no projects request.
  test:
  `crates/gclient/tests/tasks_verb.rs::tasks_project_name_resolves_via_projects`.
- 2.3.8 - Each usage rule above exits 2 with an empty `GOBBY_HOME` and sends
  no request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_usage_errors_exit_two`.
- 2.3.9 - When the `--project NAME` lookup gets a 401 `route_not_permitted`,
  the verb exits 3 and prints the code. A 403 or a 500 exits 1 with the
  daemon's code and message, and a body that is not JSON exits 3. None of
  these sends a task tool request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_project_lookup_errors_keep_operator_contract`.
- 2.3.10 - `gclient` is one patch version above the executor's starting tip
  in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.4 `config get|set` [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `config` module and routes the `config` verb
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/config.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/config_verb.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

`crates/gclient/src/command/config.rs` is new.

`gclient config get KEY [--json]`:

- Sends `GET /api/config/values` and walks the dotted KEY through `desired`.
  A leaf prints as JSON; a subtree prints as a JSON object.
- When the `active` value differs, it also prints `active: <value>`.
- `pending_restart_keys` is a list of dotted keys, and `failed_live_keys` is
  a map from dotted key to `{revision, subscriber}`. The verb reports every
  pending key, and every failed-live map key, that equals KEY or starts with
  `KEY.`, so a subtree read shows its pending and failed descendants.
- A key absent from `desired` exits 1 with `unknown or non-public key KEY`.
- `--json` prints `{key, desired, active, pending_restart, failed_live}`, the
  last two holding the matching list entries and map entries.

`gclient config set KEY VALUE`:

- Reads `GET /api/config/values` for `revision`, then sends
  `PATCH /api/config/values` with
  `{"expected_revision": <revision>, "values": {KEY: <value>}}`.
- `<value>` is VALUE parsed as JSON when it parses, else the string, so
  `20` sends a number and `fast` sends `"fast"`. A value starting with `-`
  follows `--`, because the parser reads a dash-led argument as an option:
  `gclient config set some.key -- -5`.
- On 409 `revision_conflict` it re-reads the revision and sends once more. A
  second conflict exits 1 with `changed concurrently; re-run`. A 409 with any
  other code goes to the shared fallback without a retry.
- On 422 (`validation_error`, `revision_exhausted`,
  `managed_activation_required`) it prints `code: message (path a.b)`, plus
  `action: <action>` when the body has one, and exits 1.
- On 500 `persistence_indeterminate` it prints
  `outcome unknown; re-read before retrying` and exits 1.
- On success it prints `apply_status`, `changed_keys` and any pending or
  failed-live keys. `--json` prints the success body as returned: `committed`,
  `revision`, `changed_keys`, `apply_status`, `pending_restart_keys` and
  `failed_live_keys`.
- The server computes `apply_status` over the whole snapshot, so an
  unrelated key that failed earlier still reports `failed_live`. The verb
  exits 1 only when a key in `changed_keys` is in `failed_live_keys`; it
  exits 0 otherwise and prints the other failed keys as a warning.
- `gclient config` with no subcommand, an unknown subcommand, `get` or `set`
  with the wrong number of arguments, or a leftover option exits 2.
  `Parsed::finish` runs before the token read, so a usage error sends
  nothing.

**Research context:**
- The routes and envelopes are in As-Is Facts (Config). The MCP config tools
  call the same service, which satisfies Josh's "same config API" note
  (O7).
- The retry-once rule mirrors `config_writes.py:23-54`, so the client and
  the Python CLI behave the same on a race.
- The server flattens dotted keys and owns typing and validation, so the
  client does no schema work.
- Agent tokens get 401 `route_not_permitted`, which exits 3 with that code.
- `values.py:628-653` (`be890f444e5a9577`): `_failed_live` builds the
  failed-live map, and `_mutation_body` sets `apply_status` to `failed_live`
  whenever that map is non-empty, whatever keys the write changed.
- Verification (not yet run): `cargo nextest run -p gobby-client --test config_verb --test command_mode`.

**Granularity:** twelve acceptance items, two production files plus the
release files, one outcome: reading and safely writing one config key. Every
item is one reply the compare-and-set write must handle, so the write cannot
be split without shipping a `set` that mishandles a race.

**Acceptance:**

- 2.4.1 - `config get` prints the desired value, plus the active value when
  it differs and the pending-restart flag. test:
  `crates/gclient/tests/config_verb.rs::config_get_prints_desired_and_active`.
- 2.4.2 - `config get` of an absent key exits 1. test:
  `crates/gclient/tests/config_verb.rs::config_get_unknown_key_exits_one`.
- 2.4.3 - `config set` sends `20` as a number, `true` as a boolean and `fast`
  as a string, with the read revision. test:
  `crates/gclient/tests/config_verb.rs::config_set_parses_json_literal_or_string`.
- 2.4.4 - A first 409 `revision_conflict` re-reads and succeeds on the second
  `PATCH`. test:
  `crates/gclient/tests/config_verb.rs::config_set_retries_once_on_conflict`.
- 2.4.5 - A second 409 exits 1 after exactly two `PATCH` requests. test:
  `crates/gclient/tests/config_verb.rs::config_set_second_conflict_surfaces`.
- 2.4.6 - A 422 prints the code, message, path and action and exits 1. test:
  `crates/gclient/tests/config_verb.rs::config_set_validation_error_renders_code_and_path`.
- 2.4.7 - A 500 `persistence_indeterminate` says to re-read and exits 1.
  test: `crates/gclient/tests/config_verb.rs::config_set_indeterminate_says_reread`.
- 2.4.8 - Success prints `apply_status` and the changed keys, and `--json`
  prints the success body unchanged. A changed key in `failed_live_keys`
  exits 1; a failed-live map naming only another key exits 0 with a warning.
  test: `crates/gclient/tests/config_verb.rs::config_set_prints_apply_status`.
- 2.4.9 - A 409 whose code is not `revision_conflict` exits 1 after one
  `PATCH`. test:
  `crates/gclient/tests/config_verb.rs::config_set_other_409_never_retries`.
- 2.4.10 - `config get code_index` reports a pending descendant key and a
  failed-live descendant from the map. test:
  `crates/gclient/tests/config_verb.rs::config_get_subtree_reports_descendant_flags`.
- 2.4.11 - `config`, `config bogus`, `config get`, `config set KEY` and
  `config get KEY --workspace 0:1` each exit 2 with an empty `GOBBY_HOME`
  and send no request, and `config set some.key -- -5` sends `-5` as a
  number. test:
  `crates/gclient/tests/config_verb.rs::config_usage_errors_exit_two`.
- 2.4.12 - `gclient` is one patch version above the executor's starting tip
  in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.5 `gobby node start|stop|restart` [category: code] (depends: 2.4, #24872)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: `dispatch_inner` routes `node start|stop|restart` to `node::exec_gdaemon`; the plan scopes this file one way
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/node.rs`
- `crates/gclient/tests/node_verbs.rs`
- `crates/gclient/tests/node_status_verb.rs`
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/service.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gclient/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Client: `dispatch_inner` sends `node start`, `node stop` and `node restart`
to `node::exec_gdaemon`, beside #24860's `install|enroll` routing and before
the URL or token is read. gdaemon prints the output and returns the exit
status. `help.rs::VERBS` adds the three to the `node` row's usage. 1.3's
`not running` message gains `; run gobby node start`.

gdaemon: `NodeCommand` gains `Start`, `Stop` and `Restart`, implemented in
`node_setup/service.rs` beside #24872's install step.

- When the unit file is missing, each command exits 1 with
  `node service not installed; run gobby node install` and runs no service
  command. The unit file is #24872's
  `~/Library/LaunchAgents/com.gobby.node.plist` or
  `~/.config/systemd/user/gobby-node.service`.
- macOS:
  - `start` runs `launchctl print gui/<uid>/com.gobby.node`. When the service
    is loaded it prints `node service already running` and exits 0;
    otherwise it runs `launchctl bootstrap gui/<uid> <plist>`.
  - `stop` runs `launchctl bootout gui/<uid>/com.gobby.node`, treating a
    not-loaded result as `node service already stopped` with exit 0.
  - `restart` is #24872's restart step: bootout, ignoring not loaded, then
    bootstrap.
- Linux: `start`, `stop` and `restart` run
  `systemctl --user start|stop|restart gobby-node.service`. Restart is
  #24872's restart step.
- Restart calls the same function install calls (W5), so a node restart
  keeps its terminals exactly as #24872 proves for install (F9.2).
- After `start` or `restart`, gdaemon polls `GET /api/node/status` (1.2) on
  the node's own daemon URL with the node key, every 1 s for up to 30 s. When
  the route answers, it prints `node service running; channel <state>` and
  exits 0. When it never answers, it prints the log path
  `<gobby_home>/logs/node.log` and exits 1.
- `stop` lasts until `start`, `restart` or the next login, because the unit
  stays installed and enabled.

**Research context:**
- Observed in node-install-enrollment 3.9: install writes the unit through
  `replace_file`, then on macOS boots it out and bootstraps it, and on Linux
  reloads, enables and restarts it. Its harness has stub `launchctl`,
  `systemctl` and `loginctl` scripts that record their arguments, with one
  test per OS under `#[cfg(target_os = ...)]`. This section reuses both.
- The gclient side reuses #24860's exec path, so the commands run with no
  daemon URL or token, like `install`.
- On a node, the hub-lifecycle rows (`start`, `stop`, `restart`) are `hub`
  rows whose `node_carrier` names these commands (2.7).
- Planned checks:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`
  and `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client -E 'binary(node_verbs) | binary(node_status_verb)'`.

**Granularity:** eight acceptance items across five production files in two
crates plus the release files. One outcome: an operator starts, stops and
restarts the node service. The client exec arm has no target without the
gdaemon commands, and the commands have no operator entry without it.

**Acceptance:**

- 2.5.1 - `gclient node start`, `node stop` and `node restart` exec the
  sibling `gdaemon node <verb>` with every argument unchanged and return its
  exit status, with no daemon URL or token available. test:
  `crates/gclient/tests/node_verbs.rs::node_lifecycle_verbs_exec_sibling_gdaemon`.
- 2.5.2 - On macOS, `start` bootstraps an unloaded service and reports an
  already-loaded one with exit 0. `stop` boots it out, reporting a not-loaded
  one with exit 0. `restart` runs bootout then bootstrap. test:
  `crates/gdaemon/tests/node_setup.rs::node_lifecycle_drives_launchctl`.
- 2.5.3 - On Linux, the three commands run `systemctl --user` with `start`,
  `stop` and `restart` for `gobby-node.service`. test:
  `crates/gdaemon/tests/node_setup.rs::node_lifecycle_drives_systemctl`.
- 2.5.4 - With no unit file, each command exits 1 naming
  `gobby node install`, and the stubs record no call. test:
  `crates/gdaemon/tests/node_setup.rs::node_lifecycle_without_service_exits_one`.
- 2.5.5 - After `start`, a status route that answers prints the channel state
  and exits 0. A route that never answers within the bound exits 1 and
  prints the log path. test:
  `crates/gdaemon/tests/node_setup.rs::node_start_waits_for_local_status`.
- 2.5.6 - `node restart` records the same service commands as install's
  restart step. test:
  `crates/gdaemon/tests/node_setup.rs::node_restart_matches_install_restart`.
- 2.5.7 - `gclient node status` against a stopped daemon with its unit file
  present names `gobby node start`. test:
  `crates/gclient/tests/node_status_verb.rs::node_status_stopped_hints_node_start`.
- 2.5.8 - `gdaemon` and `gclient` are each one patch version above the
  executor's starting tip in their `Cargo.toml`, `Cargo.lock` and
  `MANAGED_BIN_VERSION_PINS`. test:
  `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.6 Workspace and pane verbs refuse on a node [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: `dispatch_inner` gains the node pane guard after `Action::parse`; the plan scopes this file one way
- `crates/gclient/src/command/verbs.rs::*` — scope-reason: adds `Action::acts_on_panes` beside the enum it matches
- `crates/gclient/tests/node_pane_refusal.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

R6's guard. `verbs::Action` gains `acts_on_panes(&self) -> bool`. It is true
for every workspace and pane action (`list`, `new-tab`, `split`, `resize`,
`title`, `kill`, `capture-pane`, `send-keys`, `wait-for-output` and the rest
that `Action::parse` builds today). It is false for `node list` and
`node update`, which #24877 adds and which go to the hub's machine routes.

In `dispatch_inner`, after `verbs::Action::parse` succeeds and before the
token read:

- when `acts_on_panes()` is true and
  `gobby_core::bootstrap::read_files_home_view()` reports
  `DatastoreMode::Remote`, the verb exits 1 with
  `gclient <verb>: workspace and pane verbs do not run on a node yet; they would act on the hub host's panes. Gobby task #20202 (remote terminal attach) brings them to nodes.`;
- when `acts_on_panes()` is true and `read_files_home_view()` returns an
  error, the verb exits 3 with `gclient <verb>: cannot read bootstrap:
  <error>` before token resolution or any request. The guard fails closed
  because `--token-file` reads the token without the bootstrap, so a
  fall-through with explicit `--daemon-url` and `--token-file` would act on
  the hub host's panes.

Usage errors still exit 2 first, because `Action::parse` runs before the
guard. The TUI is out of scope; remote attach (#20202) owns it.

**Research context:**
- R6: these verbs go through `verbs.rs::run_op` to
  `LiveDaemon::workspace_op` over the daemon WebSocket. A node's front door
  splices that socket to the hub, so without the guard they act on the hub
  host's panes.
- The operator verbs (`status`, `projects`, `tasks`, `config`) and the `node`
  family route before `Action::parse` and never reach the guard.
- `read_files_home_view` treats a missing bootstrap as owner-less local
  defaults and fails closed on a present one: read errors propagate and
  invalid YAML or mode returns `FilesHomeError` (`crates/gcore/src/bootstrap.rs`
  283, 306-309, 313-328 at `77798bc4ed`). A hub with no bootstrap file
  therefore still runs pane verbs. `dispatch_inner` reads an explicit
  `--token-file` directly (`command.rs:198-203`), so the token path does
  not catch a bad bootstrap.
- Planned checks:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client -E 'binary(node_pane_refusal) | binary(command_mode)'`.

**Acceptance:**

- 2.6.1 - With a remote-mode bootstrap, each workspace and pane verb exits 1
  with the message naming #20202, reads no token and sends no request. test:
  `crates/gclient/tests/node_pane_refusal.rs::pane_verbs_refuse_on_node`.
- 2.6.2 - With a local-mode bootstrap, the same verbs reach the mock daemon
  as before. test:
  `crates/gclient/tests/node_pane_refusal.rs::pane_verbs_run_on_hub`.
- 2.6.3 - With a remote-mode bootstrap, `node list` reaches the mock daemon
  and `status` is not refused. test:
  `crates/gclient/tests/node_pane_refusal.rs::operator_and_node_verbs_pass_guard_on_node`.
- 2.6.4 - With a remote-mode bootstrap, a pane verb with a usage error exits
  2, not 1. test:
  `crates/gclient/tests/node_pane_refusal.rs::pane_usage_errors_exit_two_on_node`.
- 2.6.5 - With a present but invalid bootstrap, and with a valid explicit
  `--daemon-url` and `--token-file`, a pane verb exits 3 with the bootstrap
  error and the mock daemon records no request. test:
  `crates/gclient/tests/node_pane_refusal.rs::pane_verbs_refuse_on_unreadable_bootstrap`.
- 2.6.6 - `gclient` is one patch version above the executor's starting tip
  in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.7 Command inventory [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `docs/contracts/cli-inventory.toml`
- `tests/cli/test_cli_inventory.py`
- `crates/gclient/tests/cli_inventory.rs`

`docs/contracts/cli-inventory.toml` is new and holds the rows below,
adjusted only by the drift rule.

**Row grammar.**

- A `command` row decides exactly the click commands in its `members`. The
  checked commands are every non-group command plus every group that runs
  without a subcommand, each named by the words after `gobby`. Each member
  equals the row's `path` or starts with `path` and a space.
- A `helper` row decides exactly the modules in its `members`. The checked
  modules are those under the `gobby.cli` package that are no command's
  callback module. Each member starts with the row's `module`.
- A row whose only member is its own `path` or `module` omits `members`.
- Every checked command and every checked module is a member of exactly one
  row. Nothing matches by prefix: a new command or module, even inside a
  known family such as `tasks`, is no row's member until a change adds it.
- `disposition` is `keep`, `absorb` or `drop`.
- A `keep` or `absorb` `command` row has `carrier` (what serves it after
  S3.2) and `carried` (true when the carrier serves it today).
- A `drop` `command` row and every `helper` row have neither.
- A row with `carried = false`, every `drop` row and every `helper` row has
  `ref`, matching `#<digits>`.
- Every row has a non-empty `reason`.
- `gclient <path>` carriers name Rust command mode before S3.2 and
  `gobby <path>` afterwards. A false `carried` flag is an outstanding CLI
  implementation obligation for the row's `ref`, never a Python fallback.
  S3.1 verifies every member of each retained row before S3.2, using the
  owner's CLI proofs; an HTTP/MCP-only implementation cannot set it true.
- Node fields (R7). Every `keep` or `absorb` `command` row has `node`:
  - `carried`: the carrier runs on a node today. The row has
    `carried = true`, and its `reason` names the proof.
  - `#N` (matching `#<digits>`): not on a node until task #N delivers it.
  - `hub`: the command manages the hub host and has no node meaning. Only a
    `hub` row may have `node_carrier`, naming the node-side counterpart.
  `drop` rows and `helper` rows have no node fields.
- A `client` row records a Rust-only verb with no Python command. It has a
  unique `verb`, `carrier` equal to `gclient <verb>`, `node` and `reason`.
  Its carrier exists when the row lands.

**Drift rule.** The rows were walked at `9b148b644c`. At its starting tip, the
executor re-runs the walk. Any command or helper module added since then
goes into the row of its family's owner, or into a new row with the owner's
ref when no family row exists. Any member removed since then leaves its row.
The executor names each such change in the close summary.

```toml
# Command inventory: every Python `gobby` command and helper module, with its fate and node status.
# Written by plan node-mode (#17436; moved from client-operator-verbs, #21571). Read by S3.3 #21572.

command = [
  { path = "status", disposition = "keep", carrier = "gclient status", carried = true, node = "carried", reason = "Carried by node-mode 2.1. On a node it reports the hub through the relay; node-mode 2.10 proves it." },
  { path = "projects list", disposition = "keep", carrier = "gclient projects list", carried = true, node = "carried", reason = "Carried by node-mode 2.2; node-mode 2.10 proves it on a node." },
  { path = "projects show", disposition = "keep", carrier = "gclient projects show", carried = true, node = "carried", reason = "Carried by node-mode 2.2; node-mode 2.10 proves it on a node." },
  { path = "tasks list", disposition = "keep", carrier = "gclient tasks list", carried = true, node = "carried", reason = "Carried by node-mode 2.3; node-mode 2.10 proves it on a node." },
  { path = "tasks show", disposition = "keep", carrier = "gclient tasks show", carried = true, node = "carried", reason = "Carried by node-mode 2.3; node-mode 2.10 proves it on a node." },
  { path = "tasks ready", disposition = "keep", carrier = "gclient tasks ready", carried = true, node = "carried", reason = "Carried by node-mode 2.3; node-mode 2.10 proves it on a node." },
  { path = "tasks blocked", disposition = "keep", carrier = "gclient tasks blocked", carried = true, node = "carried", reason = "Carried by node-mode 2.3; node-mode 2.10 proves it on a node." },
  { path = "tasks search", disposition = "keep", carrier = "gclient tasks search", carried = true, node = "carried", reason = "Carried by node-mode 2.3; node-mode 2.10 proves it on a node." },
  { path = "start", disposition = "keep", carrier = "gclient start", carried = false, ref = "#21571", node = "hub", node_carrier = "gclient node start", reason = "Hub lifecycle, carried by the wave-4 #21571 plan. A node starts its own service with gclient node start (node-mode 2.5)." },
  { path = "stop", disposition = "keep", carrier = "gclient stop", carried = false, ref = "#21571", node = "hub", node_carrier = "gclient node stop", reason = "Hub lifecycle, carried by the wave-4 #21571 plan. A node stops its own service with gclient node stop (node-mode 2.5)." },
  { path = "restart", disposition = "keep", carrier = "gclient restart", carried = false, ref = "#21571", node = "hub", node_carrier = "gclient node restart", reason = "Hub lifecycle, carried by the wave-4 #21571 plan. A node restarts its own service with gclient node restart (node-mode 2.5)." },
  { path = "health", disposition = "absorb", carrier = "gclient status", carried = true, node = "carried", reason = "status probes /api/health first; node-mode 2.10 proves it on a node." },
  { path = "panes close", disposition = "absorb", carrier = "gclient kill", carried = true, node = "#20202", reason = "Existing gclient verb. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "panes read", disposition = "absorb", carrier = "gclient capture-pane", carried = true, node = "#20202", reason = "Existing gclient verb. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "panes rename", disposition = "absorb", carrier = "gclient title", carried = true, node = "#20202", reason = "Existing gclient verb. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "panes send", disposition = "absorb", carrier = "gclient send-keys", carried = true, node = "#20202", reason = "Existing gclient verb. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "panes split", disposition = "absorb", carrier = "gclient split", carried = true, node = "#20202", reason = "Existing gclient verb. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "panes wait", disposition = "absorb", carrier = "gclient wait-for-output", carried = true, node = "#20202", reason = "Existing gclient verb. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "workspaces show", disposition = "absorb", carrier = "gclient list --workspace", carried = true, node = "#20202", reason = "list prints one workspace's tabs and panes. On a node it refuses until remote terminal attach (node-mode 2.6)." },
  { path = "schema apply", disposition = "absorb", carrier = "gdaemon schema apply", carried = true, node = "hub", reason = "gdaemon owns the migration chain of the hub's datastore." },
  { path = "mcp-server", disposition = "absorb", carrier = "gmcp", carried = true, node = "carried", reason = "Every launch site starts gmcp (#24355); tests/e2e/test_hub_node_pair.py::test_gmcp_on_node_reaches_hub_through_relay proves it on a node." },
  { path = "test-quality", disposition = "drop", ref = "#21574", reason = "Python test tooling; S3.2 #21573 removes Python, deletion child #21574.", members = [
      "test-quality audit",
  ] },
  { path = "test-types", disposition = "drop", ref = "#21574", reason = "Python test tooling; S3.2 #21573 removes Python, deletion child #21574.", members = [
      "test-types audit", "test-types suppressions",
  ] },
  { path = "agents", disposition = "keep", carrier = "gclient agents", carried = false, ref = "#21564", node = "#21564", reason = "S2.7 agents, dispatch and worktrees.", members = [
      "agents check", "agents cleanup", "agents kill", "agents list", "agents runs list",
      "agents runs show", "agents show", "agents spawn", "agents stats", "agents status",
      "agents steps", "agents stop",
  ] },
  { path = "auth", disposition = "keep", carrier = "gclient auth", carried = false, ref = "#21571", node = "#21571", reason = "Operator key and credential verbs; #21555 closed without a CLI carrier. A node enrolls with gclient node enroll (#24861).", members = [
      "auth credentials", "auth key", "auth key list", "auth key revoke", "auth login",
  ] },
  { path = "clones", disposition = "keep", carrier = "gclient clones", carried = false, ref = "#21564", node = "#21564", reason = "S2.7 agents, dispatch and worktrees.", members = [
      "clones create", "clones delete", "clones list", "clones merge", "clones spawn",
      "clones sync",
  ] },
  { path = "comms", disposition = "keep", carrier = "gclient comms", carried = false, ref = "#21583", node = "#21583", reason = "Communications route family.", members = [
      "comms attach", "comms channels add", "comms channels list", "comms channels list-default",
      "comms channels remove", "comms send", "comms status",
  ] },
  { path = "cron", disposition = "keep", carrier = "gclient cron", carried = false, ref = "#21582", node = "#21582", reason = "Cron and scheduler route family.", members = [
      "cron add", "cron edit", "cron list", "cron park", "cron remove", "cron run", "cron runs",
      "cron toggle", "cron wake",
  ] },
  { path = "cutover", disposition = "keep", carrier = "gclient cutover", carried = false, ref = "#21591", node = "hub", node_carrier = "gclient node update", reason = "Install and setup route family. A node moves releases with gclient node update (#24877)." },
  { path = "datastores", disposition = "keep", carrier = "gclient datastores", carried = false, ref = "#21591", node = "hub", reason = "Install and setup route family; a node holds no datastore.", members = [
      "datastores expose", "datastores rotate-password",
  ] },
  { path = "embeddings", disposition = "keep", carrier = "gclient embeddings", carried = false, ref = "#21587", node = "#21587", reason = "AI providers, LLM routing and local models route family.", members = [
      "embeddings catalog", "embeddings doctor", "embeddings switch",
  ] },
  { path = "feedback", disposition = "keep", carrier = "gclient feedback", carried = false, ref = "#21562", node = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "feedback digest", "feedback list", "feedback observations", "feedback results",
      "feedback review", "feedback status",
  ] },
  { path = "files", disposition = "keep", carrier = "gclient files", carried = false, ref = "#21585", node = "#21585", reason = "Projects, /api/files and hub documents route family.", members = [
      "files migrate",
  ] },
  { path = "hooks", disposition = "keep", carrier = "gclient hooks", carried = false, ref = "#21569", node = "#21569", reason = "S2.11 hook ingress.", members = [
      "hooks disable", "hooks enable", "hooks list", "hooks run", "hooks status", "hooks test",
  ] },
  { path = "hub-backup", disposition = "keep", carrier = "gclient hub-backup", carried = false, ref = "#21589", node = "hub", reason = "Admin, metrics, traces and telemetry route family; backs up the hub's stores.", members = [
      "hub-backup", "hub-backup restore",
  ] },
  { path = "init", disposition = "keep", carrier = "gclient init", carried = false, ref = "#21591", node = "#21591", reason = "Install and setup route family." },
  { path = "install", disposition = "keep", carrier = "gclient install", carried = false, ref = "#21591", node = "hub", node_carrier = "gclient node install", reason = "Install and setup route family. A node installs with gclient node install (#24860)." },
  { path = "mcp-proxy", disposition = "keep", carrier = "gclient mcp-proxy", carried = false, ref = "#21566", node = "#21566", reason = "S2.10 external-MCP transport multiplexer.", members = [
      "mcp-proxy add-server", "mcp-proxy auth", "mcp-proxy call-tool", "mcp-proxy get-schema",
      "mcp-proxy import-server", "mcp-proxy list-servers", "mcp-proxy list-templates",
      "mcp-proxy list-tools", "mcp-proxy oauth-shape", "mcp-proxy recommend-tools",
      "mcp-proxy refresh", "mcp-proxy remove-server", "mcp-proxy search-tools",
      "mcp-proxy show-template", "mcp-proxy status",
  ] },
  { path = "memory", disposition = "keep", carrier = "gclient memory", carried = false, ref = "#21563", node = "#21563", reason = "S2.6 memory and search.", members = [
      "memory backfill-unscoped-lessons", "memory backup", "memory clear-graph", "memory create",
      "memory dedupe", "memory delete", "memory dream", "memory dream revert",
      "memory dream status", "memory export", "memory graph-counts", "memory invalidate",
      "memory list", "memory rebuild-crossrefs", "memory rebuild-graph", "memory recall",
      "memory reconcile", "memory reindex-embeddings", "memory restore", "memory show",
      "memory stats", "memory update",
  ] },
  { path = "merge", disposition = "keep", carrier = "gclient merge", carried = false, ref = "#21584", node = "#21584", reason = "Source control, GitHub triage and sync route family.", members = [
      "merge abort", "merge apply", "merge resolve", "merge start", "merge status",
  ] },
  { path = "nodes", disposition = "keep", carrier = "gclient node list", carried = true, node = "carried", reason = "node-install-enrollment 4.5 (#24877) carries it; node-mode 2.10 proves it on a node.", members = [
      "nodes list",
  ] },
  { path = "observations", disposition = "keep", carrier = "gclient observations", carried = false, ref = "#21562", node = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "observations list",
  ] },
  { path = "pack", disposition = "keep", carrier = "gclient pack", carried = false, ref = "#21589", node = "#21589", reason = "Admin, metrics, traces and telemetry route family." },
  { path = "panes", disposition = "keep", carrier = "gclient panes", carried = false, ref = "#21565", node = "#20202", reason = "move and swap have no gclient verb; S2.8 gterm adoption. Not on a node until remote terminal attach.", members = [
      "panes move", "panes swap",
  ] },
  { path = "pipelines", disposition = "keep", carrier = "gclient pipelines", carried = false, ref = "#21567", node = "#21567", reason = "S2.9 workflows, rules, pipelines and build.", members = [
      "pipelines approve", "pipelines check", "pipelines history", "pipelines import",
      "pipelines list", "pipelines reject", "pipelines run", "pipelines runs list",
      "pipelines runs show", "pipelines search", "pipelines show",
  ] },
  { path = "plan", disposition = "keep", carrier = "gclient plan", carried = false, ref = "#21581", node = "#21581", reason = "Plans and the plan registry route family.", members = [
      "plan coverage",
  ] },
  { path = "plans", disposition = "keep", carrier = "gclient plans", carried = false, ref = "#21581", node = "#21581", reason = "Plans and the plan registry route family.", members = [
      "plans archive", "plans list", "plans register", "plans review-evidence",
      "plans review-runs", "plans show", "plans validate",
  ] },
  { path = "postgres", disposition = "keep", carrier = "gclient postgres", carried = false, ref = "#21591", node = "hub", reason = "Install and setup route family; a node holds no datastore.", members = [
      "postgres backup", "postgres force-revoke-run", "postgres install",
      "postgres repair-code-index", "postgres restore", "postgres scoped-roles",
      "postgres status",
  ] },
  { path = "projects", disposition = "keep", carrier = "gclient projects", carried = false, ref = "#21585", node = "#21585", reason = "Project writes; Projects, /api/files and hub documents route family.", members = [
      "projects delete", "projects purge", "projects rebind", "projects refresh-verification",
      "projects rename", "projects repair",
  ] },
  { path = "qdrant", disposition = "keep", carrier = "gclient qdrant", carried = false, ref = "#21591", node = "hub", reason = "Install and setup route family; a node holds no datastore.", members = [
      "qdrant install", "qdrant status",
  ] },
  { path = "rules", disposition = "keep", carrier = "gclient rules", carried = false, ref = "#21567", node = "#21567", reason = "S2.9 workflows, rules, pipelines and build.", members = [
      "rules audit", "rules disable", "rules enable", "rules export", "rules import",
      "rules list", "rules show",
  ] },
  { path = "secrets", disposition = "keep", carrier = "gclient secrets", carried = false, ref = "#21559", node = "#21559", reason = "S2.3 config and grant issuing.", members = [
      "secrets delete", "secrets get", "secrets list", "secrets rekey", "secrets set",
  ] },
  { path = "service", disposition = "keep", carrier = "gclient service", carried = false, ref = "#21591", node = "hub", reason = "Install and setup route family; manages the hub's service.", members = [
      "service disable", "service enable", "service install", "service status",
      "service uninstall",
  ] },
  { path = "sessions", disposition = "keep", carrier = "gclient sessions", carried = false, ref = "#21562", node = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "sessions approve-dirty-paths", "sessions backfill-context-windows", "sessions delete",
      "sessions list", "sessions messages", "sessions renumber", "sessions restore",
      "sessions show", "sessions stats", "sessions summarize", "sessions terminate-terminal",
  ] },
  { path = "skills", disposition = "keep", carrier = "gclient skills", carried = false, ref = "#21580", node = "#21580", reason = "Skills route family.", members = [
      "skills disable", "skills doc", "skills enable", "skills hub add", "skills hub list",
      "skills init", "skills install", "skills list", "skills meta get", "skills meta set",
      "skills meta unset", "skills new", "skills remove", "skills search", "skills show",
      "skills update", "skills validate",
  ] },
  { path = "sync", disposition = "keep", carrier = "gclient sync", carried = false, ref = "#21591", node = "hub", reason = "Install and setup route family; syncs bundled templates into the hub's registry." },
  { path = "tasks", disposition = "keep", carrier = "gclient tasks", carried = false, ref = "#21560", node = "#21560", reason = "Task writes and maintenance; S2.4 tasks family.", members = [
      "tasks backup", "tasks clean", "tasks close", "tasks commit auto", "tasks commit link",
      "tasks commit list", "tasks commit unlink", "tasks compact analyze", "tasks compact apply",
      "tasks compact stats", "tasks create", "tasks de-escalate", "tasks delete", "tasks dep add",
      "tasks dep cycles", "tasks dep remove", "tasks dep tree", "tasks diff", "tasks doctor",
      "tasks expand apply", "tasks expand compile", "tasks expand reset", "tasks expand resume",
      "tasks expand status", "tasks expand validate-plan", "tasks label add",
      "tasks label remove", "tasks reindex", "tasks reopen", "tasks restore", "tasks stats",
      "tasks suggest", "tasks update", "tasks validation-history",
  ] },
  { path = "tokens", disposition = "keep", carrier = "gclient tokens", carried = false, ref = "#21589", node = "#21589", reason = "Admin, metrics, traces and telemetry route family.", members = [
      "tokens audit", "tokens stats",
  ] },
  { path = "ui", disposition = "keep", carrier = "gclient ui", carried = false, ref = "#21590", node = "hub", reason = "Web UI static serving route family; the hub serves the web UI.", members = [
      "ui build", "ui dev", "ui expose", "ui install-deps", "ui restart", "ui sign-in-link",
      "ui start", "ui status", "ui stop", "ui unexpose",
  ] },
  { path = "uninstall", disposition = "keep", carrier = "gclient uninstall", carried = false, ref = "#21591", node = "hub", reason = "Install and setup route family; removes the hub install." },
  { path = "unpack", disposition = "keep", carrier = "gclient unpack", carried = false, ref = "#21589", node = "#21589", reason = "Admin, metrics, traces and telemetry route family." },
  { path = "variables", disposition = "keep", carrier = "gclient variables", carried = false, ref = "#21562", node = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "variables get", "variables set",
  ] },
  { path = "webhooks", disposition = "keep", carrier = "gclient webhooks", carried = false, ref = "#21569", node = "#21569", reason = "S2.11 hook ingress.", members = [
      "webhooks list", "webhooks test",
  ] },
  { path = "workspaces", disposition = "keep", carrier = "gclient workspaces", carried = false, ref = "#21565", node = "#20202", reason = "list, create, delete and rebalance-tab; S2.8 gterm adoption. Not on a node until remote terminal attach.", members = [
      "workspaces create", "workspaces delete", "workspaces list", "workspaces rebalance-tab",
  ] },
  { path = "worktrees", disposition = "keep", carrier = "gclient worktrees", carried = false, ref = "#21564", node = "#21564", reason = "S2.7 agents, dispatch and worktrees.", members = [
      "worktrees claim", "worktrees cleanup", "worktrees create", "worktrees delete",
      "worktrees list", "worktrees release", "worktrees show", "worktrees stale",
      "worktrees stats", "worktrees sync",
  ] },
]

helper = [
  { module = "gobby.cli.__main__", disposition = "keep", ref = "#21573", reason = "Entry-point role carried by the Rust gobby binary at S3.2 #21573; deletion child #21574 removes the Python module." },
  { module = "gobby.cli.daemon", disposition = "keep", ref = "#21571", reason = "Managed datastore services for the hub lifecycle commands." },
  { module = "gobby.cli._daemon_", disposition = "keep", ref = "#21571", reason = "Lifecycle support moving with stop and restart.", members = [
      "gobby.cli._daemon_handoffs", "gobby.cli._daemon_protected_runs",
      "gobby.cli._daemon_services",
  ] },
  { module = "gobby.cli._daemon_errors", disposition = "keep", ref = "#21574", reason = "Shared daemon error formatting; leaves with the package." },
  { module = "gobby.cli.daemon_preflight", disposition = "keep", ref = "#21571", reason = "Restart start-refusal checks." },
  { module = "gobby.cli.daemon_singleton", disposition = "keep", ref = "#21571", reason = "Singleton stop gate." },
  { module = "gobby.cli._detectors", disposition = "keep", ref = "#21591", reason = "Install-time CLI detection." },
  { module = "gobby.cli._install", disposition = "keep", ref = "#21591", reason = "install command support.", members = [
      "gobby.cli._install_daemon", "gobby.cli._install_embedding_prompts",
      "gobby.cli._install_project", "gobby.cli._install_prompts", "gobby.cli._install_state",
  ] },
  { module = "gobby.cli.install_", disposition = "keep", ref = "#21591", reason = "install command support.", members = [
      "gobby.cli.install_components", "gobby.cli.install_files_home",
      "gobby.cli.install_identity", "gobby.cli.install_release", "gobby.cli.install_setup",
      "gobby.cli.install_setup_gclient", "gobby.cli.install_setup_gcode",
      "gobby.cli.install_setup_gdaemon", "gobby.cli.install_setup_ghook",
      "gobby.cli.install_setup_gmcp", "gobby.cli.install_setup_gterm",
      "gobby.cli.install_setup_impeccable", "gobby.cli.install_setup_rtk",
      "gobby.cli.install_setup_srt", "gobby.cli.install_setup_versions",
  ] },
  { module = "gobby.cli.installers", disposition = "keep", ref = "#21591", reason = "Per-CLI and service installers.", members = [
      "gobby.cli.installers", "gobby.cli.installers.agy", "gobby.cli.installers.claude",
      "gobby.cli.installers.codex", "gobby.cli.installers.compose_env",
      "gobby.cli.installers.container_restart", "gobby.cli.installers.docker_guard",
      "gobby.cli.installers.droid", "gobby.cli.installers.embedding",
      "gobby.cli.installers.falkor", "gobby.cli.installers.git_hooks",
      "gobby.cli.installers.grok", "gobby.cli.installers.hook_commands",
      "gobby.cli.installers.ide_config", "gobby.cli.installers.managed_services_lock",
      "gobby.cli.installers.mcp_config", "gobby.cli.installers.mcp_config_json",
      "gobby.cli.installers.mcp_config_shared", "gobby.cli.installers.mcp_config_toml",
      "gobby.cli.installers.postgres", "gobby.cli.installers.qdrant",
      "gobby.cli.installers.remote_preflight", "gobby.cli.installers.service",
      "gobby.cli.installers.service_common", "gobby.cli.installers.service_linux",
      "gobby.cli.installers.service_windows", "gobby.cli.installers.shared",
      "gobby.cli.installers.skill_install", "gobby.cli.installers.tmux_config",
  ] },
  { module = "gobby.cli.postgres_", disposition = "keep", ref = "#21591", reason = "postgres command support.", members = [
      "gobby.cli.postgres_backup", "gobby.cli.postgres_bootstrap",
  ] },
  { module = "gobby.cli._plan_daemon_tasks", disposition = "keep", ref = "#21581", reason = "Plan commands' daemon task calls." },
  { module = "gobby.cli._plan_validation_output", disposition = "keep", ref = "#21581", reason = "plans validate output." },
  { module = "gobby.cli._skills_", disposition = "keep", ref = "#21580", reason = "skills command support.", members = [
      "gobby.cli._skills_daemon", "gobby.cli._skills_hubs", "gobby.cli._skills_local",
      "gobby.cli._skills_metadata", "gobby.cli._skills_scaffold", "gobby.cli._skills_validation",
  ] },
  { module = "gobby.cli.config_writes", disposition = "keep", ref = "#21559", reason = "Compare-and-set config writes for install and datastores." },
  { module = "gobby.cli.hub_backup", disposition = "keep", ref = "#21589", reason = "hub-backup command support.", members = [
      "gobby.cli.hub_backup", "gobby.cli.hub_backup._content", "gobby.cli.hub_backup._integrity",
      "gobby.cli.hub_backup._manifest", "gobby.cli.hub_backup._stores",
      "gobby.cli.hub_backup._verify", "gobby.cli.hub_backup.bootstrap_restore",
      "gobby.cli.hub_backup.files_home", "gobby.cli.hub_backup.rehearsal",
  ] },
  { module = "gobby.cli.memory", disposition = "keep", ref = "#21563", reason = "memory command support.", members = [
      "gobby.cli.memory", "gobby.cli.memory._formatting", "gobby.cli.memory.common",
  ] },
  { module = "gobby.cli.tasks", disposition = "keep", ref = "#21560", reason = "tasks command support.", members = [
      "gobby.cli.tasks", "gobby.cli.tasks._crud_common", "gobby.cli.tasks._crud_detail",
      "gobby.cli.tasks._crud_listing", "gobby.cli.tasks._crud_mutations",
      "gobby.cli.tasks._crud_services", "gobby.cli.tasks._utils",
      "gobby.cli.tasks._utils.cascade", "gobby.cli.tasks._utils.claims",
      "gobby.cli.tasks._utils.config", "gobby.cli.tasks._utils.listing",
      "gobby.cli.tasks._utils.rendering", "gobby.cli.tasks._utils.resolution",
      "gobby.cli.tasks._utils.tree",
  ] },
  { module = "gobby.cli.runtime", disposition = "keep", ref = "#21574", reason = "Shared CLI runtime; leaves with the package." },
  { module = "gobby.cli.services", disposition = "keep", ref = "#21574", reason = "Shared service helpers, also imported outside the CLI; leaves with the package." },
  { module = "gobby.cli.utils", disposition = "keep", ref = "#21574", reason = "Shared CLI utilities; leaves with the package.", members = [
      "gobby.cli.utils", "gobby.cli.utils_config", "gobby.cli.utils_process",
      "gobby.cli.utils_resolution", "gobby.cli.utils_runtime", "gobby.cli.utils_shutdown",
  ] },
  { module = "gobby.cli.utils_ui", disposition = "keep", ref = "#21590", reason = "ui command support." },
  { module = "gobby.cli.ui_mode", disposition = "keep", ref = "#21590", reason = "ui command support." },
  { module = "gobby.cli.test_quality", disposition = "drop", ref = "#21574", reason = "Registration shim for the dropped test-quality group." },
  { module = "gobby.cli.test_types", disposition = "drop", ref = "#21574", reason = "Registration shim for the dropped test-types group." },
]

client = [
  { verb = "config get", carrier = "gclient config get", node = "carried", reason = "node-mode 2.4; the Python CLI has no config command. node-mode 2.10 proves it on a node." },
  { verb = "config set", carrier = "gclient config set", node = "carried", reason = "node-mode 2.4; the Python CLI has no config command. node-mode 2.10 proves it on a node." },
  { verb = "node install", carrier = "gclient node install", node = "carried", reason = "node-install-enrollment 2.2 and 3.2 (#24860); its documented-install test runs it without Python." },
  { verb = "node enroll", carrier = "gclient node enroll", node = "carried", reason = "node-install-enrollment 2.2 and 2.3 (#24860, #24861); pair_node enrolls with it (#24863)." },
  { verb = "node update", carrier = "gclient node update", node = "carried", reason = "node-install-enrollment 4.5 (#24877)." },
  { verb = "node status", carrier = "gclient node status", node = "carried", reason = "node-mode 1.3; node-mode 2.10 proves it on a node." },
  { verb = "node start", carrier = "gclient node start", node = "carried", reason = "node-mode 2.5; its tests drive launchctl and systemctl stubs." },
  { verb = "node stop", carrier = "gclient node stop", node = "carried", reason = "node-mode 2.5; its tests drive launchctl and systemctl stubs." },
  { verb = "node restart", carrier = "gclient node restart", node = "carried", reason = "node-mode 2.5; it reuses the install's restart step (#24872)." },
]
```

`tests/cli/test_cli_inventory.py` is new. It loads the file with `tomllib`,
walks `gobby.cli.cli` recursively, and lists the modules under the package
directory by file path. The module list does not import them.

- One function, `inventory_gaps(inventory, commands, modules)`, returns the
  missing, stale and duplicated members and any member outside its row's
  `path` or `module`. Every test calls it.
- A missing member names the command or module and says to add it to a
  row's `members` in `docs/contracts/cli-inventory.toml`, or to give it a row.
- A stale member names no live checked command or module.
- One further test checks the node fields and the `client` rows against the
  row grammar.

`crates/gclient/tests/cli_inventory.rs` is new. It reads the same file from
`CARGO_MANIFEST_DIR`. For each `carried = true` row whose carrier starts with
`gclient `, and for each `client` row, it runs `gclient help <verb>`, where
`<verb>` is the carrier's second word, and expects exit 0. When the carrier
has later subcommand words (`gclient node status`, `gclient config get`),
the help output must also name each of them, so a family alone does not
pass. Option words such as `--workspace` in `gclient list --workspace` are
not checked.

**Research context:**
- Counts and the runnable groups are in As-Is Facts (Click tree). A read-only
  check parsed this TOML block with `tomllib` and compared its members with
  the click tree walked at `9b148b644c` and the helper-module list. Each of
  the 279 checked commands is a member of exactly one of the 65 command rows.
  Each of the 107 helper modules is a member of exactly one of the 25 helper
  rows. No member is stale or duplicated.
- Changes from client-operator-verbs' rows (R7):
  - removed with their commands: the `auth token`, `build`,
    `hub-maintenance`, `lease` (four rows), `profiles` and `stages` rows;
    `tasks advance`, `tasks repair-lifecycle`, `tasks review` and
    `tasks stages`;
  - added: `auth key list`, `auth key revoke`,
    `sessions approve-dirty-paths`, `ui sign-in-link` and
    `workspaces rebalance-tab`. `release batch` appeared after
    client-operator-verbs' walk and was retired by #25233 (`18b3e49ba1`), so
    it has no row;
  - `mcp-server` is carried by gmcp;
  - `nodes list` is carried by `gclient node list`;
  - refs to closed #21554 and #21555 move to #21571;
  - helper rows: the `_build_` row, `installers.qwen` and
    `tasks._stage_filters` are removed; `install_setup_gmcp` is added to the
    `install_` row; `gobby.cli.daemon`, `_daemon_errors` and
    `_plan_daemon_tasks` get rows.
- The members were first resolved from the earlier family prefixes, so each
  row decides the commands it decided before. Only the matching rule
  changed.
- Owner refs are the route-family tasks under S2.12 #21568, the S2 rows and
  the S3 rows: #21559, #21560, #21562, #21563, #21564, #21565, #21566,
  #21567, #21569, #21571, #21573, #21574, #21580, #21581, #21582, #21583,
  #21584, #21585, #21587, #21589, #21590 and #21591. Node values add #20202.
- Verification (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cli_inventory.py -q`
  and `cargo nextest run -p gobby-client --test cli_inventory`.

**Granularity:** nine acceptance items, one data file and two test files,
one outcome: every Python command and helper module has exactly one recorded
decision and node status, and growth inside a known family fails. The drift
case exercises the same `inventory_gaps` function as the live checks, so it
cannot land apart from them. Only test files change, so no binary is bumped.

**Acceptance:**

- 2.7.1 - The file holds the rows in this section's TOML block, adjusted
  only by the drift rule. file: `docs/contracts/cli-inventory.toml`.
- 2.7.2 - Every non-group command and every group that runs without a
  subcommand is a member of exactly one `command` row. test:
  `tests/cli/test_cli_inventory.py::test_inventory_covers_every_command`.
- 2.7.3 - Every `command` row member is a live checked command at or under
  the row's `path`. test:
  `tests/cli/test_cli_inventory.py::test_inventory_rows_match_live_commands`.
- 2.7.4 - Every helper module is a member of exactly one `helper` row, and
  every `helper` row member is a live helper module that starts with the
  row's `module`. test:
  `tests/cli/test_cli_inventory.py::test_inventory_covers_every_helper_module`.
- 2.7.5 - Every row follows the row grammar, and paths and modules are
  unique. test:
  `tests/cli/test_cli_inventory.py::test_inventory_rows_are_well_formed`.
- 2.7.6 - Every carried `gclient` carrier names a verb that `gclient help`
  knows, and `gclient help <verb>` names each later subcommand word of a
  multiword carrier (for example `list` in `gclient node list`). test:
  `crates/gclient/tests/cli_inventory.rs::carried_rows_name_client_verbs`.
- 2.7.7 - `inventory_gaps`, given the live commands plus
  `tasks future-review` and the live modules plus
  `gobby.cli.installers.future_review`, reports both as missing and names
  the inventory file. test:
  `tests/cli/test_cli_inventory.py::test_inventory_flags_new_descendants`.
- 2.7.8 - Every `keep` or `absorb` command row has a `node` of `carried`,
  `#<digits>` or `hub`. A `carried` row has `carried = true`, only `hub`
  rows have `node_carrier`, and `drop` and `helper` rows have no node fields.
  Every `client` row has a unique `verb`, carrier `gclient <verb>`, a valid
  `node` and a reason. test:
  `tests/cli/test_cli_inventory.py::test_inventory_node_fields_are_well_formed`.
- 2.7.9 - Every `client` row's carrier names a verb that `gclient help`
  knows, and `gclient help <verb>` names each later subcommand word of a
  multiword carrier (for example `status` in `gclient node status`, `get` in
  `gclient config get`). test:
  `crates/gclient/tests/cli_inventory.rs::client_rows_name_client_verbs`.

### 2.8 Windows refusals name #25249 [category: code] (depends: 2.7, #24875)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: adds the `WINDOWS_UNSUPPORTED` constant used by the non-unix `run`; two cfg-gated functions share the name `run`
- `crates/gdaemon/tests/run_modes.rs::*` — scope-reason: adds one test of the constant's text
- `crates/gclient/src/command/node.rs`
- `crates/gclient/src/command/tests.rs::*` — scope-reason: adds one test of the node family's Windows text
- `crates/gclient/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

F11.2 (R4):

- `serve.rs` gains
  `pub const WINDOWS_UNSUPPORTED: &str = "gdaemon serve does not run on Windows yet; Gobby task #25249 (Windows Rust-only node) brings the node to Windows. Run the hub and nodes on macOS or Linux.";`.
  The `#[cfg(not(unix))]` `run` bails with it in place of today's literal,
  which pointed at the Python daemon.
- `node.rs` gains a `WINDOWS_UNSUPPORTED` constant:
  `gobby node runs on macOS and Linux; Windows nodes are Gobby task #25249 (Windows Rust-only node).`.
  Every `node` subcommand's `#[cfg(not(unix))]` path refuses with it and
  exit 2, replacing #24860's text.

**Research context:**
- Observed: the non-unix `run` is a six-line function at the end of
  `serve.rs` (818 lines). The constant adds one line, and the bail
  statement shrinks.
- CI runs on macOS and Linux, so the tests assert the constants that the
  non-unix paths use, not those paths themselves.
- Planned checks:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(run_modes)'`
  and `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client -E 'test(windows)'`.

**Acceptance:**

- 2.8.1 - `gobby_daemon::serve::WINDOWS_UNSUPPORTED` names #25249 and macOS
  or Linux, and does not name Python. test:
  `crates/gdaemon/tests/run_modes.rs::windows_refusal_names_25249`.
- 2.8.2 - The node family's Windows constant names #25249 and macOS and
  Linux. test:
  `crates/gclient/src/command/tests.rs::node_windows_refusal_names_25249`.
- 2.8.3 - `gdaemon` and `gclient` are each one patch version above the
  executor's starting tip in their `Cargo.toml`, `Cargo.lock` and
  `MANAGED_BIN_VERSION_PINS`. test:
  `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.9 Node docs: commands and platform matrix [category: docs] (depends: 2.8, #24878)
`kind: deliverable`

Targets:
- `docs/guides/node-setup.md`

`docs/guides/node-setup.md` (created by #24878) gains two sections.

- **Node commands**:
  - `gobby node status`: what each state means, its exit code, and that
    `gobby status` on a node reports the hub;
  - `gobby node start|stop|restart`: what each does on macOS and Linux, and
    that `stop` lasts until the next start or login;
  - the read and config verbs that run on a node;
  - workspace and pane verbs refuse on a node until #20202 (remote terminal
    attach), with the exact refusal text.
- **Platforms**: a table with rows macOS (supported), Linux (supported) and
  Windows (not yet supported; Gobby task #25249, Windows Rust-only node).
  It quotes both Windows refusal texts from 2.8.

**Research context:**
- Josh's ruling (memory 83a9445f) sets the matrix. The guide states the
  Windows status rather than leaving it out (F11.2).
- #24878 writes the guide's install, enrollment and update sections. This
  section adds only the two sections above.

**Acceptance:**

- 2.9.1 - The Node commands section documents `node status` states and exit
  codes, `node start|stop|restart`, and the pane-verb refusal naming #20202.
  behavior: "gobby node status" in `docs/guides/node-setup.md`.
- 2.9.2 - The Platforms table lists macOS and Linux as supported and Windows
  as not yet supported under #25249, quoting 2.8's refusal texts. behavior:
  "#25249" in `docs/guides/node-setup.md`.

### 2.10 Python-free node CLI proof [category: test] (depends: 2.9, #24863)
`kind: deliverable`

Targets:
- `tests/e2e/test_node_operator_cli.py`

`tests/e2e/test_node_operator_cli.py` is new, marked `e2e`. It imports the
`pair_hub` and `pair_node` fixtures from `tests/e2e/test_hub_node_pair.py`.
After #24863, `pair_node` enrolls with `gobby node enroll`.

- It resolves `gclient` through `gobby.utils.native_bin`, as `pair_node`
  resolves `gdaemon`.
- Tripwires: a temporary directory holds executable `python`, `python3`, `uv`
  and `gobby` stubs. Each writes a marker file and exits 1. The directory
  leads `PATH` for every node-side process, and each test asserts that no
  marker exists.
- Every `gclient` run uses `GOBBY_HOME` set to the node home and no
  `--daemon-url`, so the URL and key come from the node's bootstrap.
- Seeding: before the node commands run, the test seeds one project and
  three tasks in the hub through the hub's storage managers, as the pair
  fixtures seed keys. One task is ready, one is blocked by it, and one is
  closed.

**Research context:**
- Observed: CI has no e2e job (As-Is Facts). V2 runs this file on a Mac and
  on Linux, and #25246's readiness packet records both runs (W6).
- The pair fixtures install no SRT and no gterm, so the node reports both
  capabilities unavailable. 2.10.3 asserts the channel, not `healthy`.
- `gmcp` on a node is already proven by
  `test_gmcp_on_node_reaches_hub_through_relay` (R1).
- Planned check:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_node_operator_cli.py -v`.

**Acceptance:**

- 2.10.1 - From the node, `gclient status`, `projects list`, `projects show`
  and `tasks list|show|ready|blocked|search` succeed against the seeded hub
  and print the seeded rows, with no tripwire fired. test:
  `tests/e2e/test_node_operator_cli.py::test_node_read_verbs_run_without_python`.
- 2.10.2 - From the node, `gclient config set` changes a public key's value
  and `gclient config get` reads it back from the hub, with no tripwire
  fired. test:
  `tests/e2e/test_node_operator_cli.py::test_node_config_set_reaches_hub`.
- 2.10.3 - `gclient node status --json` reports channel `connected` with two
  capability entries, and `gclient node list` lists the node as connected.
  test:
  `tests/e2e/test_node_operator_cli.py::test_node_status_and_list_report_channel`.
- 2.10.4 - `gclient list` on the node exits 1 naming #20202 and sends no
  workspace operation to the hub. test:
  `tests/e2e/test_node_operator_cli.py::test_node_pane_verbs_refuse`.
- 2.10.5 - With the hub stopped, `gclient node status` exits 1 with
  `hub unavailable`, and `gclient status` exits 1 with `backend down` (F9.3).
  test:
  `tests/e2e/test_node_operator_cli.py::test_hub_outage_separates_node_and_hub_health`.

## D1 Windows Rust-only node
`kind: deferred`

A Windows machine cannot run `gdaemon serve` as a node: `node_setup` is
`#[cfg(unix)]`, the node bundles are Darwin and Linux only, and the non-unix
`run` refuses. Josh ruled the hub-node matrix to macOS and Linux and made a
Windows node a 1.0 obligation after hub-node. The Orchestrator filed #25249
for it (R4). This plan delivers only the refusal texts and the platform
matrix that name it (2.8, 2.9).

```yaml
deferral:
  task_ref: "#25249"
  reason: "Josh ruling (memory 83a9445f): the hub-node matrix is macOS and Linux; a Rust-only Windows node is a 1.0 obligation after hub-node, filed as #25249."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

- D1.1 - F11.1: a Windows machine installs, enrolls and runs a Rust-only node
  (`gdaemon serve` with hook ingress, agents and terminals) against a macOS
  or Linux hub, with no Python.

## V1 Plan Changelog
`kind: framing`

- 2026-10-28: First draft by Plan Writer gobby#18264 under the Orchestrator's
  rulings R1 to R7 (gobby#17281 through Triage gobby#17480).
- 2026-10-28: Consensus between Plan Writer gobby#18264 and Plan Adversary
  gobby#18269. The Orchestrator dispositioned the Enhancer's (gobby#18272)
  ENH-01 to ENH-06, and all six were applied before review. ENH-01, 04
  and 05 went in as proposed; ENH-02, 03 and 06 went in modified. The edits
  add the two reverse expansion edges to the checklist and split 2.3.1 into
  default and `--closed` cases. `machine_id` gets one source, and the
  inventory test checks the tail words of multiword carriers. `projects::find`
  matches in tiers, and 1.3 creates the `command/api.rs` request function.
  The adversary's review at `77798bc4ed` raised one blocking finding, NM-F1:
  2.6's pane guard let a bootstrap read error fall through, and explicit
  `--daemon-url` and `--token-file` bypassed the bootstrap. That was
  resolved at `ae9a2039d5`: the guard exits 3 before any token read or
  request, and acceptance 2.6.5 pins the zero-request case. The re-review
  found no remaining findings or unresolved decisions.

## V2 Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation
(`uv run gobby plans validate .gobby/plans/node-mode.md -p /Users/josh/Projects/gobby`)
and the inventory walk in 2.7, none has run yet. Each must hold on `0.5.0`
before #17436 closes:

1. `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -p gobby-client` passes,
   and `cargo clippy -p gobby-daemon -p gobby-client --all-targets -- -D warnings`
   is clean.
2. `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cli_inventory.py tests/install/test_version_pins.py tests/e2e/test_hub_node_pair.py tests/e2e/test_node_operator_cli.py`
   passes on the isolated hub (#25247 criterion 4).
3. `tests/e2e/test_node_operator_cli.py` passes on a Mac and on a Linux
   machine, and #25246's readiness packet records both runs (#25247
   criterion 2, W6).
4. `gobby node status` on one of Josh's enrolled nodes prints `healthy` with
   the hub up, and `hub unavailable` while the hub restarts.

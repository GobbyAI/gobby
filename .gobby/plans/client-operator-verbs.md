# Operator Verbs on the Client

Plan artifact: `.gobby/plans/client-operator-verbs.md`

**Plan ID:** client-operator-verbs

## Overview
`kind: framing`

Task #21571 (Operator verbs on the client) is ROADMAP row S3.1. It moves the
commands an operator types every day from the Python `gobby` CLI onto the Rust
client, `gclient`, over the daemon's public HTTP API. It also records a keep,
absorb or drop decision for every Python command, so S3.2 #21573 (The naming
switch: `gclient` → `gobby`, Python → `gobby-backend`) knows what the client
carries and what stays reachable as `gobby-backend <cmd>`.

The task asks for `start`, `stop`, `restart`, `status`, `tasks`, `lease`,
`projects` "and the rest of what an operator types every day", plus Josh's
note: `gobby config get/set code_index.sync_worker_concurrency 20`, "backed by
the same config API the MCP tools use".

This plan carries the verbs that need no lifecycle change now:

- `gclient status` (it absorbs `gobby health`) in 1.1;
- `gclient projects list|show` in 1.2;
- `gclient tasks list|show|ready|blocked|search` in 1.3;
- `gclient config get|set` in 1.4, so
  `gclient config set code_index.sync_worker_concurrency 20` works;
- the command inventory, as `docs/contracts/cli-inventory.toml`, in 1.5;
- the client docs in 1.6.

`start`, `stop`, `restart` and `lease status|promote|recover` are deferred to
D1. Their meaning changes when #21554 (Singleton lease and Python backend
lifecycle in Rust) lands, so they are planned against the landed lifecycle.
The section "S3.2 Handoff" records two obligations this plan places on S3.2.

All six deliverables change only the client, its tests, one contract file, one
Python test and docs. No daemon route, schema or config key changes.

## Decision Record
`kind: framing`

1. **Rulings.** Orchestrator gobby#14972, through LM7 gobby#15389, 2026-10-06:
   - R1 (02:36 CT): "Keep §5.2's Python CLI portion in #21554 (Singleton lease
     and Python backend lifecycle in Rust) as a thin layer: start spawns
     gdaemon serve, stop and restart go over the admin routes… D1's client
     verbs supersede it, and S3.2 deletes src/gobby/cli/daemon_lifecycle.py in
     the same change that hands the gobby name to the client. Write that
     deletion into D1 and S3.2." The reason given: until S3.2 renames the
     client, the Python `gobby` stays the operator command and every restart
     and cutover runs `uv run gobby restart`.
   - R2 (02:36 CT): option (b). "The client carries gobby lease
     status|promote|recover over /api/admin/lease/*, inside D1."
   - Found work (02:36 CT): "when gclient becomes gobby, cutover.py:36
     _BINARY_NAMES and the stamped set must include it. Put that in S3.2's
     scope in the plan."
   - 02:57 CT: ROADMAP.md stays out of this plan's commit; Lane 6's #23656
     roadmap refresh adds the S1.3 edge. "Keep the note of the two S3.2
     obligations inside the plan."
2. **Carrier: `gclient` command mode.** The verbs extend the existing
   hand-rolled parser in `crates/gclient/src/command.rs`, not a second parser.
   Global `VALUE_FLAGS` and `SWITCH_FLAGS`, plus `Parsed::finish` rejecting
   leftovers, already give every verb one exit contract. Rejected: clap. It
   would mean two parsers or a rewrite of the tested exit contract
   (`crates/gclient/tests/command_mode.rs`).
3. **One credential path.** Every command-mode verb resolves its token the way
   the workspace verbs do today: `--token-file PATH`, else
   `gobby_core::local_token::read_local_cli_token()`, which prefers
   `GOBBY_AGENT_API_TOKEN` and then reads the operator file. 1.1 moves those
   lines out of `dispatch_inner` into one function (new:
   `command::resolve_token`), so S1.4 4.3's reader rename
   (`read_api_key*`, gdaemon-front-door.md:717) touches one call site.
   - Command mode is operator-only in practice, and the daemon enforces it.
     An agent token gets 401 `route_not_permitted` on `/api/config`,
     `/api/projects` and `/api/admin/status`, which are not in
     `_AGENT_CAPABILITY_MATRIX`. It gets 401 `identity_mismatch` on
     `POST /api/mcp/*/tools/*`, because that row binds caller identity: the
     caller must also send its project, session and agent-run headers, and the
     client sends none. Under Decision Record 5 these exit 3, and the client
     prints the code. 1.6 documents this.
   - Rejected (Orchestrator, E1): parsing the agent capability token to send
     those identity headers. Agents already reach the task tools through the
     MCP proxy, and the task asks for operator verbs.
   - Rejected: reading only the operator file with `read_local_cli_token_at`.
     Its own doc comment says callers must go through `_for`, because
     sandboxed agent runs are denied the operator file. Bypassing the agent
     capability would also lift an agent run to operator authority.
4. **Lazy token for `status`.** `status` probes `GET /api/health` with no
   credential first, and reads the token only for `GET /api/admin/status`.
   A stopped daemon reports "not running" even when no token is readable.
5. **Exit codes stay the documented contract.** 0 success. 1 daemon refusal or
   error: a non-2xx reply other than 401, a tool `error` or `success: false`,
   and for `status` a degraded daemon, a typed 503 or a 502. 2 usage, decided
   before any token read or request. 3 connection refused or timed out, token
   missing, 401, or a reply that is not JSON. Errors print as
   `gclient {verb}: ...` on stderr.
6. **Error bodies are kept, and each verb sees the status first.** A new
   helper, `crates/gclient/src/command/api.rs`, returns the HTTP status with
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
   it resolves the name through 1.2's operator lookup (Adv3 F2).
7. **`tasks` goes through the MCP tool route, not REST.** It calls
   `POST /api/mcp/gobby-tasks/tools/{tool}` with the tool arguments as the
   body. The tool call lives in `tasks.rs`, its only caller (Adv3 F3). The REST `/api/tasks` route resolves `#N` against the daemon's own
   project and has no ready, blocked or search view. Project context comes
   from `--project NAME|ID` or the current directory's `.gobby/project.json`
   and is sent both as `X-Gobby-Project-Id` and as the tools' `project`
   argument. An unregistered id then fails loudly instead of listing every
   project. Writes (`create`, `update`, `close`, `reopen` and the rest) stay
   on `gobby-backend` until S2.4 #21560 (Tasks family) and its write path
   #21561 settle.
8. **`config` uses `/api/config/values`, the service the MCP config tools
   use.** `set` reads the revision, then sends a compare-and-set `PATCH`. On a
   409 `revision_conflict` it re-reads and retries once, as the Python
   `config_writes.py` helper does. The server is the type authority: VALUE is
   sent as a JSON literal when it parses as one, otherwise as a string.
   Rejected: the MCP `patch_config_values` tool, because it fronts the same
   service with a second envelope to decode. `config get|set` is a new verb;
   the Python CLI has no `config` command to retire.
9. **`gobby lease handoff` is absorbed into `stop`.** The active daemon's
   handoff route requires quiescence. S1.3's standby never serves handoff
   (gdaemon-front-door.md:1255-1260), and S1.3's stop intent releases the
   lease (gdaemon-front-door.md:1405-1410). The `stop` that D1 carries is
   therefore the handoff.
10. **Not carried by `status`.** Today's `gobby status` also prints a
    three-way schema-heads line, a lock probe and process metrics.
    - Schema heads: `gdaemon schema verify` checks the live identity receipts,
      and `gdaemon schema version` prints the embedded pin. Neither prints the
      checkout/installed/live line, so `gdaemon schema verify` is the
      operator's schema check until #21554 and S3.4 #21574 (Retire
      `gobby-backend`) collapse the heads.
    - The lock probe and pid claim belong to #21554.
    - Process metrics are in `/api/admin/status` and print with `--json`.
11. **The inventory is a contract file with rows, guarded by tests.** One TOML
    file lists every command row and helper-module row, and each row lists
    the exact commands or modules it decides. Its rows are written in 1.5
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
12. **Group decisions.**
    - Carried by this plan: `status` (absorbs `health`), `projects list|show`,
      `tasks list|show|ready|blocked|search`, and the new `config get|set`.
    - Carried by D1: `start`, `stop`, `restart`, `lease status|promote|recover`;
      `lease handoff` is absorbed into `stop`.
    - Absorbed by existing tools: the `panes` verbs that `gclient` already has
      (`close`, `read`, `rename`, `send`, `split`, `wait`), `workspaces show`
      into `gclient list --workspace`, and `schema apply` into
      `gdaemon schema apply`. `mcp-server` is absorbed into the `gobby-mcp`
      crate, #23275 (`gobby-mcp` crate: native MCP transports with OAuth and
      DCR).
    - Dropped: `auth token`, because S1.4 #21555 (API keys and node
      registration) retires the shared token; `test-quality` and `test-types`,
      because S3.4 #21574 leaves no Python to audit.
    - Everything else is kept as a gap on `gobby-backend`, with the route-family
      task that owns its takeover as `ref`.

## As-Is Facts
`kind: framing`

Evidence was read through `gcode evidence` at `dfec42f1b2`, and the citations
added after the Orchestrator's review were read at `c3a4fe3763`. None of the
cited files changed through `c3a4fe3763` (`git diff --name-only
dfec42f1b2..d6c266a340` lists six hook, workflow and agent-definition files,
none cited here, and `d6c266a340..c3a4fe3763` changes only `.gobby/plans`).
Hashes are the first 16 hex characters.

- **Command mode.** `crates/gclient/src/startup.rs:674-700`
  (`39513405fc71c9b7`) sends any first argument without a leading dash to
  `command::dispatch`. `crates/gclient/src/command.rs:86-160`
  (`d01d1ae775e67c31`) holds `VALUE_FLAGS`, `SWITCH_FLAGS` and `parse`, which
  refuses an unknown verb through `help::is_verb` and an unknown option, and
  reads any argument starting with `-` as an option. `command.rs:169-221`
  (`599bdfbe1df22004`): `dispatch_inner` calls `verbs::Action::parse`, then
  reads the token before running any verb, then prints `output.result` for
  `--json` or `output.plain`. `command.rs:1-9` (`87102e5c184960c3`) declares
  the `help`, `refs` and `verbs` modules. The file is 225 lines.
- **Help.** `crates/gclient/src/command/help.rs:1-30` (`4ed14d7514c33fb1`) and
  `84-124` (`c732c18c5c3149ee`): `VERBS` is the verb table, and `COMMON` begins
  "Every verb takes --workspace REF, --json, --daemon-url URL and
  --token-file PATH." The file is 124 lines.
- **Shared output.** `crates/gclient/src/command/verbs.rs` (595 lines):
  `CommandOutput { result, plain, status }` is `pub(super)`.
  `resolve_project` (`564-574`, `1836e18dcf4eef5e`) passes a UUID through,
  matches a name against `GET /api/projects`, and fails with exit 2 for an
  unknown name. Its one caller is `Action::run` at line 396.
- **Token.** `crates/gcore/src/local_token.rs:1-50` (`0f2383dfa29ae7f4`):
  `read_local_cli_token` resolves `gobby_home()` and prefers
  `GOBBY_AGENT_API_TOKEN`. `crates/gcore/src/lib.rs:28-40`
  (`52666a46b4fe1cfc`): `gobby_home` honours `GOBBY_HOME`.
- **Agent capability.** `src/gobby/servers/auth_service.py:60-135`
  (`d59262b519fa6874`): `_AGENT_CAPABILITY_MATRIX` includes
  `POST /api/mcp/*/tools/*` with `bind_identity` true (line 97), and excludes
  `/api/config`, `/api/projects` and `/api/admin/status`.
  `auth_service.py:150-185` (`a38da18ffacd2cca`): a bound row needs the
  caller project and session headers plus a matching agent-run or
  managed-execution header. `405-422` (`55e29e5114720f58`) returns
  `route_not_permitted` or `identity_mismatch`. `54-58`
  (`d5c0d1e691415d36`) and `258-266` (`6b66a65f6f2ec77b`) render the reason
  as a 401 body code.
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
  `crates/gdaemon/src/main.rs:30-110` (`d586d7869874eb89`) has
  `schema apply`; `main.rs:170-189` (`d1a74ae47bffa31f`) and `240-257`
  (`6e48037205452b2b`) are `schema verify` and `schema version`.
  `src/gobby/cli/schema.py:1-22` (`904c995738f91c45`) is the Python
  `schema apply`.
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
- **Lease.** `src/gobby/cli/daemon_lease.py:14-77` (`37d83044749a8100`):
  `status`, `promote`, `recover --stale-after N [--yes]` and `handoff`.
  `src/gobby/servers/routes/admin/_lease.py:48-63` (`3ec0f311d7a74cd2`): the
  handoff route requires quiescence.
- **Lifecycle plan.** `.gobby/plans/gdaemon-front-door.md:1306-1320`
  (`b3b05bd960a692a5`): #21554's §5.2 creates `src/gobby/cli/daemon_lifecycle.py`
  and moves `stop`, `restart` and `status` into it. `1255-1260`
  (`58ad896ba57305ce`): the standby serves `/api/health` as a typed 503 with
  `"state": "standby"` and the lease status, promote and recover routes, and
  nothing else. `1405-1410` (`b6cd7d826b2666f6`): the stop intent releases
  the lease. `714-720` (`c4472d32b87b77d3`): S1.4 4.3 renames the token
  readers.
- **Cutover.** `src/gobby/cli/cutover.py:25-42` (`7f083089799eb7da`):
  `_PACKAGES` and `_BINARY_NAMES` name `gcode`, `gdaemon` and `ghook` only.
- **Tests.** `crates/gclient/tests/command_mode.rs:1-30`
  (`09f67d170b8eb163`) lists 11 verbs in `VERBS`; `34-60`
  (`e4ff1742fc382e74`) asserts the `COMMON` wording;
  `161-184` (`e2ce54e971d7c156`) is its `invoke` helper.
  `crates/gclient/tests/mock_daemon/mod.rs` (1,429 lines):
  `RequestRecord { method, target, authorization, body }` at `28-34`
  (`4729c4d5ea7cfe4c`); `enqueue(method, path_prefix, status, body)` at
  `222-237` (`ee7f10195dc49cc8`) queues replies first-match by method and
  prefix; `760-800` (`346d5684e57e084f`) answers 401 to any request without
  the bearer token before recording it; `1359-1429` (`26cae13bb24d8faa`)
  writes any status code.
- **Click tree.** Walking `gobby.cli.cli` gives 354 nodes: 54 groups and 300
  commands. Two groups run without a subcommand: `hub-backup` and
  `memory dream`. The 180 Python files under the `gobby.cli` package split
  into 72 callback modules and 108 helper modules.
  `gobby.test_quality.cli` and `gobby.test_types.cli` are the only callback
  modules outside the package.

## Constraints
`kind: framing`

- Rust work loads the `rust` skill and follows `crates/CLAUDE.md`. Every
  gclient deliverable passes
  `cargo clippy -p gobby-client --all-targets -- -D warnings` and
  `crates/gclient/tests/source_size.rs`. No production file reaches 1,000
  lines. `command.rs` (225) and `help.rs` (124) are far below the 850-line
  lint, each verb lives in its own new module, and `verbs.rs` is not
  edited.
- No new crate dependency: `reqwest`, `serde_json`, `tokio` and `toml` are
  already in `crates/gclient/Cargo.toml`.
- Consumer sweeps, run at `dfec42f1b2`:
  - `gcode grep -w 'dispatch_inner|VALUE_FLAGS|SWITCH_FLAGS' crates/gclient`
    finds only `command.rs` (89, 103, 127, 138, 160, 169).
  - `gcode grep -w 'COMMON|VERBS' crates/gclient` finds `help.rs` (12, 88,
    99, 105, 108, 115, 122) and the test file's own `VERBS` in
    `command_mode.rs` (10, 39, 55).
  Every consumer sits in a file that this plan targets. `projects::rows` and
  `projects::find` are new in 1.2, and their only other caller is 1.3,
  which depends on 1.2.
- `command.rs` is targeted as `::*` in every deliverable. Each one adds a
  module declaration, which is no indexed symbol, beside its flags and its
  dispatch arm.
- This plan never edits `ROADMAP.md` (Decision Record 1).
- The client sends no credential to `/api/health` and never reads
  `~/.gobby/bootstrap.yaml`.

## P1: Operator Verbs
`kind: framing`

**Goal:** an operator runs `gclient status`, `projects`, `tasks` reads and
`config get|set` against the running daemon, and every Python command has a
recorded decision.

### 1.1 Operator HTTP helper and `status` [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `api` and `status` modules, extracts token resolution into `resolve_token`, and routes `status` before the workspace verbs' eager token read
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/help.rs::COMMON`
- `crates/gclient/src/command/api.rs`
- `crates/gclient/src/command/status.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/command_mode.rs::help_documents_one_workspace_contract_for_every_verb`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: serves and records `GET /api/health` without the bearer check
- `crates/gclient/tests/operator_support/mod.rs`
- `crates/gclient/tests/status_verb.rs`

`crates/gclient/src/command/api.rs` is new: the operator verbs' HTTP helper
(Decision Record 6).

- One request function sends JSON, with an optional bearer token, under
  `crate::daemon::REQUEST_DEADLINE` (5 s) as both connect and total timeout,
  the deadline the TUI's REST client uses (`daemon/live.rs:25`,
  `acc5e1644333ded9`, re-exported at `daemon/mod.rs:12-16`,
  `09e0961fb67e4d80`; `daemon/rest.rs:30-35`, `3484fdc92bcd278d`). It
  returns the HTTP status and the body, parsed
  when it is JSON and raw otherwise. Connection failure and timeout exit 3.
- One fallback function turns any reply a verb did not handle into a
  `CommandError`. A 401 exits 3 and prints its body `code` (for example
  `route_not_permitted`); a non-JSON body exits 3. Any other non-2xx exits 1
  with `code: message (path a.b)` from the config envelope, else `detail`.
- The helper has no MCP tool function. The tool call lives in 1.3's
  `tasks.rs`, its only caller, so 1.1 adds no code that `status` leaves
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
  (`command.rs:196-205`), which would make a stopped daemon with no token
  report a token error. Hence `resolve_token` and the routing before
  `Action::parse`.
- The typed 503 and the 502 come from gdaemon's front door; the healthy and
  degraded bodies come from Python's `/api/health`. The verb prints
  `backend.state` without a fixed list, so S1.3's `standby` needs no change.
- `/api/admin/status` is operator-only (Decision Record 3). An agent token
  gets 401 `route_not_permitted`, which exits 3 with that code printed.
- Public health is `ok` or `degraded` (`_health.py:380-392`,
  `c669c899bfe3115d`). Admin status is `healthy` only when the server is
  running, with no degraded hook runtime or services, complete event
  collection and healthy PostgreSQL stores (`_health.py:718-736`,
  `2ff8e1a3e17ba2a8`).
- Not carried: the schema-heads line, the lock probe and the pid claim
  (Decision Record 10).
- Verification commands (not yet run):
  `cargo nextest run -p gobby-client --test status_verb --test command_mode`,
  then `cargo clippy -p gobby-client --all-targets -- -D warnings`.

**Granularity:** eleven acceptance items, four production files, one outcome:
`gclient status` reports the daemon with the documented exit codes. The HTTP
helper cannot land alone, because unused Rust code fails clippy's `dead_code`
lint, so `status` is its first caller. Each later verb is its own deliverable.

**Acceptance:**

- 1.1.1 - A healthy daemon prints `healthy` with mode and uptime and exits 0.
  test: `crates/gclient/tests/status_verb.rs::status_prints_healthy_summary`.
- 1.1.2 - A degraded daemon prints `degraded:` with its services and exits 1.
  test: `crates/gclient/tests/status_verb.rs::status_degraded_exits_one`.
- 1.1.3 - A typed 503 prints `backend <state>` (tested with `starting` and
  `standby`) and exits 1, with no admin request and no token read. test:
  `crates/gclient/tests/status_verb.rs::status_typed_503_prints_backend_state`.
- 1.1.4 - A refused connection prints `not running` with the URL and exits 3.
  test: `crates/gclient/tests/status_verb.rs::status_not_running_exits_three`.
- 1.1.5 - With no token file, an empty `GOBBY_HOME` and no
  `GOBBY_AGENT_API_TOKEN`, a stopped daemon still reports `not running` and
  no token error. test:
  `crates/gclient/tests/status_verb.rs::status_not_running_needs_no_token`.
- 1.1.6 - A healthy probe with no readable token exits 3 and names the
  missing token; a 401 from `/api/admin/status` also exits 3. test:
  `crates/gclient/tests/status_verb.rs::status_missing_token_after_health_exits_three`.
- 1.1.7 - `--json` prints one object holding both bodies verbatim. test:
  `crates/gclient/tests/status_verb.rs::status_json_prints_health_and_status`.
- 1.1.8 - A 403 or 500 from `/api/admin/status` prints the daemon's code and
  message and exits 1. test:
  `crates/gclient/tests/status_verb.rs::status_error_keeps_body`.
- 1.1.9 - `gclient help` lists `status`, and its common text separates the
  workspace options from the options every verb takes. test:
  `crates/gclient/tests/command_mode.rs::help_documents_one_workspace_contract_for_every_verb`.
- 1.1.10 - A public probe of `ok` with an admin status of `degraded` and no
  degraded services prints `degraded: admin status` and exits 1. test:
  `crates/gclient/tests/status_verb.rs::status_admin_degraded_exits_one`.
- 1.1.11 - `status extra`, `status --workspace 0:1` and `status --bogus`
  each exit 2 with an empty `GOBBY_HOME` and send no request. test:
  `crates/gclient/tests/status_verb.rs::status_usage_errors_exit_two`.

### 1.2 `projects list|show` [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `projects` module and routes the `projects` verb
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/projects.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/projects_verb.rs`

`crates/gclient/src/command/projects.rs` is new:
`gclient projects list [--json]` and `gclient projects show NAME|ID [--json]`.
Both resolve the token and send one `GET /api/projects`.

- Two `pub(super)` functions carry that work. `rows` sends the request
  through the 1.1 request function and hands any reply other than 200 to
  the 1.1 fallback. `find` picks the row whose `id`, `name` or
  `display_name` equals an argument. `list` and `show` call them, and 1.3
  reuses them for `tasks --project NAME`.
- `list` prints one row per project: name, id, open task count, and the
  checkout root or `-`.
- `show` prints the row `find` picks, one field per line. No match exits 1
  with `no project NAME`.
- `--json` prints the rows, or the one row, as returned.
- `gclient projects` with no subcommand, an unknown subcommand, the wrong
  number of arguments or a leftover option exits 2. `Parsed::finish` runs
  before the token read, so a usage error sends nothing.

**Research context:**
- The route hides `_orphaned`, `_migrated` and `_global` itself, so the client
  filters nothing.
- `show` reads the list instead of `GET /api/projects/{id}` so that a name
  works with one request.
- The route is operator-only (Decision Record 3).
- Verification (not yet run): `cargo nextest run -p gobby-client --test projects_verb --test command_mode`.

**Acceptance:**

- 1.2.1 - `projects list` prints one row per project with name, id, open
  tasks and checkout root. test:
  `crates/gclient/tests/projects_verb.rs::projects_list_prints_rows`.
- 1.2.2 - `projects show` finds a project by id, by name and by display name.
  test: `crates/gclient/tests/projects_verb.rs::projects_show_matches_name_or_id`.
- 1.2.3 - `projects show` with an unknown argument exits 1 and names it.
  test: `crates/gclient/tests/projects_verb.rs::projects_show_unknown_exits_one`.
- 1.2.4 - `projects list --json` prints the route's rows unchanged. test:
  `crates/gclient/tests/projects_verb.rs::projects_list_json_prints_rows`.
- 1.2.5 - `projects`, `projects bogus`, `projects show` with no argument and
  `projects list --workspace 0:1` each exit 2 with an empty `GOBBY_HOME` and
  send no request. test:
  `crates/gclient/tests/projects_verb.rs::projects_usage_errors_exit_two`.

### 1.3 `tasks list|show|ready|blocked|search` [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `tasks` module, routes the `tasks` verb, and adds its value flags and switches
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/tasks.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: records the `X-Gobby-Project-Id` header on each request
- `crates/gclient/tests/tasks_verb.rs`

`crates/gclient/src/command/tasks.rs` is new. Its private tool call sends
`POST /api/mcp/gobby-tasks/tools/{tool}` through the 1.1 request function,
with the arguments as the body and an optional `X-Gobby-Project-Id` header.
It returns the unwrapped `result`, and hands any reply it does not handle to
the 1.1 fallback. Each subcommand calls one `gobby-tasks` tool this way.
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
- `--project NAME` reads the token and resolves through 1.2's
  `projects::rows` and `projects::find`, so the lookup keeps the operator
  error contract (Decision Record 5 and 6). A 401 exits 3 with its body
  `code`. A 403 or 500 exits 1 with the daemon's code and message, and a
  body that is not JSON exits 3. An unknown name exits 2 with
  `unknown project: NAME`. After a failed lookup no task tool is called.
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
  open and closed listing stays on `gobby-backend tasks list`.
- The row and card shapes come from `src/gobby/mcp_proxy/tools/tasks/_formatters.py:71-162`
  (`6ac386355be2a12b`): `state` nests `current_stage` and `is_closed`, and
  `task_summary_payload` carries `parent_task_id` and `dependencies`.
  `get_task` with `brief` fills `dependencies.blocked_by` and `blocking`
  (`_crud.py:507-517`, `46b7563b6d8553a5`). Test fixtures copy this shape,
  covering a staged open task, a closed task and a card with a parent and
  blockers.
- Writes are out of scope (Decision Record 7).
- Verification (not yet run): `cargo nextest run -p gobby-client --test tasks_verb --test command_mode`.

**Granularity:** nine acceptance items, two production files (`tasks.rs`
and the routing in `command.rs`), one outcome: task reads in project
context. The five subcommands share one
project rule and one tool call path, so splitting them would repeat the
same context tests.

**Acceptance:**

- 1.3.1 - `tasks list` inside a project directory calls `list_tasks` with the
  project id as both header and `project` argument and `closed: false`, and
  prints a staged open row and a closed row with their stages from the
  nested `state`; `--closed` sends `closed: true`. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_list_sends_project_header_and_arg`.
- 1.3.2 - `tasks show #N` calls `get_task` with `task_id` `#N` and the project
  header, and prints the card with its `parent_task_id` and each
  `dependencies.blocked_by` ref. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_show_resolves_ref_in_project_context`.
- 1.3.3 - `ready`, `blocked` and `search` call their tools with the flag
  mapping above, `search` taking QUERY as its position. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_ready_blocked_search_map_flags`.
- 1.3.4 - A tool result with `error`, a `success: false` reply and a
  `found: false` card each exit 1 with the message. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_tool_error_exits_one`.
- 1.3.5 - Outside a project with no `--project`, the verb exits 2 and sends
  no request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_without_project_exits_two`.
- 1.3.6 - `--all-projects` sends `all_projects: true`, no `project` argument
  and no header; `show --all-projects` exits 2. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_all_projects_omits_project`.
- 1.3.7 - `--project NAME` resolves through `GET /api/projects` to its id; an
  unknown name exits 2; a UUID sends no projects request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_project_name_resolves_via_projects`.
- 1.3.8 - Each usage rule above exits 2 with an empty `GOBBY_HOME` and sends
  no request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_usage_errors_exit_two`.
- 1.3.9 - When the `--project NAME` lookup gets a 401 `route_not_permitted`,
  the verb exits 3 and prints the code. A 403 or a 500 exits 1 with the
  daemon's code and message, and a body that is not JSON exits 3. None of
  these sends a task tool request. test:
  `crates/gclient/tests/tasks_verb.rs::tasks_project_lookup_errors_keep_operator_contract`.

### 1.4 `config get|set` [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declares the `config` module and routes the `config` verb
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/config.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/config_verb.rs`

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
  (Decision Record 8).
- The retry-once rule mirrors `config_writes.py:23-54`, so the client and
  the Python CLI behave the same on a race.
- The server flattens dotted keys and owns typing and validation, so the
  client does no schema work.
- Agent tokens get 401 `route_not_permitted`, which exits 3 with that code.
- `values.py:628-653` (`be890f444e5a9577`): `_failed_live` builds the
  failed-live map, and `_mutation_body` sets `apply_status` to `failed_live`
  whenever that map is non-empty, whatever keys the write changed.
- Verification (not yet run): `cargo nextest run -p gobby-client --test config_verb --test command_mode`.

**Granularity:** eleven acceptance items, two production files, one outcome:
reading and safely writing one config key. Every item is one reply the
compare-and-set write must handle, so the write cannot be split without
shipping a `set` that mishandles a race.

**Acceptance:**

- 1.4.1 - `config get` prints the desired value, plus the active value when
  it differs and the pending-restart flag. test:
  `crates/gclient/tests/config_verb.rs::config_get_prints_desired_and_active`.
- 1.4.2 - `config get` of an absent key exits 1. test:
  `crates/gclient/tests/config_verb.rs::config_get_unknown_key_exits_one`.
- 1.4.3 - `config set` sends `20` as a number, `true` as a boolean and `fast`
  as a string, with the read revision. test:
  `crates/gclient/tests/config_verb.rs::config_set_parses_json_literal_or_string`.
- 1.4.4 - A first 409 `revision_conflict` re-reads and succeeds on the second
  `PATCH`. test:
  `crates/gclient/tests/config_verb.rs::config_set_retries_once_on_conflict`.
- 1.4.5 - A second 409 exits 1 after exactly two `PATCH` requests. test:
  `crates/gclient/tests/config_verb.rs::config_set_second_conflict_surfaces`.
- 1.4.6 - A 422 prints the code, message, path and action and exits 1. test:
  `crates/gclient/tests/config_verb.rs::config_set_validation_error_renders_code_and_path`.
- 1.4.7 - A 500 `persistence_indeterminate` says to re-read and exits 1.
  test: `crates/gclient/tests/config_verb.rs::config_set_indeterminate_says_reread`.
- 1.4.8 - Success prints `apply_status` and the changed keys, and `--json`
  prints the success body unchanged. A changed key in `failed_live_keys`
  exits 1; a failed-live map naming only another key exits 0 with a warning.
  test: `crates/gclient/tests/config_verb.rs::config_set_prints_apply_status`.
- 1.4.9 - A 409 whose code is not `revision_conflict` exits 1 after one
  `PATCH`. test:
  `crates/gclient/tests/config_verb.rs::config_set_other_409_never_retries`.
- 1.4.10 - `config get code_index` reports a pending descendant key and a
  failed-live descendant from the map. test:
  `crates/gclient/tests/config_verb.rs::config_get_subtree_reports_descendant_flags`.
- 1.4.11 - `config`, `config bogus`, `config get`, `config set KEY` and
  `config get KEY --workspace 0:1` each exit 2 with an empty `GOBBY_HOME`
  and send no request, and `config set some.key -- -5` sends `-5` as a
  number. test:
  `crates/gclient/tests/config_verb.rs::config_usage_errors_exit_two`.

### 1.5 Command inventory [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `docs/contracts/cli-inventory.toml`
- `tests/cli/test_cli_inventory.py`
- `crates/gclient/tests/cli_inventory.rs`

`docs/contracts/cli-inventory.toml` is new and holds exactly the rows below.

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
- `gobby-backend <path>` carriers are the Python CLI after S3.2's rename.

```toml
# Command inventory: every Python `gobby` command and helper module, with its fate.
# Written by plan client-operator-verbs (#21571). Read by S3.3 #21572.

command = [
  { path = "status", disposition = "keep", carrier = "gclient status", carried = true, reason = "Carried by client-operator-verbs 1.1." },
  { path = "projects list", disposition = "keep", carrier = "gclient projects list", carried = true, reason = "Carried by client-operator-verbs 1.2." },
  { path = "projects show", disposition = "keep", carrier = "gclient projects show", carried = true, reason = "Carried by client-operator-verbs 1.2." },
  { path = "tasks list", disposition = "keep", carrier = "gclient tasks list", carried = true, reason = "Carried by client-operator-verbs 1.3." },
  { path = "tasks show", disposition = "keep", carrier = "gclient tasks show", carried = true, reason = "Carried by client-operator-verbs 1.3." },
  { path = "tasks ready", disposition = "keep", carrier = "gclient tasks ready", carried = true, reason = "Carried by client-operator-verbs 1.3." },
  { path = "tasks blocked", disposition = "keep", carrier = "gclient tasks blocked", carried = true, reason = "Carried by client-operator-verbs 1.3." },
  { path = "tasks search", disposition = "keep", carrier = "gclient tasks search", carried = true, reason = "Carried by client-operator-verbs 1.3." },
  { path = "start", disposition = "keep", carrier = "gclient start", carried = false, ref = "#21554", reason = "client-operator-verbs D1, planned once the Rust lifecycle lands." },
  { path = "stop", disposition = "keep", carrier = "gclient stop", carried = false, ref = "#21554", reason = "client-operator-verbs D1, planned once the Rust lifecycle lands." },
  { path = "restart", disposition = "keep", carrier = "gclient restart", carried = false, ref = "#21554", reason = "client-operator-verbs D1, planned once the Rust lifecycle lands." },
  { path = "lease status", disposition = "keep", carrier = "gclient lease status", carried = false, ref = "#21554", reason = "client-operator-verbs D1, over /api/admin/lease/status." },
  { path = "lease promote", disposition = "keep", carrier = "gclient lease promote", carried = false, ref = "#21554", reason = "client-operator-verbs D1, over /api/admin/lease/promote." },
  { path = "lease recover", disposition = "keep", carrier = "gclient lease recover", carried = false, ref = "#21554", reason = "client-operator-verbs D1, over /api/admin/lease/recover." },
  { path = "lease handoff", disposition = "absorb", carrier = "gclient stop", carried = false, ref = "#21554", reason = "The stop intent releases the lease; the standby never serves handoff." },
  { path = "health", disposition = "absorb", carrier = "gclient status", carried = true, reason = "status probes /api/health first." },
  { path = "panes close", disposition = "absorb", carrier = "gclient kill", carried = true, reason = "Existing gclient verb." },
  { path = "panes read", disposition = "absorb", carrier = "gclient capture-pane", carried = true, reason = "Existing gclient verb." },
  { path = "panes rename", disposition = "absorb", carrier = "gclient title", carried = true, reason = "Existing gclient verb." },
  { path = "panes send", disposition = "absorb", carrier = "gclient send-keys", carried = true, reason = "Existing gclient verb." },
  { path = "panes split", disposition = "absorb", carrier = "gclient split", carried = true, reason = "Existing gclient verb." },
  { path = "panes wait", disposition = "absorb", carrier = "gclient wait-for-output", carried = true, reason = "Existing gclient verb." },
  { path = "workspaces show", disposition = "absorb", carrier = "gclient list --workspace", carried = true, reason = "list prints one workspace's tabs and panes." },
  { path = "schema apply", disposition = "absorb", carrier = "gdaemon schema apply", carried = true, reason = "gdaemon owns the migration chain." },
  { path = "mcp-server", disposition = "absorb", carrier = "gobby-mcp", carried = false, ref = "#23275", reason = "The gobby-mcp crate serves the native MCP transports." },
  { path = "auth token", disposition = "drop", ref = "#21555", reason = "S1.4 retires the shared local CLI token." },
  { path = "test-quality", disposition = "drop", ref = "#21574", reason = "Python test tooling; S3.4 leaves no Python to audit.", members = [
      "test-quality audit",
  ] },
  { path = "test-types", disposition = "drop", ref = "#21574", reason = "Python test tooling; S3.4 leaves no Python to audit.", members = [
      "test-types audit", "test-types suppressions",
  ] },
  { path = "agents", disposition = "keep", carrier = "gobby-backend agents", carried = false, ref = "#21564", reason = "S2.7 agents, dispatch and worktrees.", members = [
      "agents check", "agents cleanup", "agents kill", "agents list", "agents runs list",
      "agents runs show", "agents show", "agents spawn", "agents stats", "agents status",
      "agents steps", "agents stop",
  ] },
  { path = "auth", disposition = "keep", carrier = "gobby-backend auth", carried = false, ref = "#21555", reason = "S1.4 API keys and node registration.", members = [
      "auth credentials", "auth key", "auth login",
  ] },
  { path = "build", disposition = "keep", carrier = "gobby-backend build", carried = false, ref = "#21567", reason = "S2.9 workflows, rules, pipelines and build." },
  { path = "clones", disposition = "keep", carrier = "gobby-backend clones", carried = false, ref = "#21564", reason = "S2.7 agents, dispatch and worktrees.", members = [
      "clones create", "clones delete", "clones list", "clones merge", "clones spawn",
      "clones sync",
  ] },
  { path = "comms", disposition = "keep", carrier = "gobby-backend comms", carried = false, ref = "#21583", reason = "Communications route family.", members = [
      "comms attach", "comms channels add", "comms channels list", "comms channels list-default",
      "comms channels remove", "comms send", "comms status",
  ] },
  { path = "cron", disposition = "keep", carrier = "gobby-backend cron", carried = false, ref = "#21582", reason = "Cron and scheduler route family.", members = [
      "cron add", "cron edit", "cron list", "cron park", "cron remove", "cron run", "cron runs",
      "cron toggle", "cron wake",
  ] },
  { path = "cutover", disposition = "keep", carrier = "gobby-backend cutover", carried = false, ref = "#21591", reason = "Install and setup route family." },
  { path = "datastores", disposition = "keep", carrier = "gobby-backend datastores", carried = false, ref = "#21591", reason = "Install and setup route family.", members = [
      "datastores expose", "datastores rotate-password",
  ] },
  { path = "embeddings", disposition = "keep", carrier = "gobby-backend embeddings", carried = false, ref = "#21587", reason = "AI providers, LLM routing and local models route family.", members = [
      "embeddings catalog", "embeddings doctor", "embeddings switch",
  ] },
  { path = "feedback", disposition = "keep", carrier = "gobby-backend feedback", carried = false, ref = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "feedback digest", "feedback list", "feedback observations", "feedback results",
      "feedback review", "feedback status",
  ] },
  { path = "files", disposition = "keep", carrier = "gobby-backend files", carried = false, ref = "#21585", reason = "Projects, /api/files and hub documents route family.", members = [
      "files migrate",
  ] },
  { path = "hooks", disposition = "keep", carrier = "gobby-backend hooks", carried = false, ref = "#21569", reason = "S2.11 hook ingress.", members = [
      "hooks disable", "hooks enable", "hooks list", "hooks run", "hooks status", "hooks test",
  ] },
  { path = "hub-backup", disposition = "keep", carrier = "gobby-backend hub-backup", carried = false, ref = "#21589", reason = "Admin, metrics, traces and telemetry route family.", members = [
      "hub-backup", "hub-backup restore",
  ] },
  { path = "hub-maintenance", disposition = "keep", carrier = "gobby-backend hub-maintenance", carried = false, ref = "#21589", reason = "Admin, metrics, traces and telemetry route family.", members = [
      "hub-maintenance abort", "hub-maintenance resume", "hub-maintenance run",
      "hub-maintenance status",
  ] },
  { path = "init", disposition = "keep", carrier = "gobby-backend init", carried = false, ref = "#21591", reason = "Install and setup route family." },
  { path = "install", disposition = "keep", carrier = "gobby-backend install", carried = false, ref = "#21591", reason = "Install and setup route family." },
  { path = "mcp-proxy", disposition = "keep", carrier = "gobby-backend mcp-proxy", carried = false, ref = "#21566", reason = "S2.10 external-MCP transport multiplexer.", members = [
      "mcp-proxy add-server", "mcp-proxy auth", "mcp-proxy call-tool", "mcp-proxy get-schema",
      "mcp-proxy import-server", "mcp-proxy list-servers", "mcp-proxy list-templates",
      "mcp-proxy list-tools", "mcp-proxy oauth-shape", "mcp-proxy recommend-tools",
      "mcp-proxy refresh", "mcp-proxy remove-server", "mcp-proxy search-tools",
      "mcp-proxy show-template", "mcp-proxy status",
  ] },
  { path = "memory", disposition = "keep", carrier = "gobby-backend memory", carried = false, ref = "#21563", reason = "S2.6 memory and search.", members = [
      "memory backfill-unscoped-lessons", "memory backup", "memory clear-graph", "memory create",
      "memory dedupe", "memory delete", "memory dream", "memory dream revert",
      "memory dream status", "memory export", "memory graph-counts", "memory invalidate",
      "memory list", "memory rebuild-crossrefs", "memory rebuild-graph", "memory recall",
      "memory reconcile", "memory reindex-embeddings", "memory restore", "memory show",
      "memory stats", "memory update",
  ] },
  { path = "merge", disposition = "keep", carrier = "gobby-backend merge", carried = false, ref = "#21584", reason = "Source control, GitHub triage and sync route family.", members = [
      "merge abort", "merge apply", "merge resolve", "merge start", "merge status",
  ] },
  { path = "nodes", disposition = "keep", carrier = "gobby-backend nodes", carried = false, ref = "#21564", reason = "S2.7 machine scoping.", members = [
      "nodes list",
  ] },
  { path = "observations", disposition = "keep", carrier = "gobby-backend observations", carried = false, ref = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "observations list",
  ] },
  { path = "pack", disposition = "keep", carrier = "gobby-backend pack", carried = false, ref = "#21589", reason = "Admin, metrics, traces and telemetry route family." },
  { path = "panes", disposition = "keep", carrier = "gobby-backend panes", carried = false, ref = "#21565", reason = "move and swap have no gclient verb; S2.8 gterm adoption.", members = [
      "panes move", "panes swap",
  ] },
  { path = "pipelines", disposition = "keep", carrier = "gobby-backend pipelines", carried = false, ref = "#21567", reason = "S2.9 workflows, rules, pipelines and build.", members = [
      "pipelines approve", "pipelines check", "pipelines history", "pipelines import",
      "pipelines list", "pipelines reject", "pipelines run", "pipelines runs list",
      "pipelines runs show", "pipelines search", "pipelines show",
  ] },
  { path = "plan", disposition = "keep", carrier = "gobby-backend plan", carried = false, ref = "#21581", reason = "Plans and the plan registry route family.", members = [
      "plan coverage",
  ] },
  { path = "plans", disposition = "keep", carrier = "gobby-backend plans", carried = false, ref = "#21581", reason = "Plans and the plan registry route family.", members = [
      "plans archive", "plans list", "plans register", "plans review-evidence",
      "plans review-runs", "plans show", "plans validate",
  ] },
  { path = "postgres", disposition = "keep", carrier = "gobby-backend postgres", carried = false, ref = "#21591", reason = "Install and setup route family.", members = [
      "postgres backup", "postgres force-revoke-run", "postgres install",
      "postgres repair-code-index", "postgres restore", "postgres scoped-roles",
      "postgres status",
  ] },
  { path = "profiles", disposition = "keep", carrier = "gobby-backend profiles", carried = false, ref = "#21567", reason = "S2.9 workflows, rules, pipelines and build.", members = [
      "profiles create", "profiles delete", "profiles disable", "profiles enable",
      "profiles list", "profiles restore", "profiles show", "profiles update",
  ] },
  { path = "projects", disposition = "keep", carrier = "gobby-backend projects", carried = false, ref = "#21585", reason = "Project writes; Projects, /api/files and hub documents route family.", members = [
      "projects delete", "projects purge", "projects rebind", "projects refresh-verification",
      "projects rename", "projects repair",
  ] },
  { path = "qdrant", disposition = "keep", carrier = "gobby-backend qdrant", carried = false, ref = "#21591", reason = "Install and setup route family.", members = [
      "qdrant install", "qdrant status",
  ] },
  { path = "rules", disposition = "keep", carrier = "gobby-backend rules", carried = false, ref = "#21567", reason = "S2.9 workflows, rules, pipelines and build.", members = [
      "rules audit", "rules disable", "rules enable", "rules export", "rules import",
      "rules list", "rules show",
  ] },
  { path = "secrets", disposition = "keep", carrier = "gobby-backend secrets", carried = false, ref = "#21559", reason = "S2.3 config and grant issuing.", members = [
      "secrets delete", "secrets get", "secrets list", "secrets rekey", "secrets set",
  ] },
  { path = "service", disposition = "keep", carrier = "gobby-backend service", carried = false, ref = "#21591", reason = "Install and setup route family.", members = [
      "service disable", "service enable", "service install", "service status",
      "service uninstall",
  ] },
  { path = "sessions", disposition = "keep", carrier = "gobby-backend sessions", carried = false, ref = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "sessions backfill-context-windows", "sessions delete", "sessions list",
      "sessions messages", "sessions renumber", "sessions restore", "sessions show",
      "sessions stats", "sessions summarize", "sessions terminate-terminal",
  ] },
  { path = "skills", disposition = "keep", carrier = "gobby-backend skills", carried = false, ref = "#21580", reason = "Skills route family.", members = [
      "skills disable", "skills doc", "skills enable", "skills hub add", "skills hub list",
      "skills init", "skills install", "skills list", "skills meta get", "skills meta set",
      "skills meta unset", "skills new", "skills remove", "skills search", "skills show",
      "skills update", "skills validate",
  ] },
  { path = "stages", disposition = "keep", carrier = "gobby-backend stages", carried = false, ref = "#21567", reason = "S2.9 workflows, rules, pipelines and build.", members = [
      "stages defaults", "stages delete", "stages list", "stages restore", "stages show",
      "stages update",
  ] },
  { path = "sync", disposition = "keep", carrier = "gobby-backend sync", carried = false, ref = "#21591", reason = "Install and setup route family." },
  { path = "tasks", disposition = "keep", carrier = "gobby-backend tasks", carried = false, ref = "#21560", reason = "Task writes and maintenance; S2.4 tasks family.", members = [
      "tasks advance", "tasks backup", "tasks clean", "tasks close", "tasks commit auto",
      "tasks commit link", "tasks commit list", "tasks commit unlink", "tasks compact analyze",
      "tasks compact apply", "tasks compact stats", "tasks create", "tasks de-escalate",
      "tasks delete", "tasks dep add", "tasks dep cycles", "tasks dep remove", "tasks dep tree",
      "tasks diff", "tasks doctor", "tasks expand apply", "tasks expand compile",
      "tasks expand reset", "tasks expand resume", "tasks expand status",
      "tasks expand validate-plan", "tasks label add", "tasks label remove", "tasks reindex",
      "tasks reopen", "tasks repair-lifecycle", "tasks restore", "tasks review", "tasks stages",
      "tasks stats", "tasks suggest", "tasks update", "tasks validation-history",
  ] },
  { path = "tokens", disposition = "keep", carrier = "gobby-backend tokens", carried = false, ref = "#21589", reason = "Admin, metrics, traces and telemetry route family.", members = [
      "tokens audit", "tokens stats",
  ] },
  { path = "ui", disposition = "keep", carrier = "gobby-backend ui", carried = false, ref = "#21590", reason = "Web UI static serving route family.", members = [
      "ui build", "ui dev", "ui expose", "ui install-deps", "ui restart", "ui start", "ui status",
      "ui stop", "ui unexpose",
  ] },
  { path = "uninstall", disposition = "keep", carrier = "gobby-backend uninstall", carried = false, ref = "#21591", reason = "Install and setup route family." },
  { path = "unpack", disposition = "keep", carrier = "gobby-backend unpack", carried = false, ref = "#21589", reason = "Admin, metrics, traces and telemetry route family." },
  { path = "variables", disposition = "keep", carrier = "gobby-backend variables", carried = false, ref = "#21562", reason = "S2.5 sessions and transcripts.", members = [
      "variables get", "variables set",
  ] },
  { path = "webhooks", disposition = "keep", carrier = "gobby-backend webhooks", carried = false, ref = "#21569", reason = "S2.11 hook ingress.", members = [
      "webhooks list", "webhooks test",
  ] },
  { path = "workspaces", disposition = "keep", carrier = "gobby-backend workspaces", carried = false, ref = "#21565", reason = "list, create and delete; S2.8 gterm adoption.", members = [
      "workspaces create", "workspaces delete", "workspaces list",
  ] },
  { path = "worktrees", disposition = "keep", carrier = "gobby-backend worktrees", carried = false, ref = "#21564", reason = "S2.7 agents, dispatch and worktrees.", members = [
      "worktrees claim", "worktrees cleanup", "worktrees create", "worktrees delete",
      "worktrees list", "worktrees release", "worktrees show", "worktrees stale",
      "worktrees stats", "worktrees sync",
  ] },
]

helper = [
  { module = "gobby.cli.__main__", disposition = "keep", ref = "#21573", reason = "python -m entry; becomes gobby-backend." },
  { module = "gobby.cli._build_", disposition = "keep", ref = "#21567", reason = "build command support.", members = [
      "gobby.cli._build_daemon", "gobby.cli._build_options", "gobby.cli._build_output",
  ] },
  { module = "gobby.cli._daemon_", disposition = "keep", ref = "#21554", reason = "Lifecycle support moving with stop and restart.", members = [
      "gobby.cli._daemon_handoffs", "gobby.cli._daemon_protected_runs",
      "gobby.cli._daemon_services",
  ] },
  { module = "gobby.cli.daemon_preflight", disposition = "keep", ref = "#21554", reason = "Restart start-refusal checks." },
  { module = "gobby.cli.daemon_singleton", disposition = "keep", ref = "#21554", reason = "Singleton stop gate." },
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
      "gobby.cli.install_setup_gterm", "gobby.cli.install_setup_impeccable",
      "gobby.cli.install_setup_rtk", "gobby.cli.install_setup_srt",
      "gobby.cli.install_setup_versions",
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
      "gobby.cli.installers.postgres", "gobby.cli.installers.qdrant", "gobby.cli.installers.qwen",
      "gobby.cli.installers.remote_preflight", "gobby.cli.installers.service",
      "gobby.cli.installers.service_common", "gobby.cli.installers.service_linux",
      "gobby.cli.installers.service_windows", "gobby.cli.installers.shared",
      "gobby.cli.installers.skill_install", "gobby.cli.installers.tmux_config",
  ] },
  { module = "gobby.cli.postgres_", disposition = "keep", ref = "#21591", reason = "postgres command support.", members = [
      "gobby.cli.postgres_backup", "gobby.cli.postgres_bootstrap",
  ] },
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
      "gobby.cli.tasks._crud_services", "gobby.cli.tasks._stage_filters",
      "gobby.cli.tasks._utils", "gobby.cli.tasks._utils.cascade", "gobby.cli.tasks._utils.claims",
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

`crates/gclient/tests/cli_inventory.rs` is new. It reads the same file from
`CARGO_MANIFEST_DIR`. For each `carried = true` row whose carrier starts with
`gclient `, it runs `gclient help <verb>`, where `<verb>` is the carrier's
second word, and expects exit 0.

**Research context:**
- Counts and the two runnable groups are in As-Is Facts (Click tree). A
  read-only check parsed this TOML block with `tomllib` and compared its
  members with the click tree walked at `dfec42f1b2` and the helper-module
  list. Each of the 302 checked commands is a member of exactly one of the
  74 command rows. Each of the 108 helper modules is a member of exactly one
  of the 23 helper rows. No member is stale, and every member sits at or
  under its row's key. The same check, given `tasks future-review` and
  `gobby.cli.installers.future_review` as extra live entries, reported both
  as missing. No `gobby.cli`, `gobby.test_quality` or `gobby.test_types`
  file changed through `d6c266a340`.
- The members were resolved once from the earlier family prefixes, so each
  row decides the commands it decided before. Only the matching rule
  changed.
- `config get|set` has no row, because no Python command exists for it.
- Owner refs are the route-family tasks under S2.12 #21568 and the S2 rows:
  #21559, #21560, #21562, #21563, #21564, #21565, #21566, #21567, #21569,
  #21580, #21581, #21582, #21583, #21584, #21585, #21587, #21589, #21590 and
  #21591.
- When #21554's §5.2 adds `daemon_lifecycle.py`, it is a callback module
  (it registers `stop`, `restart` and `status`), so it needs no helper row.
- Verification (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cli_inventory.py -q`
  and `cargo nextest run -p gobby-client --test cli_inventory`.

**Granularity:** seven acceptance items, one data file and two test files,
one outcome: every Python command and helper module has exactly one recorded
decision, and growth inside a known family fails. The drift case exercises
the same `inventory_gaps` function as the live checks, so it cannot land
apart from them.

**Acceptance:**

- 1.5.1 - The file holds exactly the rows in this section's TOML block. file:
  `docs/contracts/cli-inventory.toml`.
- 1.5.2 - Every non-group command and every group that runs without a
  subcommand is a member of exactly one `command` row. test:
  `tests/cli/test_cli_inventory.py::test_inventory_covers_every_command`.
- 1.5.3 - Every `command` row member is a live checked command at or under
  the row's `path`. test:
  `tests/cli/test_cli_inventory.py::test_inventory_rows_match_live_commands`.
- 1.5.4 - Every helper module is a member of exactly one `helper` row, and
  every `helper` row member is a live helper module that starts with the
  row's `module`. test:
  `tests/cli/test_cli_inventory.py::test_inventory_covers_every_helper_module`.
- 1.5.5 - Every row follows the row grammar, and paths and modules are
  unique. test:
  `tests/cli/test_cli_inventory.py::test_inventory_rows_are_well_formed`.
- 1.5.6 - Every carried `gclient` carrier names a verb that `gclient help`
  knows. test:
  `crates/gclient/tests/cli_inventory.rs::carried_rows_name_client_verbs`.
- 1.5.7 - `inventory_gaps`, given the live commands plus
  `tasks future-review` and the live modules plus
  `gobby.cli.installers.future_review`, reports both as missing and names
  the inventory file. test:
  `tests/cli/test_cli_inventory.py::test_inventory_flags_new_descendants`.

### 1.6 Client docs for the operator verbs [category: docs] (depends: 1.5)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/admin/gclient-commands.md`
- `docs/guides/gclient-user-guide.md`

`gclient-commands.md` gains `status`, `projects`, `tasks` and `config` in its
verb list. It also gains the credential rule (Decision Record 3): the
operator verbs are operator-only, and an agent token exits 3 with
`route_not_permitted` or `identity_mismatch`, so agents keep using the MCP
proxy. It also gains the project context rule for `tasks`, open-only and
closed-only listing, the `--` rule for dash-led config values, and a
pointer to `docs/contracts/cli-inventory.toml` for commands that stay on
`gobby-backend`. Its `_Last verified` date moves to the landing date.

`docs/guides/gclient-user-guide.md` gains a section `## Operator commands`
with the same verbs, one example each, and the exit codes.
`docs/guides/cli-commands.md` documents the Python CLI and is left for S3.2.

**Research context:**
- `gclient-commands.md` (52 lines) documents the workspace verbs and exit
  codes. The user guide (969 lines) has no command-mode section.
- The skill reference reaches agents after the next daemon start syncs
  bundled templates.

**Acceptance:**

- 1.6.1 - The admin reference documents the four operator verbs and their
  credential and project rules. behavior: "gclient config set" in
  `src/gobby/install/shared/skills/gobby/references/admin/gclient-commands.md`.
- 1.6.2 - The user guide has an operator commands section with the four
  verbs and the exit codes. behavior: "## Operator commands" in
  `docs/guides/gclient-user-guide.md`.

## D1 Lifecycle and lease verbs on the client (depends: 1.6)
`kind: deferred`

`gclient start`, `stop`, `restart` and `lease status|promote|recover` wait for
#21554 (Singleton lease and Python backend lifecycle in Rust). Per R1, #21554
keeps a thin Python layer (`start` spawns `gdaemon serve`; `stop` and
`restart` go over the admin routes), and these client verbs supersede it. The
Orchestrator adds a blocked-by on #21554 when expansion creates this task.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "start, stop, restart and the lease verbs change meaning when #21554 lands the Rust lease and lifecycle; they are planned against that landed behavior."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
    - D1.4
```

- D1.1 - `gclient start` starts the daemon the way #21554's thin Python
  layer does and waits for `/api/health`. `gclient stop` and `gclient
  restart` go over the admin routes with the options the Python commands have
  then.
- D1.2 - `gclient lease status|promote|recover` call `/api/admin/lease/*`,
  including on a standby, and `recover` takes `--stale-after SECONDS` and
  requires `--yes`.
- D1.3 - `gobby lease handoff` gets no client verb: `gclient stop` is the
  handoff, because the stop intent releases the lease.
- D1.4 - The inventory rows for `start`, `stop`, `restart`, the three lease
  verbs and `lease handoff` become `carried = true`. The Python
  `daemon_lifecycle.py` that #21554 creates stays registered until S3.2
  #21573 removes it in the change that hands the `gobby` name to the client.

## S3.2 Handoff
`kind: framing`

This plan places two obligations on S3.2 #21573 (The naming switch: `gclient`
→ `gobby`, Python → `gobby-backend`). The Orchestrator carries them into that
task's plan; this plan does not edit `ROADMAP.md` or #21573.

1. Delete `src/gobby/cli/daemon_lifecycle.py` (created by #21554's §5.2,
   gdaemon-front-door.md:1312) in the same change that hands the `gobby`
   name to the client (R1).
2. Add the client to `src/gobby/cli/cutover.py`'s `_PACKAGES` and
   `_BINARY_NAMES` (lines 25-42 name `gcode`, `gdaemon` and `ghook` today)
   and to the stamped coherent set, so a cutover builds, signs, stamps and
   promotes the renamed client with the other binaries (found work, 02:36 CT).

S3.2 also turns the inventory's `gobby-backend <path>` carriers into the real
command names.

## Rollout
`kind: framing`

- 1.1 through 1.6 land through the Merge Manager in order. Each changes only
  the client, tests, the inventory file or docs, so none needs a daemon
  restart or a schema change.
- The new verbs reach operators when `gclient` is rebuilt and promoted with
  `uv run gobby install gclient`, at a time the Orchestrator schedules. That
  path promotes the binary with `stage_and_promote_binary_file`, outside the
  stamped set (`src/gobby/cli/install_setup_gclient.py:163-174`,
  `50875bff6cde592e`; `src/gobby/cli/install.py:335-345`,
  `c05972f5ecc47e67`). `promote_workspace_binary_set` rejects `gclient` as an
  unknown member (`src/gobby/install/bin_set_coherence.py:181-183`,
  `81c572e395aabafb`), so it is not used.
- After 1.5 lands, a change that adds a Python command or a `gobby.cli`
  helper module, even inside a known family, also adds it to an inventory
  row's `members`; the test failure names it and the file.
- D1 is expanded as a needs-planning task and waits for #21554.

## V1 Plan Changelog
`kind: verification`

- 2026-10-06 03:20 CDT: First draft by the Lane 7 Plan Writer gobby#15544 on
  #21571, with rulings R1 and R2, the 02:36 found work and the 02:57 ROADMAP
  exclusion from the Orchestrator gobby#14972 through LM7 gobby#15389. 1.6
  depends on 1.5 instead of 1.4, so the docs never point at an inventory
  that has not landed. The 1.3 subcommand mapping is a list, not a table.
  The inventory rows were checked mechanically against the click tree.
- 2026-10-06 03:32 CDT: Enhancer pass applied. plan-enhancer-taskless-old
  ran once (run 63639c22) on `5dd12f12`. The Orchestrator gobby#14972
  dispositioned its seven suggestions through LM7:
  - E1, modified: Decision Record 3 now says command mode is operator-only
    in practice, because agent tokens get 401 `route_not_permitted`, or
    `identity_mismatch` on the bound task-tool row. 1.6 documents it. The
    identity-header plumbing is rejected; agents use the MCP proxy.
  - E2, accepted: the helper returns the status and the body, and each verb
    handles its typed 503, 409 `revision_conflict` and 500
    `persistence_indeterminate` before the shared fallback. A non-JSON body
    stays exit 3. A 409 with another code never retries (1.4.9).
  - E3, accepted: 1.3 names the nested JSON paths, and its fixtures copy the
    formatter shape.
  - E4, accepted: `status` exits 0 only when public health is `ok` and admin
    status is `healthy` (1.1.10).
  - E5, accepted: `Parsed::finish` runs before the token read and any
    request, and each verb has an exit-2, zero-request usage test with an
    empty `GOBBY_HOME` (1.1.11, 1.2.5, 1.3.8, 1.4.11).
  - E6, accepted: `--closed` is closed-only.
  - E7, accepted: `config set --json` prints the success body, and
    `config get` reports pending and failed-live descendants from the list
    and the map (1.4.10).
  - Writer addition in E7's area: `apply_status` covers the whole snapshot
    (`values.py:628-653`), so `set` exits 1 only when one of its own changed
    keys failed live (1.4.8).
- 2026-10-06 03:52 CDT: Adv3 gobby#15470 findings on `bf51d729` applied:
  - F1, accepted: inventory rows list their exact `members`, and nothing
    matches by prefix, so a new command or module inside a known family
    fails. 1.5.7 covers it with synthetic `tasks future-review` and
    `gobby.cli.installers.future_review` entries. The members were resolved
    from the earlier prefixes, so no disposition changed.
  - F2, accepted: `tasks --project NAME` resolves through 1.2's
    `projects::rows` and `projects::find` under the operator error contract
    (1.3.9). `verbs.rs` is no longer a target.
  - F3, accepted: the MCP tool call moves from 1.1's `api.rs` to 1.3's
    `tasks.rs`, its only caller, so each leaf passes clippy on its own.
  - N1, accepted: V2 separates the observed draft checks from the planned
    implementation checks.
- 2026-10-06 03:56 CDT: Consensus. The writer W3 gobby#15544 and Adv3
  gobby#15470 agree on this plan. Adv3 verified F1, F2, F3 and N1 resolved
  on `1e51f03e`. It independently parsed the inventory: 74 command rows with
  302 exact members and 23 helper rows with 108, no duplicate or out-of-row
  member, row metadata unchanged from `bf51d729`, and both synthetic
  descendants absent. Standard validation exits 0. No disagreement remains,
  and none went to the Orchestrator. The enhancer dispositions, rulings R1
  and R2, the 02:36 found work and the 02:57 ROADMAP exclusion stand as
  recorded above. M1 and expansion wait for the Orchestrator's pass and
  Josh's approval (LM7 gobby#15389); Adv3 then derives M1 from the committed
  plan.
- 2026-10-06 04:09 CDT: Orchestrator gobby#14972 review of `53b8bedf`, PASS
  WITH NOTES, through LM7. Applied:
  - Must fix: Rollout promoted `gclient` through
    `promote_workspace_binary_set`, which rejects it. Rollout and V2 now
    use `uv run gobby install gclient`, which promotes it with
    `stage_and_promote_binary_file` outside the stamped set.
  - Note: 1.1's request deadline is `crate::daemon::REQUEST_DEADLINE` (5 s),
    which the TUI's REST client already uses.
  - Note: the row grammar's carrier rule covers `command` rows only;
    `helper` rows have no carrier.

## V2: Verification
`kind: verification`

Observed on this draft:

- `uv run gobby plans validate <draft> -p /Users/josh/Projects/gobby` exits 0
  in standard mode (2026-10-06, the writer and Adv3 gobby#15470).
- The read-only inventory check in 1.5's Research context found every
  checked command and helper module in exactly one row, and flagged both
  synthetic descendants as missing.

Planned checks (not run; the files they test do not exist yet):

- `cargo nextest run -p gobby-client --test status_verb --test projects_verb --test tasks_verb --test config_verb --test command_mode --test cli_inventory --test source_size`
  must pass.
- `cargo clippy -p gobby-client --all-targets -- -D warnings` must pass.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cli_inventory.py -q`
  must pass.
- `uv run gobby plans validate .gobby/plans/client-operator-verbs.md -p /Users/josh/Projects/gobby --mode expansion`
  must exit 0 after M1 and before handoff.
- After `uv run gobby install gclient` promotes the client (Rollout),
  against the live daemon: `gclient status`,
  `gclient projects list`, `gclient tasks ready` from the main checkout, and
  `gclient config get code_index.sync_worker_concurrency` must each exit 0.

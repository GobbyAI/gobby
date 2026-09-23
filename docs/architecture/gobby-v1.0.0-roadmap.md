# Gobby 1.0 task tree: gdaemon front door, strangler port, hub and node

## Context

`docs/architecture/evolution.md` is the authoritative staged path, and the task graph does not track it. Stages 1 and 2 have zero tasks. Stage 3 is tracked only by #17488, an escalated umbrella whose prose still argues the "hub-and-spoke / machine-local worker" hypothesis the doc superseded. The doc itself is stale on five points (closed #20255 named as current stage; gterminal/gclient described as unmerged when both are on `0.5.0` via `7ccd140a73`; `daemon-native-runtime-boundary.md` listed as open when it is archived and #18902 closed; `GOBBY_RUNTIME_MODE` cited as precedent when #18902 deleted it; #19664 described as blocked on closed #17678).

Decisions taken with Josh on 2026-09-01:

1. **`gdaemon` becomes the public front door first.** It takes `:60887`/`:60888`, reverse-proxies HTTP and WS to the Python daemon on a loopback port, and owns the mode enum, the singleton lease, and machine registration from day one. Subsystem absorption then happens behind that boundary, one routing-table change at a time. This inverts the roadmap's "Python front door delegating to a `:60890` sidecar" and amends evolution.md decision 4 to "mode boundary first, mode semantics last". The Python-side `rust_migration` flags, `APIRoute` compare wrapper, and mismatch latch are not built; compare mode is a proxy feature.
2. **Story C (hosted gobby.ai) is deferred** until Gobby is proven in local and Tailscale production. Hosted-only items stay labeled `hosted` with no edges into the spine.
3. **M0 lease is database-wide, one active daemon per shared hub**; standbys expose lease control only. Fixes the #20009 regression before the two-machine smoke.
4. **Lightweight execution.** No `.gobby/plans/` artifact for this restructure; each stage epic gets its own Full plan when reached. #17488 is retired and a newly named root epic represents the goal.
5. The second physical machine exists. #19600 becomes a scheduled milestone gated on a human-closed stability checkpoint after the #21363 burndown.
6. **`ROADMAP.md` is the single canonical roadmap; `docs/architecture/evolution.md` retires.** All focus is on this path. Everything else is a side quest: documented as such in ROADMAP.md and labeled `sidequest` in the task graph, allowed to run concurrently (the retirement epic already does), never a blocker of anything on the path.
7. **Hub and node is the same daemon on every machine.** `hub` mode owns the datastores and everything database-backed: maintenance loops, dream, cron, retention, backups, upgrades. `node` mode is the per-machine daemon: it registers to the hub, authenticates with a machine API key, never holds datastore credentials, forwards every semantic call to the hub, runs only machine-local duties (agents, worktrees, gterm supervision, hook ingress and its ledger), and requires a hub connection (offline is a typed error, never a fallback). `standalone` is hub and node on one box. This settles the authority matrix #19647 was chartered to research; the M0 "one active daemon, remote DSNs" shape is the Python-era transition only.
8. **The Python daemon can be a node.** Its storage layer is direct psycopg throughout, so node mode brokers rather than rewrites: `gdaemon` node mode exposes loopback PostgreSQL, Qdrant, and FalkorDB endpoints tunneled over the authenticated channel to the hub `gdaemon`, which maps the machine API key to a machine-scoped PostgreSQL role under row-level security. The Python daemon on a node points its DSNs at localhost, holds no datastore credential, and loses access when the key is revoked. HTTP-only node semantics arrive with the Rust node (S2.7). Consequence: story B ships after S1 plus the transitional Python node semantics (S4.1a), months before agents, terminals, and hooks are absorbed.

9. **Naming switch, done early.** Today `gobby` is the Python daemon and its CLI. Destination: `gobby` is the interface people run all the time (the renamed `gclient`, package `gobby-client`, carrying the operator verbs over the public API); `gdaemon` is the Rust daemon in one of three modes (package `gobby-daemon`); `gterm` is the herdr-based PTY host (package `gobby-terminal`). The switch happens as soon as the client is real and carries the daily verbs (S3.2), not at Python retirement: the Python console script renames to `gobby-backend`, `gdaemon` spawns it as an internal backend, and operator-only Python commands not yet carried by the Rust `gobby` stay reachable as `gobby-backend <cmd>` until absorbed.

Outcome: one live epic tree mirroring the path, a rewritten ROADMAP.md carrying the destination architecture, naming, and decision record, evolution.md and the stale Rust migration docs deleted.

## Answer to "after #21363 and #21334, are we ready to test multi-machine?"

1. **M0 two-machine smoke (#19600)**: datastores on the hub PC, one active Python daemon, standbys under the advisory lease. Runbook: `docs/guides/remote-docker-acceptance.md` (machine A hosts Docker only, machine B runs the active daemon, machine C is the denied tailnet device). Code landed (#19648). Ready after the stability checkpoint plus the four pre-flight fixes below. Independent of #21334 and the legacy wiki retirement epic.
2. **Hub/node story B**: zero lines exist (no `hub`/`node` mode, no `/api/hub/*`, no `api_keys`, no WS enrollment, no `machine_id` on `attention_states`, no daemon-WS frame source). With decision 8, it arrives after S1 (front door, modes, lease, registration and API keys, datastore tunnel) plus S4.1a (transitional Python node semantics) and S4.2 (hub-only loops). Both hub and nodes may still be Python-backed behind their `gdaemon` front doors at that point; the port continues behind the stable boundary.

### Pre-flight defects that block the M0 smoke

| Defect | Where | Evidence |
|---|---|---|
| Singleton lease is `$GOBBY_HOME`-path-scoped | `src/gobby/deployment.py:12-30`; `src/gobby/daemon_lease.py:156` | #19648 (`9bb751a171`) keyed on `hashtext(namespace), hashtext(current_database())` and added `tests/e2e/test_single_active_daemon.py` (distinct homes, expects standby). #20009 (`a236287d16`, `daemon-native-runtime-boundary.md` §1.1) re-keyed on `deployment_token(data_root)` for grant-identity coherence, added `tests/test_daemon_lease.py:196-230` pinning that two tokens both acquire, and never touched the e2e. Two Macs at `/Users/josh/.gobby` collide and bump each other's `deployment_runtime.fencing_epoch`; Mac + Linux hub both run active. |
| Terminal list/get/`attach_locator` not machine-filtered | `src/gobby/servers/routes/terminals.py:39-84`; `src/gobby/storage/terminals.py:584-623` (filter at `:479-497` unused) | Foreign tmux row yields local `host_socket` mixed with the other machine's `socket_path`/`pane_id`. `terminal_ws.py:220-284` scopes correctly. Files owned by the #21334 worktree; coordinate. |
| Runbook selects projects by `repo_path` | `docs/guides/remote-docker-acceptance.md:490` | Removed by #19651; must be `checkout.root_path`. |
| `shared-stack.md` says every client runs its own daemon; checklist stub pending | `docs/guides/shared-stack.md:3-6,139` | Contradicts the runbook and the lease decision. |
| `tests/e2e/test_shared_datastores_m0.py` never written | M0 plan §4.1.1 | #19600 criteria promise it; rewrite to the runbook. |

## Task-graph mechanics that shape the tree (verified in code by the review)

- **`allow_automation=false` on everything this plan touches**: every task it creates, reparents, retitles, or closes (including #17488, currently `true`). `gobby build` is for non-gobby projects.
- No parent→child `blocked_by` edges: `_close_eligible_ancestors` (`_stage_utils.py:149`) auto-closes an unclaimed, manifest-free parent when its last child closes. Only sibling and cross-branch edges.
- Reparenting is safe: `update_task(parent_task_id=…)` validates cycles and rewrites descendant paths; `covers:` labels are plan-id keyed; `plans.root_task_ref` and Task Mapping are `#N` keyed. Reparent #21334 (≈25 descendants) in a quiet window.
- #21363 is recreated nightly by title, so nothing may depend on it; use a manual checkpoint leaf instead.
- #19600 carries `covers:m0-shared-datastores-bridge:4.1:*` and is that active plan's only open deliverable: it stays at `19585.19590.19600`; #19585 adopts whole. Closing #19585 auto-archives the M0 plan only when the acceptance actually passes.
- #17488 is `root_task_ref` of the active strategy plan `two-daemon-hub` and is the `task_ref` of the deferred obligation `hub-owned-files-home` D3 (line 67 of the completed plan). The plan-coverage deferral rule accepts only `completed`/`already_implemented`/`duplicate`; escalated closes need `override_justification` (`_lifecycle_close.py:153`). So #17488 closes `duplicate` of a new leaf that carries the D3.1 obligation verbatim, then `gobby-plans archive_plan plan_id=two-daemon-hub`, then the `task_ref` in `.gobby/plans/completed/hub-owned-files-home.md` is re-pointed to the leaf.
- Tools: `create_task`, `update_task` (reparent/retitle/description/task_type), `add_label`/`remove_label`, `add_dependency`/`remove_dependency`, `de_escalate_task(task_id, reason)`, `close_task(reason, changes_summary, override_justification, preview=true)`, `check_dependency_cycles`, `get_dependency_tree`; `gobby-plans:archive_plan`.

## Part A — Task tree

Legend: `NEW` = create; `ADOPT` = reparent (+ retitle/description where noted); edges as `X blocked_by Y`; every NEW or ADOPT task is set `allow_automation=false`.

### A1. Root and stage nodes

```
ROOT  NEW epic p1  "Gobby 1.0: gdaemon front door, strangler port, hub and node"
      labels: architecture, evolution, remote-stack, gobby-1.0
      description: goal (stories A and B on gdaemon + gterm + gobby, Python retired; story C deferred with
                   datastore TLS and Qdrant auth listed as not-yet-tasks), link to ROADMAP.md, side quests
                   (#21771 legacy wiki retirement and the rest of the `sidequest` list), the #21363 burndown as the
                   stability feed, retired #17488 backlink, and the rule that nothing in this tree is dispatched
                   by `gobby build`
├─ CHK     NEW task p1 (manual, leaf)  "Daemon stability checkpoint before the hub-PC move"
├─ #19585  ADOPT epic   retitle "M0: shared datastores bridge and two-machine acceptance"   (plan stays active)
│   └─ 19590 → #19600 stays; de_escalate; blocked_by CHK, FW.1–FW.4 (found-work leaves under #21363, see A2)
├─ #21334  ADOPT epic   (reparent only)                                                    Stage 0
├─ S1      NEW epic p1  "Stage 1: gdaemon front door owns the network boundary"
├─ S2      NEW epic p1  "Stage 2: strangler absorption of the Python daemon behind the front door"
├─ S3      NEW epic p2  "Stage 3: the gobby client takes the name; Python becomes gobby-backend, then retires"
└─ S4      NEW epic p2  "Stage 4: hub and node live (story B)"
```

Edges: `S2 blocked_by S1`; `S4 blocked_by S1, #19600`; S3's edges live on its sub-epics (A5). No ROOT edges. S1 starts now in its own worktree, concurrent with #21334, #21771, and the #21363 burndown.

### A2. Found work for the #21363 burndown, the checkpoint, and #19600

Code bugs the surveys found are created as children of #21363 (label `found-work`, plus `m0-smoke` on the two that gate the smoke) so the burndown agent completes them. Doc fixes are done in this task's docs commit (Part B), not filed. #21363 itself is never a dependency; its leaves are.

| Ref | Type | Title | Validation criteria (summary) |
|---|---|---|---|
| FW.1 | NEW bug p1, code/backend, under #21363 | Scope the daemon singleton lease to the shared database | `deployment_advisory_key` for the lease uses `current_database()`; fencing epoch and grant secret stay keyed per deployment token; `tests/test_daemon_lease.py::test_deployment_scoped_lease_and_epoch` rewritten to assert a second token is refused; `tests/e2e/test_single_active_daemon.py` green. (S4.1a later exempts `runtime_mode: node` from the lease.) |
| FW.2 | NEW bug p1, code/backend, under #21363 | Machine-scope terminal list, get, and `attach_locator` | `GET /api/terminals` and `/api/terminals/{id}` pass `require_machine_id()`; `attach_locator` refuses foreign rows with `machine_ownership_mismatch`; tests in `tests/servers/routes/` and `tests/storage/` cover a foreign tmux row. Note: coordinate with the #21334 worktree, which owns these files. |
| FW.3 | NEW bug p3, code/backend, under #21363 | Remove the dormant hook `machine_id` fallbacks | `hooks/event_handlers/_session_start/flow.py` (lines 158, 253, 541, 587) and `hooks/session_lookup.py:279` require `event.machine_id`; a missing value is a typed ingress error; focused hook tests pass. Does not gate the smoke. |
| CHK | NEW task p1, `manual`, under ROOT | Daemon stability checkpoint before the hub-PC move | Human-closed after the #21363 burndown; #21363 referenced in the description only. |
| #19600 | ADOPT (in place) | keep title; description addendum | `de_escalate_task` (second machine available; smoke scheduled). Addendum: criteria are `hub-pc-datastore-move.md` R0–R7 plus `remote-docker-acceptance.md` Phases 1–9 as fixed in Part B; the unwritten e2e is dropped. Edges: `blocked_by CHK, FW.1, FW.2`. |

S1.3 (lease in Rust) is `blocked_by FW.1`.

### A3. S1 sub-epics (front door)

| Ref | Title | Scope notes |
|---|---|---|
| S1.1 | `gdaemon serve`: axum front door on `:60887`/`:60888` proxying HTTP and WS to Python on loopback | axum/tokio/hyper in `crates/gdaemon/Cargo.toml`; split the 430-line `main.rs` into `cli/server/proxy/state`; bearer pass-through (Python keeps verifying); `GET /api/health` native; Python `bind_port` moves to an internal loopback port via bootstrap; the Python `gobby start` launches `gdaemon serve`, which spawns and supervises the Python backend (after S3.2 the Rust `gobby start` does this and the backend is `gobby-backend`). WS proxy must pass the 31-message `tests/servers/fixtures/terminal_ws_golden/` corpus and chat WS unchanged. Compare mode (call both, return Python, log mismatch) is a proxy flag added when S2.3 needs it. Adds the internal-port row to `docs/guides/system-requirements.md`. |
| S1.2 | Mode enum `standalone`/`hub`/`node` and the mode-assembled service container | Designed fresh (`crates/gcore/src/config/tests/standalone_removed.rs` asserts the old one is gone). `standalone` default; bootstrap key; `/api/health` reports mode. Boundary semantics only (ports, whether to hold datastores, whether to register to a hub); duties come in S4. Mirrors `src/gobby/app_context.py::ServiceContainer`. |
| S1.3 | Singleton lease and backend lifecycle in Rust | `gdaemon` holds the database-wide lease (FW.1 contract), bumps `deployment_runtime.fencing_epoch`, starts Python only when active, serves `/api/admin/lease/*` natively; standby serves lease control only. Retires `daemon_lease.py`, `daemon_lease_control.py`, `servers/lease_fence.py`, `cli/daemon_lease.py`. `gcore` has no lease code today. |
| S1.4 | Node registration over WS and machine API keys | Hub accepts node registration on the WS boundary and mints a machine API key (`machine_api_keys`, hashed, revocable; `Authorization: Bearer` + `X-Gobby-Machine-Id`); `machines` gains platform/capabilities/heartbeat/daemon-endpoint columns (migration ≥ 421, never a baseline edit); `/api/machines` list/get; `terminal.machine_id → daemon endpoint` resolution that #20202 needs; the front door authenticates node traffic by key and forwards it to the hub backend. Nodes hold no datastore credentials. Per-user auth and multi-user remain #17769 (S4.4). |
| S1.6 | Datastore tunnel for node mode | Node `gdaemon` listens on loopback PostgreSQL, Qdrant, and FalkorDB ports and multiplexes the byte streams over the authenticated channel to the hub `gdaemon`, which connects on the node's behalf as a machine-scoped PostgreSQL role (row-level security on `machine_id` columns; the capability-role machinery in `baseline.sql` and `src/gobby/storage/managed_credentials.py` is the precedent), the shared Qdrant/FalkorDB endpoints with the node's key checked at the hub. Bootstrap on a node: `datastore_mode: node`, DSNs fixed to the loopback tunnel ports, `hub_daemon_url` required. Revoking the key drops the tunnel. `gcode` on the node uses the same tunnel (S4.5 shrinks to configuration). |
| S1.5 | HTTP contract corpus for the proxied surface | `tests/contracts/http/`: health, config schema/values, reduced `GET /api/tasks`, runtime handshake; 401 body and `X-Gobby-Local-Token` alias; the HTTP-200 `{"status":"error","message":"Internal error occurred but request acknowledged","error_logged":true}` quirk (`src/gobby/servers/exception_handlers.py`); fixture format with a schema-version field; replay harness. Dual-consumed by pytest and Rust, following `terminal_ws_golden`. Parity gate for every S2 takeover. |

Edges: `S1.2 blocked_by S1.1`; `S1.3 blocked_by S1.2, FW.1`; `S1.4 blocked_by S1.2`; `S1.6 blocked_by S1.4`; S1.5 independent. S1.3 note: the singleton lease is held by `hub` and `standalone` only; a node registers instead of leasing.

S1's description carries the per-boundary validation gate every S2 plan must satisfy: fixture parity, error-path parity, side-by-side execution, route-scoped rollback via the proxy table, observability; plus the atomic-task standard (one boundary, one rollback story, one validation target) and "boundary-first, no long-lived Rust branch".

### A4. S2 sub-epics (absorption, ordered so story B emerges mid-port; hook ingress late because the rule engine is entangled with sessions and MCP dispatch)

| Ref | Title | Scope notes |
|---|---|---|
| S2.1 | `gcore` async Postgres layer | Pool vs `spawn_blocking` decision (`crates/gcore/Cargo.toml` has only sync `postgres`), repository seam, grant-role compatibility with `baseline@420`. Foundation for every DB-backed family. |
| S2.2 | Native WS transport in `gdaemon` | `:60888` accept, auth handshake, subscription filter, broadcast envelope (`src/gobby/servers/websocket/broadcast.py`); extend the golden-corpus pattern to the event envelope. Foundation for terminal, chat, and session WS takeovers. |
| S2.3 | Config, runtime handshake, and grant issuing | `/api/config/*`, `/api/runtime/*`; server half of `gcore::config` and `gcore::grant` (issuing, HMAC verification, rejection matrix). First takeover; exit = zero-mismatch compare soak, backend-kill fallback, delegate flip and rollback. |
| S2.4 | Tasks family | Reduced `GET /api/tasks` first, then `GET /api/tasks/{id}`, then write path, stages, dependencies, the `gobby-tasks` MCP server (after S2.12). Child: #20822 (authority matrix at the hub boundary) reparents under the write-path child. |
| S2.5 | Sessions and transcripts | Reduced `GET /api/sessions` first; session WS, handoffs, statusline ingestion, resumability decision. |
| S2.6 | Memory and search | Memory repos, recall injection, dream; `gcore::search::rrf_merge` and `qdrant/` exist. |
| S2.7 | Attention, agents, dispatch, worktrees with machine scoping | Rust re-implementation of the S4.1a transitional semantics (attention `machine_id` and roster, response routing, dispatcher machine gate), plus worktree/clone APIs and agent spawn with the SRT chokepoint. Node semantics become HTTP-only here; the Python backend on nodes retires when S2.7, S2.8, and S2.11 close (S4.1b). |
| S2.8 | Daemon-side gterm adoption and terminal WS | Port `src/gobby/terminals/{host_manager,leases,write_coordinator,host_reconcile,ws_protocol}.py`, `terminal_ws.py`, `proxy_relay.py`; `gdaemon` adopts `gterm` by epoch. gterm, gclient, and the three protocols untouched; `terminal_ws_golden` is the contract. Blocked by #21334 (Stage 0 hardening rewrites WriteCoordinator and host respawn). |
| S2.9 | Workflows, rules, pipelines, build, validation | Rule engine, stage manifests, `gobby build` service. |
| S2.10 | External-MCP transport multiplexer as a delegated backend | Behind Python `ToolProxyService` via the `is_internal()` seam. |
| S2.11 | Hook ingress and the node-local envelope ledger | Freeze `POST /api/hooks/execute`; claim/dedupe/replay/STOP-epoch ledger per decision 9; replaces `hooks/inbox.py`, `envelope_dedupe.py`, `servers/routes/mcp/hooks.py`; `ghook` spool and its three frozen schemas untouched. Requires S2.5, S2.9, S2.10. |
| S2.12 | MCP front door flip | `/mcp` endpoint, `ToolProxyService` enforcement, internal `gobby-*` servers re-fronted as their managers move. Last MCP step. |
| S2.13 | Remaining route families (enumerated now, one child epic each) | Children created at tree-build time with one-line scope from the gap inventory: skills; plans and plan registry; cron/scheduler; communications (Telegram/Slack); source control, GitHub triage, sync; projects, `/api/files`, and hub documents (the `/api/hub/user`, `/api/hub/chat/attachments` cutover and the `attachments/<project-id>/` move happen here, once, in Rust); code-index orchestration (gateway, prune, sync worker) and its shared dependencies; AI providers, LLM routing, local models; voice; admin, metrics, traces, telemetry; web UI static serving (`_app_ui.py`); install/setup; WS chat (last). |

Edges: `S2.3 blocked_by S1.5, S2.1`; `S2.4 blocked_by S2.3`; `S2.5 blocked_by S2.4, S2.2`; `S2.6 blocked_by S2.4`; `S2.7 blocked_by S2.4, S1.4`; `S2.8 blocked_by S2.2, #21334`; `S2.9 blocked_by S2.5, S2.7`; `S2.10 blocked_by S2.4`; `S2.11 blocked_by S2.5, S2.9, S2.10`; `S2.12 blocked_by S2.10, S2.11`; `S2.13 blocked_by S2.4, S2.2`.

### A5. S3 sub-epics (naming switch, then retirement)

| Ref | Title | Exit |
|---|---|---|
| S3.1 | Operator verbs on the client | `gclient start|stop|restart|status|tasks|lease|projects|…` over the public API; `start` launches `gdaemon serve`. Daily verbs first; the long tail of `src/gobby/cli/` (175 modules) is inventoried with a keep/absorb/drop decision per command. |
| S3.2 | Naming switch: `gclient` → `gobby`, Python → `gobby-backend` | The Python console script and package entry point rename to `gobby-backend` (`gdaemon` spawns it; humans never type it in normal use); the `gobby-client` binary ships as `gobby`; installers, Homebrew, release workflows (`.github/workflows/release-gclient.yml`), `gobby cutover`, docs, and the AGENTS.md development-commands block updated; `uv run gobby` becomes `uv run gobby-backend` for the operator-only Python commands not yet carried. Exit: `gobby` on PATH is the Rust client; `gobby start` brings up story A end to end. |
| S3.3 | Parity ledger | `docs/contracts/parity-ledger.md`, generated from the FastAPI route table plus WS messages, MCP tools, and CLI verbs, each row with a Rust status. Exit = every row `delegated` or dropped by recorded decision. References #21357 for the native terminal default flip. |
| S3.4 | Retire `gobby-backend` | One commit removes the Python daemon package, `gobby-backend`, the Python installers, proxy compare scaffolding, and pyproject; all golden corpora green against `gdaemon` alone; CI and Homebrew targets updated. |

Edges: `S3.1 blocked_by #21334, S1.1`; `S3.2 blocked_by S3.1`; `S3.4 blocked_by S3.3, S2`. S3 itself carries no stage-level edge; S3.1 and S3.2 start as soon as the client is real.

### A6. S4 sub-epics and leaves (hub and node live)

| Ref | Title | Source and mechanics |
|---|---|---|
| S4.1 | Node mode: the per-machine daemon that holds no datastore credentials and runs only local duties | ADOPT #17436; retitle (drop "worker"); description rewritten to decisions 7 and 8: registers via S1.4, authenticates with its machine API key, reaches datastores only through the S1.6 tunnel, runs agents/worktrees/gterm supervision/hook ledger locally, originating-machine dispatch, typed offline errors; fold #17440's status-state bullets in and close #17440 `duplicate`; ADOPT #20438 as a leaf beneath it. `remove_dependency(#17436, #19647)`. Two children: |
| S4.1a | Transitional Python node semantics (throwaway, re-implemented by S2.7) | NEW epic under S4.1. `runtime_mode: node` in Python bootstrap: skip the singleton lease, skip hub-only loops (S4.2), register through the local `gdaemon`; `machine_id` on `attention_states` plus hub-wide roster aggregation and response routing to the owning machine; dispatcher machine-eligibility gate (two-daemon-hub D4); worktree/agent/terminal reads stay machine-scoped (FW.2 lands first). Each leaf carries label `transitional` and names its S2.7 successor. |
| S4.1b | Rust node duties | Cross-reference to S2.7, S2.8, S2.11; closes when the Python backend on a node is no longer needed. |
| S4.2 | Hub mode: the datastore owner that runs everything database-backed | NEW. Maintenance loops, dream, cron, retention, backups, upgrades run only in `hub` (and `standalone`); node startup refuses to start them; `/api/health` reports role. First cut guards the existing Python loops behind the mode flag (`runner_init/*`, `scheduler/`, `memory_dream`, `runner_maintenance*`); absorbed later with each family. |
| S4.3 | Remote `gobby` attach | #20202 stays under #21334 (plan home); referenced. `remove_dependency(#20202, #19647)`; keep `blocked_by #19600`. |
| S4.4 | Per-user auth and multi-user: user-scoped API keys and secrets, lift `require_sole_user()` | ADOPT #17769; `remove_dependency(#17769, #19647)`; `blocked_by S1.4`; label `later`. |
| S4.5 | `gcode` on nodes | NEW leaf. With S1.6 `gcode` keeps its grant-carried datastore traits and receives tunnel DSNs from the node daemon's handshake; the daemon-API adapter (#18902's "preserve datastore traits" customer) stays a hosted-era side quest. `blocked_by S1.6`. |
| S4.6 | Hub transcript archive research | ADOPT #19652 as a leaf (from #17435); implementation children after it passes. |
| S4.7 | Hosted terminal-relay privacy stance | ADOPT orphan #20203 as a leaf; labels `hosted, deferred`; no spine edges. |
| S4.8 | Move Telegram/comms attachments onto hub `files_home` | NEW `feature` leaf, category code/backend; carries `deferred-from:hub-owned-files-home:D3` and #17488's D3.1 criterion verbatim; `blocked_by` the S2.13 hub-documents child. This leaf is what #17488 closes as a duplicate of. |
| #19647 | Close `obsolete` | Decision 7 settles the authority matrix; hosted specifics are a side quest; SSR is off-spine. `de_escalate_task` first, then `close_task(reason="obsolete", changes_summary=…)` with the ROADMAP decision cited. |

Edges: `S4.1a blocked_by S1.2, S1.4, S1.6, S4.2, FW.2`; `S4.1b blocked_by S2.7, S2.8, S2.11`; `S4.2 blocked_by S1.2`; `S4.4 blocked_by S1.4`; `S4.5 blocked_by S1.6`. Story B is testable when S1, S4.1a, and S4.2 close; S4 as a whole closes with S4.1b.

### A7. Dispositions

| Task | Action |
|---|---|
| #17488 | After all six children are reparented out or closed: append backlink to description; `add_dependency(#17488, S4.8, related)`; `close_task(reason="duplicate", changes_summary=…, override_justification=…, preview=true)`; then `gobby-plans:archive_plan plan_id=two-daemon-hub`; then edit `.gobby/plans/completed/hub-owned-files-home.md` D3 `task_ref` → S4.8. |
| #17435 | Reparent #19652 → S4.6 leaf and #20438 → under S4.1; close `obsolete` (no plan label, no deferral label). |
| #19647 | `de_escalate_task`, then close `obsolete` citing ROADMAP decision 7; edges removed from #17436, #17769, #20202 first. |
| #17438 fleet (+ #19582) | `update_task(parent_task_id="")`; `add_label gobby-pro`. Off-spine (ROADMAP 0.7.0+). |
| #17440 | Bullets copied into S4.1; close `duplicate`. |
| #19585 / #19590 | Not closed. #19585 reparents under ROOT and is retitled; M0 plan stays active. |
| #19600 | Stays in place; `de_escalate_task`; description addendum; edges per A2. |
| #20822 | Reparent under the S2.4 write-path child; keep its existing `blocked_by`. |
| #21334 | Reparent under ROOT (quiet window). |
| #21355 (under #21334 P5) | Leave; scope shrinks to the Stage 0 paragraph. |
| herdr plans (`herdr-terminal-client.md`, `herdr-terminal-client-qa-fixes.md`, `herdr-foundation-landing.md`, `herdr-client-completion.md`) | Stay active and unedited until the client lands under #21334. No `archive_plan`. |
| Side quests | `add_label sidequest` on #19670, #19664, #18779, #21504, #20201, #20203, #17438, #19880, #19564, #18498. No dependency edges from any of them into ROOT's subtree; `remove_dependency` if one is found. |
| #21355 | Description addendum re-targeting its snapshot obligations to ROADMAP.md. |

## Part B — Docs (first commit, before task creation; task refs patched in a second commit)

1. **`ROADMAP.md` rewritten as the canonical roadmap** (target ≈200 lines; absorbs evolution.md's durable content, drops release-line marketing prose):
   - **Status line**: refreshed 2026-09-01; "this document is the roadmap and the architecture decision record; the live tracker is epic ROOT".
   - **Where we are (2026-09-01)**: Python 0.5.0 daemon is the runtime; PostgreSQL/FalkorDB/Qdrant hub; Rust owns DDL (`baseline@420`) and the `gcode`/`ghook`/`gdaemon` bridgehead; gterminal and gclient merged on `0.5.0`, gclient a skeleton owned by #21334; runtime-boundary and reactive-config plans completed; `GOBBY_RUNTIME_MODE` gone; M0 shared-datastore code landed, physical smoke pending; M0 operating model is one active daemon per shared hub.
   - **Naming**: a today/destination table: `gobby` (Python daemon + CLI today) → `gobby` is the Rust client and interface; `gclient` → gone (renamed); `gdaemon` = the Rust daemon, `standalone|hub|node`; `gterm` = the herdr-based PTY host; `gobby-backend` = the transitional name of the Python package until S3.4; `gcode` and `ghook` unchanged.
   - **Destination**: the three user stories (A solo, B Tailscale homelab, C hosted, with C marked deferred); target architecture bullets (one `gdaemon` binary, three modes; `gterm` and `gobby` permanently separate processes; protocols are the durable contract with committed golden corpora; public CLI + daemon API is the plugin surface; hub documents vs machine checkouts split); the destination HTTP/files_home table and on-disk tree from evolution.md, condensed.
   - **The path**: PRE, Stage 0–4 exactly as Part A, each with its task ref and the cross-stage edges; the front-door inversion explained in Stage 1; the per-boundary validation gate and atomic-task standard stated once.
   - **Side quests**: the rule (concurrent, never blockers, labeled `sidequest`) and the list with refs: legacy wiki retirement (#21771; replacement planning follows retirement), plugin system (#20201), hosted privacy stance and story C items (#20203; PG tunnel, datastore TLS, Qdrant auth as not-yet-tasks), Gobby Pro fleet (#17438), UI design elevation (#19880), fast-mode controls (#19564), post-0.5.0 enhancement keeps (#18498), SWE-bench (`docs/plans/SWE-BENCH.md`). The feedback-findings burndown (#21363) is listed separately as the stability work that feeds CHK and hosts the `found-work` leaves gating #19600; it is neither a side quest nor a dependency itself.
   - **Decision record**: evolution.md's 1–9 condensed to one line each; add **10. gdaemon front door first (2026-09-01)**, **11. M0 lease is database-wide, one active daemon, transition shape only (2026-09-01)**, **12. ROADMAP.md is canonical; evolution.md retired; side quests documented, never blockers (2026-09-01)**, **13. Hub and node is the same daemon on every machine: hub owns datastores and all database-backed work; node holds no datastore credentials, authenticates by machine API key, forwards semantics, runs local duties only, requires a hub connection (2026-09-01)**, **14. The Python daemon can be a node: node `gdaemon` brokers PostgreSQL, Qdrant, and FalkorDB over the authenticated channel as a machine-scoped role; HTTP-only node semantics come with the Rust node (2026-09-01)**, **15. Naming: `gobby` is the client and interface, taken from `gclient` as soon as it carries the daily operator verbs; the Python package becomes `gobby-backend` until it retires; `gdaemon` and `gterm` keep their names (2026-09-01)**; decisions 4 and 5 amended in place (decision 5's "ships as `gclient` until the Stage-2 rename" becomes "until S3.2"). Story C differs from story B only in who hosts the hub and in WAN latency, which is why the tunnel is a LAN-era bridge.
   - **Ports and the proxied surface** (≈15 lines): `:60887/:60888` public (gdaemon), Python internal loopback port, `:60889` dev web UI, `:60891` managed Postgres, `:60890` released; the freeze set; the HTTP-200 error envelope; the deferred-boundary list (`/api/admin/status`, `POST /api/hooks/execute`, sessions `include_resumability`); multiplexer-before-internal-servers; the second pattern (versioned CLI contract plus thin gateway).
   - **References**: completed plans by `completed/` path (`daemon-native-runtime-boundary.md`, `shared-remote-stack.md`, `machine-scoped-worktrees-clones.md`, `project-checkout-identity.md`, `two-daemon-hub.md` after its archive); live plans at their live paths (`.gobby/plans/herdr-terminal-client.md`, `herdr-terminal-client-qa-fixes.md`, `herdr-foundation-landing.md`, `herdr-client-completion.md`, `m0-shared-datastores-bridge.md`, `hub-pc-datastore-move.md`, `retire-legacy-wiki.md`); `docs/architecture/hub-owned-files-home.md`; `docs/guides/shared-stack.md`; `docs/guides/remote-docker-acceptance.md`.
2. **Delete** `docs/architecture/evolution.md`, `docs/plans/rust-migration-epic.md`, `docs/plans/abandoned/rust-port.md`, `docs/plans/abandoned/rust-port-early-foundation.md`.
3. **Re-point live references to evolution.md** (found by `gcode grep -l -F evolution.md`): `docs/architecture/hub-owned-files-home.md` lines 13, 120, 166, 233 → `ROADMAP.md` sections; `docs/plans/herdr-terminal-recovery-decision.md:90` → ROADMAP.md. The live herdr plans (`herdr-client-completion.md` with 10 mentions, leaf 5.1 = #21355 targeting evolution.md's "Remaining path"; `herdr-terminal-client.md` with 2) are left byte-for-byte to avoid plan-artifact drift; instead #21355's task description gets an addendum: evolution.md retired 2026-09-01, the 5.1 snapshot obligations apply to ROADMAP.md "Where we are" and "The path". Completed plans keep their historical mentions.
4. **Runbook fixes in this commit**: `docs/guides/remote-docker-acceptance.md:490` selects on `.checkout.root_path` (no `repo_path` remains in the guide); `docs/guides/shared-stack.md` states one active daemon per shared hub with standbys on lease control, notes that this is the transition shape before hub/node modes, and its line-139 checklist stub is replaced by the checklist derived from `remote-docker-acceptance.md` Phases 1–9.
5. Memory hygiene at close: `search_memories` for "evolution.md" and "staged path"; update any memory that names evolution.md as authoritative to ROADMAP.md (the memory skill's update-in-place rule).

## Part C — Execution order (this session)

1. `create_task(claim=true)`: chore, docs, "Make ROADMAP.md canonical, retire evolution.md, and rebuild the task tree as the Gobby 1.0 epic".
2. Part B edits; `git commit --only` ROADMAP.md, the two re-pointed docs, the two runbook fixes, and the four deletions.
3. Create ROOT and S1–S4 (`allow_automation=false`, as for every task below); then S1–S4 sub-epics, S2.13 family children, CHK, S4.8, and FW.1–FW.3 under #21363; then all edges (including the three `remove_dependency` calls on #19647); then adoptions (reparent/retitle/description); then `de_escalate_task` #19600, #19647; then closes (#19647, #17440, #17435, #17488 last); then `archive_plan` for `two-daemon-hub` only; then the `hub-owned-files-home.md` D3 `task_ref` edit.
4. `check_dependency_cycles`; `get_dependency_tree ROOT`; `list_blocked_tasks` sanity.
5. Patch ROADMAP.md with the assigned `#NNNNN` refs; second commit (includes the D3 `task_ref` edit).
6. `close_task` the working task with the second commit SHA; `review_task_memories`; memory hygiene per Part B.5; one new memory: ROADMAP.md is canonical, the front-door ordering, and the lease scope, with rationale.

## Verification

- `list_tasks parent_task_id=ROOT` returns CHK, #19585, #21334, S1, S2, S3, S4; `get_dependency_tree ROOT` matches the edges above; `check_dependency_cycles` clean.
- `list_tasks parent_task_id=#21363 label=found-work` returns FW.1–FW.3 with validation criteria set.
- `gcode grep -F "repo_path" docs/guides/remote-docker-acceptance.md -m 5` returns nothing; `docs/guides/shared-stack.md` has no "pending the operator-coordinated" stub.
- `get_task #17488` closed `duplicate` with backlink; `list_tasks parent_task_id=#17488` returns only closed tasks; `two-daemon-hub` shows archived in `gobby-plans`.
- `get_task #19600`: parent unchanged, not escalated, `blocked_by` CHK, FW.1, FW.2.
- `get_task #17436`, `#17769` reparented under S4 with the edges above and no `#19647` edge; `#19647` closed `obsolete`; `#20203` has a parent.
- `gcode grep -l -F "evolution.md" -m 50` returns only completed plans, `herdr-terminal-client.md`, and `herdr-client-completion.md`; `gcode grep -F "rust-migration-epic" ROADMAP.md docs/architecture -m 5` returns nothing; the four deleted files are gone.
- `list_tasks label=sidequest` returns the ten side-quest roots; `get_dependency_tree ROOT` shows no upstream blocker carrying that label.
- No Python or Rust source changes in this task; PRE leaves carry their own tests.

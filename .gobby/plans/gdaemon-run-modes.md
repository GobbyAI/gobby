Plan artifact: `.gobby/plans/gdaemon-run-modes.md`

# Gobby 1.0 Stage 1 slice: run modes

**Plan ID:** gdaemon-run-modes

## Overview
`kind: framing`

This is the P2 slice (S1.2, with S4.2 #21575 pulled forward) of the Stage 1
front-door plan of record, `.gobby/plans/gdaemon-front-door.md`. The root is the
existing phase epic #21553 (Run modes) under #21543. The slice is planned on its
own so it can be reviewed, approved, and expanded directly under #21553.
Expanding the whole plan of record would mint duplicate phase epics and stamp the
P4 and P5 sections, which are still unreviewed. The plan of record keeps a
one-line pointer to this file. Expansion creates phase sub-epics only for
multi-phase plans (`src/gobby/tasks/expansion/_apply.py`), so the single P2
phase expands its deliverables directly under #21553.

When the slice closes:
- Bootstrap names the run mode (`standalone`, `hub`, `node`).
- `/api/health` reports it.
- A node runner starts none of the hub-only maintenance.

The slice is preparatory (Decision 7). It does not yet produce a hub and a node
that are both operational at once.

## Decision Record
`kind: framing`

1. **Mode encoding (unchanged from the plan of record).**
   - Keep `datastore_mode: local | remote` and add `hub: bool`, default `false`.
   - `(local, false)` is `standalone`, `(local, true)` is `hub`, and
     `(remote, false)` is `node`.
   - `(remote, true)` is rejected.
   - Reconcile #21578's `runtime_mode: node` wording to `datastore_mode: remote`
     plus `hub: false` when this slice expands.
2. **Python owns the mode in Stage 1.** The Rust bootstrap parser gains `hub`
   in the same leaf as its first Rust consumer: the node relay or the lease
   (plan of record 4.4 and 5.1). Until then this slice makes no Rust change.
   - Today no production Rust code reads `datastore_mode`.
     `gobby_core::bootstrap::FilesHomeView`, `parse_files_home_view`, and
     `read_files_home_view_at` are used only by their own tests in
     `crates/gcore/src/bootstrap.rs`.
   - `gdaemon serve` reads `HubDatabaseBootstrap` through `load_enabled_bootstrap`
     (`crates/gdaemon/src/serve.rs`), and that struct has no mode.
   - `hub` and `run_mode()` are bootstrap-only in this slice, like `front_door`.
     `DaemonConfig`, `BootstrapConfig.to_config_dict`, and
     `CliRuntime._overlay_bootstrap` (`src/gobby/cli/runtime.py`) stay unchanged,
     and every consumer reads `runner.bootstrap_config` or
     `server.bootstrap_config` directly.
3. **`/api/health` carries `mode` from Python.**
   - `health_check` adds `"mode": bootstrap_config.run_mode()` to its payload.
   - gdaemon's native `health` family forwards to Python and only adds
     `x-gobby-served-by` (`crates/gdaemon/src/front_door/health.rs::native_health`).
     The field therefore reaches clients under both `proxy` and `native`
     routing, with no gdaemon change.
   - While the backend is down, the typed 503 carries no mode. That body belongs
     to gdaemon, and the 5.2 supervisor owns it.
4. **The `AppState` mode container moves out of this slice.** The plan of
   record's 2.2 (`crates/gdaemon/src/state.rs` with
   `ModeServices::{Standalone, Hub, Node}`) has no Stage 1 consumer.
   - P1 landed `FrontDoorState` and the static `FAMILIES` table instead.
   - Every variant would be empty until 4.4 adds the node channel and registry
     and 5.1 adds the lease.
   - The container is built by whichever of those leaves adds the first
     mode-only handle.
   - 2.2's remaining obligation, that a node serves health with `mode: node`, is
     2.1.4 here.
5. **Hub-only means hub-row maintenance.** A loop is hub-only when its work
   retains, sweeps, refreshes, or schedules shared hub rows without a machine
   filter. A node's runner talks to the hub database, so running that loop there
   would duplicate or sweep the hub's work. Loops that act only on the local
   machine keep running in every mode: processes, agent runs listed for this
   machine, tmux, installed binaries, local worktrees, and the local hook inbox
   and quarantine. When a machine-local loop contains one unscoped shared-row
   step, that step is gated inside its own function so both of its callers are
   covered. Per-run rows that carry a `machine_id` are recovered by the owning
   machine in every mode. 2.2 pins the exact sets.
6. **`gobby datastores expose` promotes a machine to hub.** It is already the
   documented hub-setup step (`docs/guides/shared-stack.md`), and it runs only on
   a `local` bootstrap. It writes `hub: true` into its staged bootstrap, and its
   existing rollback restores the prior bootstrap, including the prior flag.
   `gobby install` keeps writing `hub: false`.
7. **This slice is preparatory.** A running node is out of its scope.
   - `run_gobby` (`src/gobby/runner.py`) takes `ActiveDaemonLease` for every
     bootstrap before it builds the runner. A second daemon on the shared
     database therefore serves standby only (`docs/guides/shared-stack.md`).
   - This slice leaves startup and the lease untouched. 2.1.4 proves the health
     contract on an `HTTPServer` with a `node` bootstrap. 2.2 proves the loop
     gates on runner functions.
   - The plan of record owns the operational node. Its 5.1 moves the lease into
     gdaemon, where a `node` never leases, and lists `run_gobby` in its Targets.
     Its 4.4 adds the native node channel and relay and the first hub-node pair
     test. That keeps the thin native node, and no Python node daemon is added
     here.

## Constraints
`kind: framing`

- **P1 has landed.** These facts hold on `0.5.0`.
  - #23041 added the bootstrap `front_door` block and `backend_ports`.
  - #23043 added `gdaemon serve` with the typed 503 and the forwarding native
    `health` family.
  - #23044 made ghook treat the typed 503 as unreachable.
  - #23045 made the runner own the front door (`src/gobby/runner_front_door.py`).
    Python binds `127.0.0.1` at public+100.
  - `runner.bootstrap_config` is the runner's parsed bootstrap
    (`src/gobby/runner_init/servers.py` passes it to `HTTPServer`, which stores it
    as `server.bootstrap_config`).
- **Corpus coordination.** The HTTP corpus slice
  (`.gobby/plans/gdaemon-http-contract-corpus.md`, root #21552) records
  `/api/health` in `tests/contracts/http/health_ok.json`. Either slice may land
  first, and 2.1 states what happens in each order.
- **No backward compatibility.** A bootstrap with `hub` absent parses as
  `hub: false`. There is no migration.
- **Test isolation.** pytest runs use
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never the full suite.

## P2: Run modes (S1.2, #21553; S4.2 #21575 pulled forward)
`kind: framing`

**Goal**: bootstrap names the mode, health reports it, and a node refuses to run
hub maintenance.

### 2.1 `hub` flag, writers, and the mode on `/api/health` [category: config]
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::*` — scope-reason: `BootstrapConfig` gains `hub` and `run_mode()`; `bootstrap_from_mapping` and `_parse_mode_owner_fields` parse and reject `(remote, true)`; `hub` defaults to false so constructor sites need no edit
- `src/gobby/config/bootstrap_io.py::inject_local_files_home`
- `src/gobby/config/bootstrap_io.py::_merge_owner_fields`
- `src/gobby/cli/install_setup.py::ensure_daemon_config`
- `src/gobby/cli/datastores.py::expose_datastores`
- `docs/guides/shared-stack.md`
- `tests/cli/test_datastores_expose.py::*` — scope-reason: promotion to `hub: true` and rollback of the prior flag
- `src/gobby/install/shared/config/bootstrap.yaml::*` — scope-reason: the bundled template gains the `hub: false` line and its comment
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: `health_check` inside `create_health_router` adds the `mode` field
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `docs/guides/configuration.md`
- `tests/config/test_bootstrap.py::*` — scope-reason: mode derivation, the `(remote, true)` rejection, and writer defaults
- `tests/config/test_files_home.py::*` — scope-reason: consumer of `inject_local_files_home` and `ensure_daemon_config`; written files gain the `hub` line
- `tests/cli/test_install_setup.py::*` — scope-reason: consumer of `ensure_daemon_config`; written files gain the `hub` line
- `tests/servers/test_admin_health.py::*` — scope-reason: health reports the mode for each bootstrap
- `tests/contracts/http/health_ok.json::*` — scope-reason: re-recorded corpus fixture whose `/api/health` body gains `"mode": "standalone"` when the corpus slice landed first

**Research context:**

Parser (`src/gobby/config/bootstrap.py`):
- `DatastoreMode = Literal["local", "remote"]`. `BootstrapConfig` carries
  `datastore_mode` (default `"local"`), `front_door: FrontDoorConfig`,
  `files_home`, and `hub_daemon_url`.
- `bootstrap_from_mapping` parses the mapping. `_parse_datastore_mode` rejects
  other mode values. `_parse_mode_owner_fields` enforces `files_home` for
  `local` and `hub_daemon_url` for `remote`.

Changes to the parser:
- Add `hub: bool = False`, parsed with the existing `_parse_yaml_bool(value, "hub")`.
- Reject `(remote, true)` in `_parse_mode_owner_fields` with
  `BootstrapConfigError("hub: true requires datastore_mode: local")`.
- Add `RunMode = Literal["standalone", "hub", "node"]` and a
  `BootstrapConfig.run_mode()` method that derives it from the pair.

Writers:
- `ensure_daemon_config` (`src/gobby/cli/install_setup.py`) sets
  `data["datastore_mode"] = data.get("datastore_mode") or "local"`. Add
  `data.setdefault("hub", False)`.
- `inject_local_files_home` (`src/gobby/config/bootstrap_io.py`) does
  `data.setdefault("datastore_mode", "local")`. Add `data.setdefault("hub", False)`.
- `_merge_owner_fields`: its `remote` branch drops `files_home` and now also
  drops `hub`, so converting a hub to a node cannot leave a rejected pair.
- The bundled template `src/gobby/install/shared/config/bootstrap.yaml`
  (`datastore_mode: "local"` at line 10) gains `hub: false` with a one-line
  comment naming the three modes.
- `update_bootstrap_yaml` merges through `_merge_owner_fields` and needs no
  change of its own.
- `expose_datastores` (`src/gobby/cli/datastores.py`) copies the bootstrap into
  `candidate` and sets `services_bind_address`. It also sets
  `candidate["hub"] = True` (Decision 6). Both failure paths call
  `_restore_compose_state(gobby_home, previous, was_running)`, which restores
  `previous`, so a failed exposure leaves the prior flag. The shared-stack
  guide's hub setup states that exposure makes the machine a hub.

Scope boundary (Decision 7): 2.1.4 proves the health contract on an
`HTTPServer` built with a `node` bootstrap. It does not start a node daemon
beside an active hub. Startup and `ActiveDaemonLease` are untouched here.

Health: `health_check` (`src/gobby/servers/routes/admin/_health.py`, inside
`create_health_router(server)`) builds `payload` with `status`,
`degraded_services`, `hook_runtime`, and `install_dir`. Add
`"mode": server.bootstrap_config.run_mode()`. `HTTPServer` defaults
`bootstrap_config` to `BootstrapConfig()`, so the result is `standalone` when no
bootstrap is given. `tests/servers/test_admin_health.py` already drives
`GET /api/health`.

Derived carrier: `crates/gcore/assets/config/runtime_config_contract.json` is
regenerated because a `.py` under `src/gobby/config/` changes. Rust reads
nothing new (Decision 2). `docs/guides/configuration.md` `### Bootstrap`
documents `hub`, the three modes, and the rejected pair.

Corpus coordination:
- If `tests/contracts/http/health_ok.json` exists when this leaf lands (the
  corpus slice's 3.1 landed first), re-record it with
  `GOBBY_RECORD_HTTP_CONTRACTS=1` and the isolated test hub. Commit the rewritten
  file, whose body gains `"mode": "standalone"`, unmasked.
- If it does not exist yet, 3.1 records the field when it lands, and this leaf
  touches no corpus file.

Consumers unchanged:
- `src/gobby/cli/install.py` — no-edit-reason: it calls `ensure_daemon_config` for its side effect and reads fields that keep their meaning; `hub` defaults to false.
- `src/gobby/cli/install_files_home.py` — no-edit-reason: it calls `ensure_daemon_config` and `inject_local_files_home` with unchanged signatures.
- `src/gobby/cli/installers/postgres.py` — no-edit-reason: it calls `ensure_daemon_config` and `inject_local_files_home` with unchanged signatures.
- `tests/cli/test_install_coverage.py` — no-edit-reason: it patches `inject_local_files_home` by name; the signature is unchanged.
- `tests/integration/sandbox/test_public_ghook_install.py` — no-edit-reason: it asserts install behavior that the extra `hub` line leaves intact; re-run as verification.
- `src/gobby/cli/runtime.py` — no-edit-reason: `CliRuntime._overlay_bootstrap` overlays `datastore_mode` only; `hub` is bootstrap-only (Decision 2).
- `src/gobby/config/app.py` — no-edit-reason: `DaemonConfig` gains no mode field; `hub` is bootstrap-only (Decision 2).

Verification planned, from the worktree root:
- `uv run python scripts/generate_runtime_config_contract.py` regenerates the
  carrier.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py tests/config/test_files_home.py tests/cli/test_install_setup.py tests/cli/test_datastores_expose.py tests/servers/test_admin_health.py tests/config/test_runtime_config_contract.py tests/config/test_config_authority_audit.py -v`.
- `uv run ruff check` and `uv run mypy` on the changed files.

**Acceptance:**

- 2.1.1 - The parser derives `standalone`, `hub`, and `node` from the pair and rejects `(remote, true)`. test: `tests/config/test_bootstrap.py::test_run_mode_from_datastore_mode_and_hub`.
- 2.1.2 - Fresh and injected bootstraps carry `hub: false`, and converting to `remote` drops `hub`. test: `tests/config/test_bootstrap.py::test_writers_emit_hub_flag`.
- 2.1.3 - `/api/health` reports the mode name. test: `tests/servers/test_admin_health.py::test_health_reports_run_mode`.
- 2.1.4 - A `node` bootstrap serves health with `mode: node`. test: `tests/servers/test_admin_health.py::test_node_bootstrap_reports_node_mode`.
- 2.1.5 - The config contract carrier is regenerated and the configuration guide documents the modes. file: `crates/gcore/assets/config/runtime_config_contract.json`.
- 2.1.6 - `gobby datastores expose` writes `hub: true`, and a failed exposure restores the prior flag. test: `tests/cli/test_datastores_expose.py::test_expose_promotes_hub_and_rollback_restores_flag`.

### 2.2 Node runners skip hub-only loops (S4.2) [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`
- `src/gobby/runner_lifecycle_subsystems.py::init_subsystems`
- `src/gobby/runner_lifecycle_agents.py::_reconcile_task_close_reviews`
- `src/gobby/runner_lifecycle_agents.py::_cleanup_terminal_agent_completion_subscribers`
- `src/gobby/runner_init/services.py::_schedule_scoped_tool_backfill`
- `src/gobby/runner_init/services.py::_request_memory_projection_repair`
- `tests/test_runner_lifecycle_periodic.py`
- `tests/runner_init/test_services_mcp_stack.py::*` — scope-reason: node-mode skip of the scoped tool backfill, and unchanged scheduling in the other modes
- `tests/ai/test_ai_runner_lease_lifecycle.py::*` — scope-reason: node-mode skip of projection repair after a lease re-ack and after a rebuild; SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: node-mode startup assertions for the gated phases; SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/agents/test_task_close_review_recovery.py::*` — scope-reason: node-mode skip of close-review reconciliation; the `_runner` fake gains `bootstrap_config`
- `tests/test_runner_approval_timeout.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_bin_freshness.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_maintenance_startup.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_resource_monitor.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_skill_maintenance.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_workflow_audit_maintenance.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_bm25_startup.py::*` — scope-reason: SimpleNamespace runner fakes gain `bootstrap_config=BootstrapConfig()`
- `tests/test_runner_lifecycle.py::*` — scope-reason: SimpleNamespace runner fakes that reach the gated functions gain `bootstrap_config=BootstrapConfig()`; terminal-completion recovery with local and foreign runs

**Granularity:** eight acceptance items, one outcome: a `node` runner starts
no shared-row maintenance, and every other mode behaves as today. Every gate
reads the same `run_mode()` and is proven by the same focused test run. Split
leaves would each close with a node that still sweeps or writes shared rows
through a launch site that is not yet gated, so no split leaf is independently
safe to ship. Items 2.2.1 to 2.2.8 stay as separate checks inside this one leaf.

Scope boundary (Decision 7): this leaf gates loops and launch sites on runner
functions. It does not make a node operational beside an active hub. Startup
and `ActiveDaemonLease` are untouched, and the operational node belongs to the
plan of record's 5.1 and 4.4.

**Research context:** The plan of record's 2.3 named `GobbyRunner._initialize_runtime_services` and
`runner_init/services.py`. Those mostly build services; their two shared-row
launch sites are listed after the startup phases below. The loops start in two
places:

- `start_periodic_tasks(runner, *, tracker, **loops)`
  (`src/gobby/runner_lifecycle_periodic.py`) creates each periodic task with
  `asyncio.create_task(..., name=...)` from the `_default_loops()` map.
- `init_subsystems` (`src/gobby/runner_lifecycle_subsystems.py`) runs the
  startup phases through `timed_startup_phase`.

Periodic tasks, by task name, for Decision 5.

Hub-only: skipped in `node` mode, 18 periodic tasks.
- `metrics-cleanup`
- `test-schema-sweep`
- `tool-result-cleanup`
- `workflow-audit-cleanup`
- `metrics-archive`
- `model-metadata-refresh`
- `provider-capability-refresh`: its snapshot is shared per provider. The
  agy, grok, and qwen collectors probe local CLIs, so the hub's CLI
  versions define the fleet catalog. That gap predates this plan and stays
  open for node capability design.
- `span-cleanup`
- `unmodeled-observation-cleanup`
- `loop-progress-cleanup`
- `memory-reconcile`
- `zombie-message-cleanup`
- `comms-message-cleanup`
- `skill-retention-purge`
- `chat-attachment-cleanup`
- `hook-receipt-retention`
- `approval-timeout-expiry`
- `metric-snapshot`: it writes this process's metrics into the shared
  `metric_snapshots` table and runs an unscoped retention delete
  (`src/gobby/runner_maintenance/telemetry_loops.py`,
  `src/gobby/storage/metric_snapshots.py`), so a node would sweep the hub's rows.

Machine-local: started in every mode.
- `resource-monitor`: local CPU and memory.
- `hook-inbox-drain`: the local inbox.
- `hook-quarantine-retention`: local files; it takes no database.
- `bin-freshness`: `~/.gobby/bin` updates.
- `expired-isolation-cleanup`: reaps this machine's worktrees and clones after
  local Git evidence rechecks.
- `tmux-window-repair`: the local tmux server.
- `generation-endpoint-health`: `GenerationEndpointHealthCoordinator` is
  in-memory, takes no database, and feeds only this machine's
  `/api/status`.

Hub-only startup phases in `init_subsystems`, skipped in `node` mode:
- `code_index_bm25` (`_repair_code_index_bm25`): it repairs shared PostgreSQL
  indexes. Skipping it leaves `code_index_bm25_ready` false, so the code-index
  tasks (`_start_code_index_tasks`), which start only when it is true, are
  skipped with it as one unit.
- `metrics_cleanup` (`_cleanup_metrics_on_startup`)
- `expansion_cleanup` (`_cleanup_stale_expansion_runs_on_startup`)
- `vector_store` (`_initialize_vector_store`): it can recreate the shared
  collection and rebuild it from hub memories.
- `core_services` (`_start_core_services`): it starts the communications
  manager, one poller per deployment, and `SessionLifecycleManager`
  (`src/gobby/sessions/lifecycle.py`). That manager's expire, transcript, and
  KG-queue loops query the whole shared database with no machine filter
  (`_expire_stale_sessions`, `TranscriptProcessingMixin`,
  `_process_pending_graph_memories`). D1 records the node transcript gap.
- `cron_scheduler` (`_start_cron_scheduler`): this also covers the memory dream,
  which runs as a cron job.
- `pipeline_recovery` (`_recover_pipelines`)
- `system_automation_start` (`_start_system_automation_loop`)

`agent_lifecycle_monitor` is machine-local and runs in every mode. The plan of
record's "agent launchers" reading was checked against the code:
- The monitor's check loop reads runs through
  `list_active_for_machine(require_machine_id())`
  (`src/gobby/agents/lifecycle_monitor.py::_get_active_terminal_runs`), and it
  works on local panes, prompts, process memory, and orphan reaping.
- `cleanup_stale_pending_runs` passes this machine's id.
- Its reconciliation callback (`_reconcile_agent_lifecycle_state`) rotates
  credentials through `principals_due_for_rotation(machine_id)` and reclassifies
  runs by `machine_id`. The parked non-task resume lists by `machine_id`.
- The one unscoped step is `_reconcile_task_close_reviews`
  (`src/gobby/runner_lifecycle_agents.py`): `TaskCloseReviewStore.list_reconcilable`
  selects every active or undelivered review. It is reached from both the
  monitor's callback and the `completion_subscriber_recovery` startup phase, so
  it returns 0 at its top in `node` mode and logs one INFO skip (Decision 5).

Terminal-completion recovery is scoped to this machine in every mode.
`completion_subscriber_recovery` also calls
`_cleanup_terminal_agent_completion_subscribers`
(`src/gobby/runner_lifecycle_agents.py`). It lists every completion id
(`CompletionSubscriberManager.list_completion_ids`), loads each run with no
machine check, wakes the subscribers, removes the acknowledged rows, and marks
close-review delivery. `wake` persists the message before routing it, and
`wake_result_is_delivered` accepts `ism_persisted`, so nothing downstream
filters a wrong-machine delivery.
- The function skips a run whose `machine_id` differs from
  `require_machine_id()`, before it reads the subscribers. `AgentRun.machine_id`
  is required (`src/gobby/storage/agents/_models.py`).
- The rule holds in every mode, since each machine runs this phase for its own
  runs. In `standalone` every run carries this machine's id, so its behavior is
  unchanged.
- A foreign run's subscriber rows stay until its owning machine recovers them.

Service-construction launch sites in `src/gobby/runner_init/services.py` that
write shared rows, gated in `node` mode at the top of each function with one
INFO skip line:
- `_schedule_scoped_tool_backfill` is called from `init_stateful_services`. It
  calls `schedule_scoped_embedding_backfill`, which starts
  `maybe_backfill_scoped_tool_embeddings` (`src/gobby/runner_init/mcp_stack.py`).
  That embeds `GLOBAL_PROJECT_ID` tools into the shared store and writes the
  shared config version marker. In `node` mode it schedules nothing.
- `_request_memory_projection_repair` runs
  `memory_manager.reconcile_stores(dry_run=False)` against the shared stores. The
  embedding lease calls it after a same-generation re-ack
  (`request_projection_repair`, `src/gobby/runner_init/embedding_lease.py`), and
  `_request_memory_services_rebuild` calls it after a healthy rebuild. The
  guard in the repair function covers both callers after startup. In `node`
  mode the rebuild still re-prepares the node's own `memory_services`, and only
  the shared reconcile is skipped.

Every other phase runs in every mode, including the terminal host, MCP
connections, agent run reconciliation, the WebSocket server, and the UI dev
server.

Mechanism:
- Define a module constant `HUB_ONLY_PERIODIC_TASKS` (a frozenset of the task
  names above) in `runner_lifecycle_periodic.py`, and `HUB_ONLY_STARTUP_PHASES`
  in `runner_lifecycle_subsystems.py`.
- When `runner.bootstrap_config.run_mode() == "node"`, skip each listed task or
  phase and log one INFO line per skip: `skipping hub-only <name> in node mode`.
- `standalone` and `hub` behave exactly as today.
- The mode is read once at the top of each function.

- The read is `runner.bootstrap_config.run_mode()`, with no fallback. A
  runner without a bootstrap is invalid, so the direct-call tests whose
  `SimpleNamespace` fakes carry no `bootstrap_config` gain an explicit
  `BootstrapConfig()` (Targets). `MagicMock` runners need no edit: their
  `run_mode()` is never `"node"`.

#21575 is closed as `duplicate` of the leaf created from this section.

Consumers unchanged:
- `src/gobby/runner_lifecycle.py` — no-edit-reason: it calls `start_periodic_tasks` and `init_subsystems` with unchanged signatures.
- `tests/test_runner_lifecycle_startup.py` — no-edit-reason: it monkeypatches `init_subsystems` out; verification only.
- `tests/test_runner_shutdown.py` — no-edit-reason: it patches `_init_subsystems` out; verification only.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: the monitor itself is unchanged; it only registers the callbacks; verification only.
- `src/gobby/runner_init/embedding_lease.py` — no-edit-reason: it invokes `request_projection_repair` unchanged; the guard lives in the callee.
- `src/gobby/runner_init/mcp_stack.py` — no-edit-reason: `schedule_scoped_embedding_backfill` is reached only through the gated `_schedule_scoped_tool_backfill` on the daemon path.
- `src/gobby/servers/routes/memory.py` — no-edit-reason: its reconcile endpoint runs only on an explicit operator request and is not maintenance.
- `tests/storage/definitions/test_revisions.py` — no-edit-reason: it monkeypatches `_schedule_scoped_tool_backfill` out; verification only.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_runner_lifecycle_periodic.py tests/test_runner_lifecycle_subsystems.py tests/agents/test_task_close_review_recovery.py tests/runner_init/test_services_mcp_stack.py tests/ai/test_ai_runner_lease_lifecycle.py tests/storage/definitions/test_revisions.py tests/test_runner_approval_timeout.py tests/test_runner_bin_freshness.py tests/test_runner_maintenance_startup.py tests/test_runner_resource_monitor.py tests/test_runner_skill_maintenance.py tests/test_runner_workflow_audit_maintenance.py tests/test_bm25_startup.py tests/test_runner_lifecycle.py tests/test_runner_lifecycle_startup.py tests/test_runner_shutdown.py tests/agents/test_lifecycle_monitor.py -v`.

**Acceptance:**

- 2.2.1 - A `node` runner starts exactly the machine-local periodic tasks and logs one skip per hub-only task. test: `tests/test_runner_lifecycle_periodic.py::test_node_mode_skips_hub_only_periodic_tasks`.
- 2.2.2 - A `node` runner skips exactly the hub-only startup phases, with the code-index tasks skipped alongside `code_index_bm25`, and still starts `agent_lifecycle_monitor`; each skip is logged. test: `tests/test_runner_lifecycle_subsystems.py::test_node_mode_skips_hub_only_phases`.
- 2.2.3 - `standalone` and `hub` start every periodic task as today. test: `tests/test_runner_lifecycle_periodic.py::test_standalone_and_hub_start_every_periodic_task`.
- 2.2.4 - `standalone` and `hub` run every startup phase as today. test: `tests/test_runner_lifecycle_subsystems.py::test_standalone_and_hub_run_every_phase`.
- 2.2.5 - In `node` mode `_reconcile_task_close_reviews` returns 0 without listing reviews, from both the monitor callback and startup recovery. test: `tests/agents/test_task_close_review_recovery.py::test_node_mode_skips_close_review_reconciliation`.
- 2.2.6 - In `node` mode `_schedule_scoped_tool_backfill` starts no backfill and writes no config marker, and `standalone` and `hub` schedule it as today. test: `tests/runner_init/test_services_mcp_stack.py::test_node_mode_skips_scoped_tool_backfill`.
- 2.2.7 - In `node` mode neither a lease re-ack nor a healthy rebuild calls `reconcile_stores`, and the rebuild still re-prepares `memory_services`; `hub` repairs as today. test: `tests/ai/test_ai_runner_lease_lifecycle.py::test_node_mode_skips_projection_repair`.
- 2.2.8 - Terminal-completion recovery wakes and removes subscribers only for runs owned by this machine; a foreign terminal run's subscribers get no wake, no row removal, and no close-review mark. test: `tests/test_runner_lifecycle.py::test_terminal_completion_recovery_skips_foreign_machine_runs`.

## D1 Node-scoped transcript processing (depends: 2.2)
`kind: deferred`

`core_services` is hub-only in 2.2 because `SessionLifecycleManager` also runs
shared-row loops: session expiry and purges, and the knowledge-graph queue.
Transcript processing is already machine-scoped:
`get_pending_transcript_sessions` (`src/gobby/storage/sessions/_transcript.py`)
selects only sessions whose `machine_id` is this machine's (641bc44427), and the
owning machine writes the derived rows and keeps the sidecar and archive on its
own disk. Transcript files live on the machine that ran the session, so the hub
cannot read a node's transcripts, and in `node` mode nothing processes them. The
gap is starting the machine-local part of the manager on a node, which does not
belong to run modes.

Acceptance item D1.1: a `node` runner processes the transcripts of sessions
whose `machine_id` is its own, and no other session's.

```yaml
deferral:
  task_ref: "TBD-node-transcript-processing"
  reason: "Needs a machine-scoped transcript query and an artifact-ownership decision outside the run-modes slice; created at expansion per the plan-coverage contract."
  owner: "program-director"
  original_acceptance_items:
    - D1.1
```

## V1: Plan Changelog
`kind: framing`

- 2026-09-29: Sliced out of the plan of record's P2 under #23098 (option (a),
  PD-confirmed), rooted at #21553, and refreshed against the landed P1 code.
  - Python owns the mode in Stage 1. The Rust parser gains `hub` with its first
    consumer.
  - `/api/health` gets `mode` from Python, which reaches clients through both
    proxy and native routing.
  - The `AppState` container moves to its first consumer (plan of record 4.4 or
    5.1). The node-health obligation becomes 2.1.4.
  - The loop guard's Targets move to `start_periodic_tasks` and
    `init_subsystems`, with pinned hub-only and machine-local sets.
  - The plan of record's 2.3 is renumbered 2.2.
- 2026-09-29: Enhancer pass (run 13930863); the PD accepted all six. E1 moves
  `metric-snapshot` to hub-only. E2 makes `gobby datastores expose` write
  `hub: true` (Decision 6, 2.1.6). E3 adds `code_index_bm25`, `vector_store`, and
  `core_services` to the hub-only phases, with D1 for node transcripts. E4 keeps
  the mode bootstrap-only. E5 moves the `SimpleNamespace`-fake tests to Targets.
  E6 spells out the contract regeneration and worktree-root validation. On the
  PD's required check, `agent_lifecycle_monitor` proved machine-scoped, so it
  runs in every mode, and its one unscoped step, close-review reconciliation, is
  gated inside `_reconcile_task_close_reviews` (2.2.5).
- 2026-09-29: Adversary review (gobby#14579) at 02f0274, blocking findings
  RM-01 to RM-04 accepted. RM-01: the scoped tool backfill and memory projection
  repair are gated in `node` mode inside their own functions (2.2.6, 2.2.7).
  RM-02: terminal-completion recovery skips runs owned by another machine
  (2.2.8). RM-03: Decision 7 records the preparatory scope and names the plan
  of record's 5.1 and 4.4 as owners of the operational node. RM-04: each leaf
  runs its own focused commands, and V2 runs once both leaves land.
- 2026-09-29: Adversary recheck at 9c519b3. RM-01 to RM-04 resolved. 2.2 gains
  a Granularity decision that keeps its eight items in one leaf, and 2.1 and 2.2
  each carry the Decision 7 scope boundary in their Research context.
- 2026-09-29: Consensus with the Adversary (gobby#14579) at 3e29899. RM-01 to
  RM-04, the Granularity decision, and the scope boundary are resolved, with no
  open design objection. The Adversary derives and applies M1 from these bytes.
- 2026-10-01: R6's review found `generation-endpoint-health` machine-local:
  its coordinator is in-memory, takes no database, and feeds only this
  machine's `/api/status`. On the PD's ruling (gobby#14972) it moves to the
  machine-local list, leaving 18 hub-only periodic tasks, and L7's
  correction 32ad98fb04 runs it in every mode. `provider-capability-refresh`
  stays hub-only, with a note on the local CLI probes. 2.2.1 and M1 are
  unchanged.
- 2026-10-04: D1's premise corrected while specifying #23112. The pending
  transcript query already filters by `machine_id`
  (`src/gobby/storage/sessions/_transcript.py:25-51`, 641bc44427), and artifact
  ownership is settled in code. The gap is that a node starts no machine-local
  lifecycle loop. D1.1 and M1 are unchanged; #23112 carries the atomic spec.

## V2: Verification
`kind: verification`

After each leaf, run that leaf's own "Verification planned" commands. Run the
combined commands below once both leaves have landed, and again before the PD
lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py tests/config/test_files_home.py tests/cli/test_install_setup.py tests/cli/test_datastores_expose.py tests/servers/test_admin_health.py tests/config/test_runtime_config_contract.py tests/config/test_config_authority_audit.py tests/test_runner_lifecycle_periodic.py tests/test_runner_lifecycle_subsystems.py tests/agents/test_task_close_review_recovery.py tests/runner_init/test_services_mcp_stack.py tests/ai/test_ai_runner_lease_lifecycle.py tests/test_runner_lifecycle.py -v
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/gdaemon-run-modes.md -p .
```

Run these from the worktree root.

Do not run the full pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: '`hub` flag, writers, and the mode on `/api/health`'
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: The parser derives `standalone`, `hub`, and `node`
    from the pair and rejects `(remote, true)`. test: `tests/config/test_bootstrap.py::test_run_mode_from_datastore_mode_and_hub`.

    2.1.2: Fresh and injected bootstraps carry `hub: false`, and converting to `remote`
    drops `hub`. test: `tests/config/test_bootstrap.py::test_writers_emit_hub_flag`.

    2.1.3: `/api/health` reports the mode name. test: `tests/servers/test_admin_health.py::test_health_reports_run_mode`.

    2.1.4: A `node` bootstrap serves health with `mode: node`. test: `tests/servers/test_admin_health.py::test_node_bootstrap_reports_node_mode`.

    2.1.5: The config contract carrier is regenerated and the configuration guide
    documents the modes. file: `crates/gcore/assets/config/runtime_config_contract.json`.

    2.1.6: `gobby datastores expose` writes `hub: true`, and a failed exposure restores
    the prior flag. test: `tests/cli/test_datastores_expose.py::test_expose_promotes_hub_and_rollback_restores_flag`.'
  labels:
  - covers:gdaemon-run-modes:2.1:2.1.1
  - covers:gdaemon-run-modes:2.1:2.1.2
  - covers:gdaemon-run-modes:2.1:2.1.3
  - covers:gdaemon-run-modes:2.1:2.1.4
  - covers:gdaemon-run-modes:2.1:2.1.5
  - covers:gdaemon-run-modes:2.1:2.1.6
  tdd: true
  source_section: '2.1'
  assigned_agent: backend-developer
- title: Node runners skip hub-only loops (S4.2)
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: A `node` runner starts exactly the machine-local periodic
    tasks and logs one skip per hub-only task. test: `tests/test_runner_lifecycle_periodic.py::test_node_mode_skips_hub_only_periodic_tasks`.

    2.2.2: A `node` runner skips exactly the hub-only startup phases, with the code-index
    tasks skipped alongside `code_index_bm25`, and still starts `agent_lifecycle_monitor`;
    each skip is logged. test: `tests/test_runner_lifecycle_subsystems.py::test_node_mode_skips_hub_only_phases`.

    2.2.3: `standalone` and `hub` start every periodic task as today. test: `tests/test_runner_lifecycle_periodic.py::test_standalone_and_hub_start_every_periodic_task`.

    2.2.4: `standalone` and `hub` run every startup phase as today. test: `tests/test_runner_lifecycle_subsystems.py::test_standalone_and_hub_run_every_phase`.

    2.2.5: In `node` mode `_reconcile_task_close_reviews` returns 0 without listing
    reviews, from both the monitor callback and startup recovery. test: `tests/agents/test_task_close_review_recovery.py::test_node_mode_skips_close_review_reconciliation`.

    2.2.6: In `node` mode `_schedule_scoped_tool_backfill` starts no backfill and
    writes no config marker, and `standalone` and `hub` schedule it as today. test:
    `tests/runner_init/test_services_mcp_stack.py::test_node_mode_skips_scoped_tool_backfill`.

    2.2.7: In `node` mode neither a lease re-ack nor a healthy rebuild calls `reconcile_stores`,
    and the rebuild still re-prepares `memory_services`; `hub` repairs as today. test:
    `tests/ai/test_ai_runner_lease_lifecycle.py::test_node_mode_skips_projection_repair`.

    2.2.8: Terminal-completion recovery wakes and removes subscribers only for runs
    owned by this machine; a foreign terminal run''s subscribers get no wake, no row
    removal, and no close-review mark. test: `tests/test_runner_lifecycle.py::test_terminal_completion_recovery_skips_foreign_machine_runs`.'
  labels:
  - covers:gdaemon-run-modes:2.2:2.2.1
  - covers:gdaemon-run-modes:2.2:2.2.2
  - covers:gdaemon-run-modes:2.2:2.2.3
  - covers:gdaemon-run-modes:2.2:2.2.4
  - covers:gdaemon-run-modes:2.2:2.2.5
  - covers:gdaemon-run-modes:2.2:2.2.6
  - covers:gdaemon-run-modes:2.2:2.2.7
  - covers:gdaemon-run-modes:2.2:2.2.8
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
```

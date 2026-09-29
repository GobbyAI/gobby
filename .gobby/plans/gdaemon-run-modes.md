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

The plan of record's 4.4 (the first hub-node pair test) needs that node.

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
   retains, sweeps, refreshes, or schedules shared hub rows. A node's runner talks
   to the hub database, so running that loop there would duplicate the hub's
   work. Loops that act only on the local machine keep running in every mode:
   processes, tmux, installed binaries, local worktrees, the local hook inbox and
   quarantine, and this process's metrics. 2.2 pins the exact sets.

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
- `src/gobby/install/shared/config/bootstrap.yaml::*` — scope-reason: the bundled template gains the `hub: false` line and its comment
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: `health_check` inside `create_health_router` adds the `mode` field
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `docs/guides/configuration.md`
- `tests/config/test_bootstrap.py::*` — scope-reason: mode derivation, the `(remote, true)` rejection, and writer defaults
- `tests/config/test_files_home.py::*` — scope-reason: consumer of `inject_local_files_home` and `ensure_daemon_config`; written files gain the `hub` line
- `tests/cli/test_install_setup.py::*` — scope-reason: consumer of `ensure_daemon_config`; written files gain the `hub` line
- `tests/servers/test_admin_health.py::*` — scope-reason: health reports the mode for each bootstrap
- `tests/contracts/http/health_ok.json`

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

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py tests/config/test_files_home.py tests/cli/test_install_setup.py tests/servers/test_admin_health.py -v`.
Also the runtime config contract check that the 1.1 leaf used, and
`uv run ruff check` and `uv run mypy` on the changed files.

**Acceptance:**

- 2.1.1 - The parser derives `standalone`, `hub`, and `node` from the pair and rejects `(remote, true)`. test: `tests/config/test_bootstrap.py::test_run_mode_from_datastore_mode_and_hub`.
- 2.1.2 - Fresh and injected bootstraps carry `hub: false`, and converting to `remote` drops `hub`. test: `tests/config/test_bootstrap.py::test_writers_emit_hub_flag`.
- 2.1.3 - `/api/health` reports the mode name. test: `tests/servers/test_admin_health.py::test_health_reports_run_mode`.
- 2.1.4 - A `node` bootstrap serves health with `mode: node`. test: `tests/servers/test_admin_health.py::test_node_bootstrap_reports_node_mode`.
- 2.1.5 - The config contract carrier is regenerated and the configuration guide documents the modes. file: `crates/gcore/assets/config/runtime_config_contract.json`.

### 2.2 Node runners skip hub-only loops (S4.2) [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`
- `src/gobby/runner_lifecycle_subsystems.py::init_subsystems`
- `tests/test_runner_lifecycle_periodic.py`
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: node-mode startup assertions for the gated subsystem phases

**Research context:** The plan of record's 2.3 named `GobbyRunner._initialize_runtime_services` and
`runner_init/services.py`. Those build services and start no loop. The loops start
in two places:

- `start_periodic_tasks(runner, *, tracker, **loops)`
  (`src/gobby/runner_lifecycle_periodic.py`) creates each periodic task with
  `asyncio.create_task(..., name=...)` from the `_default_loops()` map.
- `init_subsystems` (`src/gobby/runner_lifecycle_subsystems.py`) runs the
  startup phases through `timed_startup_phase`.

Periodic tasks, by task name, for Decision 5.

Hub-only: skipped in `node` mode.
- `metrics-cleanup`
- `test-schema-sweep`
- `tool-result-cleanup`
- `workflow-audit-cleanup`
- `metrics-archive`
- `model-metadata-refresh`
- `provider-capability-refresh`
- `generation-endpoint-health`
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

Machine-local: started in every mode.
- `metric-snapshot`: this process's OTel metrics.
- `resource-monitor`: local CPU and memory.
- `hook-inbox-drain`: the local inbox.
- `hook-quarantine-retention`: local files; it takes no database.
- `bin-freshness`: `~/.gobby/bin` updates.
- `expired-isolation-cleanup`: reaps this machine's worktrees and clones after
  local Git evidence rechecks.
- `tmux-window-repair`: the local tmux server.

Hub-only startup phases in `init_subsystems`, skipped in `node` mode:
- `metrics_cleanup` (`_cleanup_metrics_on_startup`)
- `expansion_cleanup` (`_cleanup_stale_expansion_runs_on_startup`)
- `agent_lifecycle_monitor` (`_start_agent_lifecycle_monitor`): the plan of
  record's "agent launchers".
- `cron_scheduler` (`_start_cron_scheduler`): this also covers the memory dream,
  which runs as a cron job.
- the code-index tasks (`_start_code_index_tasks`)
- `pipeline_recovery` (`_recover_pipelines`)
- `system_automation_start` (`_start_system_automation_loop`)

Every other phase runs in every mode, including the terminal host, MCP
connections, the WebSocket server, and the UI dev server.

Mechanism:
- Define a module constant `HUB_ONLY_PERIODIC_TASKS` (a frozenset of the task
  names above) in `runner_lifecycle_periodic.py`, and `HUB_ONLY_STARTUP_PHASES`
  in `runner_lifecycle_subsystems.py`.
- When `runner.bootstrap_config.run_mode() == "node"`, skip each listed task or
  phase and log one INFO line per skip: `skipping hub-only <name> in node mode`.
- `standalone` and `hub` behave exactly as today.
- The mode is read once at the top of each function.

Existing callers pass mock runners, so the mode check must not require a real
bootstrap. A mock's `run_mode()` returns a mock that is never `"node"`, so every
existing test keeps starting every loop. #21575 is closed as `duplicate` of the
leaf created from this section.

Consumers unchanged:
- `src/gobby/runner_lifecycle.py` — no-edit-reason: it calls `start_periodic_tasks` and `init_subsystems` with unchanged signatures.
- `tests/test_runner_approval_timeout.py` — no-edit-reason: it calls `start_periodic_tasks` with a mock runner whose mode is never `node`; verification only.
- `tests/test_runner_bin_freshness.py` — no-edit-reason: same mock-runner call; verification only.
- `tests/test_runner_lifecycle.py` — no-edit-reason: it drives `start_periodic_tasks` and `init_subsystems` through mock runners; verification only.
- `tests/test_runner_maintenance_startup.py` — no-edit-reason: same mock-runner call; verification only.
- `tests/test_runner_resource_monitor.py` — no-edit-reason: same mock-runner call; verification only.
- `tests/test_runner_skill_maintenance.py` — no-edit-reason: same mock-runner call; verification only.
- `tests/test_runner_workflow_audit_maintenance.py` — no-edit-reason: same mock-runner call; verification only.
- `tests/test_bm25_startup.py` — no-edit-reason: it drives `init_subsystems` with a mock runner; verification only.
- `tests/test_runner_lifecycle_startup.py` — no-edit-reason: it drives `init_subsystems` with a mock runner; verification only.
- `tests/test_runner_shutdown.py` — no-edit-reason: it drives `init_subsystems` with a mock runner; verification only.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_runner_lifecycle_periodic.py tests/test_runner_lifecycle_subsystems.py tests/test_runner_lifecycle.py tests/test_runner_maintenance_startup.py -v`.

**Acceptance:**

- 2.2.1 - A `node` runner starts exactly the machine-local periodic tasks and logs one skip per hub-only task. test: `tests/test_runner_lifecycle_periodic.py::test_node_mode_skips_hub_only_periodic_tasks`.
- 2.2.2 - A `node` runner skips exactly the hub-only startup phases and logs each skip. test: `tests/test_runner_lifecycle_subsystems.py::test_node_mode_skips_hub_only_phases`.
- 2.2.3 - `standalone` and `hub` start every periodic task and phase as today. test: `tests/test_runner_lifecycle_periodic.py::test_standalone_and_hub_start_every_periodic_task`.

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

## V2: Verification
`kind: verification`

After each leaf, and before the PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py tests/config/test_files_home.py tests/cli/test_install_setup.py tests/servers/test_admin_health.py tests/test_runner_lifecycle_periodic.py tests/test_runner_lifecycle_subsystems.py tests/test_runner_lifecycle.py -v
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/gdaemon-run-modes.md -p /Users/josh/Projects/gobby
```

Do not run the full pytest suite.

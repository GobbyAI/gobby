Plan artifact: `.gobby/plans/placed-agent-launch.md`

# Placed agent launch: spawn_agent into a named gclient tab or split

**Plan ID:** placed-agent-launch

## Overview
`kind: framing`

Task #22904 sits under epic #22691 (tier 3: runbooks as ordinary pipelines, #22895).

A runbook is an ordinary pipeline whose `mcp` steps call `gobby-agents:spawn_agent`.
The only primitive missing is launching an agent into a chosen gclient tab or split
and learning where it landed. This plan adds that primitive to `spawn_agent` itself, so
that every existing guard, lease, isolation path, sandbox wrap and caller-project
authority still applies. That holds whether the caller is a session, a pipeline `mcp`
step, the CLI or cron.

The pane is reserved before any side effect of the spawn. It is bound to the agent's
terminal while that terminal is still `pending`, which is before provider exec. A
placed agent therefore never exists unplaced, and a refused placement spawns nothing.

## Constraints
`kind: framing`

- Runbooks are ordinary pipelines (#22895). There is no runbook schema, service,
  deployment ledger, migration or runbook-specific API. Generic parallel fan-out is
  not required.
- Placement is a `spawn_agent` input. A pipeline reaches it through an existing `mcp`
  step. Guards, leases, isolation and caller-project authority are preserved.
- Validation happens before side effects: a refused placement spawns nothing.
- Sandbox and role authority follow the closed #22899 decisions. Profiles are
  definition-only, and `spawn_agent` gains no sandbox input.
- This planning task contains no code and no live spawning.
- Josh's acceptance (via PD #14610, 2026-09-26; scope clarified the same day):
  - Every developer seat launched through a pipeline runs inside managed SRT before
    provider exec.
  - Placement preserves canonical SRT wrapping, policy, grants, leases, isolation and
    resume.
  - Any invalid input or wrap failure is refused before any unplaced or unsandboxed
    agent side effect.
  - The isolated acceptance tests include a two-seat tab+split ordinary pipeline and a
    live-seat refusal.
  - Current hand-launched seats are out of scope; they need no relaunch.
- Josh's rulings of record:
  - Spawned agents already run in gclient panes, and plans keep the as-is state
    separate from the to-be design (memory 556ec801).
  - `spawn_agent` creates the gclient pane itself (memory 7faa183d, constraint 1).
    This plan delivers that constraint and nothing else from the parked meeseeks
    lifecycle.
- Monolith ceiling: these targets are already at or above 850 lines:
  - `_implementation.py`: 964
  - `spawn_executor.py`: 962
  - `workspace_ops.py`: 973
  - `storage/workspaces.py`: 973

  New logic lands in new modules. Each deliverable that touches one of these files
  names its move.

## Decision Record
`kind: framing`

1. **Input shape.** Add `spawn_agent(placement=...)`, where `placement` is either of:

   ```text
   {"tab": {"workspace": <workspace ref>, "title": <seat label>}}
   {"split": {"pane": <pane ref>, "axis": "right"|"down", "title": <seat label>}}
   ```

   - `title` is required and is the seat label. It becomes the tab title for `tab`, or
     the pane label for `split`.
   - Refs use the existing workspace reference grammar (`node:workspace:tab:pane`,
     `workspace_contract.py`).
   - Rejected: a separate `seat` field or column. The label already exists on
     `workspace_tabs.title` and on the pane label, so a new field would need a schema
     change and adds no power.
2. **Validate, then reserve, then spawn.** Placement preflight is side-effect free and
   runs before worktree or clone isolation is created, which happens early today at
   `spawn_agent_impl`. The preflight covers:
   - ref resolution;
   - actor scope (`ActorScope.admits` for the caller's project);
   - node match;
   - the live-seat check;
   - the SRT requirement.

   Only after every existing guard passes does the pane reservation insert a row. The
   existing guards are: parent session, `can_spawn` depth, task context,
   `active_task_response_if_blocked`, `TaskSpawnLease`, and `reserve_agent_slot`. The
   reserved row is an in-flight pane (terminal NULL), or an in-flight tab with its
   first pane.

   Rejected: preflighting after isolation. That leaves worktrees behind for refused
   placements.
3. **Bind before exec.** The reserved pane is bound with
   `set_pane_terminal(owns_terminal=True)` to the terminal row that
   `TerminalManager.create_pending` creates in the spawn executor. The as-is order,
   VERIFIED by R2 #14640, is:
   1. `_prepare_provider_sandbox`;
   2. `prepare_sandbox_launch` (SRT policy plus a real `--preflight`);
   3. `wrap_provider_command`;
   4. `create_pending`;
   5. `reserve_observer` / `prepare_spawn`;
   6. provider exec.

   Binding right after `create_pending` therefore follows a successful SRT wrap and
   precedes exec. A wrap failure never creates a terminal row, so only the
   reservation needs releasing.
   - Rejected: adopting after launch via `tab_create`/`pane_split(terminal_id=...)`.
     `WorkspaceOps._adoptable` requires `state == "live"`, which means the agent would
     run unplaced first, and gclient's `created` handler would already have opened it
     unplaced. That violates Josh's acceptance.
4. **Synchronous result for placed launches.** When `placement` is set, `spawn_agent`
   awaits the spawn phase (`_execute_spawn_phase`) inline instead of scheduling it in
   the background.
   - Success returns the full `build_spawn_response` payload plus `workspace`,
     `tab_ref` and `pane_ref`.
   - A wrap, provider or bind failure returns `success: false` with a typed
     `placement_error` or the existing spawn error.

   A pipeline `mcp` step therefore fails deterministically, and a later split step can
   use `${{steps.<id>.output.pane_ref}}`. Unplaced spawns keep today's background
   behaviour.
5. **Compensation.** Every failure after reservation runs the existing
   `cleanup_failed_spawn` path and then removes the reserved pane or tab (the same
   removal as `WorkspaceOps._roll_back`). The failures covered are: guard refusal after
   reservation, provider or wrap failure, bind conflict, and cancellation.
   - A pane whose terminal was bound with `owns_terminal=True` kills that terminal on
     removal, so no orphan shell or unplaced agent survives.
   - Placed launches default `cleanup_isolation_on_failure` to true for isolation that
     this call created. As-is, the default is false (`spawn_agent_impl`,
     `_failure_cleanup.py`), which leaves a worktree behind after a refused seat.
     Reused worktrees and clones (`worktree_id` / `clone_id`) are never removed.
6. **Managed SRT is mandatory for placed launches.** Preflight refuses with
   `placement_error: "sandbox_required"` unless the effective sandbox config is
   `enabled` with `backend == "srt"`. The config checked is the daemon-owned
   `agent_sandbox_config` after `apply_write_grant`, or the managed runtime profile's
   config. Preflight also runs the side-effect-free `verify_srt_installation`.
   - The full SRT policy and `--preflight` need the isolation cwd and the run_id, so
     they stay where they are. VERIFIED by R2: `_prepare_provider_sandbox` catches
     `OSError`/`ValueError`/`SrtRuntimeError`, fails the run, and returns "Sandbox
     startup failed closed" before `create_pending`. Placed launches reuse that path
     unchanged, and its failure releases the reservation.
   - Unplaced spawns keep today's config-driven behaviour. Hand-launched seats are out
     of scope.
7. **Live-seat refusal.** Preflight refuses with `placement_error: "seat_live"` when
   the target workspace already holds a tab (for `tab`) or a pane (for `split`) whose
   title or label equals `title` and whose bound terminal is `pending` or `live`.
   Re-running a runbook pipeline therefore cannot double-launch a seat. A seat whose
   terminal has ended can be relaunched.
   - An in-flight reservation with the same title also counts as live.
   - Preflight's seat check is repeated inside `reserve` under a per-workspace
     `asyncio.Lock` held across check and insert, so two concurrent launches of one
     seat cannot both pass. The daemon is a single process, and the in-flight pane set
     is per process too (VERIFIED by R2).
   - As-is, the only spawn idempotency key is `task_id` (`_spawn_guards.py`). Taskless
     seats had no guard until this check.
8. **Parent identity.** A placed spawn from a pipeline is parented to the pipeline's
   child session. That session is created by `PipelineExecutor` with
   `source="pipeline"`, the run's `project_id` and `parent_session_id=caller`, and
   `_inject_agent_parent_session_argument` fills in `parent_session_id` for `mcp` steps.
   The pipeline caller depends on how the run started:

   | Entry point | Caller |
   | --- | --- |
   | CLI or HTTP | the system session |
   | MCP `run_pipeline` | the calling session |
   | cron | the cron session |

   - The project is resolved from the parent session by
     `_resolve_spawn_project_context`. The workspace actor is the parent session id.
     VERIFIED by R2: the CLI takes the project from the cwd's `.gobby/project.json`;
     cron takes it from `job.project_id`.
   - No new identity mechanism is added.
   - Fail-closed gap: when child-session registration fails, `PipelineExecutor` falls
     back to the caller session. For CLI runs that is the system session, and the
     project may then resolve from ambient context. A placed spawn refuses with
     `placement_error: "parent_unresolved"` when its parent is the system session
     itself, or when the project did not come from an explicit `project_path` or the
     parent session.
9. **Seat spawn blocks are policy, not code, today.** No bundled rule blocks
   `spawn_agent` by seat. The blocks live in role files, and #22899 /
   `agent-definition-profiles.md` plan their enforcement.
   - Decision: running a pipeline is not a bypass. A seat forbidden from `spawn_agent`
     is equally forbidden from starting a spawning pipeline through `run_pipeline`.
   - When enforcement lands (outside this plan), it keys on the originating
     non-pipeline session in the pipeline child's parent chain. It must be in-tool or
     at `run_pipeline`, because pipeline `mcp` steps call the proxy with
     `enforce_workflow=False` and skip the rule engine (VERIFIED by R2:
     `workflows/pipeline/handlers.py`, `tool_execution.py`).
   - Runbook pipelines are started by the operator (CLI) or by the PD.
10. **gclient reconciliation.** gclient must not show a placed agent twice or unplaced.
    - A `created` event for a terminal that a known pane already holds opens nothing
      new. This is today's behaviour in `apply_event`.
    - When a `pane.added` / `tab.created` event binds a terminal that gclient opened
      unplaced, gclient moves the terminal into that pane and drops the unplaced
      surface.

    This covers any ordering of the workspace stream and the lifecycle stream, with no
    change to daemon event payloads.
11. **Deferred elsewhere, not here:**
    - closing a pane when its agent ends (`end_agent_run` closes pane and terminal,
      memory 7faa183d, constraint 4);
    - the idle TTL and lifecycle declarations;
    - lane queues;
    - `dispatch_batch` retirement with `gobby build`.

    All of these belong to #22691 siblings.

## Evidence (as-is, VERIFIED unless marked)
`kind: framing`

- **spawn_agent MCP signature:** `create_spawn_agent_registry` in
  `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`. Parameters: prompt, agent,
  task_id, allow_closed_task, isolation, branch_name, base_branch, clone_id,
  worktree_id, cleanup_isolation_on_failure, workflow, provider, model,
  reasoning_effort, reasoning_required, timeout, parent_session_id, project_path,
  notify_parent_on_completion, terminal_backend, droid_mode, extra_write_paths,
  write_paths_reason, reserved_run_id. There is no placement or sandbox input.
- **Response:** the immediate MCP reply is `status: "starting"` with run_id,
  child_session_id and isolation fields, returned before any terminal exists.
  `build_spawn_response` (`_response.py`) adds pid, terminal_id and backend in the
  background finalize step. No tab_ref or pane_ref exists anywhere.
- **Guard order in `spawn_agent_impl`:**
  1. parent session required, then `can_spawn`;
  2. `resolve_spawn_task_context`;
  3. the sandbox config is resolved;
  4. isolation is created;
  5. `active_task_response_if_blocked`;
  6. `TaskSpawnLease.acquire`;
  7. `reserve_agent_slot` (`_spawn_guards.py`, per-project lock);
  8. the background `_run_spawn_phase`.

  Failures route to `_spawn_failure`, which calls `cleanup_failed_spawn`
  (`_failure_cleanup.py`) with these steps: record error, kill terminal, terminalize
  the run, clean up isolation, delete the child session. `reserved_run_id` is only for
  the close reviewer; it is not an idempotency key. Task retries dedupe through
  `task_spawn_lease`.
- **Terminal path:** `execute_spawn` calls `_runtime_spawn` (`spawn_executor.py`), which
  calls `TerminalManager.create_pending`, then native `reserve_observer`, then
  `prepare_spawn`, then `_promote_prepared`. The SRT wrap is applied in
  `spawn_executor_providers.py` and `spawn_executor_support.py` through
  `wrap_provider_command`.
- **Sandbox defaults:** `daemon_owned_sandbox_config` in `agents/sandbox.py` defaults to
  `enabled=True`, `backend="srt"`, `allow_network=False`, but the `agent_sandbox`
  config can set `enabled: false` or `backend: provider-native`. `spawn_agent_impl`
  records `sandbox_enabled=False` at session creation. `update_sandbox_enabled`
  (`agents/session.py`) exists for the later update (INFERRED: set after the wrap).
- **Workspace substrate:** `WorkspaceOps.tab_create` and `pane_split`
  (`terminals/workspace_ops.py`) do the following in order:
  1. `_pane_source` checks scope before insert;
  2. `mark_spawn_in_flight`;
  3. insert the row;
  4. `_fill` binds or spawns;
  5. `clear_spawn_in_flight`;
  6. emit `tab.created` / `pane.added`.

  `_fill` rolls back on failure. `_adoptable` requires a live terminal on the
  workspace's node. `WorkspaceManager.set_pane_terminal` (`storage/workspaces.py`) has
  no state check and raises `UniqueViolation` when another pane holds the terminal.
  Tab titles and pane labels exist (`create_tab(title=)`, `rename_pane`).
- **gclient:**
  - `apply_event` (`crates/gclient/src/app/apply.rs`) opens a `created` terminal
    unplaced only when `pane_for_terminal` is `None`.
  - The live path `apply_live_event` (`crates/gclient/src/app/live.rs`) calls
    `ensure_live_pane` on `created`.
  - User spawns are placed client-side by `placement_op`
    (`live_loop/workspace_actions.rs`).
  - The agent `created` broadcast comes from `broadcast_agent_event`
    (`runner_broadcasting.py`), after the terminal exists (INFERRED: after
    promotion).
- **Pipeline identity:** `PipelineExecutor` registers the child session
  (`source="pipeline"`, `project_id`, `parent_session_id=caller`, `agent_depth=0`) and
  falls back to the caller session if creation fails. `_inject_agent_parent_session_argument`
  (`mcp_proxy/services/tool_execution.py`) fills in `parent_session_id` for
  `spawn_agent` calls.
- **Seat blocks:** the only rules naming `spawn_agent` are memory surfacing,
  require-restraint, block-direct-provider-launch and no-agent-spawn-for-merge. None of
  them is a seat block.
- **Prior designs:**
  - `deploy-runbook.md` placement `{tab:{title}} | {split:{slot,axis}}`;
  - `completed/runbooks.md` §1.5 (reserve the pane before the process, reply with
    pane_ref/tab_ref);
  - `completed/gclient-workspaces.md` decision #10 excluded spawn-time placement.

  Josh's ruling of 2026-09-26 (memory 7faa183d) supersedes that exclusion.

## P1: Daemon placement
`kind: framing`

Daemon-side reservation primitives (1.1) and their use by `spawn_agent` (1.2).

### 1.1 Agent pane reservation primitives [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/workspace_agent_panes.py`
- `tests/terminals/test_workspace_agent_panes.py`

Add a new module, `workspace_agent_panes.py`. It owns the placement lifecycle for
agent panes, so `workspace_ops.py` (973 lines) and `storage/workspaces.py` (973 lines)
stay unchanged. It is constructed from the same `WorkspaceManager`, `TerminalManager`
and event-publish callback that `WorkspaceOps` receives. It reuses `_pane_of`
(`workspace_contract.py`), `mint_pane_id`, `mark_spawn_in_flight`,
`clear_spawn_in_flight`, `create_tab`, `add_pane`, `set_pane_terminal` and
`remove_pane`.

New API (all names are new):
- `AgentPlacement`: a frozen dataclass parsed from the `placement` input. It has two
  variants:
  - `tab`: workspace ref and title;
  - `split`: pane ref, axis in {`right`, `down`}, and title.

  A missing or empty title, an unknown axis, an unknown key or both variants at once
  each raise `AgentPlacementError("invalid_placement")`.
- `AgentPaneReserver.preflight(actor, project_id, worktree_id, placement)`: read-only.
  It does the following:
  - resolves the ref through the workspace resolver (`not_found` / `invalid_ref`);
  - checks that the actor scope admits `project_id` (`forbidden`);
  - requires the workspace's machine to be this node (`invalid_op`);
  - for `split`, requires the target pane's tab project to equal `project_id`
    (`forbidden`);
  - applies the live-seat check: the same title on a tab (for `tab`) or pane label
    (for `split`) in that workspace whose terminal state is in `_ACTIVE_STATES`
    (`pending`/`live`) gives `seat_live`.

  It returns a `ResolvedPlacement`.
- `reserve(resolved)`: under a per-workspace `asyncio.Lock` held across check and
  insert, repeats the live-seat check (counting in-flight reservations with the same
  title as live), then marks the pane in flight and inserts it. It uses `create_tab`
  with the title for `tab`, or `add_pane` beside the pane plus the label for `split`.
  It returns `ReservedPane(pane_id, tab_id, workspace_id, pane_ref, tab_ref)`. Nothing
  is emitted yet.
- `bind(reserved, terminal_id)`: calls `set_pane_terminal(owns_terminal=True)`, clears
  the in-flight mark, and emits `tab.created` or `pane.added` with the bound pane. A
  `UniqueViolation` or a missing row raises `AgentPlacementError("busy"/"not_found")`.
- `release(reserved)`: clears the in-flight mark and removes the pane (and a tab it
  emptied), publishing the removal. It is idempotent and tolerates a row that is
  already gone. It does not kill the terminal: the spawn cleanup owns that.

**Research context:**
- The reserve-then-fill order mirrors `WorkspaceOps.tab_create` and
  `WorkspaceOps.pane_split` (the in-flight mark protects the unbound pane from sweeps
  and from workspace close). `_fill` / `_roll_back` show the rollback and publish
  semantics to copy.
- `_adoptable` is deliberately not reused, because it requires a `live` terminal
  (decision 3).
- Seat label: `workspace_tabs.title` via `create_tab(title=)`, and pane labels via
  `rename_pane`. Both are truncated by `truncate_title`, so compare the truncated
  value.
- Errors: reuse the `WorkspaceOpError` codes (`invalid_ref`, `not_found`, `forbidden`,
  `invalid_op`, `busy`) plus the new `invalid_placement`, `seat_live` and
  `sandbox_required` (the last is raised by 1.2) under a new `AgentPlacementError`.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/terminals/test_workspace_agent_panes.py -v`, plus ruff and mypy on the new
  module.

**Acceptance:**

- 1.1.1 - Invalid placement shapes (no title, bad axis, both variants, unknown key) raise `invalid_placement` without touching storage. test: `tests/terminals/test_workspace_agent_panes.py::test_invalid_placement_shapes_refused`.
- 1.1.2 - Preflight refuses an unknown ref, an out-of-scope project and a foreign node, and inserts no row. test: `tests/terminals/test_workspace_agent_panes.py::test_preflight_refusals_have_no_side_effects`.
- 1.1.3 - Preflight refuses `seat_live` when the same-titled tab or pane holds a pending or live terminal, and allows relaunch when that terminal has ended. test: `tests/terminals/test_workspace_agent_panes.py::test_live_seat_refused_ended_seat_allowed`.
- 1.1.4 - Reserve then bind produces a bound pane with `owns_terminal=True`, clears the in-flight mark, and emits exactly one `tab.created` or `pane.added`. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_bind_emits_once`.
- 1.1.6 - Two concurrent reservations of the same workspace and title yield exactly one reservation and one `seat_live` refusal. test: `tests/terminals/test_workspace_agent_panes.py::test_concurrent_same_seat_reserves_once`.
- 1.1.5 - Release removes the reserved pane, and the tab it emptied, and is idempotent. test: `tests/terminals/test_workspace_agent_panes.py::test_release_is_idempotent`.

### 1.2 spawn_agent placement input, SRT requirement, binding and reply [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_response.py::build_spawn_response`
- `src/gobby/agents/spawn_models.py::SpawnRequest`
- `src/gobby/agents/spawn_executor.py::_runtime_spawn`
- `src/gobby/agents/spawn_executor_runtime.py`
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py`

Add `placement: dict | None = None` to the `spawn_agent` tool in
`create_spawn_agent_registry` and pass it to `spawn_agent_impl`. Placement handling
lives in a new module, `_placement.py`; `spawn_agent_impl` gains only three call sites.
Move the placed-launch branch into the new `_placement.py` so that `_implementation.py`
(964 lines) does not grow past the ceiling. The three call sites are:

1. Right after the parent/`can_spawn` checks and the sandbox config resolution, and
   before isolation is created: `preflight_placement(...)`. It returns `None` when no
   placement is given. It does the following:
   - raises `sandbox_required` unless the effective sandbox config is `enabled` with
     `backend == "srt"` and `verify_srt_installation` passes;
   - raises `parent_unresolved` when the parent session is the system session itself,
     or when the project came from ambient context rather than from `project_path` or
     the parent session;
   - runs `AgentPaneReserver.preflight`. On
   refusal, return `{"success": false, "placement_error": code, "error": message}`
   before any side effect.
2. After `reserve_agent_slot` succeeds: `reserve_placement(...)`. It calls
   `AgentPaneReserver.reserve` and stores the reservation on the `SpawnRequest` (new
   optional field `SpawnRequest.placement_reservation`, together with the binder
   callback).
3. At dispatch: with a reservation, await `_execute_spawn_phase()` inline instead of
   scheduling `_run_spawn_phase` in the background. On success, return
   `build_spawn_response(...)` with the new `workspace`, `tab_ref` and `pane_ref`
   fields. `build_spawn_response` gains an optional `placement` argument that adds
   them.

Every failure path after reservation, including `CancelledError`, calls `release`
after `cleanup_failed_spawn` inside the `_spawn_failure` routing. A placed launch
defaults `cleanup_isolation_on_failure` to true for isolation it created, so a refused
or failed seat leaves no worktree or clone. Reused `worktree_id` / `clone_id`
isolation is never removed.

In `spawn_executor.py`, `_runtime_spawn` calls `request.placement_reservation`'s bind
callback immediately after `manager.create_pending` returns the terminal id. That is
before `reserve_observer`/`prepare_spawn`, so it precedes provider exec and the SRT
wrap. A bind failure fails the pending terminal through the existing
`_settle_native_spawn_failure` path and returns a failed `SpawnResult`, so the
provider never executes.

Split `spawn_executor.py` (962 lines): move `_runtime_spawn` and `_promote_prepared`
into the new `spawn_executor_runtime.py` and re-export both names from
`spawn_executor.py`. The facade stays the patch target (memory e82d82f5), and existing
`patch("gobby.agents.spawn_executor....")` sites keep working.

**Research context:**
- The guard order and the cleanup path are as described in the Evidence section.
  Placement preflight sits before isolation (decision 2). Reservation sits after
  `reserve_agent_slot` so that slot, lease and task refusals never leave a pane. The
  path is `release_unattached`, then the existing refusals, and only then the
  reservation.
- Parent and project: `_resolve_spawn_project_context` (`_factory.py`) resolves the
  project from the parent session. For pipeline `mcp` steps the parent is the
  pipeline child session (decision 8). The workspace actor passed to preflight is
  `parent_session_id`.
- SRT: the effective config is `managed_runtime_profile.sandbox_config` or
  `apply_write_grant(agent_sandbox_config(daemon_config), write_grant)`. `wrap_provider_command`
  is the single wrap entry. Verify that a wrap exception returns a failed
  `SpawnResult` before any provider process starts (memory 0a4ac03d says fail-closed,
  INFERRED). If it does not, fixing that path is part of this deliverable.
- `dispatch_batch` does not gain placement: it retires with `gobby build`.
- Rejected alternatives:
  - A separate `place_agent` tool: it duplicates the guards and cannot bind before
    exec.
  - Post-launch adoption: see decision 3.
- Planned checks:
  - `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/spawn_agent/test_placement.py tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py tests/mcp_proxy/tools/spawn_agent/test_factory.py -v`
  - ruff, and mypy on `src/`.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py` — no-edit-reason: Registers the registry; the new optional `placement` parameter defaults to None.
- `src/gobby/ask/agents.py` — no-edit-reason: Unplaced caller of spawn_agent_impl; the placement default None keeps the background path.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: Unplaced dispatch caller; retires with gobby build and gains no placement.
- `src/gobby/feedback/agent.py` — no-edit-reason: Unplaced caller; the placement default None keeps the background path.
- `src/gobby/scheduler/executor.py` — no-edit-reason: Cron agent jobs stay unplaced; cron runbooks reach placement through pipeline mcp steps.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: The HTTP spawn route stays unplaced; placement is an MCP input reached by pipelines.
- `src/gobby/mcp_proxy/tools/spawn_agent/_execution.py` — no-edit-reason: Calls build_spawn_response without placement; the new argument is optional.
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py` — no-edit-reason: Builds SpawnRequest; the new placement_reservation field defaults to None.
- `src/gobby/agents/resume_executor.py` — no-edit-reason: Resume imports _runtime_spawn from the facade re-export and never carries a placement.
- `src/gobby/agents/spawn_executor_codex.py` — no-edit-reason: Imports _runtime_spawn from the facade re-export; the call is unchanged.
- `src/gobby/agents/spawn_executor_providers.py` — no-edit-reason: Reads SpawnRequest fields only; the SRT wrap is reused unchanged.
- `src/gobby/agents/spawn_executor_support.py` — no-edit-reason: The wrap_provider_command path is reused unchanged.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: Patches through the spawn_executor facade, which keeps the moved names.
- `tests/agents/test_srt_spawn.py` — no-edit-reason: The SRT wrap behaviour is unchanged for unplaced spawns.
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` — no-edit-reason: Existing unplaced calls are unaffected by an optional parameter.
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py` — no-edit-reason: Existing unplaced calls are unaffected by an optional parameter.
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py` — no-edit-reason: Existing unplaced failure paths are unchanged.
- `tests/agents/conftest.py` — no-edit-reason: Builds SpawnRequest without a placement; the new field defaults to None.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: Unplaced spawn path; optional parameter and field defaults.
- `tests/agents/test_local_context_setup.py` — no-edit-reason: Unplaced spawn path; optional parameter and field defaults.
- `tests/agents/test_native_spawn.py` — no-edit-reason: Unplaced native spawn; the bind hook is skipped without a reservation.
- `tests/agents/test_spawn_executor_providers.py` — no-edit-reason: The provider wrap is unchanged.
- `tests/agents/test_verified_review_regressions.py` — no-edit-reason: Unplaced SpawnRequest construction; the field defaults to None.
- `tests/ask/test_permissions.py` — no-edit-reason: Unplaced SpawnRequest construction; the field defaults to None.
- `tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py` — no-edit-reason: Patches _runtime_spawn through the facade re-export.
- `tests/mcp_proxy/tools/spawn_agent/test_fallback_agent.py` — no-edit-reason: Unplaced registry calls; the optional parameter defaults to None.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: Unplaced calls; optional parameter and field defaults.
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py` — no-edit-reason: Task dedupe is unchanged for unplaced spawns.
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py` — no-edit-reason: Calls build_spawn_response without placement.
- `tests/mcp_proxy/tools/spawn_agent/test_project_context.py` — no-edit-reason: Project resolution is unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py` — no-edit-reason: Project scope is unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_worktree_reference_resolution.py` — no-edit-reason: Isolation resolution is unchanged.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: The close reviewer spawn stays unplaced.
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py` — no-edit-reason: Unplaced calls; the optional parameter defaults to None.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: dispatch_batch gains no placement.
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` — no-edit-reason: Provider resolution is unchanged.
- `tests/skills/test_reference_library.py` — no-edit-reason: References the registry name only.
- `tests/tasks/test_plan_gate.py` — no-edit-reason: The unplaced plan-gate spawn path is unchanged.
- `tests/terminals/fakes.py` — no-edit-reason: Fake SpawnRequest consumers need no placement field.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: The tmux backend is unplaced; placement uses native terminals.
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: Unplaced spawn_agent_impl calls; the optional parameter defaults to None.

**Acceptance:**

- 1.2.1 - A refused preflight (`invalid_placement`, `seat_live`, `not_found`, `forbidden`) creates no isolation, no child session, no agent run, no terminal and no pane. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_refused_placement_has_no_side_effects`.
- 1.2.2 - A placed spawn with a sandbox config that is not SRT, or not enabled, is refused with `sandbox_required` before any side effect. Unplaced spawns are unaffected. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_launch_requires_managed_srt`.
- 1.2.3 - The pane is bound to the pending terminal before `reserve_observer`/`prepare_spawn`, and the provider argv is the SRT-wrapped command. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_bind_precedes_provider_exec_and_srt_wrap`.
- 1.2.4 - An SRT wrap failure returns `success: false`, runs `cleanup_failed_spawn`, releases the pane, and never starts the provider. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_wrap_failure_refuses_and_releases_pane`.
- 1.2.5 - A successful placed spawn returns synchronously with run_id, terminal_id, workspace, tab_ref and pane_ref. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_spawn_reply_carries_refs`.
- 1.2.6 - A slot, lease or active-task refusal after preflight leaves no pane, and a bind `busy` conflict fails the terminal and releases the pane. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_refusals_release_reservation`.
- 1.2.8 - A placed spawn whose parent is the system session, or whose project resolved from ambient context, is refused with `parent_unresolved` before any side effect. A pipeline child parented to the system or cron session is accepted. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_system_parent_fallback_refused`.
- 1.2.9 - A placed spawn that fails after creating its own worktree removes that worktree, and one that reused a `worktree_id` keeps it. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_failed_placed_spawn_cleans_created_isolation_only`.
- 1.2.7 - `spawn_executor` re-exports `_runtime_spawn` and `_promote_prepared` from `spawn_executor_runtime`. symbol: `_runtime_spawn`. file: `src/gobby/agents/spawn_executor_runtime.py`.

## P2: gclient placement reconciliation
`kind: framing`

Client-side handling of daemon-placed terminals.

### 2.1 gclient reconciliation of daemon-placed terminals [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/apply.rs::apply_event`
- `crates/gclient/src/app/live.rs::apply_live_event`
- `crates/gclient/tests/placed_agent.rs`

gclient already skips an unplaced open when `pane_for_terminal` knows the terminal
(`apply_event`). This deliverable covers the reverse ordering: a `created` lifecycle
event is handled, and `ensure_live_pane` opens an unplaced surface, before the
`tab.created`/`pane.added` workspace event that binds the terminal arrives. When that
binding event arrives, gclient moves the already-open terminal into the bound pane
and removes the unplaced surface; it neither duplicates the terminal nor leaves it
unplaced. It also handles the case where a `created` event arrives for a terminal
that is already bound: it opens nothing new.

**Research context:**
- The `created` handlers are `apply_event` (`crates/gclient/src/app/apply.rs`) and
  `apply_live_event` → `ensure_live_pane` (`crates/gclient/src/app/live.rs`).
- Workspace layout events are applied through the workspace ops/panes modules
  (`crates/gclient/src/app/workspace_ops.rs`, `workspace_panes.rs`).
- Client-side placement for user spawns is `placement_op`
  (`live_loop/workspace_actions.rs`), which is unchanged.
- The daemon binds before exec (1.2), so the workspace event is normally emitted
  first. The lifecycle and workspace streams are separate, which is why both orders
  are tested.
- Load the `rust` skill and `crates/CLAUDE.md` before editing.
- Planned checks: `cargo test -p gclient --test placed_agent`, `cargo clippy -p gclient`.
  A crate change goes live only after a rebuild and install through
  `promote_workspace_binary_set`, and gclient is promoted separately (it is not in the
  stamped set). A PD slot is required.

**Acceptance:**

- 2.1.1 - A binding workspace event that arrives before `created` means the terminal opens only in its bound pane. test: `crates/gclient/tests/placed_agent.rs::bind_then_created_opens_once_in_pane`.
- 2.1.2 - A `created` event that arrives before the binding workspace event gets its unplaced surface moved into the bound pane, leaving exactly one surface. test: `crates/gclient/tests/placed_agent.rs::created_then_bind_moves_into_pane`.

## P3: Runbook pipeline acceptance
`kind: framing`

The ordinary-pipeline runbook proof for Josh's acceptance.

### 3.1 Two-seat runbook pipeline acceptance [category: code] (depends: 1.2, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/pipelines/runbook-two-seat-example.yaml`
- `tests/workflows/test_runbook_placed_pipeline.py`

Add a bundled example runbook: an ordinary pipeline with no new schema and two `mcp`
steps.

1. `seat_a`: `gobby-agents:spawn_agent` with
   `placement: {tab: {workspace: ${{inputs.workspace}}, title: ${{inputs.seat_a_title}}}}`.
2. `seat_b`: `gobby-agents:spawn_agent` with
   `placement: {split: {pane: ${{steps.seat_a.output.pane_ref}}, axis: right, title: ${{inputs.seat_b_title}}}}`.

Each step's failure stops the pipeline through the existing step error semantics.
The template carries only inputs, the two steps, and prompts and agents supplied as
inputs. It carries no guard logic of its own, because the guards live in
`spawn_agent`.

The isolated acceptance test runs this pipeline end to end against an isolated test
daemon (temporary state and ports, test hub `DATABASE_URL`, `GOBBY_TEST_PROTECT=1`).
Provider processes are replaced by a stub command, and the SRT wrap is exercised with
the managed wrapper stubbed at the SRT binary boundary.

**Research context:**
- Pipeline `mcp` steps run through the tool proxy with the child session seeded.
  `_inject_agent_parent_session_argument` fills in `parent_session_id` (decision 8).
  Step outputs are addressable as `${{steps.<id>.output.<field>}}`.
- The bundled pipelines live in `src/gobby/install/shared/workflows/pipelines/`
  (`ask.yaml`, `expand-task.yaml`, `gobby-merge.yaml`). They sync to the DB registry
  (templates are not live config), so the test imports the template into the isolated
  daemon.
- Existing executor test patterns: `tests/workflows/test_pipeline_executor_child_session.py`.
- The CLI entry is `gobby pipelines run runbook-two-seat-example --input ...`, with the
  system-session parent. Cron uses the same pipeline with the cron-session parent. The
  MCP entry is `run_pipeline` with the calling-session parent.
- Planned check: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/workflows/test_runbook_placed_pipeline.py -v`. It needs a PD slot because it
  starts an isolated daemon.

**Acceptance:**

- 3.1.1 - The two-seat pipeline places seat A in a new titled tab and seat B in a right split of seat A's pane. Both terminals are SRT-wrapped and bound before exec, and both replies carry pane refs. test: `tests/workflows/test_runbook_placed_pipeline.py::test_two_seat_tab_and_split`.
- 3.1.2 - Re-running the pipeline while seat A is live fails at `seat_a` with `seat_live` and spawns nothing. test: `tests/workflows/test_runbook_placed_pipeline.py::test_rerun_refuses_live_seat`.
- 3.1.3 - An invalid pane ref for seat B fails the step with no spawn, and seat A is unaffected. test: `tests/workflows/test_runbook_placed_pipeline.py::test_invalid_ref_refuses_without_spawn`.
- 3.1.4 - An SRT wrap failure for seat B fails the step, leaves no seat B pane and no provider process, and leaves seat A unaffected. test: `tests/workflows/test_runbook_placed_pipeline.py::test_wrap_failure_refuses_seat`.
- 3.1.5 - A CLI-started run parents both agents to the pipeline child session, whose parent is the system session, and resolves the pipeline's project. test: `tests/workflows/test_runbook_placed_pipeline.py::test_cli_run_parent_and_project`.

## 4 Verification
`kind: verification`

End-to-end check after 1.1, 1.2, 2.1 and 3.1 land, run in an isolated environment only:
- 1.1 and 1.2 focused pytest;
- `cargo test -p gclient --test placed_agent`;
- the 3.1 isolated-daemon pipeline test.

After a PD-scheduled restart and gclient promotion, the operator smoke test is:

```sh
gobby pipelines run runbook-two-seat-example
```

It runs against a scratch workspace and must show two placed, SRT-wrapped seats and a
refused re-run. Researchers never touch the live daemon or its seats.

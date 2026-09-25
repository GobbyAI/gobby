# Deploy Runbook: Declarative Multi-Agent Deployment Replacing dispatch_batch

Plan artifact: `.gobby/plans/deploy-runbook.md`

**Plan ID:** deploy-runbook

## Overview
`kind: framing`

Task #22895 (Plan deploy_runbook to replace dispatch_batch) under epic #22691
(Agent definitions and deploy_runbook: incremental planning and delivery).
Today the only way to launch more than one agent in a single call is
`gobby-agents:dispatch_batch`: a list of task suggestions fanned out through
`spawn_agent` with a semaphore, returning a transient list of run ids and no
durable record. It cannot express a seat (a standing session in a workspace
pane), cannot be replayed or retried, cannot be queried after the call
returns, and its only consumer is the merge orchestrator.

Josh's ruling for this epic: what is up in the workspace panes is the runbook.
This plan defines `deploy_runbook`, a declarative YAML document naming the
seats and task workers a project runs, a durable deployment ledger, one
service that preflights the whole document before any side effect and then
launches each slot as an ordinary `spawn_agent` call, MCP and CLI surfaces
over that service, and the deletion of `dispatch_batch` with every consumer
migrated. Placed launch (a seat landing in a named tab or split) is #22904's;
this plan carries the seat slot through preflight and the ledger and defers
its live launch to that task as a typed deferral. No shell runbook scripts,
no `gobby build` compatibility, no sandbox key in the document.

## Decision Record
`kind: framing`

1. **Runbook is a YAML document, not a second definition schema (DR-P1).**
   `schema: gobby.runbook/1`, `name`, `project` (path or id), optional
   `workspace` (name or ref), `slots: [...]`. A slot is
   `{id, kind: seat|task, agent, prompt?, task? (task kind only), placement?
   (seat kind only: {tab: {title}} | {split: {slot, axis: horizontal|vertical}}),
   provider?, model?, reasoning_effort?, isolation? none|worktree|clone,
   extra_write_paths? + write_paths_reason?}`. Every override is a
   `spawn_agent` parameter of the same name; the document declares nothing a
   definition already owns. Rejected keys, refused with a hint naming the
   owner: `sandbox` and `sandbox_profile` (definition-only, #22899),
   `parent_session_id`, `reserved_run_id`, `terminal_backend`, `blocked_tools`,
   `rules`, `skills`. Slot ids are stable strings unique within the document;
   `split.slot` names an earlier seat slot. The model is pydantic with
   `extra="forbid"` in a new `src/gobby/agents/runbooks/models.py`. Identity is
   `(project_id, name)`; content identity is the SHA-256 of the canonical JSON
   dump of the validated model.
2. **Input is inline for MCP and a file for the CLI (DR-P1).** `deploy_runbook`
   takes `runbook` (a document as a dict or YAML string) and nothing else names
   a file: the daemon does not read project files on behalf of a caller. The
   CLI reads `.gobby/runbooks/<name>.yaml` for a bare name or any path the
   user passes, and posts the parsed document. No bundled runbooks directory
   and no registry or sync table: a runbook is an input, the deployment row is
   the record. Rejected: `runbook_path` on the tool (file authority would leak
   across projects and machines) and a synced `runbooks` template dir (nothing
   would consume the rows).
3. **Durable ledger in migration 452 (DR-P3).** `runbook_deployments` (id,
   project_id, machine_id, name, runbook_hash, runbook_json, workspace_id,
   state, requested_by_session_id, error, created_at, updated_at) and
   `runbook_slots` (deployment_id, slot_id, position, kind, agent_name,
   task_id, run_id, tab_ref, pane_ref, status, error, updated_at; primary key
   `(deployment_id, slot_id)`). Deployment states: `accepted`, `deploying`,
   `ready`, `partial`, `completed`, `failed`, `stopped`. Slot statuses:
   `pending`, `starting`, `ready`, `completed`, `failed`, `stopped`. A partial
   unique index on `(project_id, name)` where the state is one of `accepted`,
   `deploying`, `ready`, `partial` is the concurrency and replay key
   (Decision 8). `runbook_json` is stored so retry relaunches from the
   accepted document, not from a caller's possibly changed copy. No column on
   `agent_runs`: membership is the `runbook_slots.run_id` reference.
4. **One service, preflight then launch (DR-P2, DR-P5).** A new
   `src/gobby/agents/runbooks/service.py` runs preflight over the whole
   document with zero side effects: the model validates; the project resolves
   and equals the caller's project (`project_mismatch` otherwise); every
   `agent` resolves through `AgentDefinitionManager.get_by_name`; every task
   slot's `task` resolves in that project; the placement graph is acyclic with every `split.slot` naming an
   earlier seat slot; every seat slot carries a placement; and the static
   capacity check holds (Decision 6). Any finding refuses the call with the
   full findings list and no row written. Only after preflight does the
   service insert the deployment as `accepted`, move it to `deploying`, and
   launch slots in document order: seat slots one at a time in placement
   order (a tab before the splits that name it), then task slots concurrently
   under a semaphore of `min(task_slots, max_active_agents_for_project)`.
5. **A slot is exactly a `spawn_agent` call.** The service invokes the
   registered `spawn_agent` tool through
   `InternalToolRegistry.call("spawn_agent", arguments)`
   (`src/gobby/mcp_proxy/tools/internal.py:290`), so every guard, lease, and
   isolation path that protects a hand-issued spawn protects a runbook slot,
   and a change to `spawn_agent` never needs a runbook mirror. Because that
   call bypasses the proxy's `PARENT_SESSION_TOOLS` injection, the service
   passes `parent_session_id` and `project_path` explicitly on every call.
   The task-slot prompt is the `Implement task #N: <title>\n\nDescription:`
   synthesis `dispatch_batch` uses today, so `_suggestion_task_description`
   (`_factory.py:79`, whose only caller is `dispatch_batch` at `:716`) moves
   into the service with the deletion. Rejected: calling `spawn_agent_impl`
   directly (skips the tool-level argument coercion and the caller-session
   plumbing the tool wraps) and a private launcher (a second spawn path).
6. **Capacity: seats are exempt, task slots stay under the cap (DR-P5).** A
   seat is a standing session, not a worker; counting it against
   `max_active_agents` would make a full runbook refuse its own workers.
   `_count_active_agents` (`_spawn_guards.py:366`) excludes runs present in
   `runbook_slots` with `kind = 'seat'`, so `agent_slot_cap_refusal` and
   `reserve_agent_slot` see task runs only. Preflight hard-refuses only the
   static case `task_slots > max_active_agents_for_project`
   (`capacity_static`); `dry_run` reports cap, active count, and headroom;
   live admission inside `spawn_agent` applies to every slot alike: no
   bypass flag is added to `spawn_agent`, a seat spawn passes the same
   `agent_slot_cap_refusal` as a task spawn, and a `cap_reached` refusal of
   either kind fails that slot and leaves the deployment `partial` for retry.
   Seats launch first within a deployment (3.1 step 4), so they are admitted
   before the runbook's own workers and, once running, no longer count.
   Rejected: a spawn-side exemption flag for seats (a second admission path)
   and reserving all task slots up front (the per-project lock in
   `reserve_agent_slot` is held per call, and a bulk reservation would
   duplicate admission).
7. **Readiness and state are derived, never polled (DR-P3).** A slot is
   `starting` when the spawn result reports `status: starting`; it becomes
   `ready` once its `agent_runs` row has `child_session_id` set and status
   `running`, `completed` on run completion, `failed` on run failure or spawn
   refusal, `stopped` after `stop`. Deployment state is computed from slot
   statuses on every read and persisted: `deploying` while the launch loop
   runs; then `ready` when every slot is ready or completed, `completed` when
   every slot is completed, `partial` when at least one slot failed and one
   did not, `failed` when every slot failed, `stopped` after stop. Waiting on
   a slot is the existing `wait_for_agent(run_id)`; no new wait primitive.
8. **Replay, concurrency, and change (DR-P4).** A second `deploy_runbook` for
   an active `(project_id, name)` with the same `runbook_hash` returns the
   existing deployment with `replayed: true` and launches nothing. A different
   hash is refused `runbook_changed` naming the active deployment id; the
   caller stops it or renames. Two concurrent callers race on the partial
   unique index: the loser catches the unique violation, re-reads the winner,
   and applies the same replay-or-refuse rule. No in-process lock is needed.
9. **Retry relaunches failed slots only; stop touches owned resources only
   (DR-P4, DR-P5).** `retry_runbook_deployment` relaunches slots in status
   `failed` from `runbook_json`, in the same order rules, and leaves every
   other slot alone; a deployment in `completed`, `failed`-with-no-slots, or
   `stopped` is refused `deployment_not_retryable`. `stop_runbook_deployment`
   kills every owned run that is still active through the existing kill
   path and closes every tab or pane this deployment created (recorded
   `tab_ref` and `pane_ref`), never a pane it did not create. A foreign pane
   is never adopted: placement creates new tabs and splits only.
10. **Restart mid-launch reconciles on read, without a clock (DR-P4).** The
    service keeps an in-memory set of deployment ids it is currently
    launching. A `deploying` row whose id is not in that set belongs to a
    launch a restart interrupted: on the next read, its `pending` slots with
    no `run_id` become `failed` with error `launch_interrupted`, slots with a
    `run_id` follow their `agent_runs` row, and the state recomputes. No
    startup sweep and no timeout: nothing else in the daemon knows a
    deployment exists, and a read is the only moment the answer matters.
11. **Authority (DR-P2).** MCP: any session may call the tool; seats are
    blocked by the sibling plan's `seat-no-spawn` rule, which must list
    `gobby-agents:deploy_runbook` beside `spawn_agent` (cross-plan obligation
    recorded in Constraints). The caller session is the parent of every
    slot run and is recorded as `requested_by_session_id`. CLI: the daemon
    HTTP tool endpoint with the local auth token; the parent is the system
    session (`storage/sessions/_constants.py::system_session_id`, precedent
    `scheduler/executor.py:304`) unless `--session` names one. Cross-project
    deployment is refused `project_mismatch`. Sandbox comes from the agent
    definition and #22899 only; a runbook cannot loosen it.
12. **Seat slots require placement and wait on #22904 (DR-P3).** Josh's
    execution-model ruling: every agent is an interactive session in a pane
    except one-shots. A seat slot without `placement` is a preflight finding
    (`placement_required`). Until #22904 lands `placement` on `spawn_agent`
    with a `tab_ref`/`pane_ref` reply, preflight refuses any seat slot with
    `placement_unsupported`, so a document of task slots deploys today and a
    document with seats deploys the day #22904 lands, with no runbook change.
    Typed deferral D1.
13. **`dispatch_batch` is deleted, not deprecated (DR-P6).** No shim, no alias.
    The merge orchestrator, the only runtime consumer, deploys an inline
    task-slot runbook named `merge-<resolution_id>` and reads
    `slots[].run_id`. `PARENT_SESSION_TOOLS` gains `deploy_runbook` in 4.1 and
    drops `dispatch_batch` here. Every test that exercised `dispatch_batch` is retargeted
    to the service or deleted where the behavior no longer exists; the
    clone-parameter parity test is retargeted, not deleted.
14. **No gclient verb.** gclient is a workspace-operation client (list,
    new-tab, split, send-keys, ...). Deployment is a daemon-side agent
    operation reached through the MCP tool or `gobby runbooks`; pane refs
    surface in slot status. A gclient `deploy` would be a second HTTP client
    for the same endpoint.
15. **Long-running tools.** `deploy_runbook` and `retry_runbook_deployment`
    join `EXTENDED_TIMEOUT_TOOL_NAMES` (`wait_tools.py:20`), which also feeds
    the client-guarded and heartbeat sets, so a multi-slot launch is not cut
    off by the default tool timeout.
16. **Schema carriers.** Migration 452 carries a `GRANT SELECT, INSERT,
    UPDATE, DELETE ... TO gobby_daemon_runtime` line as migration 440 does,
    and the five derived carriers named in the plan-coverage contract are
    Targets of 1.1. Whether `crates/gcore/src/schema/assets.rs`, `verify.rs`,
    and `verify_tests.rs` also change is decided by the executor with a
    read-only `gdaemon schema plan`, not listed speculatively.

## As-Is Facts
`kind: framing`

- `dispatch_batch` is
  `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry.dispatch_batch`
  (`:625-782`, registered at `:618`). It takes `suggestions` (rows with
  `ref`/`task_ref`/`task_id`/`id`, `title`, optional `prompt`, per-row
  `agent`/`provider`/`model`/`effort`/`isolation`/`worktree`/`clone`/
  `extra_write_paths`), refuses taskless rows and rows without both prompt
  and title, synthesizes the prompt through `_suggestion_task_description`
  (`:79`), bounds `asyncio.gather` with a semaphore of
  `min(len, max_active_agents_for_project)` (`_spawn_guards.py:266`), and
  returns `{dispatched, results: [{task_ref, run_id, success, agent,
  external_write_grant, error?}]}`, discarding `status` and
  `child_session_id`. Nothing durable records the batch.
- `spawn_agent` returns `{success: True, status: "starting",
  external_write_grant, run_id, worktree_id, branch_name, child_session_id,
  isolation, clone_id, reasoning}` (`_implementation.py:955-964`); it accepts
  `isolation` `none|worktree|clone`, `parent_session_id`, `project_path`,
  `target_project_id`, `terminal_backend`, `extra_write_paths`,
  `write_paths_reason`, and `reserved_run_id` (reviewer-internal).
- Admission: `reserve_agent_slot(*, db, project_id, project_path)`
  (`_spawn_guards.py:325-344`) holds a per-project `asyncio.Lock` around
  `agent_slot_cap_refusal` (`:274-298`), which compares
  `_count_active_agents(db, project_id)` (`:366-390`: `agent_runs` in
  `pending`/`running` joined to sessions by project, excluding the task-close
  reviewer) with `max_active_agents_for_project`. `TaskSpawnLease`
  (`:169-229`) and `active_task_spawn_blocker` (`:232`) refuse a second
  active run per task. `_is_parent_merge_orchestrator_run` (`:398-408`) keys
  on the agent names `merge-worker` and `merge-orchestrator` and nothing
  `dispatch_batch`-specific.
- `PARENT_SESSION_TOOLS = frozenset({"dispatch_batch", "evaluate_spawn",
  "spawn_agent"})` (`mcp_proxy/services/tool_execution.py:30`) is the set the
  proxy injects the caller session into. `InternalToolRegistry.call(name,
  arguments, context=None)` (`internal.py:290`) coerces arguments and awaits
  the tool function; `get_tool` (`:430`), `merge_from` (`:439`).
- Registry composition: `create_agents_registry` (`agents_registry.py:35-131`)
  builds `AgentsRegistryContext` (`agents_context.py:30-58`) and calls the
  coordination, query, lifecycle, checkpoint, and spawn registrations;
  `register_agent_spawn_tools` (`agents_spawn_tools.py`) merges
  `create_spawn_agent_registry(...)`. `mcp_proxy/registries.py` (611 lines)
  calls it at `:349` and already carries `workspace_manager` and
  `workspace_ops_resolver` (`:90-91`, used at `:410-413` for the workspaces
  registry). `agents_query_tools.py` is at the 1,000-line ceiling.
- Workspaces: tables `workspaces`, `workspace_tabs`, `workspace_panes` from
  `crates/gcore/assets/schema/migrations/440_add_workspaces.sql` with a
  `GRANT` to `gobby_daemon_runtime`; `WorkspaceManager`
  (`storage/workspaces.py:437`, 973 lines) and `WorkspaceOps`
  (`terminals/workspace_ops.py`, 973 lines: `tab_create` `:262`,
  `pane_split` `:354`, `pane_close` `:458`). `#22904` owns `placement` on
  `spawn_agent`; nothing in the spawn path reads a placement today.
- CLI: `gobby agents spawn` (`cli/agents.py:178-307`) posts
  `/api/mcp/gobby-agents/tools/spawn_agent` with `daemon_auth_headers()` and
  requires `--session`; the route is `servers/routes/mcp/tools.py:53`. Groups
  register in `cli/__init__.py:95-134`.
- System session: `SYSTEM_SESSION_SOURCE`, `system_session_id(machine_id)`,
  `ensure_system_session(db)` in `storage/sessions/_constants.py:72-126`;
  `scheduler/executor.py:304` spawns with `parent_session_id=system_session_id()`.
- Latest migration is `451_drop_comms_routing_rules.sql`; the 450 commit
  touched `catalog.manifest.json`, `grant/bundle.rs`, `schema/assets.rs`,
  `verify.rs`, `verify_tests.rs`, `gcore/tests/schema_contract.rs`,
  `gdaemon/tests/cli_contract.rs`, and `schema_expected_identity.json`.
- Consumers of `dispatch_batch`: `merge-orchestrator.yaml` (prompt `:65-70`,
  allowlist `:400`, handlers `:435-445` reading
  `(tool_output.get('result') or tool_output).get('results')`),
  `rules/restraint/require-restraint-skill.yaml:40`,
  `skills/gobby/references/agents/spawning.md:18`, `docs/guides/agents.md`,
  `docs/guides/workflows-overview.md:65`, `docs/reference-audit/agents.json`,
  and the tests listed in 5.1. Historical mentions only: `docs/reviews/agents.md`,
  `docs/reviews/mcp_proxy-tools.md`, four files under `docs/plans/completed/`.
- Tests: `tests/mcp_proxy/tools/test_parallel_dispatch.py::TestDispatchBatch`
  (`:390` fixture with a `MagicMock` runner, patches
  `_factory.get_project_context`, `_factory._load_agent_body`,
  `_factory.spawn_agent_impl`); `tests/mcp_proxy/tools/spawn_agent/conftest.py`
  provides `db`, `manager`, `mock_runner`, `agent_body`, `isolation_context`,
  `build_agent_body`; `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py`
  covers the guards; `tests/storage/test_workspaces.py` and
  `tests/cli/test_cli_agents.py` are the storage and CLI patterns.

## Constraints
`kind: framing`

- No code in this planning task; leaves below are the implementation. No
  shell runbook scripts (Josh abandoned them). No `gobby build` compatibility
  (memory 4644eba6). No new runner logging, probes, or instrumentation;
  existing OTLP only. Sandbox comes from definitions and #22899 only.
- Sizes: `_factory.py` 784, `_spawn_guards.py` 408, `cli/agents.py` 736,
  `cli/__init__.py` 150, `registries.py` 611, `agents_registry.py` 131,
  `agents_context.py` 58, `wait_tools.py` 261, `tool_execution.py` 833,
  `merge-orchestrator.yaml` 683, `storage/workspaces.py` 973,
  `workspace_ops.py` 973, `agents_query_tools.py` 1004 (at the ceiling: no
  runbook tool goes there). Every new production module is budgeted under
  700 lines; the service splits preflight from launch so neither approaches
  the ceiling.
- Cross-plan obligation: the `seat-no-spawn` rule in
  `.gobby/plans/agent-definition-profiles.md` (#22902, 2.2) must block
  `gobby-agents:deploy_runbook` beside `spawn_agent`; the Plan Writer carries
  that edit in the #22902 round-1 repair draft. This plan does not edit that
  plan.
- Boundaries: #22904 owns `placement` on `spawn_agent` and the
  `tab_ref`/`pane_ref` reply (D1); #22903 owns activation of existing
  sessions; #22899 owns `sandbox_profile`; #21565 owns `terminal_backend`.
- Rollout: 1.1 is a schema migration and 3.1 changes the admission count, so
  the landed branch needs a PD-owned daemon restart (announced globally
  before and after, outside quiet hours 04:45–06:45 CT) before any
  deployment is attempted. Seat redeploy after a restart is Josh through
  `gobby runbooks deploy`, because seats are blocked from the tool.
- Daemon-side tests use the isolated test hub (`DATABASE_URL` from AGENTS.md)
  and never the running daemon. Do not run the full pytest suite.

## P1: Durable Ledger
`kind: framing`

**Goal:** a deployment and its slots survive the call that created them.

### 1.1 Migration 452 and the deployment storage manager [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/452_add_runbook_deployments.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: derived carrier regenerated for migration 452
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: derived carrier for the new tables' runtime grants
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: derived carrier pinning the schema identity
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: derived carrier pinning the schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: derived carrier regenerated for the new schema identity
- `src/gobby/storage/runbook_deployments.py`
- `tests/storage/test_runbook_deployments.py`

**Granularity:** eight Target files but one behavior: the migration and its
five derived carriers are one commit by the plan-coverage contract (a
migration without its carriers fails the schema identity gate), and the
storage manager is the only reader of the new tables, tested against the
migrated schema in the same run.

**Research context:** Migration 440 is the shape to copy: `CREATE TABLE`,
indexes, then one `GRANT SELECT, INSERT, UPDATE, DELETE ON <tables> TO
gobby_daemon_runtime;`. Column types follow the referenced columns in
`baseline.sql` (`agent_runs.id`, `sessions.id`, `tasks.id`, `workspaces.id`,
`projects.id`); the executor copies them rather than assuming `TEXT`.

`452_add_runbook_deployments.sql`:

- `runbook_deployments`: `id` primary key; `project_id` not null, references
  `projects(id)` on delete cascade; `machine_id` not null; `name` not null;
  `runbook_hash` not null; `runbook_json` `JSONB` not null; `workspace_id`
  nullable, references `workspaces(id)` on delete set null; `state` not null
  with a check constraint over the seven Decision 3 states;
  `requested_by_session_id` nullable, references `sessions(id)` on delete set
  null; `error` nullable; `created_at`, `updated_at` timestamps defaulting to
  now. Partial unique index `runbook_deployments_active_name` on
  `(project_id, name)` where `state IN ('accepted','deploying','ready','partial')`.
- `runbook_slots`: `deployment_id` references `runbook_deployments(id)` on
  delete cascade; `slot_id`; `position` integer not null (document order);
  `kind` check `('seat','task')`; `agent_name` not null; `task_id` nullable,
  references `tasks(id)` on delete set null; `run_id` nullable, references
  `agent_runs(id)` on delete set null; `tab_ref`, `pane_ref` nullable text;
  `status` check over the six Decision 3 statuses; `error` nullable;
  `updated_at`. Primary key `(deployment_id, slot_id)`; index
  `runbook_slots_run_id` on `(run_id)` because `_count_active_agents` (3.1)
  joins on it.
- The `GRANT` line for both tables.

Derived carriers: regenerate `catalog.manifest.json` and
`schema_expected_identity.json` with the project's schema tooling, extend
`grant/bundle.rs` for the two tables, and update the identity pins in
`schema_contract.rs` and `cli_contract.rs`. The executor runs a read-only
`gdaemon schema plan` before committing to confirm whether
`crates/gcore/src/schema/assets.rs`, `verify.rs`, or `verify_tests.rs` need
edits for a plain two-table migration (the 450 commit touched them for a
drop) and adds them to the commit only if the plan says so. Rust edits load
the `rust` skill; the crate rebuild and promotion follow `AGENTS.md`
Architecture Facts and are announced, not silent.

`storage/runbook_deployments.py`: `RunbookDeployment` and `RunbookSlot`
dataclasses mirroring the columns, and `RunbookDeploymentManager(db)` with the
hub transaction pattern (`with self.db.transaction() as conn: conn.execute(...,
(%s,))`): `create(deployment, slots)` inserting the row and its slots in one
transaction and raising the storage layer's unique-violation error unchanged
so the service can apply Decision 8; `get(deployment_id)`;
`get_active_by_name(project_id, name)`; `list_for_project(project_id,
states=None, limit=50)`; `set_state(deployment_id, state, error=None)`;
`update_slot(deployment_id, slot_id, *, status, run_id=None, tab_ref=None,
pane_ref=None, error=None)`; `list_slots(deployment_id)`;
`slot_for_run(run_id)`. Model the module on `storage/workspaces.py`'s manager
shape and `tests/storage/test_workspaces.py`'s fixtures. No listener, no
cache.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/storage/test_runbook_deployments.py -q` against the isolated test hub;
`cargo test -p gobby-core --test schema_contract` and `cargo test -p
gobby-daemon --test cli_contract`; `uv run mypy src/gobby/storage/runbook_deployments.py`.

**Acceptance:**

- 1.1.1 - Migration 452 creates both tables with the check constraints, the
  partial unique index, and the runtime grant, and the schema identity
  carriers match the migrated catalog. file:
  `crates/gcore/assets/schema/migrations/452_add_runbook_deployments.sql`.
  test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 1.1.2 - Creating a deployment writes the row and every slot atomically;
  a second active deployment with the same `(project_id, name)` raises the
  unique violation, and one in a terminal state does not. symbol:
  `src/gobby/storage/runbook_deployments.py::RunbookDeploymentManager.create`.
  test: `tests/storage/test_runbook_deployments.py::test_active_name_is_unique_per_project`.
- 1.1.3 - Slot updates persist `status`, `run_id`, `tab_ref`, `pane_ref`,
  and `error`, and `slot_for_run` resolves a run to its deployment and slot.
  test: `tests/storage/test_runbook_deployments.py::test_slot_updates_and_run_lookup`.

## P2: Runbook Document
`kind: framing`

**Goal:** a runbook is validated as a whole before anything happens.

### 2.1 Runbook model and preflight [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/runbooks/__init__.py`
- `src/gobby/agents/runbooks/models.py`
- `src/gobby/agents/runbooks/preflight.py`
- `tests/agents/runbooks/__init__.py`
- `tests/agents/runbooks/test_models.py`
- `tests/agents/runbooks/test_preflight.py`

**Research context:** `AgentDefinitionBody` (`workflows/agent_models.py`) is
the pydantic shape to mirror for strictness (`StrictStr`, validators with
migration hints). Agent lookup is `AgentDefinitionManager.get_by_name`
(`storage/definitions/agents.py:329`). Task resolution reuses the task
manager's ref/id lookup that `dispatch_batch` uses through
`_suggestion_task_description` (`_factory.py:79-93`: it accepts `ref`,
`task_ref`, `task_id`, `id`); actionability is the task's status and open
blockers as `claim_task` judges them; "no active run" is
`storage/agents/_queries.py::get_active_run_for_task` (`:84`). The cap is
`max_active_agents_for_project(project_path)` (`_spawn_guards.py:266`) and the
active count is `_count_active_agents(db, project_id)` (`:366`). Project
resolution follows `_factory.py::_resolve_spawn_project_context` (`:182`).

`models.py`: `Placement` (`tab: TabPlacement{title}` xor `split:
SplitPlacement{slot, axis}`), `RunbookSlot`, `Runbook` with
`schema: Literal["gobby.runbook/1"]`, all `extra="forbid"`. Validators: slot
ids unique and non-empty; `task` present iff `kind == "task"`; `placement`
absent on task slots; `split.slot` names an earlier `seat` slot (acyclic by
construction); `isolation` in `none|worktree|clone`; `extra_write_paths`
requires `write_paths_reason`; the rejected keys of Decision 1 produce a
message naming the owner. `Runbook.content_hash()` is the SHA-256 of
`model_dump(mode="json")` serialized with sorted keys. `parse_runbook(source:
str | dict)` accepts a YAML string or a mapping.

`preflight.py`: `preflight(runbook, *, project_id, project_path, db,
task_manager, definitions, workspace_id: str | None, placement_supported: bool)
-> PreflightReport`
returning `findings: list[{slot_id | None, code, message}]` and, when empty,
the launch plan: `placement_order` (seat slot ids, tabs before the splits
that name them), `task_slots`, `cap`, `active`, `headroom`, and resolved
`tasks: {slot_id: task_id}`. Codes: `project_mismatch`, `agent_unknown`,
`task_unresolved`, `workspace_required` (any seat slot exists and
`workspace_id` is None: the service resolves the `workspace` argument, else
the document's `workspace`, through `WorkspaceManager.resolve_reference`
before calling preflight), `placement_required`, `placement_unsupported`
(when `placement_supported` is false and any seat slot exists),
`capacity_static`. Task status and active-run checks are not preflight
findings: `active_task_spawn_blocker(run_storage, task_id, *,
requested_agent_name, parent_session_id)` (`_spawn_guards.py:232`) is the
sole caller of `_is_parent_merge_orchestrator_run` (`:398`) and carries an
exemption preflight cannot reproduce without duplicating it, so a task with
an active run or a non-actionable status fails its own slot at spawn time
(3.1 step 4) and leaves the deployment `partial`. Preflight performs reads
only; it never inserts, spawns, or reserves.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/agents/runbooks/test_models.py tests/agents/runbooks/test_preflight.py -q`;
`uv run mypy src/gobby/agents/runbooks/`.

**Acceptance:**

- 2.1.1 - A document with an unknown key, a duplicate slot id, a task slot
  with placement, a seat slot with `task`, a split naming a later or unknown
  slot, or any rejected key is refused with a message naming the offending
  slot and, for rejected keys, the owning mechanism. symbol:
  `src/gobby/agents/runbooks/models.py::Runbook`. test:
  `tests/agents/runbooks/test_models.py::test_runbook_rejects_invalid_shapes`.
- 2.1.2 - The content hash is stable across key order and whitespace and
  changes on any semantic change. test:
  `tests/agents/runbooks/test_models.py::test_content_hash_is_canonical`.
- 2.1.3 - Preflight reports every finding across the whole document in one
  pass (unknown agent, unresolved task, seat slot without a workspace,
  missing placement, static capacity) and writes nothing; a task with an
  active run is not a finding. symbol:
  `src/gobby/agents/runbooks/preflight.py::preflight`. test:
  `tests/agents/runbooks/test_preflight.py::test_preflight_collects_all_findings_without_side_effects`.
- 2.1.4 - A clean document yields a launch plan with tabs ordered before
  their splits and the capacity headroom. test:
  `tests/agents/runbooks/test_preflight.py::test_preflight_launch_plan_orders_placement`.

## P3: Deployment Service
`kind: framing`

**Goal:** one service owns launch, readiness, replay, retry, stop, and
recovery, and every slot is a `spawn_agent` call.

### 3.1 Launch path: ledger insert, spawn seam, readiness, seat cap exemption [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/runbooks/service.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::_count_active_agents`
- `tests/agents/runbooks/test_service.py`
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py::*` — scope-reason: add the seat-exemption case beside the existing cap tests

**Granularity:** two lifecycle owners live in this package (launch here,
post-launch lifecycle in 3.2), split so each has its own tests and commit;
the seat cap exemption stays with launch because it is the admission rule
the launch loop depends on, and it is one query edit.

**Research context:** `RunbookDeploymentService(db, manager,
task_manager, definitions, spawn, *, placement_supported)` where `spawn` is
an async callable `(arguments: dict) -> dict` that 4.1 binds to
`registry.call("spawn_agent", arguments)`. `deploy(runbook, *, project_id,
project_path, parent_session_id, workspace=None, dry_run=False)`:

1. Resolve `workspace` (the argument, else the document's `workspace`)
   through `WorkspaceManager.resolve_reference` to a `workspace_id` or None,
   then preflight (2.1). Findings refuse with `{success: False, code:
   "preflight_failed", findings}`. `dry_run` returns the report and stops.
2. Replay check (Decision 8): `manager.get_active_by_name`; same hash returns
   the existing deployment via 3.2's read path with `replayed: True`; a
   different hash refuses `runbook_changed` with `active_deployment_id`.
3. Insert the deployment as `accepted` with `runbook_json`, every slot
   `pending`; a unique violation here re-reads and applies step 2.
4. Add the id to the in-memory `_launching` set, set `deploying`, and launch:
   seat slots sequentially in `placement_order`, each spawn passing
   `placement` (`{tab: {title}}` or `{split: {pane_ref: <the ref recorded
   for the named slot>, axis}}`) and `workspace`; then task slots under
   `asyncio.Semaphore(min(len(task_slots), cap))` with `asyncio.gather`. Every
   spawn call carries `agent`, `prompt` (task slots without a prompt get the
   `Implement task #N: <title>\n\nDescription: ...` synthesis moved from
   `_factory.py::_suggestion_task_description`), `task_id` for task slots,
   `parent_session_id`, `project_path`, and the slot's overrides by their
   `spawn_agent` names. A result with `success: True` marks the slot
   `starting` with `run_id`, `tab_ref`, and `pane_ref` from the reply; a
   refusal marks it `failed` with the reply's `error` (and `code` when
   present: `cap_reached` for admission, which applies to seat and task
   slots alike per Decision 6, and the spawn guard's code for a task whose
   status or active run blocks it). An exception from a spawn is
   caught per slot and recorded the same way; the loop never aborts on one
   slot.
5. Remove the id from `_launching`, recompute and persist the state
   (Decision 7), and return the deployment view: `{success, deployment_id,
   name, state, runbook_hash, replayed, slots: [{slot_id, kind, agent,
   status, run_id, child_session_id, task_id, tab_ref, pane_ref, error}]}`.

Readiness: `refresh(deployment_id)` reads each slot's `agent_runs` row through
`LocalAgentRunManager` (`storage/agents/_manager.py:17`) and maps run status
to slot status per Decision 7 (`running` with `child_session_id` set is
`ready`; the run's terminal statuses map to `completed` or `failed`). 3.2
owns reconciliation of interrupted launches; 3.1's `refresh` treats an id in
`_launching` as live.

Seat cap exemption: `_count_active_agents` (`_spawn_guards.py:366-390`) adds
`AND NOT EXISTS (SELECT 1 FROM runbook_slots rs WHERE rs.run_id = agent_runs.id
AND rs.kind = 'seat')` to its `WHERE` clause, keeping the existing project
join, status filter, close-reviewer exclusion, and optional
`parent_session_id` filter. `agent_slot_cap_refusal` and `reserve_agent_slot`
are unchanged callers.

Seat launch is written here so the executor implements the placement
argument mapping and the sequential order, but it cannot run live until
#22904 lands (D1): with `placement_supported=False` preflight refuses seat
slots, and the service test drives the seat path with a fake `spawn` that
returns `tab_ref`/`pane_ref`.

Tests: `tests/agents/runbooks/test_service.py` uses the isolated hub with
real `RunbookDeploymentManager` rows, `tests/mcp_proxy/tools/spawn_agent/conftest.py`
fixtures for agent bodies, and a recording fake `spawn`. Guard test: insert
an `agent_runs` row referenced by a `runbook_slots` row with `kind='seat'`
and one with `kind='task'` and assert the count includes only the task run.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/agents/runbooks/test_service.py
tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py -q`; `uv run mypy
src/gobby/agents/runbooks/ src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py`.

**Acceptance:**

- 3.1.1 - A document that fails preflight leaves no deployment row, no slot
  row, and no spawn call. symbol:
  `src/gobby/agents/runbooks/service.py::RunbookDeploymentService.deploy`.
  test: `tests/agents/runbooks/test_service.py::test_preflight_failure_has_no_side_effects`.
- 3.1.2 - Each task slot becomes exactly one spawn call carrying
  `parent_session_id`, `project_path`, `task_id`, the synthesized or given
  prompt, and the slot overrides under their `spawn_agent` names; successes
  record `run_id` and `starting`, refusals record `failed` with the error,
  and one refused slot does not stop the others. test:
  `tests/agents/runbooks/test_service.py::test_task_slots_launch_as_spawn_calls`.
- 3.1.3 - Slot and deployment status follow `agent_runs`: a run with
  `child_session_id` and status `running` reads `ready`, a completed run
  `completed`, a failed run `failed`, and the deployment state is `ready`,
  `partial`, `completed`, or `failed` per Decision 7. symbol:
  `src/gobby/agents/runbooks/service.py::RunbookDeploymentService.refresh`.
  test: `tests/agents/runbooks/test_service.py::test_status_derives_from_agent_runs`.
- 3.1.4 - A run referenced by a `kind='seat'` slot is excluded from the
  active-agent count and a `kind='task'` slot run is included. symbol:
  `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::_count_active_agents`.
  test: `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py::test_seat_runs_are_exempt_from_cap`.
- 3.1.5 - Seat slots launch one at a time in placement order, a tab slot
  before the splits that name it, each spawn carrying `placement` and the
  split carrying the pane ref recorded for the named slot, and the reply's
  `tab_ref`/`pane_ref` are stored on the slot. test:
  `tests/agents/runbooks/test_service.py::test_seat_slots_launch_in_placement_order`.

### 3.2 Lifecycle: replay, retry, stop, restart reconciliation [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/runbooks/service.py`
- `tests/agents/runbooks/test_service.py`

**Research context:** Kill path: `agents_lifecycle_tools.py:217` registers
`kill_agent`; the service calls the same implementation it wraps
(`agents/kill.py`) for each owned run whose `agent_runs` status is still
active. Pane close: `WorkspaceOps.pane_close(actor, pane, *, node=None)`
(`workspace_ops.py:458`) and `WorkspaceManager.close_tab` (`:712`); the
service receives a `workspace_ops_resolver` (the callable `registries.py:91`
already carries) and resolves panes and tabs by the recorded refs through
`WorkspaceManager.resolve_reference` (`storage/workspaces.py:474`). A ref
that no longer resolves is skipped, not an error.

`get(deployment_id)`: read the row, apply reconciliation (below), refresh,
persist, and return the view. `retry(deployment_id)`: refuse
`deployment_not_found` or `deployment_not_retryable` (state `completed` or
`stopped`, or no `failed` slot); otherwise rebuild the `Runbook` from
`runbook_json`, run preflight for the failed slots only (agent, task, and
capacity checks; placement checks against the recorded refs of the slots
that stay), set `deploying`, and run the 3.1 launch loop over the failed
slots in the same order rules, resetting each to `pending` first.
`stop(deployment_id)`: for every slot with an active run, kill it; for every
slot with a recorded `pane_ref` or `tab_ref`, close it; mark each slot
`stopped`, the deployment `stopped`. A slot already `completed` keeps that
status. Stop is idempotent.

Reconciliation (Decision 10): inside `get`, `retry`, and `stop`, a
`deploying` row whose id is absent from `_launching` has its `pending` slots
without `run_id` set to `failed` with `error='launch_interrupted'`, its other
slots refreshed from `agent_runs`, and its state recomputed. No clock, no
sweep.

Concurrency test: two coroutines call `deploy` with the same document; one
insert wins, the other returns the winner with `replayed: True`; a third
call with a changed document is refused `runbook_changed`.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/agents/runbooks/test_service.py -q`.

**Acceptance:**

- 3.2.1 - Replaying an active deployment with the same hash returns it with
  `replayed: true` and launches nothing; a changed hash is refused
  `runbook_changed` naming the active deployment; two concurrent callers
  converge on one deployment. test:
  `tests/agents/runbooks/test_service.py::test_replay_and_changed_hash`.
- 3.2.2 - Retry relaunches only `failed` slots from the stored document,
  leaves `ready` and `completed` slots untouched, and is refused for a
  completed or stopped deployment. symbol:
  `src/gobby/agents/runbooks/service.py::RunbookDeploymentService.retry`.
  test: `tests/agents/runbooks/test_service.py::test_retry_relaunches_failed_slots_only`.
- 3.2.3 - Stop kills every active owned run, closes only the panes and tabs
  this deployment recorded, marks slots and deployment `stopped`, and is
  idempotent. symbol:
  `src/gobby/agents/runbooks/service.py::RunbookDeploymentService.stop`.
  test: `tests/agents/runbooks/test_service.py::test_stop_kills_owned_runs_and_closes_owned_panes`.
- 3.2.4 - A `deploying` row not being launched by this process is reconciled
  on read: pending slots without a run become `failed: launch_interrupted`,
  slots with runs follow `agent_runs`, and the state recomputes. test:
  `tests/agents/runbooks/test_service.py::test_interrupted_launch_reconciles_on_read`.

## P4: Surfaces
`kind: framing`

**Goal:** the service is reachable from any session over MCP and from the
shell over the daemon HTTP tool endpoint.

### 4.1 MCP tools on gobby-agents [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/agents_runbook_tools.py`
- `src/gobby/mcp_proxy/tools/agents_context.py::AgentsRegistryContext`
- `src/gobby/mcp_proxy/tools/agents_registry.py::create_agents_registry`
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: pass the workspace ops resolver it already holds into create_agents_registry
- `src/gobby/mcp_proxy/wait_tools.py::EXTENDED_TIMEOUT_TOOL_NAMES`
- `src/gobby/mcp_proxy/services/tool_execution.py::PARENT_SESSION_TOOLS`
- `tests/mcp_proxy/tools/test_agents_runbook_tools.py`
- `tests/mcp_proxy/test_wait_tools.py::*` — scope-reason: pin the two new extended-timeout names beside the existing membership tests

**Research context:** Registration follows `agents_spawn_tools.py::register_agent_spawn_tools(registry, ctx)`:
a new `register_agent_runbook_tools(registry, ctx)` constructs
`RunbookDeploymentService` with `ctx.db`, `RunbookDeploymentManager(ctx.db)`,
`ctx.task_manager`, `AgentDefinitionManager(ctx.db)`,
`spawn=lambda arguments: registry.call("spawn_agent", arguments)` (resolved
at call time, so registration order does not matter), and
`ctx.workspace_ops_resolver`, with `placement_supported=False` until #22904
flips it. `AgentsRegistryContext` (`agents_context.py:30`) gains
`workspace_ops_resolver: Callable[[], WorkspaceOps | None] | None = None`;
`create_agents_registry` (`agents_registry.py:35`) accepts and forwards it and
calls the new registration after the spawn one; `registries.py:349` passes
the resolver it receives at `:91`. Tools, all on `gobby-agents`:

- `deploy_runbook(runbook: dict | str, workspace: str | None = None,
  dry_run: bool = False, parent_session_id: str | None = None)`. The parent
  is the injected caller session when present, else the explicit argument,
  else `system_session_id()` after `ensure_system_session(ctx.db)`. This
  leaf adds `deploy_runbook` to `PARENT_SESSION_TOOLS` (`tool_execution.py:30`)
  so the proxy injects the caller session exactly as it does for
  `spawn_agent`; 5.1 later drops `dispatch_batch` from the same set. Project:
  when the caller session resolves a project
  (`_factory.py::_resolve_spawn_project_context`), that is the deployment's
  project and a document `project` that resolves elsewhere is
  `project_mismatch`; when no session project resolves (the CLI's
  system-session parent), the document's `project` is required and is
  resolved through the same helper, and a document without one is refused
  `project_required` before preflight.
- `get_runbook_deployment(deployment_id: str)`.
- `retry_runbook_deployment(deployment_id: str)`.
- `stop_runbook_deployment(deployment_id: str)`.

Tool descriptions state that seats are blocked by rule and that a slot is a
`spawn_agent` call. `wait_tools.py:20` adds `deploy_runbook` and
`retry_runbook_deployment` to `EXTENDED_TIMEOUT_TOOL_NAMES`;
`tests/mcp_proxy/test_wait_tools.py` pins membership as it does for the
existing names. Tests follow `test_parallel_dispatch.py::TestDispatchBatch.registry_deps`
(`:390`) with a fake service injected through the context.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/mcp_proxy/tools/test_agents_runbook_tools.py
tests/mcp_proxy/test_wait_tools.py tests/mcp_proxy/test_registries.py -q`;
`uv run mypy src/gobby/mcp_proxy/tools/agents_runbook_tools.py`.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/agents_checkpoint_tools.py` — no-edit-reason: reads existing context fields only; the new field has a default of None.
- `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py` — no-edit-reason: reads existing context fields only; the new field has a default of None.
- `src/gobby/mcp_proxy/tools/agents_query_tools.py` — no-edit-reason: reads existing context fields only, and the module is at the size ceiling.
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py` — no-edit-reason: reads existing context fields only; the runbook registration is a sibling function, not an edit here.
- `src/gobby/mcp_proxy/tools/coordination.py` — no-edit-reason: reads existing context fields only.
- `src/gobby/mcp_proxy/tools/agents.py` — no-edit-reason: calls create_agents_registry without the new keyword, which defaults to None.
- `src/gobby/mcp_proxy/stdio_proxy.py` — no-edit-reason: consumes the tuple by name; membership changes need no code change.
- `tests/agents/test_terminal_timeout_checkpoint.py` — no-edit-reason: constructs the context with existing fields; the new field defaults.
- `tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py` — no-edit-reason: constructs the context with existing fields; the new field defaults.
- `tests/runner_init/test_detection_registry_composition.py` — no-edit-reason: composes the registry without the new keyword, which defaults.
- `tests/ask/test_permissions.py` — no-edit-reason: composes the registry without the new keyword, which defaults.
- `tests/events/test_coordination_waits.py` — no-edit-reason: composes the registry without the new keyword, which defaults.
- `tests/mcp_proxy/test_mcp_proxy_stdio.py` — no-edit-reason: exercises the guarded-tool path with existing names; the added names change no assertion.

**Acceptance:**

- 4.1.1 - The four tools are registered on `gobby-agents` and forward to the
  service with the caller session as parent for an MCP caller (injected
  through `PARENT_SESSION_TOOLS`) and the system session for a caller with
  no session. symbol:
  `src/gobby/mcp_proxy/tools/agents_runbook_tools.py::register_agent_runbook_tools`.
  test: `tests/mcp_proxy/tools/test_agents_runbook_tools.py::test_deploy_runbook_resolves_parent_session`.
- 4.1.2 - `dry_run=true` returns the preflight report and creates nothing;
  a refusal returns the findings list with `success: false`. test:
  `tests/mcp_proxy/tools/test_agents_runbook_tools.py::test_dry_run_and_refusal_shapes`.
- 4.1.3 - `deploy_runbook` and `retry_runbook_deployment` are extended-timeout
  tools. symbol: `src/gobby/mcp_proxy/wait_tools.py::EXTENDED_TIMEOUT_TOOL_NAMES`.
  test: `tests/mcp_proxy/test_wait_tools.py::test_runbook_tools_use_extended_timeout`.
- 4.1.4 - A caller session with a project deploys into that project and a
  document naming another project is refused `project_mismatch`; a
  system-session caller deploys into the document's `project`, and a
  document without one is refused `project_required`. test:
  `tests/mcp_proxy/tools/test_agents_runbook_tools.py::test_deploy_runbook_resolves_project`.

### 4.2 CLI `gobby runbooks` [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/runbooks.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: register the runbooks group in the add_command block
- `tests/cli/test_cli_runbooks.py`

**Research context:** `cli/agents.py::spawn` (`:178-307`) is the pattern:
resolve the daemon URL, `daemon_auth_headers()`, post
`/api/mcp/gobby-agents/tools/<tool>`, print the JSON or a table, exit
non-zero on `success: false`. `cli/__init__.py:95-134` is the `add_command`
block. `tests/cli/test_cli_agents.py` mocks the HTTP call.

Commands under a `runbooks` group:

- `deploy <name-or-path> [--workspace NAME] [--session ID] [--dry-run]
  [--wait] [--json]`: a bare name resolves to
  `<project>/.gobby/runbooks/<name>.yaml`; a path is used as given; the file
  is parsed with `parse_runbook` locally so a malformed document fails before
  the request. Posts `deploy_runbook` with the document inline. `--wait`
  never polls: for each task slot whose `run_id` the deploy result returns,
  it posts `wait_for_agent` through the same tool endpoint, one request per
  run bounded by that tool's own timeout (seat runs are standing sessions
  and are not waited on), then posts `get_runbook_deployment` once for the
  final view. Prints one line per slot:
  `slot  kind  agent  status  run_id  pane_ref  error`.
- `status <deployment-id> [--json]`, `retry <deployment-id>`,
  `stop <deployment-id>`: post the matching tool.

The system session is the default parent (Decision 11); `--session` sets
`parent_session_id`.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/cli/test_cli_runbooks.py -q`; `uv run mypy src/gobby/cli/runbooks.py`.

**Acceptance:**

- 4.2.1 - `gobby runbooks deploy <name>` reads `.gobby/runbooks/<name>.yaml`,
  refuses a malformed document locally, and posts the parsed document to the
  `deploy_runbook` tool endpoint with the auth headers. symbol:
  `src/gobby/cli/runbooks.py::deploy`. test:
  `tests/cli/test_cli_runbooks.py::test_deploy_posts_parsed_runbook`.
- 4.2.2 - `status`, `retry`, and `stop` post their tools and exit non-zero on
  a refusal. test: `tests/cli/test_cli_runbooks.py::test_lifecycle_commands_forward_and_fail_on_refusal`.
- 4.2.3 - The group is registered on the root CLI. behavior: "runbooks" in
  `src/gobby/cli/__init__.py`.

## P5: Retirement
`kind: framing`

**Goal:** `dispatch_batch` is gone and every consumer names `deploy_runbook`.

### 5.1 Delete dispatch_batch and migrate its consumers [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: delete dispatch_batch and the moved _suggestion_task_description, and drop the dispatch_batch registration from create_spawn_agent_registry
- `src/gobby/mcp_proxy/services/tool_execution.py::PARENT_SESSION_TOOLS`
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — scope-reason: prompt, allowlist, and handlers move from dispatch_batch to deploy_runbook
- `src/gobby/install/shared/workflows/rules/restraint/require-restraint-skill.yaml::*` — scope-reason: the tool list names deploy_runbook instead of dispatch_batch
- `src/gobby/install/shared/skills/gobby/references/agents/spawning.md`
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: contract assertions follow the orchestrator's new tool and handlers
- `tests/mcp_proxy/tools/test_parallel_dispatch.py::*` — scope-reason: delete TestDispatchBatch and the fixtures only it used
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: retarget the dispatch isolation-parity class to the service
- `tests/mcp_proxy/tools/spawn_agent/test_worktree_reference_resolution.py::*` — scope-reason: retarget the dispatch worktree-reference cases to the service
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: the registry no longer lists dispatch_batch
- `tests/workflows/test_developer_guidance_rules.py::*` — scope-reason: the restraint rule parameter names deploy_runbook
- `tests/agents/runbooks/test_service.py`

**Granularity:** twelve Target files but one behavior, the deletion: a tree
where the tool is gone but a consumer still names it fails the orchestrator
contract test and the rule test, so the deletion and the retargets are one
commit. The test edits are of two kinds, delete (behavior gone) and retarget
(behavior moved to the service), listed per module below.

**Research context:** `_factory.py:618-782` registers and defines
`dispatch_batch`; `_suggestion_task_description` (`:79`) has no other caller
and moved in 3.1. `tool_execution.py:30` drops `dispatch_batch` (4.1
already added `deploy_runbook`), leaving `frozenset({"deploy_runbook",
"evaluate_spawn", "spawn_agent"})`.

Merge orchestrator (`merge-orchestrator.yaml`, 683 lines): the prompt
(`:65-70`) tells the orchestrator to use `dispatch_batch` for parallel-safe
steps; it now says to call `deploy_runbook` with an inline document
`{schema: gobby.runbook/1, name: "merge-<resolution_id>", slots: [{id:
<task ref>, kind: task, agent: merge-worker, task: <task ref>, isolation:
worktree}, ...]}`, to read `slots[].run_id` from the result, and to `retry`
the same deployment rather than re-deploy when a slot failed. The allowlist
(`:400`) swaps `dispatch_batch` for `deploy_runbook` and adds
`retry_runbook_deployment` and `get_runbook_deployment`, so the retry
instruction is executable inside the step. The three handlers (`:435-445`)
keep their variables: `merge_worker_completed: false`,
`post_worker_merge_status_checked: false`, and `current_batch_run_ids` now
computed from `[s.get('run_id') for s in ((tool_output.get('result') or
tool_output).get('slots') or []) if s.get('run_id')]`; a fourth
`on_mcp_success` handler on `deploy_runbook` binds a new step variable
`current_deployment_id: null` from `(tool_output.get('result') or
tool_output).get('deployment_id')`, and the same run-id expression also
runs on `retry_runbook_deployment` success so a retried slot's run joins
the wait set. The `wait_for_agent`
and `list_agent_runs` allowlist entries stay. `_is_parent_merge_orchestrator_run`
keys on agent names, so the worker guard is unchanged.
`require-restraint-skill.yaml:40` lists the spawn tools that require the
restraint skill; `dispatch_batch` becomes `deploy_runbook`, and
`test_developer_guidance_rules.py:172` pins that list. `spawning.md:18`
describes the batch tool; it now describes the runbook document and the
four tools in the same length.

Tests: `test_parallel_dispatch.py::TestDispatchBatch` (`:390-560`) and the
fixtures only it uses are deleted; the remaining classes stay.
`test_initial_variables.py::TestDispatchBatchIsolationParity` (`:1068-1319`)
is retargeted to `tests/agents/runbooks/test_service.py`: the explicit
suggestion contract (`:1068`) becomes a slot-override forwarding test, the
taskless rejection (`:1140`) becomes a model test already covered by 2.1.1,
the clone-parameter forwarding (`:1178`) becomes
`test_slot_isolation_forwards_clone_params`, and the without-isolation case
(`:1263`) becomes part of the same test. `test_worktree_reference_resolution.py:233-284`
retargets its worktree-reference cases to the service with the same
expectations. `test_factory.py:302` drops `dispatch_batch` from the expected
tool list. `test_merge_orchestrator_contract.py:53,975` asserts the new tool
name, the two lifecycle tools in the allowlist, the run-id and
deployment-id handler expressions, and that `dispatch_batch` appears
nowhere in the definition.

Consumers unchanged:
- `docs/reviews/agents.md` — no-edit-reason: dated review record of the tool as it was; history is not rewritten.
- `docs/reviews/mcp_proxy-tools.md` — no-edit-reason: dated review record; history is not rewritten.
- `docs/plans/completed/cc-worktrees.md` — no-edit-reason: archived plan; history is not rewritten.
- `docs/plans/completed/orchestrator-cron.md` — no-edit-reason: archived plan; history is not rewritten.
- `docs/plans/completed/pipeline-heartbeat.md` — no-edit-reason: archived plan; history is not rewritten.
- `docs/plans/completed/task-expander-pipeline-draft.md` — no-edit-reason: archived plan; history is not rewritten.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/mcp_proxy/tools/test_parallel_dispatch.py tests/mcp_proxy/tools/spawn_agent
tests/agents/test_merge_orchestrator_contract.py
tests/workflows/test_developer_guidance_rules.py tests/agents/runbooks -q`;
`gcode grep -w dispatch_batch -m 50` returns only the six unchanged history
files; `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 5.1.1 - `gobby-agents` no longer registers `dispatch_batch`, `_factory.py`
  no longer defines it or `_suggestion_task_description`, and
  `PARENT_SESSION_TOOLS` no longer names `dispatch_batch`. symbol:
  `src/gobby/mcp_proxy/services/tool_execution.py::PARENT_SESSION_TOOLS`.
  test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_registry_lists_spawn_tools_without_dispatch_batch`.
- 5.1.2 - The merge orchestrator's prompt, allowlist, and handlers name
  `deploy_runbook`, the allowlist carries `retry_runbook_deployment` and
  `get_runbook_deployment`, the run-id handler reads `slots[].run_id` on
  deploy and retry, a handler binds `current_deployment_id` from the deploy
  result, and `dispatch_batch` appears nowhere in the definition. test:
  `tests/agents/test_merge_orchestrator_contract.py::test_orchestrator_deploys_workers_as_runbook`.
- 5.1.3 - The restraint rule requires the skill before `deploy_runbook`.
  test: `tests/workflows/test_developer_guidance_rules.py::test_restraint_rule_covers_deploy_runbook`.
- 5.1.4 - Slot overrides for `isolation: clone` forward the clone parameters
  to the spawn call exactly as `dispatch_batch` did, and a slot without
  isolation forwards none. test:
  `tests/agents/runbooks/test_service.py::test_slot_isolation_forwards_clone_params`.

## P6: Documentation
`kind: framing`

**Goal:** the guides describe runbooks and no longer describe the batch tool.

### 6.1 Guides and reference audit [category: docs] (depends: 5.1)
`kind: deliverable`

Targets:
- `docs/guides/agents.md`
- `docs/guides/workflows-overview.md`
- `docs/reference-audit/agents.json::*` — scope-reason: replace the dispatch_batch entry with the four runbook tools

**Research context:** `docs/guides/agents.md` names `dispatch_batch` at
`:16`, `:276`, and `:383` (overview list, parallel dispatch section, tool
table); `docs/guides/workflows-overview.md:65` lists it among agent tools;
`docs/reference-audit/agents.json:464-472` is the hand-maintained audit
entry (version 1). Edits: a "Runbooks" section in `agents.md` with the
document grammar (Decision 1), the lifecycle states (Decision 3), the
preflight-then-launch rule, replay and retry semantics, the seat cap
exemption, the placement deferral, and the CLI; the tool table gains the
four tools and loses `dispatch_batch`; `workflows-overview.md` lists
`deploy_runbook`; the audit entry is replaced by four entries of the same
shape.

**Acceptance:**

- 6.1.1 - The agents guide documents the runbook document, lifecycle, and
  CLI and no longer names `dispatch_batch`. behavior: "Runbooks" section in
  `docs/guides/agents.md`.
- 6.1.2 - The workflows overview and reference audit name the four runbook
  tools. behavior: "deploy_runbook" in `docs/reference-audit/agents.json`.

## D1 Seat slots launch through placed spawn (depends: 3.1)
`kind: deferred`

Seat slots are modeled, preflighted, and ledgered by this plan, and the
service's seat launch loop is implemented and tested against a fake spawn
(3.1.5). Live seat launch needs `spawn_agent` to accept `placement` and reply
with `tab_ref` and `pane_ref`, which #22904 (placed launch plan) owns. When
that lands, 4.1's registration flips `placement_supported` to true, the
`placement_unsupported` preflight code stops firing, and the D1 live check
in V1 runs. Until then a document with a seat slot is refused at preflight
and a task-only document deploys.

```yaml
deferral:
  task_ref: "#22904"
  reason: "External prerequisite: placement on spawn_agent and the tab_ref/pane_ref reply are delivered by the placed-launch plan."
  owner: "program-director"
  original_acceptance_items:
    - 3.1.5
```

## V1: Verification
`kind: verification`

Run after the final edit of each leaf and again before the PD lands the
branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_runbook_deployments.py tests/agents/runbooks tests/mcp_proxy/tools/spawn_agent tests/mcp_proxy/tools/test_parallel_dispatch.py tests/mcp_proxy/tools/test_agents_runbook_tools.py tests/mcp_proxy/test_wait_tools.py tests/mcp_proxy/test_registries.py tests/cli/test_cli_runbooks.py tests/agents/test_merge_orchestrator_contract.py tests/workflows/test_developer_guidance_rules.py -q
cargo test -p gobby-core --test schema_contract && cargo test -p gobby-daemon --test cli_contract
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
gcode grep -w dispatch_batch -m 50
uv run gobby plans validate .gobby/plans/deploy-runbook.md -p /Users/josh/Projects/gobby
```

Live check after the PD-owned restart: `gobby runbooks deploy --dry-run` on a
two-task-slot document prints the preflight report with cap and headroom;
`gobby runbooks deploy` on it returns a deployment id with two `starting`
slots, `gobby runbooks status <id>` reads `ready` once both runs are running,
a second `deploy` of the same file returns the same id with `replayed: true`,
and `gobby runbooks stop <id>` kills both runs and reads `stopped`. From the
Plan Writer seat, `gobby-agents:deploy_runbook` is blocked by
`seat-no-spawn` once #22902 lands. D1 live check after #22904: a document
with one tab seat and one split seat deploys, both panes appear in the
workspace, and `stop` closes only those two panes. Do not run the full
pytest suite; the restart is announced globally before and after, outside
quiet hours.

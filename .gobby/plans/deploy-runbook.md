# Runbooks As Pipelines With Placed Agent Launch

Plan artifact: `.gobby/plans/deploy-runbook.md`

**Plan ID:** deploy-runbook

## Overview
`kind: framing`

Task #22895 (Plan runbooks as existing pipelines with placed agent launch)
under epic #22691 (Agent definitions and deploy_runbook: incremental planning
and delivery). Josh's ruling for the epic: what is up in the workspace panes
is the runbook. This plan makes a runbook an ordinary pipeline tagged
`runbook`. It launches through the existing pipeline entrypoints: MCP
`run_pipeline`, `gobby pipelines run`, the web run route and a cron
`pipeline` job. Each seat is one `mcp` step that calls `spawn_agent` with a
placement, so every seat inherits the spawn guards, slot cap, lease, isolation
and sandbox rules. The pipeline execution and its step rows are the deployment
record, and each seat step's output carries its `run_id` and pane refs. A
first guard step refuses when any requested seat is already held, whether by
a runbook-launched agent or by a hand-launched roster session. After a
restart, a seat step that already launched adopts its original run.

This plan is the single active runbooks coverage owner under #22691. It owns
its new deliverables (P5–P9) and holds typed deferrals (D2–D12) for the
placed-launch leaves it reuses, keyed by their existing acceptance item IDs.

The earlier design in this file (a `gobby.runbook/1` document, an
`agents/runbooks` service, a `runbook_deployments` ledger migration and the
`deploy_runbook` tool family) is withdrawn. None of it exists in code, so its
removal needs no Targets.

## Decision Record
`kind: framing`

1. **A runbook is a pipeline.** It is a `PipelineDefinition` whose `tags`
   include `runbook`, built only from existing step kinds (`mcp`, `exec`,
   `wait`, `condition`, `approval`) and `resume_on_restart`. There is no
   runbook schema, service, table or tool family.
2. **Tags become part of the pipeline definition.** `PipelineDefinition` has
   no `tags` field today. Bundled sync writes `tags=["gobby"]` and project
   import writes none. 5.1 adds `tags`, carries them through both sync paths
   and adds a tag filter to `list_pipelines` and `gobby pipelines list`.
3. **Runbooks are bundled definitions with explicit inputs.** #22691 requires
   a bundled `runbook`-tagged pipeline (PD ruling, 2026-09-30). The example
   runbook lives under `src/gobby/install/shared/workflows/pipelines/` and
   syncs with `gobby` and `runbook` tags. Everything project-specific is an
   input: workspace, seat titles, role files and the requested seats. The
   guard refuses with a named cause in a project that lacks the role files
   or roster.
4. **Each seat is one placed `spawn_agent` step.** The step passes `agent`,
   `prompt`, `placement` and the optional `isolation`. Seats run as
   `agent: default` with a prompt that names the seat's role file under
   `.gobby/roles/`. Seat definitions (#22988, #22992) and interactive
   activation (#22903) are outside this plan and do not block it. Placement,
   its `seat_live` refusal, the managed SRT requirement and cleanup are the
   reused placed-launch leaves (D2–D12).
5. **Seat identity is the placement seat key.** A seat is
   `(workspace, canonical title)`, the key placed-launch decision 7 refuses
   on. #23015 persists the validated placement, canonical title included,
   into the run's resume metadata. 6.1 publishes that as a `seat` field in
   `list_running_agents` and `list_agent_runs`. No new column, table or
   session variable is added.
6. **Admission is atomic per seat and per runbook; capacity is a snapshot.**
   The seat level is placed launch's per-workspace reservation lock (1.1.6,
   1.4.10). It serializes every entrypoint, because they all run in the one
   daemon process. The runbook level is the guard step (6.2). It runs after
   the execution row exists and refuses when any other execution of the same
   pipeline in the project is `pending`, `running` or `waiting_approval`, so
   at most one of two concurrent runs proceeds. Capacity is an early
   fail-closed snapshot. `reserve_agent_slot` stays authoritative for each
   seat, and an unrelated spawn can take a slot between seat steps. That
   leaves a partial deployment, which the seat relaunch recovers (8.1.4).
   Any failed, truncated or partial lookup refuses.
7. **The guard also covers hand-launched seats.** Seats Josh launched by hand
   have no agent run. The guard maps each seat's role file through
   `.gobby/roles/roster.md` to every session ref in a row for that file,
   since one role file can have several rows. It refuses when any of those
   sessions is live or any ref does not resolve, and passes only when
   every one has ended.
8. **Restart reconciles each seat step by its invocation id.** Runbooks set
   `resume_on_restart: true`. Recovery keeps completed step outputs and
   re-runs only unfinished steps. The executor gives every step a
   deterministic `invocation_id`, a UUIDv5 of the execution id and step id
   (7.2). Each seat step passes it to `spawn_agent` as `reserved_run_id`, so
   the run row carries the execution-and-step identity from preparation,
   before provider exec. Two steps that spawn the same seat get distinct
   ids, and a pane move or rename changes nothing. A re-run step whose run
   already exists never reaches placement: `spawn_agent` reconciles it
   first. A run that started is adopted and keeps its
   original `run_id`, whether it is still live, ended, moved or renamed. A
   run that never started refuses with `seat_launch_unsettled`, and nothing
   launches. Recovery re-registers `pipeline-<execution id>` and normally
   reuses the existing child session. Reconciliation checks the run's parent
   by that external id, so a replacement child is also accepted. Resume runs
   the definition snapshot taken at launch (7.1), so a sync during the
   restart cannot change the resumed steps. Public resume of a `failed`
   execution resets every step (`pipeline_executor.py:546-561`), so it is
   refused for any execution whose launch snapshot is a runbook (8.1).
9. **Launch identity follows the existing parent chain.** A depth-0 pipeline
   registers a child session (`source="pipeline"`) whose parent is the caller:
   the calling session for MCP, the system session for the CLI and web routes,
   and the cron session for cron. `_inject_agent_parent_session_argument` makes
   that child the `parent_session_id` of every seat. Placed launch 1.4.8
   accepts a pipeline child parented to the system or cron session and refuses
   the system session itself with `parent_unresolved`. No caller is exempt
   from parent authorization. When cron cannot create its session, the
   pipeline does not start (7.3).
10. **Stop uses the recorded run ids.** When a pipeline completes, its child
    session is marked `deleted`, and `cancel_pipeline` kills only agents
    whose parent is the still-active child. Seats outlive the pipeline, and
    seats parented to a replaced child are outside `cancel_pipeline`. To stop
    a seat, read its `run_id` from `gobby pipelines runs show <execution-id>
    --json` or `get_pipeline_status`, then call `kill_agent` with it. That
    touches only the runbook's own runs and their panes.
11. **Readiness is the spawn reply.** A placed `spawn_agent` returns after the
    terminal is bound, with `run_id`, `terminal_id`, `workspace`, `tab_ref`
    and `pane_ref` (1.4.6). `wait_for_agent` waits for completion, so a
    standing seat never satisfies it, and runbooks do not call it.
12. **Sandbox comes from definitions.** A runbook carries no sandbox key.
    Placed launch refuses anything other than managed SRT (1.4.2, D10).
    #22899 owns profiles.
13. **`dispatch_batch` stays until build retirement.** Its consumers are the
    merge orchestrator, `require-restraint-skill.yaml`, the spawning
    reference and `PARENT_SESSION_TOOLS`. It retires with `gobby build` and
    the merge-orchestrator debt. This plan migrates nothing.
14. **Launch policy is owned by #22995.** `agent-definition-profiles.md`
    Decision 6 and its 2.2 (criterion 2.2.1) block every seat, the Program
    Director included, from `gobby-workflows:run_pipeline`, the exposed
    `gobby-workflows:pipeline:<name>` tools and a shell `gobby pipelines
    run`. Runbooks launch from the operator's CLI, web or gclient surfaces or
    an authorized cron job. This plan adds no enforcement and does not assume
    that rule is live.
15. **Out of scope, with owners.** The parked meeseeks lifecycle input
    (memory 7faa183d) stays with its existing owner. Definition persistence
    (#22906), seat definitions (#22988, #22992), interactive activation
    (#22903), #22660 and the #23008 remainder stay with their owners. The
    placed-launch P4 network-policy leaves stay with their owners under
    #23004, outside this lane.
16. **Section numbers avoid reused item IDs.** Deferral coverage matches
    acceptance item IDs as text in task criteria. This plan's own deliverables
    therefore start at P5, so no item here reuses a placed-launch ID
    (`1.1.1`–`3.1.5`).

## As-Is Facts
`kind: framing`

- `PipelineStep` (`workflows/pipeline_models.py:43`) takes exactly one of
  `exec`, `prompt`, `invoke_pipeline`, `mcp`, `wait`, plus `condition`,
  `approval`, `tools`, `input` and `timeout_seconds`. `PipelineDefinition`
  (`:116`) has `resume_on_restart` and no `tags`.
- `workflows/sync_pipelines.py` creates bundled rows with `tags=["gobby"]`.
  `workflows/imports.py::_upsert_pipeline` (`:140`) sets no tags.
  `update_pipeline` accepts `tags` (`mcp_proxy/tools/workflows/_pipelines.py:175`).
  `list_pipelines` (`_pipeline_discovery.py:9`) and
  `PipelineDefinitionManager.list_all` take no tag filter.
- `gobby pipelines run -i` passes inputs as `dict[str, str]`
  (`cli/pipelines.py:252-257`), and the executor overlays caller inputs
  without coercion (`pipeline_executor.py:498-504`). The safe evaluator
  allows `split` (`safe_evaluator.py:297`).
- `pipeline/handlers.py::execute_mcp_step` calls the tool with the pipeline
  child session as ambient session. It raises on `success: False` or an
  `error` key, which fails the step and the execution.
- `pipeline_executor.py::_execute` registers the child session with external
  id `pipeline-<execution id>` at depth 0 (`:445-459`), and session
  registration reuses the existing row (`storage/sessions/_crud.py:150-233`).
  It restores completed outputs on resume (`:571-585`) and runs a step's
  handler before writing the step `COMPLETED` with its output (`:716-722`).
  A resumed `failed` execution resets every step (`:546-561`).
- `run_pipeline` pre-creates the execution without `definition_json`
  (`mcp_proxy/tools/workflows/_pipeline_execution.py:333-350`), and
  `resume_interrupted_pipelines` (`:611`) reloads the pipeline by name. The
  column exists and the direct executor path fills it
  (`pipeline_executor.py:167-188`). Only MCP `resume_pipeline` (`:377`)
  resumes a `failed` execution.
- Daemon startup re-queues `running` executions of `resume_on_restart`
  pipelines and marks the rest `interrupted`
  (`storage/pipeline_executions.py:715`).
- `_close_pipeline_session` (`pipeline_executor_events.py`) marks the child
  session `deleted` when the pipeline ends. `cancel_pipeline`
  (`_pipeline_execution.py:188`) kills runs listed by the active child.
- `gobby pipelines runs show` (`cli/pipelines_runs.py:28-98`) drops each
  step's `output_json`. `get_pipeline_status` (`_pipeline_execution.py:710`)
  returns step outputs.
- The HTTP route `POST /api/pipelines/run` (`servers/routes/pipelines.py:269`)
  passes no session, so the executor uses `system_session_id()` as caller.
  Cron `pipeline` jobs run through `CronExecutor._execute_pipeline`
  (`scheduler/executor.py:431`) with the session from `_create_cron_session`
  (`:286`), and continue with no session when it fails.
- `StepRenderer.build_render_context` (`pipeline/renderer.py:99`) exposes
  `inputs`, `steps`, session and project keys, and no step identity.
  `reserved_run_id` is accepted only for the task-close reviewer
  (`spawn_agent/_factory.py:420-440`), and a spawn uses it as the new run's
  id (`_implementation.py:569`). A run records `started_at` and
  `terminal_id` (`storage/agents/_models.py:57-63`).
- `spawn_agent` builds the resume snapshot in
  `spawn_agent/_runtime.py::build_spawn_context` and persists it on the run
  row during preparation, before provider exec (`_implementation.py:575`,
  `:689`). Task spawns already answer a retry with the existing active run
  (`spawn_agent/_idempotency.py::active_task_spawn_response`).
- `_list_run_payload` (`mcp_proxy/tools/agents_query_tools.py:74`) exposes
  `task_ref`, `agent_name` and `branch_name` from resume metadata. The file is
  998 lines.
- `reserve_agent_slot` (`spawn_agent/_spawn_guards.py:374`) serializes the
  per-project cap check from `max_active_agents_for_project(project_path)`
  under an in-process lock, one launch at a time. Hand-launched sessions are
  not agent runs and do not count against the cap.
- `.gobby/roles/roster.md` rows have the form `| <file>.md | gobby#N |`,
  pinned by `tests/workflows/test_default_agent_role_contract.py::_probe_documented_lookup`.
  No Python reads the roster today. One role file can have several rows:
  the canonical roster has three `code-reviewer.md` rows.
- `plans/deferral.py::validate_deferral` matches each original acceptance
  item ID as text in the task's validation criteria. Its ownership check walks
  the recovery epic's dependency edges (`_dependency_closure`), not its
  children, or accepts a `cited-parent:` label whose parent carries
  `out-of-scope-for:<epic>`.

## Constraints
`kind: framing`

- No code in this planning task. The deliverables below are the
  implementation.
- No new launch tool, deployment table, migration or document schema. The
  one new tool is the read-only guard in 6.2.
- No duplicate task for any reused placed-launch leaf.
- `agents_query_tools.py` stays under 1,000 lines: 6.1 moves
  `_list_run_payload` into a new module before extending it.
  `_factory.py` and `pipeline_executor.py` gain only a call or a context
  key; reconciliation lives in a new module.
- Every refusal names the held seat and its holder (run id or session ref).
- 7.3 changes cron behavior for every pipeline job: a cron pipeline whose
  cron session cannot be created now fails instead of running under the
  system session.
- Tests use the isolated test hub and `GOBBY_TEST_PROTECT=1`. Live placed
  launch runs only in D1, against an isolated daemon.
- 5.1–8.1 change imported Python and a bundled definition. After landing, the
  PD restarts the daemon from the main checkout outside quiet hours
  (04:45–06:45 CT) with global notices before and after. Bundled sync at
  startup installs the runbook.
- No registry write during drafting, or while the Merge Manager holds the
  shared index.

## Placement Leaf Reconciliation
`kind: framing`

PD ruling (2026-09-30). The placed-launch leaves sit under #22691 by a
parent-only move. Each keeps its criteria, commits, dependencies and
historical `covers:placed-agent-launch:*` labels. This plan references each
one through a typed deferral carrying that leaf's exact acceptance item IDs:

| Deferral | Task | Placed-launch section | State |
| --- | --- | --- | --- |
| D2 | #23009 | 1.1 Agent pane reservation primitives | open |
| D3 | #23010 | 1.2 Executor binds a placed terminal before exec | open |
| D4 | #23011 | 1.3 One daemon-scoped reserver reaches spawn_agent | open |
| D5 | #23012 | 1.4 spawn_agent placement input, compensation and reply | open |
| D6 | #23013 | 1.5 Workspace mutations refuse in-flight panes atomically | open, retained |
| D7 | #23014 | 1.6 Spawn failure cleanup | closed |
| D8 | #23015 | 1.7 Placed resume re-places against current state | open, retained |
| D9 | #23017 | 1.9 In-doubt spawn ownership and restart recovery | closed |
| D10 | #23016 | 1.8 spawn_agent and resume never launch unsandboxed | open |
| D11 | #23018 | 2.1 gclient reconciliation of daemon-placed terminals | open |
| D12 | #23019 | 3.1 Two-seat placement acceptance fixture | open |

Item 1.8.4 moved to placed-launch 4.3.1 and stays with the P4 owners.
Closed planning evidence: #22904 (placed launch) and #22899 (sandbox
profiles).

Finalization, run by the coordinator after approval:

1. Add `deferred-from:deploy-runbook:<section>` to each task in the table.
   Existing labels, criteria and commits stay unchanged.
2. `validate_deferral` needs each task in #22691's dependency closure. Where
   #22691 does not already depend on a task, add a #22691 `blocked-by` edge
   to it. Parentage alone does not satisfy the check.
3. Register deploy-runbook against #22691 once the Merge Manager releases the
   shared index.
4. Create the D1 task with `blocked-by` edges on #23012, #23015, #23016,
   #23019 and this plan's 7.2 and 8.1 leaves, keeping its hold label and
   provenance.
5. Verify the consolidated coverage and dependency closure in the database.
   Then archive the superseded placed-agent-launch narrative and registry
   row. Old leaf pointers resolve through the archived path or the immutable
   commit `3acd5c126c`.

## P5: Tagged Pipelines
`kind: framing`

**Goal:** a pipeline can carry the `runbook` tag from its YAML, and runbooks
can be listed by tag.

### 5.1 Pipeline tags from YAML and a tag filter [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/pipeline_models.py::PipelineDefinition`
- `src/gobby/workflows/sync_pipelines.py::*` — scope-reason: bundled rows keep `gobby` and add the YAML tags
- `src/gobby/workflows/imports.py::_upsert_pipeline`
- `src/gobby/storage/definitions/pipelines.py::PipelineDefinitionManager`
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_discovery.py::list_pipelines`
- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py::*` — scope-reason: expose the optional tag argument on the registered list_pipelines tool
- `src/gobby/cli/pipelines_catalog.py::*` — scope-reason: add the --tag option to gobby pipelines list
- `tests/workflows/test_imports.py::*` — scope-reason: cover YAML tags on project import
- `tests/workflows/test_workflows_sync.py::*` — scope-reason: cover bundled tags merged with gobby
- `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py::*` — scope-reason: cover the tag filter
- `tests/cli/test_cli_pipelines.py::*` — scope-reason: cover the --tag option

**Granularity:** seven production Target files carry one behavior, the
`tags` field from YAML to row to filter. Splitting the model from its sync
and filter would leave tags parsed and never stored, or a filter with nothing
to match.

**Research context:** `PipelineDefinition` gains
`tags: list[str] = Field(default_factory=list)`, each tag a non-empty
string. Bundled sync writes `sorted({"gobby", *definition.tags})`. Project
import writes the YAML tags on create and on update. `list_all` gains
`tag: str | None`, filtering rows whose `tags` contain it (the executor
confirms the JSONB containment form against `list_definition_rows`).
`list_pipelines` and `gobby pipelines list --tag runbook` pass it through.
`update_pipeline`'s existing `tags` argument is unchanged. The `gobby` tag
marks bundled rows for the delete guard (`_pipelines.py:241`), so project
import rejects a YAML that declares `gobby`.

Consumers unchanged:
- `src/gobby/workflows/pipeline_loader.py` — no-edit-reason: it validates `definition_json` into `PipelineDefinition`, and an absent `tags` key defaults to empty.
- `src/gobby/workflows/definitions.py` — no-edit-reason: it re-exports `PipelineDefinition` unchanged.
- `src/gobby/servers/routes/pipeline_definitions.py` — no-edit-reason: it constructs or stores pipeline rows through the unchanged manager API, and the optional `tags` field defaults to empty.
- `src/gobby/mcp_proxy/tools/workflows/_auto_export.py` — no-edit-reason: it dumps `definition_json`, which now carries the YAML `tags`, so export round-trips them.
- `src/gobby/cli/sync.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `src/gobby/sessions/lifecycle.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `src/gobby/storage/definitions/__init__.py` — no-edit-reason: it re-exports the manager unchanged.
- `tests/workflows/test_agent_models.py` — no-edit-reason: it constructs or stores pipeline rows through the unchanged manager API, and the optional `tags` field defaults to empty.
- `tests/mcp_proxy/tools/workflows/test_query.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/mcp_proxy/tools/test_rule_tools.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_import.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/mcp_proxy/tools/workflows/test_pipeline_crud.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/mcp_proxy/tools/workflows/test_registry_surface.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/mcp_proxy/tools/workflows/test_workflow_project_scope.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/servers/routes/test_rules_routes.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/servers/routes/test_servers_routes_pipeline_definitions.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/storage/definitions/test_enabled_reconciliation.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/storage/definitions/test_pipelines_manager.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/workflows/test_loader_overrides.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/workflows/test_pipeline_loader.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/workflows/test_retired_bundled_definitions.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.
- `tests/workflows/test_workflow_variables.py` — no-edit-reason: its existing calls keep their arguments, and the new `tag` filter is optional.

**Acceptance:**

- 5.1.1 - A project pipeline YAML with `tags: [runbook]` imports with
  `runbook` in the row's tags, and a re-import with changed tags updates them.
  test: `tests/workflows/test_imports.py::test_pipeline_yaml_tags_persist_on_import`.
- 5.1.2 - A bundled pipeline YAML with tags syncs with `gobby` plus those
  tags. test:
  `tests/workflows/test_workflows_sync.py::test_bundled_pipeline_tags_merge_with_gobby`.
- 5.1.3 - `list_pipelines(tag="runbook")` returns only tagged pipelines in
  scope, and a project YAML declaring `gobby` is rejected. test:
  `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py::test_list_pipelines_filters_by_tag`.
- 5.1.4 - `gobby pipelines list --tag runbook` prints only tagged pipelines.
  test: `tests/cli/test_cli_pipelines.py::test_list_filters_by_tag`.

## P6: Seat Identity And Guard
`kind: framing`

**Goal:** a live seat is visible as a seat, and a runbook refuses before it
launches anything when a seat is held, a sibling execution is live, or the
cap cannot fit it.

### 6.1 Seat field in run listings [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/agents_run_payload.py`
- `src/gobby/mcp_proxy/tools/agents_query_tools.py::*` — scope-reason: import the moved _list_run_payload from its new module
- `tests/mcp_proxy/tools/test_agents_run_payload.py`

**Research context:** `_list_run_payload` (`agents_query_tools.py:74`) is
used by `list_agent_runs` (`:755`) and `list_running_agents` (`:897`). The file
is 998 lines. Split: `src/gobby/mcp_proxy/tools/agents_query_tools.py::_list_run_payload`
moves unchanged to `src/gobby/mcp_proxy/tools/agents_run_payload.py`, and
only there gains `seat`. #23015 (D8) adds `placement` to the resume snapshot
(`agents/resume_metadata.py::build_resume_metadata`) with the kind, workspace
id and canonical title. `seat` is
`{"workspace": <id>, "title": <canonical title>}` when
`resume_metadata_json["placement"]` carries both, else `None`. The snapshot is built in
`spawn_agent/_runtime.py::build_spawn_context` at spawn, before provider
exec, so a fresh placed run carries its seat from launch. The key names are
the ones #23015 lands; the 6.1 executor reads them from its
`build_resume_metadata`. The test builds runs with and without that key, so
it does not wait on #23015. D1 proves the field on real placed runs.

**Acceptance:**

- 6.1.1 - A run whose resume metadata carries a placement lists
  `seat: {workspace, title}` in `list_running_agents` and `list_agent_runs`,
  and an unplaced run lists `seat: null`. test:
  `tests/mcp_proxy/tools/test_agents_run_payload.py::test_seat_from_placement_metadata`.
- 6.1.2 - `agents_query_tools.py` ends the leaf under 1,000 lines and the
  existing payload fields are unchanged. symbol:
  `src/gobby/mcp_proxy/tools/agents_run_payload.py::_list_run_payload`.
  test: `tests/mcp_proxy/tools/test_agents_run_payload.py::test_payload_fields_unchanged`.

### 6.2 Runbook seat guard tool [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/runbook_seats.py`
- `src/gobby/mcp_proxy/tools/runbook_seat_tools.py`
- `src/gobby/mcp_proxy/tools/agents_registry.py::*` — scope-reason: register the guard tool on gobby-agents
- `tests/agents/test_runbook_seats.py`

**Research context:** `gobby-agents:check_runbook_seats(workspace, requested,
catalogue)` is read-only and is the runbook's first step. `requested` is the
comma-separated seat names from the CLI-safe `seats` input. `catalogue` is a
list of `{name, title, role_file}`. The guard refuses an empty `requested`, a
duplicate name, and a name that is not exactly a catalogue `name`. It then
checks only the requested seats. Called from a pipeline `mcp` step, its
ambient session is the pipeline child. It finds its own execution id from
the child's external id `pipeline-<execution id>`, and refuses when that
session is not a pipeline child. `workspace` resolves through
`WorkspaceManager.resolve_reference` (`storage/workspaces.py:496`). Each
title is canonicalized with `truncate_title`, and two requested seats with
the same `(workspace_id, title)` key refuse. The guard returns
`success: True` with the checked seats and the free slot count only when
every check below passes. Otherwise it returns `success: False` with an
`error` naming each held seat and its holder, which fails the step
(`execute_mcp_step`). Checks, in order:

1. Sibling executions. Any other execution of the same pipeline name in the
   project whose status is `pending`, `running` or `waiting_approval` refuses.
   The caller's own row exists before step 1 runs, so two concurrent runs each
   see the other and both refuse, or one refuses.
2. Runbook-launched seats. Every run in the project whose status is in
   `ACTIVE_AGENT_RUN_STATUSES` (`storage/agents/_constants.py:31`) is read
   with no page cap. A run whose 6.1 `seat` equals a requested key refuses,
   naming its `run_id`.
3. Hand-launched seats. `.gobby/roles/roster.md` under the project root is
   parsed with the pinned row form. A seat resolves the `gobby#N` of every
   row whose file equals its `role_file` through the session manager. Any
   session in `LIVE_SESSION_STATUSES` (`storage/sessions/_constants.py:23`)
   refuses, naming the ref. Any ref that does not resolve refuses. The seat
   passes only when every matching row's session has ended. A seat with no
   row passes. A requested seat whose role file is missing refuses.
4. Capacity. When `max_active_agents_for_project` minus the active count is
   below the number of seats requested, the guard refuses. This is an early
   snapshot (Decision 6). Each seat's `reserve_agent_slot` stays
   authoritative.

A missing or unparseable roster, a storage error, or a query that returns a
truncated page refuses with the cause. Seat-level atomicity stays with
placement's `seat_live`. The guard is the visible first refusal and the
runbook-level admission check.

**Acceptance:**

- 6.2.1 - A seat held by a run in any active status (queued, pending or
  running) refuses with that run id, and an ended run's seat passes. test:
  `tests/agents/test_runbook_seats.py::test_live_run_seat_refuses`.
- 6.2.2 - A roster row whose session is in any `LIVE_SESSION_STATUSES`
  member (active, paused, interrupted, awaiting_input, awaiting_approval,
  awaiting_handoff) refuses with the session ref; an ended session passes; an unresolvable
  ref, a missing roster, a malformed roster and a missing role file each
  refuse. A role file with two rows, one session ended and one live,
  refuses with the live ref whichever row comes first. test:
  `tests/agents/test_runbook_seats.py::test_roster_seats_and_stale_refs`.
- 6.2.3 - Two executions of one runbook that both reach the guard both see a
  live sibling, and at most one passes. test:
  `tests/agents/test_runbook_seats.py::test_concurrent_executions_admit_at_most_one`.
- 6.2.4 - A storage error, a truncated run query and a caller that is not a
  pipeline child session each refuse. test:
  `tests/agents/test_runbook_seats.py::test_uncertain_lookup_fails_closed`.
- 6.2.5 - Fewer free slots than requested seats refuses before any launch.
  test: `tests/agents/test_runbook_seats.py::test_capacity_shortfall_refuses`.
- 6.2.6 - An empty, duplicate, unknown or whitespace-padded seat name, and
  two seats with one canonical key, each refuse before any seat lookup.
  test: `tests/agents/test_runbook_seats.py::test_requested_seats_validated`.

## P7: Pipeline Resume And Seat Adoption
`kind: framing`

**Goal:** a restarted pipeline resumes the steps it launched with, a seat
step that already launched adopts its run, and cron never launches under the
system session.

### 7.1 Pipeline resume runs the launch-time definition [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::run_pipeline`
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::resume_interrupted_pipelines`
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::resume_pipeline`
- `tests/mcp_proxy/tools/test_mcp_proxy_tools_pipeline_resume.py::*` — scope-reason: cover the snapshot on pre-create, startup resume and failed resume

**Research context:** `run_pipeline` pre-creates the execution without
`definition_json` (`_pipeline_execution.py:333-350`), so startup recovery
reloads the pipeline by name (`:655-678`). Bundled sync runs at daemon start,
the same moment recovery runs. The column already exists, and the direct
executor path fills it (`pipeline_executor.py:167-188`). `run_pipeline`
writes `pipeline.model_dump_json()` on pre-create. `resume_interrupted_pipelines`
and `resume_pipeline` (which today reloads by name, `:423-449`) validate that
snapshot into `PipelineDefinition` before any claim or reset and resume from
it. A missing or malformed snapshot refuses with a typed error: startup
recovery marks the execution `failed`, `resume_pipeline` leaves it
unchanged, and nothing launches. This applies to every `resume_on_restart`
pipeline.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_exposed.py` — no-edit-reason: it calls `run_pipeline` with unchanged arguments, and the snapshot is written inside it.
- `src/gobby/runner_lifecycle_subsystems.py` — no-edit-reason: it calls `run_pipeline` and `resume_interrupted_pipelines` with unchanged signatures.
- `tests/events/test_mcp_tool_changes.py` — no-edit-reason: it references `run_pipeline` by name, and the name and signature are unchanged.

**Acceptance:**

- 7.1.1 - A pipeline launched through `run_pipeline` stores its definition
  snapshot, and a definition changed after launch leaves the resumed step
  graph unchanged. test:
  `tests/mcp_proxy/tools/test_mcp_proxy_tools_pipeline_resume.py::test_resume_uses_launch_snapshot`.
- 7.1.2 - A missing or malformed snapshot fails the execution on startup
  recovery, is refused by `resume_pipeline` with its steps unchanged, and
  runs no step. test:
  `tests/mcp_proxy/tools/test_mcp_proxy_tools_pipeline_resume.py::test_missing_snapshot_fails_closed`.

### 7.2 Pipeline spawns reconcile by step invocation id [category: code] (depends: 7.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/pipeline_executor.py::*` — scope-reason: put each step's deterministic invocation_id into its template context
- `src/gobby/workflows/pipeline/renderer.py::StepRenderer`
- `src/gobby/workflows/pipeline/renderer.py::_RESERVED_CONTEXT_KEYS`
- `src/gobby/mcp_proxy/tools/spawn_agent/_seat_adoption.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: route reserved_run_id from an authenticated pipeline caller to authority checks and reconciliation before any placement or launch work
- `tests/workflows/test_pipeline_invocation_id.py`
- `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py`

**Research context:** Decision 8. The executor sets
`invocation_id = uuid5(NAMESPACE_URL, f"gobby-pipeline:{execution_id}:{step_id}")`
in each step's context, and `StepRenderer.build_render_context` and
`should_run_step` expose it as a reserved name: `invocation_id` joins
`_RESERVED_CONTEXT_KEYS` (`renderer.py:71`), so a step with that id is not
flattened over it (`:118`, `:357`). It is stable across restarts, because
the execution id and step ids are.

`reserved_run_id` is reviewer-internal today (`_factory.py:420-440`). The
factory separates the ambient caller (`get_current_session_id()`,
`:401`) from the declared `parent_session_id`, and any caller may name a
pipeline child as its declared parent. Authority therefore comes from the
ambient caller: a pipeline MCP step calls the proxy with the pipeline
child as its ambient session (`pipeline/handlers.py:47-83`).
`_seat_adoption.py::authorize_pipeline_invocation` runs when
`reserved_run_id` is set and the ambient caller is a session with
`source="pipeline"`. It refuses `invocation_unauthorized` unless all of
these hold:

- The caller's external id is `pipeline-<execution_id>` and the caller
  is not deleted.
- The resolved declared parent is the caller itself.
- Execution `<execution_id>` exists, is `running`, and has the caller's
  `project_id`, which is also the spawn's project.
- `reserved_run_id` equals the `invocation_id` of a step in that
  execution's launch snapshot (7.1).

When the ambient caller is not a pipeline session, the existing
reviewer branch runs unchanged, and the queued task-close reviewer path
is untouched. Authorization runs before any run lookup or adoption.
`_seat_adoption.py::reconcile_pipeline_invocation` then runs before
placement preflight, isolation and launch:

- No run with that id: the ordinary path runs, including placement
  preflight and `seat_live`, and the new run is created with that id.
- A run with that id whose parent session is missing, lacks
  `source="pipeline"`, has another `project_id`, or has another external
  id than the caller: refuse `invocation_conflict`.
- A run with that id that has `started_at` set: return
  `success: True`, `adopted: True`, its `run_id` and `status`, and the
  `workspace`, `tab_ref` and `pane_ref` of the workspace pane bound to its
  `terminal_id`, else `null`. Nothing is reserved, isolated or launched.
- Any other run with that id (prepared and never started, or failed before
  start): refuse `seat_launch_unsettled` naming the run and its status.
  Nothing launches, and the operator stops or settles that run before a
  relaunch.

`started_at` is the launch witness: `start_run_or_cleanup`
(`_failure_cleanup.py:368`) runs after the provider process and its
terminal exist (`_execution.py:170`) and sets `status = 'running'` with
`started_at` (`storage/agents/_lifecycle.py:328`). A prepared or rolled-back
run never gets it. An adopted seat whose pane is gone leaves `pane_ref`
null, so a following split step fails `not_found`. The tests use a mock
reserver and launcher, so this leaf closes without #23012. The live proof
through the real preflight with the original seat still held is D1's 8.1.9.

Split: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` is 784 lines and
`src/gobby/workflows/pipeline_executor.py` is 866, so the reconciliation
lives in the new `src/gobby/mcp_proxy/tools/spawn_agent/_seat_adoption.py`,
and each existing file gains only a call or one context key.

Consumers unchanged:
- `src/gobby/dispatch/stage_pipeline.py` — no-edit-reason: it constructs `StepRenderer` unchanged, and the new reserved `invocation_id` name only adds a context key.
- `tests/workflows/test_pipeline_executor_child_session.py` — no-edit-reason: it drives the executor unchanged, and the added context key does not alter its assertions.
- `tests/workflows/test_pipeline_executor_nested.py` — no-edit-reason: it drives the executor unchanged, and the added context key does not alter its assertions.
- `tests/workflows/test_pipeline_renderer.py` — no-edit-reason: its render and condition cases keep their inputs and outputs, and the added context key does not alter them.

**Acceptance:**

- 7.2.1 - A step's `invocation_id` is the same before and after a restart,
  two steps of one execution get different ids, and a step whose id is
  `invocation_id` does not override the reserved name. test:
  `tests/workflows/test_pipeline_invocation_id.py::test_invocation_id_is_stable_per_step`.
- 7.2.2 - A placed spawn from a pipeline child whose `reserved_run_id` names
  a started run returns that run with `adopted: true` and makes no
  placement, reserver or launch call, whether the seat is still live or
  ended, moved or was renamed. test:
  `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_started_run_is_adopted`.
- 7.2.3 - Two steps that spawn the same seat in sequence create two runs,
  and neither adopts the other. test:
  `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_distinct_steps_do_not_share_runs`.
- 7.2.4 - A run with the id that was prepared and never started, or failed
  before start, refuses `seat_launch_unsettled` and launches nothing. test:
  `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_unstarted_run_refuses`.
- 7.2.5 - A non-pipeline caller that names a real pipeline child as
  `parent_session_id` keeps the reviewer-internal refusal and reaches no
  lookup. A pipeline caller refuses `invocation_unauthorized` when its
  declared parent is another session, its execution is missing, not
  running or in another project, or the id is not one of its steps'
  invocation ids. A run with the id under another execution or another
  project refuses `invocation_conflict`. A replacement child with the
  same external id adopts. The queued task-close reviewer spawn is
  unchanged. test:
  `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_invocation_authority`.

### 7.3 Cron pipeline launch requires a cron session [category: code]
`kind: deliverable`

Targets:
- `src/gobby/scheduler/executor.py::CronExecutor`
- `tests/scheduler/test_cron_runbook_chain.py`

**Research context:** A cron `pipeline` job through `CronExecutor` parents
the pipeline child to the cron session. As-is, `_execute_pipeline`
(`scheduler/executor.py:431`) continues with `session_id=None` when
`_create_cron_session` returns `None` or no session manager exists, and the
executor then uses the system session. This leaf makes `_execute_pipeline`
fail the cron run with a typed error in both cases, before
`create_execution`. The existing
`tests/scheduler/test_cron_executor.py::test_execute_pipeline_recreates_missing_system_session`
keeps passing, because it recreates the system session and still gets a
cron session. The chain test drives a real cron job through
`PipelineExecutor` with a stub `gobby-agents` proxy that records each call's
ambient session and project.

Consumers unchanged:
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `src/gobby/runner_init/project_purge.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `src/gobby/scheduler/scheduler.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/dispatch/test_spawn_forwarding.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/reports/test_retirement.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/scheduler/test_cron_executor.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/scheduler/test_cron_integration.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/scheduler/test_cron_scheduler.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/scheduler/test_cron_shell_output.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/scheduler/test_dispatch_executor.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.
- `tests/scheduler/test_system_automation_loop.py` — no-edit-reason: it constructs or drives `CronExecutor` through the unchanged constructor and `execute` entry; only the pipeline action's missing-session branch changes.

**Acceptance:**

- 7.3.1 - A cron `pipeline` job parents the pipeline child to the cron
  session, and each spawn the pipeline makes carries that child as parent
  and the job's project. test:
  `tests/scheduler/test_cron_runbook_chain.py::test_cron_chain_identity`.
- 7.3.2 - A failed cron session create, and a missing session manager, fail
  the cron run with a typed error and create no execution. test:
  `tests/scheduler/test_cron_runbook_chain.py::test_cron_session_failure_refuses`.

## P8: Example Runbook
`kind: framing`

**Goal:** one bundled runbook proves the shape end to end against a stub
spawn, across the MCP and HTTP entrypoints and a restart.

### 8.1 Bundled planning council runbook [category: code] (depends: 5.1, 6.2, 7.1, 7.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/pipelines/planning-council.yaml`
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::resume_pipeline`
- `src/gobby/cli/pipelines_runs.py::show_pipeline_run`
- `tests/workflows/test_runbook_pipeline.py`
- `tests/cli/test_cli_pipelines.py::*` — scope-reason: cover `-i seats=adversary` reaching the executor as a string and step outputs in `runs show --json`

**Research context:** `planning-council` has `type: pipeline`,
`tags: [runbook]`, `resume_on_restart: true`, and inputs `workspace`
(required), `writer_title` (default `Plan Writer`), `adversary_title`
(default `Plan Adversary`), `writer_role_file` (default `plan-writer.md`),
`adversary_role_file` (default `plan-adversary.md`) and `seats` (default
`writer,adversary`). `seats` is a scalar string because `-i` passes strings.
Steps:

1. `guard`: `mcp` `gobby-agents:check_runbook_seats` with `workspace`,
   `requested: ${{ inputs.seats }}` and a `catalogue` of two entries,
   `{name: writer, title: ${{ inputs.writer_title }}, role_file: ${{ inputs.writer_role_file }}}`
   and the same for `adversary`.
2. `writer`: `condition: ${{ 'writer' in inputs.seats.split(',') }}`; `mcp`
   `gobby-agents:spawn_agent` with `agent: default`, a prompt telling the
   session to read `.gobby/roles/_common.md` and then its role file,
   `reserved_run_id: ${{ invocation_id }}`, and
   `placement: {tab: {workspace: ${{ inputs.workspace }}, title: ${{ inputs.writer_title }}}}`.
3. `adversary_split`: runs when both seats are requested; the same spawn
   for the adversary role file with
   `placement: {split: {pane: ${{ steps.writer.output.pane_ref }}, axis: right, title: ${{ inputs.adversary_title }}}}`.
4. `adversary_tab`: runs when only the adversary is requested; the same
   spawn with a tab placement titled `adversary_title`.

Conditions run through `StepRenderer.should_run_step`, which evaluates the
`${{ }}` body with `SafeExpressionEvaluator` and skips the step when false.
The guard has already refused any seat name the conditions would not match.
The outputs are the `run_id` and `pane_ref` of each seat step that ran. An
adopted seat whose pane is gone makes the split step fail `not_found`, and
the execution waits failed for the operator.

`resume_pipeline` reads the tags from the execution's launch snapshot
(7.1), before any claim or reset, and refuses a `failed` execution whose
snapshot tags contain `runbook`, with an error that names the seat relaunch
through `seats`. The current definition's tags play no part.
`show_pipeline_run` adds each step's decoded `output` to `--json` and prints
a `run_id` line per step that has one.

Consumers unchanged:
- `src/gobby/cli/pipelines.py` — no-edit-reason: it registers the `runs` command group, and `show_pipeline_run` keeps its name and options.

Tests run the real `PipelineExecutor` against the isolated test hub with a
stub `gobby-agents` proxy. The stub records each call's arguments, ambient
session and project, and answers `spawn_agent` like a placed reply. Adoption
itself is proven against real `spawn_agent` in 7.2 and live in D1. The
stub checks that each seat call carries `reserved_run_id` equal to the
step's `invocation_id`.

**Acceptance:**

- 8.1.1 - Bundled sync installs the runbook with the `gobby` and `runbook`
  tags, and it validates as a `PipelineDefinition`. file:
  `src/gobby/install/shared/workflows/pipelines/planning-council.yaml`.
  test: `tests/workflows/test_runbook_pipeline.py::test_runbook_syncs_bundled_and_tagged`.
- 8.1.2 - MCP and HTTP launches parent both seats to the pipeline child
  session, whose parent is the caller or the system session, and resolve the
  project. test:
  `tests/workflows/test_runbook_pipeline.py::test_entrypoint_parent_chain`.
- 8.1.3 - A restart between the writer's spawn and its completed write
  re-runs the writer step on the reused pipeline child, and the second
  spawn carries the same parent and the same `reserved_run_id`. A restart after the writer completed
  keeps its `run_id`, skips the guard and launches only the adversary. test:
  `tests/workflows/test_runbook_pipeline.py::test_restart_reruns_on_same_child`.
- 8.1.4 - A partial deploy keeps the launched seat's output. A fresh run
  with `seats: "adversary"` passes the guard and launches only the
  adversary, and a run that asks for the writer while its run is active is
  refused. `gobby pipelines run -i seats=adversary` hands the executor that
  string unchanged. test:
  `tests/workflows/test_runbook_pipeline.py::test_partial_deploy_relaunches_missing_seat`.
  test: `tests/cli/test_cli_pipelines.py::test_run_passes_seats_input_as_string`.
- 8.1.5 - `resume_pipeline` on a failed runbook execution refuses with the
  typed error and changes no step or output, including after the current
  definition lost its `runbook` tag, changed or was deleted. test:
  `tests/workflows/test_runbook_pipeline.py::test_failed_runbook_resume_refused`.
- 8.1.6 - `gobby pipelines runs show --json` includes each seat step's
  `run_id` for a completed and for a failed runbook execution. test:
  `tests/cli/test_cli_pipelines.py::test_runs_show_includes_step_outputs`.

## P9: Documentation
`kind: framing`

**Goal:** operators and agents can find, launch, recover and stop a runbook
with the existing tools.

### 9.1 Runbook guide and pipeline reference [category: docs] (depends: 7.3, 8.1)
`kind: deliverable`

Targets:
- `docs/guides/pipelines.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/runbooks.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/recovery.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/overview.md`
- `src/gobby/install/shared/skills/gobby/catalog.json::*` — scope-reason: register the runbooks topic beside the other pipelines references
- `tests/skills/test_reference_library.py::*` — scope-reason: add test_pipelines_runbooks_topic_is_registered beside the existing reference cases

**Research context:** `docs/guides/pipelines.md` gains a "Runbooks" section
covering Decisions 1–14: the tag, the bundled definition and its inputs, the
guard step, one placed `spawn_agent` step per seat, the entrypoints and
their parent chain, the operator-only launch policy, restart adoption and
the launch snapshot, the refused `failed` resume, partial relaunch with
`seats`, and stop through `kill_agent` with run ids read from
`gobby pipelines runs show <execution-id> --json` or `get_pipeline_status`.
The new skill reference `runbooks.md` carries the same operator steps in
reference form, registered as a pipelines topic in `catalog.json` (beside
`references/pipelines/recovery.md`, `:561`) and listed in the pipelines
`overview.md` topic table. `recovery.md` gains one paragraph: a runbook that
failed with `seat_launch_unsettled`, `invocation_conflict` or `seat_live` has
a held or unsettled seat, which the operator finds by its `seat` field or
run id before any relaunch.

**Acceptance:**

- 9.1.1 - The pipelines guide documents runbooks, their guard, entrypoints,
  restart and stop. behavior: "## Runbooks" in `docs/guides/pipelines.md`.
- 9.1.2 - The skill reference names `check_runbook_seats`, the `seats` input,
  `runs show --json` and `kill_agent` by recorded run id. file:
  `src/gobby/install/shared/skills/gobby/references/pipelines/runbooks.md`.
- 9.1.3 - The recovery reference explains the post-restart
  `seat_launch_unsettled`, `invocation_conflict` and `seat_live` failures.
  behavior: "seat_launch_unsettled" in
  `src/gobby/install/shared/skills/gobby/references/pipelines/recovery.md`.
- 9.1.4 - The runbooks reference is a registered pipelines topic that the
  catalog lists and `get_skill_file` loads. test:
  `tests/skills/test_reference_library.py::test_pipelines_runbooks_topic_is_registered`.

## D1 Live placed runbook acceptance (depends: 7.2, 8.1)
`kind: deferred`

The bundled runbook runs against an isolated daemon with real placed launch.
This section alone owns the live obligations below and the test file
`tests/workflows/test_placed_runbook_live.py`; 7.2 and 8.1 close on their
stubbed proofs. The task is blocked by #23012, #23015, #23016 and #23019 and
needs a PD slot for the isolated daemon.

| Item | Obligation | Artifact |
| --- | --- | --- |
| 8.1.7 | The writer lands in a titled tab and the adversary in a right split, both SRT-wrapped; `list_running_agents` shows each seat's `seat` field; a re-run is refused at the guard with both run ids. | test: `tests/workflows/test_placed_runbook_live.py::test_live_two_seat_runbook` |
| 8.1.8 | `kill_agent` on the run ids from `runs show --json` ends both seats and frees their panes. | test: `tests/workflows/test_placed_runbook_live.py::test_live_stop_by_run_ids` |
| 8.1.9 | A daemon restart between the writer's spawn and its completed write resumes, adopts the writer's original `run_id` by its invocation id and opens no second terminal, including after the writer's pane was renamed. | test: `tests/workflows/test_placed_runbook_live.py::test_live_crash_window_adopts_seat` |

```yaml
deferral:
  task_ref: "TBD-after-23019"
  reason: "External prerequisite: placed spawn, placed resume, the SRT launch guard and the placement fixture are reused placed-launch leaves under #22691."
  owner: "program-director"
  original_acceptance_items:
    - 8.1.7
    - 8.1.8
    - 8.1.9
```

## D2 Agent pane reservation primitives
`kind: deferred`

Reused leaf #23009, placed-launch 1.1. It supplies the seat key, the
atomic per-workspace reservation and release (Decisions 5 and 6).

Provenance: task #23009. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.1.1 | test: `tests/terminals/test_workspace_agent_panes.py::test_invalid_placement_shapes_refused` |
| 1.1.2 | test: `tests/terminals/test_workspace_agent_panes.py::test_preflight_refusals_have_no_side_effects` |
| 1.1.3 | test: `tests/terminals/test_workspace_agent_panes.py::test_live_seat_refused_across_kinds_ended_seat_allowed` |
| 1.1.4 | test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_bind_emits_once` |
| 1.1.5 | test: `tests/terminals/test_workspace_agent_panes.py::test_release_is_idempotent` |
| 1.1.6 | test: `tests/terminals/test_workspace_agent_panes.py::test_concurrent_same_seat_reserves_once` |
| 1.1.7 | test: `tests/terminals/test_workspace_agent_panes.py::test_split_axis_maps_to_storage_axis` |
| 1.1.8 | test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_stores_final_worktree_association` |
| 1.1.9 | test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_insert_failure_leaves_nothing` |
| 1.1.10 | test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_cancelled_before_return_leaves_nothing` |
| 1.1.11 | test: `tests/terminals/test_workspace_agent_panes.py::test_release_kills_only_an_active_owned_terminal` |
| 1.1.12 | test: `tests/terminals/test_workspace_agent_panes.py::test_release_steps_are_independent` |
| 1.1.13 | test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_rollback_failure_leaves_sweepable_residue` |
| 1.1.14 | test: `tests/terminals/test_workspace_agent_panes.py::test_mark_held_until_settle` |
| 1.1.15 | test: `tests/terminals/test_workspace_agent_panes.py::test_split_reserve_refuses_moved_target` |

```yaml
deferral:
  task_ref: "#23009"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.1.1
    - 1.1.2
    - 1.1.3
    - 1.1.4
    - 1.1.5
    - 1.1.6
    - 1.1.7
    - 1.1.8
    - 1.1.9
    - 1.1.10
    - 1.1.11
    - 1.1.12
    - 1.1.13
    - 1.1.14
    - 1.1.15
```

## D3 Executor binds a placed terminal before exec
`kind: deferred`

Reused leaf #23010, placed-launch 1.2. A seat's terminal is bound to its
pane before the provider starts.

Provenance: task #23010. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.2.1 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_bind_follows_wrap_and_precedes_exec` |
| 1.2.2 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_bind_failure_fails_pending_terminal` |
| 1.2.3 | symbol: `_runtime_spawn`; file: `src/gobby/agents/spawn_executor_runtime.py` |
| 1.2.4 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_placed_timeout_holds_pending_then_late_settlement` |
| 1.2.5 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_unplaced_timeout_unchanged` |
| 1.2.6 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_placed_cancellation_has_one_owner` |
| 1.2.7 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_in_doubt_claim_spans_prepare` |
| 1.2.8 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_late_prepare_failure_requires_proven_absence` |
| 1.2.9 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_every_exit_releases_or_hands_off_the_claim` |
| 1.2.10 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_contains_storage_failures` |
| 1.2.11 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_retries_settlement_until_storage_recovers` |
| 1.2.12 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_identityless_row_stays_pending_until_absence_proven` |
| 1.2.13 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_retry_survives_cancellation_until_shutdown` |
| 1.2.14 | test: `tests/agents/test_spawn_executor_placement_bind.py::test_indeterminate_create_is_recovered_by_read_back` |

```yaml
deferral:
  task_ref: "#23010"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.2.1
    - 1.2.2
    - 1.2.3
    - 1.2.4
    - 1.2.5
    - 1.2.6
    - 1.2.7
    - 1.2.8
    - 1.2.9
    - 1.2.10
    - 1.2.11
    - 1.2.12
    - 1.2.13
    - 1.2.14
```

## D4 One daemon-scoped reserver reaches spawn_agent
`kind: deferred`

Reused leaf #23011, placed-launch 1.3. Every entrypoint shares one reserver,
which is what makes seat admission atomic across entrypoints (Decision 6).

Provenance: task #23011. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.3.1 | test: `tests/terminals/test_composition_roots.py::test_configure_terminals_builds_one_agent_pane_reserver` |
| 1.3.2 | test: `tests/mcp_proxy/tools/test_agents_spawn_tools.py::test_spawn_registry_resolves_one_daemon_reserver` |

```yaml
deferral:
  task_ref: "#23011"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.3.1
    - 1.3.2
```

## D5 spawn_agent placement input, compensation and reply
`kind: deferred`

Reused leaf #23012, placed-launch 1.4. It supplies the placed seat step, the
reply the runbook records (Decision 11) and the parent-chain acceptance for
system and cron callers (Decision 9).

Provenance: task #23012. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.4.1 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_refused_placement_has_no_side_effects` |
| 1.4.2 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_launch_requires_managed_srt` |
| 1.4.3 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_wrap_failure_refuses_and_releases_pane` |
| 1.4.4 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_returned_failure_releases_pane_once` |
| 1.4.5 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_exceptions_and_cancellation_release_pane` |
| 1.4.6 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_spawn_reply_carries_refs` |
| 1.4.7 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_refusals_leave_no_pane` |
| 1.4.8 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_parent_and_project_provenance` |
| 1.4.9 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_failed_placed_spawn_cleans_created_isolation_only` |
| 1.4.10 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_concurrent_placed_spawns_share_one_reserver` |
| 1.4.11 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_reserve_failure_and_cancellation_clean_dispatch_state` |
| 1.4.12 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_bind_publish_failure_keeps_release_kill_backstop` |
| 1.4.13 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_duplicate_placed_request_precedence` |
| 1.4.14 | test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_timeout_race_keeps_pane_until_owner_settles` |

```yaml
deferral:
  task_ref: "#23012"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.4.1
    - 1.4.2
    - 1.4.3
    - 1.4.4
    - 1.4.5
    - 1.4.6
    - 1.4.7
    - 1.4.8
    - 1.4.9
    - 1.4.10
    - 1.4.11
    - 1.4.12
    - 1.4.13
    - 1.4.14
```

## D6 Workspace mutations refuse in-flight panes atomically
`kind: deferred`

Reused leaf #23013, placed-launch 1.5, retained at the Adversary's request.
A seat's pane cannot be closed, moved or swapped while its launch is in
flight.

Provenance: task #23013. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.5.1 | test: `tests/storage/test_workspaces.py::test_guarded_mutations_refuse_in_flight_panes` |
| 1.5.2 | test: `tests/storage/test_workspaces.py::test_guard_and_insert_serialize` |
| 1.5.3 | test: `tests/storage/test_workspaces.py::test_add_pane_refuses_moved_beside_target` |
| 1.5.4 | test: `tests/storage/test_workspaces.py::test_empty_workspace_close_serializes_with_new_tab` |
| 1.5.5 | test: `tests/storage/test_workspaces.py::test_sweep_keeps_orphaned_panes` |
| 1.5.6 | test: `tests/terminals/test_workspace_ops.py::test_ops_refuse_in_flight_and_retry_orphaned_kill` |
| 1.5.7 | file: `src/gobby/storage/workspace_layout.py` |
| 1.5.8 | file: `src/gobby/terminals/workspace_pane_io.py` |
| 1.5.9 | test: `tests/terminals/test_workspace_ops.py::test_close_refuses_membership_drift_since_read` |

```yaml
deferral:
  task_ref: "#23013"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.5.1
    - 1.5.2
    - 1.5.3
    - 1.5.4
    - 1.5.5
    - 1.5.6
    - 1.5.7
    - 1.5.8
    - 1.5.9
```

## D7 Spawn failure cleanup
`kind: deferred`

Delivered leaf #23014, placed-launch 1.6, closed `completed` at `0229892d08`. A failed seat launch
cleans up once and never leaves a running provider.

Provenance: task #23014. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.6.1 | test: `tests/storage/test_terminal_kill_settlement.py::test_mark_kill_failed_cas` |
| 1.6.2 | test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_failed_kill_orphans_and_keeps_isolation` |
| 1.6.3 | test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_runs_once_per_attempt` |
| 1.6.4 | test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_survives_cancellation` |
| 1.6.5 | test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_steps_are_independent` |
| 1.6.6 | symbol: `SpawnCleanupOnce`; file: `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py` |
| 1.6.7 | test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_held_terminal_defers_isolation_to_owner` |

```yaml
deferral:
  task_ref: "#23014"
  reason: "Delivered placed-launch leaf; its closure is the evidence for this obligation."
  owner: "program-director"
  original_acceptance_items:
    - 1.6.1
    - 1.6.2
    - 1.6.3
    - 1.6.4
    - 1.6.5
    - 1.6.6
    - 1.6.7
```

## D8 Placed resume re-places against current state
`kind: deferred`

Reused leaf #23015, placed-launch 1.7, retained at the Adversary's request.
It persists the placement in the resume snapshot that 6.1 reads as the seat
identity (Decision 5).

Provenance: task #23015. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.7.1 | test: `tests/agents/test_resume_placement.py::test_snapshot_carries_validated_placement` |
| 1.7.2 | test: `tests/agents/test_resume_placement.py::test_placed_resume_replaces_before_exec` |
| 1.7.3 | test: `tests/agents/test_resume_placement.py::test_placed_resume_refusals_park_successor` |
| 1.7.4 | test: `tests/agents/test_resume_placement.py::test_placed_resume_cleanup_once` |
| 1.7.5 | file: `src/gobby/agents/resume_executor_settlement.py` |
| 1.7.6 | test: `tests/agents/test_resume_placement.py::test_placed_resume_cancel_keeps_in_doubt_owner` |

```yaml
deferral:
  task_ref: "#23015"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.7.1
    - 1.7.2
    - 1.7.3
    - 1.7.4
    - 1.7.5
    - 1.7.6
```

## D9 In-doubt spawn ownership and restart recovery
`kind: deferred`

Delivered leaf #23017, placed-launch 1.9, closed `completed` at `50e12a6490`. A launch whose outcome is
in doubt keeps its seat until its owner settles it.

Provenance: task #23017. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.9.1 | test: `tests/terminals/test_in_doubt_kill_truth.py::test_in_doubt_registry_claims_defers_and_releases` |
| 1.9.2 | test: `tests/terminals/test_in_doubt_kill_truth.py::test_kill_terminal_refuses_held_ids` |
| 1.9.3 | test: `tests/terminals/test_in_doubt_kill_truth.py::test_kill_terminal_requires_proven_kill` |
| 1.9.4 | test: `tests/terminals/test_in_doubt_kill_truth.py::test_record_orphan_identity_cas` |
| 1.9.5 | test: `tests/terminals/test_host_reconcile_orphans.py::test_reconcile_recovers_orphan_identity` |
| 1.9.6 | test: `tests/terminals/test_in_doubt_kill_truth.py::test_probe_is_strict_and_reaper_honors_claims` |

```yaml
deferral:
  task_ref: "#23017"
  reason: "Delivered placed-launch leaf; its closure is the evidence for this obligation."
  owner: "program-director"
  original_acceptance_items:
    - 1.9.1
    - 1.9.2
    - 1.9.3
    - 1.9.4
    - 1.9.5
    - 1.9.6
```

## D10 spawn_agent and resume never launch unsandboxed
`kind: deferred`

Reused leaf #23016, placed-launch 1.8. Seats launch only under managed SRT
(Decision 12). Item 1.8.4 moved to placed-launch 4.3.1 and is outside this
lane.

Provenance: task #23016. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 1.8.1 | test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_unsandboxed_config_refused_before_side_effects` |
| 1.8.2 | test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_runtime_profile_config_is_gated` |
| 1.8.3 | test: `tests/agents/test_resume_sandbox_gate.py::test_resume_refuses_unsandboxed_config` |
| 1.8.5 | test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_loopback_mcp_calls_are_rule_enforced` |
| 1.8.6 | test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_gate_refuses_when_isolated_srt_is_missing` |

```yaml
deferral:
  task_ref: "#23016"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 1.8.1
    - 1.8.2
    - 1.8.3
    - 1.8.5
    - 1.8.6
```

## D11 gclient reconciliation of daemon-placed terminals
`kind: deferred`

Reused leaf #23018, placed-launch 2.1. The client shows daemon-placed seats
in their panes.

Provenance: task #23018. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 2.1.1 | test: `crates/gclient/tests/placed_agent.rs::bind_then_created_opens_once_in_pane` |
| 2.1.2 | test: `crates/gclient/tests/placed_agent.rs::created_then_bind_moves_into_pane` |
| 2.1.3 | test: `crates/gclient/tests/placed_agent.rs::reconnect_projects_bound_terminal_once` |

```yaml
deferral:
  task_ref: "#23018"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 2.1.1
    - 2.1.2
    - 2.1.3
```

## D12 Two-seat placement acceptance fixture
`kind: deferred`

Reused leaf #23019, placed-launch 3.1. It proves placement-level refusal for
a two-seat pipeline. Roster-level refusal is this plan's 6.2.

Provenance: task #23019. The item text of record is `.gobby/plans/placed-agent-launch.md` at commit `3acd5c126c`, whose M1 was applied at `90831a98bf`. The historical `covers:placed-agent-launch:*` labels stay.

| Item | Artifact of record |
| --- | --- |
| 3.1.1 | test: `tests/workflows/test_placed_pipeline_fixture.py::test_two_seat_tab_and_split` |
| 3.1.2 | test: `tests/workflows/test_placed_pipeline_fixture.py::test_rerun_refuses_live_seat` |
| 3.1.3 | test: `tests/workflows/test_placed_pipeline_fixture.py::test_invalid_ref_refuses_without_spawn` |
| 3.1.4 | test: `tests/workflows/test_placed_pipeline_fixture.py::test_wrap_failure_refuses_seat` |
| 3.1.5 | test: `tests/workflows/test_placed_pipeline_fixture.py::test_cli_run_parent_and_project` |

```yaml
deferral:
  task_ref: "#23019"
  reason: "Reused placed-launch leaf; its criteria and commits are unchanged and it stays the implementation owner."
  owner: "program-director"
  original_acceptance_items:
    - 3.1.1
    - 3.1.2
    - 3.1.3
    - 3.1.4
    - 3.1.5
```

## V1 Plan Changelog
`kind: verification`

- 2026-09-30: Full rewrite for #22895. The runbook document, service, ledger
  migration and `deploy_runbook` tool family are withdrawn in favor of tagged
  pipelines with placed `spawn_agent` steps. `dispatch_batch` retirement moves
  to build retirement. The Adversary's four pre-draft obligations (cron chain,
  restart window, durable seat identity, atomic admission) are Decisions 5–9
  and acceptance 6.2.x and 7.1.x. Per the PD's consolidation ruling, this
  plan owns runbooks coverage under #22691 and references the reused
  placed-launch leaves through typed deferrals D2–D12.
- 2026-09-30: Enhancer pass (run d94b46a5) and the Adversary's early read,
  all dispositions by the PD. PE-01: the live obligations move to D1 alone
  as 8.1.7–8.1.9. PE-02: capacity is an early snapshot and a partial
  deployment is recoverable. PE-03: `runs show --json` exposes step run ids.
  PE-04: `seats` is a CLI-safe scalar validated by the guard. PE-05: resume
  runs the launch-time definition snapshot (7.1). PE-06: recovery reuses the
  pipeline child. PE-07: canonical guard keys, complete active queries and
  live roster holds. PE-08: cron identity is its own leaf (7.3). The
  Adversary's restart gap is closed by seat adoption from the spawn-time
  resume snapshot (7.2), and `failed` runbook resume is refused (8.1.5). Per
  PD rulings, the runbook is bundled with explicit inputs, and launch policy
  stays with #22995 (Decision 14). Docs move to P9.
- 2026-09-30: Adversary round on f713f1e. ADOPTION_ORDER, ORIGINAL_STEP and
  PREPARED_NOT_READY: seat adoption by placement is replaced by per-step
  invocation ids passed as `reserved_run_id` (Decision 8, 7.2). A re-run step
  reconciles before placement, adopts only a run with `started_at`, and
  refuses an unstarted one. 7.2 no longer needs #23015, and closes on stubs;
  D1 8.1.9 owns the real-preflight proof. FAILED_RESUME_TAG: failed resume
  reads the launch snapshot before any claim or reset (7.1, 8.1.5). 9.1
  registers the runbooks topic in the catalog and overview. 6.2.2 covers
  every live session status.
- 2026-09-30: Adversary round on 20efd6d. INVOCATION_AUTHORITY: the
  widened `reserved_run_id` path authorizes the ambient pipeline caller,
  its running same-project execution and a snapshot step's invocation id
  before any lookup, and reconciliation checks the original run's parent
  source, project and external id (7.2, 7.2.5). TARGET_COMPLETENESS: 7.2
  targets `_RESERVED_CONTEXT_KEYS` and pins collision protection (7.2.1),
  and 9.1 targets `tests/skills/test_reference_library.py`, removed from
  7.1 and 8.1 Consumers unchanged.
- 2026-09-30: Consensus. The Adversary (gobby#14579) rechecked bccc937
  and resolved INVOCATION_AUTHORITY and TARGET_COMPLETENESS with no
  remaining blocking findings. Narrative consensus between the Writer and
  the Adversary on this candidate. The changelog moves to this canonical
  V1 heading, and verification becomes V2 with its commands unchanged.
- 2026-09-30: Renewed consensus after PD review of M1 04a68a48. The PD
  found one blocker: a role file can have several roster rows. The guard
  now resolves every matching row and passes only when all have ended
  (Decision 7, 6.2, 6.2.2). The Adversary (gobby#14579) rechecked
  4fbd036 and confirmed renewed consensus. The superseded M1 is withdrawn
  for a fresh derive.


## V2: Verification
`kind: verification`

Run after each leaf's final edit and again before the PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_imports.py tests/workflows/test_workflows_sync.py tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py tests/cli/test_cli_pipelines.py tests/mcp_proxy/tools/test_agents_run_payload.py tests/mcp_proxy/tools/test_agent_live_stats.py tests/agents/test_runbook_seats.py tests/workflows/test_runbook_pipeline.py tests/scheduler/test_cron_runbook_chain.py tests/scheduler/test_cron_executor.py tests/workflows/test_pipeline_executor_child_session.py tests/mcp_proxy/tools/test_mcp_proxy_tools_pipeline_resume.py tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py tests/workflows/test_pipeline_invocation_id.py tests/skills/test_reference_library.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate "$(git rev-parse --show-toplevel)/.gobby/plans/deploy-runbook.md" -p /Users/josh/Projects/gobby
```

Validate the absolute artifact path: from a task worktree, a relative path
under `-p` resolves to the main checkout's copy. After the PD-owned restart,
`gobby pipelines list --tag runbook` lists the bundled `planning-council`.
Do not run the full pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Pipeline tags from YAML and a tag filter
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '5.1.1: A project pipeline YAML with `tags: [runbook]` imports
    with `runbook` in the row''s tags, and a re-import with changed tags updates them.
    test: `tests/workflows/test_imports.py::test_pipeline_yaml_tags_persist_on_import`.

    5.1.2: A bundled pipeline YAML with tags syncs with `gobby` plus those tags. test:
    `tests/workflows/test_workflows_sync.py::test_bundled_pipeline_tags_merge_with_gobby`.

    5.1.3: `list_pipelines(tag="runbook")` returns only tagged pipelines in scope,
    and a project YAML declaring `gobby` is rejected. test: `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py::test_list_pipelines_filters_by_tag`.

    5.1.4: `gobby pipelines list --tag runbook` prints only tagged pipelines. test:
    `tests/cli/test_cli_pipelines.py::test_list_filters_by_tag`.'
  labels:
  - covers:deploy-runbook:5.1:5.1.1
  - covers:deploy-runbook:5.1:5.1.2
  - covers:deploy-runbook:5.1:5.1.3
  - covers:deploy-runbook:5.1:5.1.4
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: Seat field in run listings
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '6.1.1: A run whose resume metadata carries a placement lists
    `seat: {workspace, title}` in `list_running_agents` and `list_agent_runs`, and
    an unplaced run lists `seat: null`. test: `tests/mcp_proxy/tools/test_agents_run_payload.py::test_seat_from_placement_metadata`.

    6.1.2: `agents_query_tools.py` ends the leaf under 1,000 lines and the existing
    payload fields are unchanged. symbol: `src/gobby/mcp_proxy/tools/agents_run_payload.py::_list_run_payload`.
    test: `tests/mcp_proxy/tools/test_agents_run_payload.py::test_payload_fields_unchanged`.'
  labels:
  - covers:deploy-runbook:6.1:6.1.1
  - covers:deploy-runbook:6.1:6.1.2
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
- title: Runbook seat guard tool
  category: code
  task_type: feature
  depends_on:
  - '6.1'
  validation_criteria: '6.2.1: A seat held by a run in any active status (queued,
    pending or running) refuses with that run id, and an ended run''s seat passes.
    test: `tests/agents/test_runbook_seats.py::test_live_run_seat_refuses`.

    6.2.2: A roster row whose session is in any `LIVE_SESSION_STATUSES` member (active,
    paused, interrupted, awaiting_input, awaiting_approval, awaiting_handoff) refuses
    with the session ref; an ended session passes; an unresolvable ref, a missing
    roster, a malformed roster and a missing role file each refuse. A role file with
    two rows, one session ended and one live, refuses with the live ref whichever
    row comes first. test: `tests/agents/test_runbook_seats.py::test_roster_seats_and_stale_refs`.

    6.2.3: Two executions of one runbook that both reach the guard both see a live
    sibling, and at most one passes. test: `tests/agents/test_runbook_seats.py::test_concurrent_executions_admit_at_most_one`.

    6.2.4: A storage error, a truncated run query and a caller that is not a pipeline
    child session each refuse. test: `tests/agents/test_runbook_seats.py::test_uncertain_lookup_fails_closed`.

    6.2.5: Fewer free slots than requested seats refuses before any launch. test:
    `tests/agents/test_runbook_seats.py::test_capacity_shortfall_refuses`.

    6.2.6: An empty, duplicate, unknown or whitespace-padded seat name, and two seats
    with one canonical key, each refuse before any seat lookup. test: `tests/agents/test_runbook_seats.py::test_requested_seats_validated`.'
  labels:
  - covers:deploy-runbook:6.2:6.2.1
  - covers:deploy-runbook:6.2:6.2.2
  - covers:deploy-runbook:6.2:6.2.3
  - covers:deploy-runbook:6.2:6.2.4
  - covers:deploy-runbook:6.2:6.2.5
  - covers:deploy-runbook:6.2:6.2.6
  tdd: true
  source_section: '6.2'
  implementation_domain: backend
- title: Pipeline resume runs the launch-time definition
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '7.1.1: A pipeline launched through `run_pipeline` stores its
    definition snapshot, and a definition changed after launch leaves the resumed
    step graph unchanged. test: `tests/mcp_proxy/tools/test_mcp_proxy_tools_pipeline_resume.py::test_resume_uses_launch_snapshot`.

    7.1.2: A missing or malformed snapshot fails the execution on startup recovery,
    is refused by `resume_pipeline` with its steps unchanged, and runs no step. test:
    `tests/mcp_proxy/tools/test_mcp_proxy_tools_pipeline_resume.py::test_missing_snapshot_fails_closed`.'
  labels:
  - covers:deploy-runbook:7.1:7.1.1
  - covers:deploy-runbook:7.1:7.1.2
  tdd: true
  source_section: '7.1'
  implementation_domain: backend
- title: Pipeline spawns reconcile by step invocation id
  category: code
  task_type: feature
  depends_on:
  - '7.1'
  validation_criteria: '7.2.1: A step''s `invocation_id` is the same before and after
    a restart, two steps of one execution get different ids, and a step whose id is
    `invocation_id` does not override the reserved name. test: `tests/workflows/test_pipeline_invocation_id.py::test_invocation_id_is_stable_per_step`.

    7.2.2: A placed spawn from a pipeline child whose `reserved_run_id` names a started
    run returns that run with `adopted: true` and makes no placement, reserver or
    launch call, whether the seat is still live or ended, moved or was renamed. test:
    `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_started_run_is_adopted`.

    7.2.3: Two steps that spawn the same seat in sequence create two runs, and neither
    adopts the other. test: `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_distinct_steps_do_not_share_runs`.

    7.2.4: A run with the id that was prepared and never started, or failed before
    start, refuses `seat_launch_unsettled` and launches nothing. test: `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_unstarted_run_refuses`.

    7.2.5: A non-pipeline caller that names a real pipeline child as `parent_session_id`
    keeps the reviewer-internal refusal and reaches no lookup. A pipeline caller refuses
    `invocation_unauthorized` when its declared parent is another session, its execution
    is missing, not running or in another project, or the id is not one of its steps''
    invocation ids. A run with the id under another execution or another project refuses
    `invocation_conflict`. A replacement child with the same external id adopts. The
    queued task-close reviewer spawn is unchanged. test: `tests/mcp_proxy/tools/spawn_agent/test_seat_adoption.py::test_invocation_authority`.'
  labels:
  - covers:deploy-runbook:7.2:7.2.1
  - covers:deploy-runbook:7.2:7.2.2
  - covers:deploy-runbook:7.2:7.2.3
  - covers:deploy-runbook:7.2:7.2.4
  - covers:deploy-runbook:7.2:7.2.5
  tdd: true
  source_section: '7.2'
  implementation_domain: backend
- title: Cron pipeline launch requires a cron session
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '7.3.1: A cron `pipeline` job parents the pipeline child to
    the cron session, and each spawn the pipeline makes carries that child as parent
    and the job''s project. test: `tests/scheduler/test_cron_runbook_chain.py::test_cron_chain_identity`.

    7.3.2: A failed cron session create, and a missing session manager, fail the cron
    run with a typed error and create no execution. test: `tests/scheduler/test_cron_runbook_chain.py::test_cron_session_failure_refuses`.'
  labels:
  - covers:deploy-runbook:7.3:7.3.1
  - covers:deploy-runbook:7.3:7.3.2
  tdd: true
  source_section: '7.3'
  implementation_domain: backend
- title: Bundled planning council runbook
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  - '6.2'
  - '7.1'
  - '7.2'
  validation_criteria: '8.1.1: Bundled sync installs the runbook with the `gobby`
    and `runbook` tags, and it validates as a `PipelineDefinition`. file: `src/gobby/install/shared/workflows/pipelines/planning-council.yaml`.
    test: `tests/workflows/test_runbook_pipeline.py::test_runbook_syncs_bundled_and_tagged`.

    8.1.2: MCP and HTTP launches parent both seats to the pipeline child session,
    whose parent is the caller or the system session, and resolve the project. test:
    `tests/workflows/test_runbook_pipeline.py::test_entrypoint_parent_chain`.

    8.1.3: A restart between the writer''s spawn and its completed write re-runs the
    writer step on the reused pipeline child, and the second spawn carries the same
    parent and the same `reserved_run_id`. A restart after the writer completed keeps
    its `run_id`, skips the guard and launches only the adversary. test: `tests/workflows/test_runbook_pipeline.py::test_restart_reruns_on_same_child`.

    8.1.4: A partial deploy keeps the launched seat''s output. A fresh run with `seats:
    "adversary"` passes the guard and launches only the adversary, and a run that
    asks for the writer while its run is active is refused. `gobby pipelines run -i
    seats=adversary` hands the executor that string unchanged. test: `tests/workflows/test_runbook_pipeline.py::test_partial_deploy_relaunches_missing_seat`.
    test: `tests/cli/test_cli_pipelines.py::test_run_passes_seats_input_as_string`.

    8.1.5: `resume_pipeline` on a failed runbook execution refuses with the typed
    error and changes no step or output, including after the current definition lost
    its `runbook` tag, changed or was deleted. test: `tests/workflows/test_runbook_pipeline.py::test_failed_runbook_resume_refused`.

    8.1.6: `gobby pipelines runs show --json` includes each seat step''s `run_id`
    for a completed and for a failed runbook execution. test: `tests/cli/test_cli_pipelines.py::test_runs_show_includes_step_outputs`.'
  labels:
  - covers:deploy-runbook:8.1:8.1.1
  - covers:deploy-runbook:8.1:8.1.2
  - covers:deploy-runbook:8.1:8.1.3
  - covers:deploy-runbook:8.1:8.1.4
  - covers:deploy-runbook:8.1:8.1.5
  - covers:deploy-runbook:8.1:8.1.6
  tdd: true
  source_section: '8.1'
  implementation_domain: backend
- title: Runbook guide and pipeline reference
  category: docs
  task_type: chore
  depends_on:
  - '7.3'
  - '8.1'
  validation_criteria: '9.1.1: The pipelines guide documents runbooks, their guard,
    entrypoints, restart and stop. behavior: "## Runbooks" in `docs/guides/pipelines.md`.

    9.1.2: The skill reference names `check_runbook_seats`, the `seats` input, `runs
    show --json` and `kill_agent` by recorded run id. file: `src/gobby/install/shared/skills/gobby/references/pipelines/runbooks.md`.

    9.1.3: The recovery reference explains the post-restart `seat_launch_unsettled`,
    `invocation_conflict` and `seat_live` failures. behavior: "seat_launch_unsettled"
    in `src/gobby/install/shared/skills/gobby/references/pipelines/recovery.md`.

    9.1.4: The runbooks reference is a registered pipelines topic that the catalog
    lists and `get_skill_file` loads. test: `tests/skills/test_reference_library.py::test_pipelines_runbooks_topic_is_registered`.'
  labels:
  - covers:deploy-runbook:9.1:9.1.1
  - covers:deploy-runbook:9.1:9.1.2
  - covers:deploy-runbook:9.1:9.1.3
  - covers:deploy-runbook:9.1:9.1.4
  tdd: false
  source_section: '9.1'
  assigned_agent: tech-writer
```

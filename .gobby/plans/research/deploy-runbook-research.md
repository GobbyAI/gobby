# Working context: #22895 deploy_runbook plan (Plan Writer gobby#14578)

Task #22895 claimed 2026-09-25 ~19:40 UTC (id 4795c9f3). #22902 escalated
(reason: blocked by #22912), PD approved; re-claim after #22912 lands. Deliverable:
decision-complete `.gobby/plans/deploy-runbook.md`, validated, committed; no code.
Adversary gobby#14579 prep message e287e9a3 (DR-P1..P6 below). PD summary 19:3x.

## Verified facts (2026-09-25)

- `dispatch_batch` = `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry.dispatch_batch`
  (625-782; registry name at 618). Takes `suggestions` (dicts with ref/task_ref/task_id/id,
  title, optional prompt, per-row agent/provider/model/effort/isolation/worktree/clone/
  extra_write_paths overrides), refuses taskless rows and rows without prompt+title,
  synthesizes `Implement task #N: title\n\nDescription:` prompt via
  `_suggestion_task_description`, semaphore = min(len, `max_active_agents_for_project`)
  (`_spawn_guards.py:266`), `asyncio.gather`, returns `{dispatched, results:[{task_ref,
  run_id, success, agent, external_write_grant, error?}]}`. Discards `status`
  (`starting`) and `child_session_id`. No durable identity/state.
- `spawn_agent` (315-615) params incl. `reserved_run_id` (reviewer-internal, 420-432),
  `terminal_backend`, `parent_session_id`, `notify_parent_on_completion`; impl
  `_implementation.py` (964 lines; result build ~927-964 returns success/status
  starting/run_id/child_session_id). Spawn takes task dispatch mutex; refuses a second
  active run per task; outcome atomic/observable (memory ac7eb132).
- HTTP tool endpoint `router.post("/{server_name}/tools/{tool_name}")` at
  `src/gobby/servers/routes/mcp/tools.py:53`; `gobby agents spawn` posts it
  (`src/gobby/cli/agents.py:200-307`, `daemon_auth_headers`). Registry composed in
  `src/gobby/mcp_proxy/tools/agents_spawn_tools.py:83`.
- `PARENT_SESSION_TOOLS = {dispatch_batch, evaluate_spawn, spawn_agent}`
  (`mcp_proxy/services/tool_execution.py:30`, 833 lines).
- Consumers of dispatch_batch (owned): `merge-orchestrator.yaml` (prompt 65-70,
  allowlist 400, handlers 433-447 reading `results[].success/run_id`; 683 lines);
  `rules/restraint/require-restraint-skill.yaml:40`; `skills/gobby/references/agents/spawning.md:18`;
  `docs/guides/agents.md:16,276,383`; `docs/guides/workflows-overview.md:65`;
  `docs/reference-audit/agents.json:464-472` (hand-maintained audit, version 1).
  Tests: `tests/mcp_proxy/tools/test_parallel_dispatch.py` TestDispatchBatch (401
  dispatches_multiple, 438 bounds_concurrent_spawns, 493 empty, 514 partial_failure);
  `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` TestDispatchBatchIsolationParity
  (1068 honors_explicit_suggestion_contract, 1140 rejects_taskless, 1178 forwards_clone_params,
  1263 without_isolation_params); `test_worktree_reference_resolution.py:233-284`;
  `test_factory.py:302`; `tests/agents/test_merge_orchestrator_contract.py:53,975`;
  `tests/workflows/test_developer_guidance_rules.py:172` (restraint param). No web/ use.
- `list_agent_runs(parent_session_id, status, limit)` `agents_query_tools.py:739` (1004 lines: at ceiling, do not grow).
- agent_runs columns (baseline.sql:1827): id, machine_id, parent_session_id, child_session_id,
  agent_name, provider, model, status, prompt, task_id, worktree_id, clone_id, terminal_id,
  resume_metadata_json, ... no metadata/label column for a deployment marker.
- Latest migration 451 (`451_drop_comms_routing_rules.sql`); next is 452. Migration 450
  commit touched: catalog.manifest.json, bundle.rs, schema/assets.rs, verify.rs,
  verify_tests.rs, tests/schema_contract.rs, gdaemon/tests/cli_contract.rs,
  storage/schema_expected_identity.json (derived-carriers table in plan-coverage.md).
- Workspace layout: `storage/workspaces.py` (973 lines, ceiling) LayoutLeaf/LayoutSplit
  JSON, `_place`, `mint_pane_id`, WorkspaceTab/WorkspacePane classes; ops in
  `terminals/workspace_ops.py` (tab_create 372, pane_split 460, pane_close 550);
  WS op table `servers/websocket/workspace_ws.py` WORKSPACE_OPS; `_identity_env` sets
  GOBBY_PANE_REF etc. No role/runbook columns landed (#22695 pins no such fields).
- gclient command mode (#22695, closed 17:40): verbs list,new-tab,split,resize,title,
  select,send-keys,capture-pane,wait-for-output,kill,help in `crates/gclient/src/command.rs`
  + `command/verbs.rs`; `launch` omitted until #22904. #22904 (placed launch plan, open,
  unclaimed, Researcher) owns `placement` on spawn + tab_ref/pane_ref reply. #22903
  (apply_agent_definition plan, open) owns activation of existing sessions.
- Retired runbooks plan `.gobby/plans/runbooks.md` (3002 lines, commit 22a5d4b077):
  decisions 12/14/20 (scripts of gclient verbs; launch via spawn_agent with placement +
  definition sandbox; program-director unsandboxed; no launch line may disable sandbox);
  §2.1 scripts stopped at first failure with CREATED_TAB= lines, no rollback. Josh
  abandoned the scripts (task text); #22899 owns sandbox_profile (definition-only).
- Sizes: _factory.py 784, _spawn_guards.py 408, cli/agents.py 736, agents.py 58,
  routes/mcp/tools.py 74, test_initial_variables.py 1319 (test), merge contract 1193 (test).
- Josh: no new runner telemetry (use OTLP); no gobby build compatibility (memory 4644eba6).

## Adversary decision pressures (e287e9a3)
DR-P1 identity/input (versioned, stable slot ids, seat vs task, no second definition
schema); DR-P2 preflight/authority (no-spawn seat rule must name the new tool; caller vs
parent_session_id; cross-project decision); DR-P3 lifecycle accepted/starting/ready/failed,
durable ids, query/wait, who owns launch->placement->activation (#22904/#22903 as typed
deferrals); DR-P4 retry/concurrency (same key same/changed content, concurrent callers,
disconnect, restart mid-launch, foreign pane; replay finds owned deployment); DR-P5
fail-before-start, retry-failed-only vs redeploy, cancel, capacity via existing admission
(reserve_agent_slot, TaskSpawnLease, active_task_response_if_blocked in _spawn_guards);
DR-P6 retire each consumer explicitly.

## Design of record

Written into `.gobby/plans/deploy-runbook.md` (validates clean, 2026-09-25). The plan's
Decision Record (16 decisions) is the authority; this file keeps the verified facts and
the round history only.

Advisor decisions encoded (2026-09-25 pre-draft review): seat runs exempt from the
active-agent cap via `_count_active_agents` NOT EXISTS on `runbook_slots.kind='seat'`;
preflight hard-refuses only the static case (task slots > cap), dry_run reports headroom,
live admission fails slots into `partial`; restart mid-launch reconciled on read from an
in-memory launching set (no clock, no sweep); spawn seam = `InternalToolRegistry.call
("spawn_agent", ...)` with explicit `parent_session_id`/`project_path`;
`_suggestion_task_description` (only caller dispatch_batch at `_factory.py:716`) moves
into the service; slot isolation none|worktree|clone; MCP inline document only, CLI reads
`.gobby/runbooks/<name>.yaml`, no bundled runbooks dir; no gclient verb (Decision 14);
`deploy_runbook` + `retry_runbook_deployment` in EXTENDED_TIMEOUT_TOOL_NAMES;
PARENT_SESSION_TOOLS swaps dispatch_batch -> deploy_runbook in 5.1; executor confirms
schema/assets.rs, verify.rs, verify_tests.rs via read-only `gdaemon schema plan`; seat
redeploy after restart is Josh via CLI.

Verified this epoch: `agent_slot_cap_refusal` (`_spawn_guards.py:274-298`) compares
`_count_active_agents(db, project_id)` with the cap and reports the caller's share;
`InternalToolRegistry.call(name, arguments, context=None)` awaits async tool funcs and
runs sync ones in a thread (`internal.py:290`); `AgentsRegistryContext` is
`agents_context.py:30-58`; `create_agents_registry` kwargs start at `agents_registry.py:35`;
`registries.py:91` carries `workspace_ops_resolver`; existing tests
`tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py`, `tests/mcp_proxy/test_wait_tools.py`,
`tests/mcp_proxy/test_registries.py`, `tests/storage/test_workspaces.py`,
`tests/cli/test_cli_agents.py`.

Cross-plan obligation: #22902 draft (`/Users/josh/.claude/plans/tidy-inventing-flute.md`)
2.2 `seat-no-spawn` block list must add `gobby-agents:deploy_runbook`.

Round history: advisor pass 2026-09-25 applied eight repairs (preflight drops task-state codes, seats admitted like any spawn, merge-orchestrator deployment-id handler and lifecycle allowlist, workspace_required, project resolution for CLI callers, PARENT_SESSION_TOOLS split across 4.1/5.1, --wait via wait_for_agent, 1.1.1 test symbol); Adversary round 1 pending.

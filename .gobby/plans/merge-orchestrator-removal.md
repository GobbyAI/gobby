Plan artifact: `.gobby/plans/merge-orchestrator-removal.md`

# Merge-orchestrator removal with no replacement merge orchestration

**Plan ID:** merge-orchestrator-removal

## Context
`kind: framing`

Josh's spawn network override plan (saved under #23436, **Write and validate the
per-spawn SRT network profile plan**) states: "retire batch spawning only after its
separate removal is installed and confirmed in the DB registry. Do not migrate its
orchestration." and "Do not move merge orchestration to another path." This plan
owns that separate removal. Its downstream dependent is deferred section D1, **Batch
spawn retirement**, of `.gobby/plans/spawn-network-override.md`: D1 stays on hold
until this plan's removal is installed and evidenced.

Live state on 2026-10-05: `uv run gobby agents show merge-orchestrator` prints an
installed, enabled global row. The dispatcher spawns it for the `pr` stage
(`pr_work_rule`), for the `merge` stage when no deterministic workspace merge
applies (`merge_rule`), and for workspace-merge conflicts (the
`merge-recovery:workspace-conflict` label). Its prompt dispatches `merge-worker`
through `spawn_agent` or `dispatch_batch`.

End state:

- `pr` stage: the dispatcher submits the stage for review itself. The
  trajectory-monitor review gate from #17654, **require independent PR trajectory
  review**, is unchanged.
- `merge` stage: the existing deterministic workspace merge lands every task that
  has a workspace, root tasks included. A task built with `isolation: none` has
  nothing to land, so the dispatcher completes the stage. Every other missing
  source escalates.
- Workspace-merge conflict: the merge stage fails with `needs_human=True` and the
  conflicted files in the reason. No agent is spawned.
- `merge-worker` stays as a standalone, single-workspace agent that an operator
  or seat may spawn. `record_pr_verdict` stays as an operator tool.

None of these paths is merge orchestration. The `pr` change is a stage-state
transition; the root-task change widens the source check of a deterministic git
merge that already lands root integration workspaces (#14360, **root epic merge
stage must land local integration workspace**); the conflict and missing-source
paths hand the task to a human.

The installed DB row is removed by bundled sync, not by hand: `sync_bundled_agents`
(`src/gobby/agents/sync.py`) soft-deletes every Gobby-tagged installed agent row
whose definition is missing from the bundled root. The sweep runs on the next
`gobby sync` or daemon start from the main checkout after deliverable 2.1 lands.
The Orchestrator owns that restart.

Historical records stay untouched: `.gobby/plans/completed/*`,
`docs/reference-audit/*`, `docs/evidence/*`, `docs/design/task-authority-audit.md`,
and `docs/plans/cross-repo-submit-profile.md` describe past states. The schema
baseline seeds in `crates/gcore/assets/schema/baseline.sql` and
`crates/gcore/assets/schema/seed.manifest.json` keep `default_agent:
merge-orchestrator` on the `pr` and `merge` rows: `is_live_mutable_seed_field`
(`crates/gcore/src/schema/verify.rs`) lists `task_stages_registry.default_agent`,
`description`, and `bundled_hash` as live-mutable, and startup stage-registry sync
(`src/gobby/storage/tasks/_stage_registry_loader.py`) rewrites the rows from
`stages.yaml`. No schema carrier or crate rebuild is needed.

## Decision Record
`kind: framing`

Orchestrator rulings of 2026-10-05 on the writer's recommendations:

1. `pr` stage: keep the stage; the dispatcher submits it for review. Rejected
   alternative: retire the `pr` stage entirely (stage row, `--pr` build flag, the
   `submit` and `fix-merge` profiles, `record_pr_verdict`, trajectory-monitor).
   Rejected because the stage still carries the independent trajectory review Josh
   required in #17654, and retiring it changes a delivery gate and the build
   surface, which is outside a removal. The Orchestrator puts this alternative to
   Josh at approval so he can flip it.
2. Workspace-merge conflict: fail the merge stage with `needs_human=True` and
   delete the recovery label. Rejected alternative: the dispatcher spawns
   `merge-worker` for the conflict, which is the replacement orchestration Josh
   excluded.
3. Root task with only its own `worktree_id` or `clone_id`: land it with the
   existing deterministic merge. Rejected alternative: escalate every root-leaf
   merge, which turns every isolated root build into manual work although the
   same local-landing path already serves root integration workspaces.
4. Merge stage with no workspace: complete the stage when the task's isolation is
   `none`; escalate any other missing source. Rejected alternative: drop `merge`
   from isolation-none manifests. Only a completed `merge` stage cascades
   descendant closure (`cascade_descendants=stage_name == "merge"` in
   `src/gobby/storage/tasks/_stage_state_transitions.py`), so removing the stage
   would leave parked leaves open.
5. `merge-worker` and `record_pr_verdict` stay.

## P1: Agentless PR and merge stages
`kind: framing`

The dispatcher works both delivery stages without spawning an agent.

### 1.1 PR stage submits for review without an agent [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/registry/stages.yaml::stages`
- `src/gobby/dispatch/rules.py::pr_work_rule`
- `src/gobby/dispatch/actions.py::AdvanceStageAction`
- `src/gobby/dispatch/dispatcher.py::execute_action`
- `src/gobby/dispatch/stage_advance.py`
- `tests/dispatch/test_stage_advance.py`
- `tests/dispatch/test_pr_rules.py::*` — scope-reason: replace merge-orchestrator spawn and no-agent cases with the submit action
- `tests/dispatch/test_rules.py::*` — scope-reason: replace pr routing cases that expect merge-orchestrator
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: replace the pr heartbeat spawn test and the pr registry assertion
- `tests/dispatch/test_delivery_chain.py::*` — scope-reason: drive the pr segment through the submit action
- `tests/dispatch/test_pr_full_walk.py::*` — scope-reason: the fake registry's pr row has no default agent

Consumers unchanged:
- `src/gobby/build/observability.py` — no-edit-reason: reads AdvanceStageAction fields that keep their names and types.
- `tests/dispatch/test_actions_surface.py` — no-edit-reason: asserts the action class is exported, which stays true.

Change `pr_work_rule` in `src/gobby/dispatch/rules.py`: when the current stage is
`pr` in state `in_progress`, return
`AdvanceStageAction(task_id=..., stage_name="pr", method="submit_for_review")`.
Keep the existing work-attempt exhaustion check ahead of it (escalate
`pr_max_work_attempts` when the stage's work budget is spent). Drop the
`agent_slug`, `has_agent`, and `pr_no_agent` arguments. In
`src/gobby/dispatch/actions.py`, add `"submit_for_review"` to the
`AdvanceStageAction.method` `Literal`.

Move the `AdvanceStageAction` branch of `execute_action` out of
`src/gobby/dispatch/dispatcher.py` (857 lines) into the new module
`src/gobby/dispatch/stage_advance.py`. The new module holds one synchronous
function, `advance_stage(manager: StageStatesManager, action: AdvanceStageAction)
-> StageState`, that maps `complete_stage`, `approve_review`, and
`submit_for_review` to the matching `StageStatesManager` method with
`by_session_id=action.by_session_id` (and `validation_override_reason` for
`complete_stage`) and raises `ValueError` on an unknown method. `execute_action`
keeps the mutex release and `_stage_states_manager` lookup, then returns
`await run_db(advance_stage, manager, action)`. The function takes the manager
as an argument, so the new module never imports `dispatcher.py`.

In `src/gobby/install/shared/registry/stages.yaml`, remove `default_agent:
merge-orchestrator` from the `pr` row and set its description to "Independent
trajectory review of the delivered work before merge." Keep `reviewer_agent:
trajectory-monitor` and `review_policy: required`.

**Research context:** `pr` is already in `_AUTO_ADVANCE_NON_AGENT_STAGES` and
`_DISABLED_AGENT_EXCLUDED_STAGES` (`src/gobby/dispatch/rules.py`), so
`auto_advance_ready_rule` starts it without a default agent and
`disabled_agent_escalation_rule` skips it. The stage registry loader
(`_stage_registry_loader.py`) and `_stage_registry.py` already exempt `pr` from
the reviewer requirement and accept a row without `default_agent` (the
`expansion` row has none). `StageStatesManager.submit_for_review`
(`src/gobby/storage/tasks/_stage_states.py`) takes `by_session_id`; the dispatcher
uses `"dispatcher"`, as `StartStageAction` does. After submission,
`pr_review_rule` spawns trajectory-monitor and `pr_advance_rule` completes the
stage on approval; neither changes. A rejection returns the stage to `ready`
(`reject_review` in `_stage_state_transitions.py`); the next heartbeat restarts
and resubmits it, and the storage cap escalates `pr_review_failed:max` at the
review-round limit. This is the same loop merge-orchestrator ran, without an
agent turn. The merge-orchestrator's other `pr` work was
`probe_branch_protection` "for awareness" and `record_pr_verdict(approve)`, which
is a no-op on a stage with a reviewer agent; neither is carried forward.
Consumer sweep (`grep -rlw` over `src tests`): `pr_work_rule` is used in
`rules.py`, `test_delivery_chain.py`, `test_pr_rules.py`, `test_rules.py`;
`execute_action` in `dispatcher.py` and `test_dispatcher.py`;
`AdvanceStageAction` in the files listed in Targets and Consumers unchanged.
`tests/dispatch/test_dispatcher.py::test_build_context_loads_stage_registry_and_bundled_agents`
asserts the `pr` row's `default_agent`; replace that assertion with
`default_agent is None`. Replace
`test_real_heartbeat_pr_stage_spawns_merge_orchestrator_without_false_no_agent`
with a heartbeat test that moves an `in_progress` `pr` stage to `needs_review`
with no spawn. In `test_rules.py`, replace `test_pr_rule_routes_to_merge_orchestrator`
and `test_pr_rule_escalates_when_merge_orchestrator_missing`, drop the `"pr"`
entry from the stage-agent mapping near the top of the file, and change the
`BASE_RULES` pr row of `test_review_dispatch_remains_single_existing_agent_per_stage`
to expect the submit action. Planned checks:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/dispatch/test_stage_advance.py tests/dispatch/test_pr_rules.py tests/dispatch/test_rules.py tests/dispatch/test_dispatcher.py tests/dispatch/test_delivery_chain.py tests/dispatch/test_pr_full_walk.py -q`,
then `uv run ruff check`, `uv run ruff format --check`, and `uv run mypy src/` on the
changed files.

**Acceptance:**

- 1.1.1 - An `in_progress` `pr` stage yields a `submit_for_review` advance action and never a spawn. test: `tests/dispatch/test_pr_rules.py::test_pr_work_submits_stage_for_review`.
- 1.1.2 - The dispatcher executes `submit_for_review`, moving the `pr` stage to `needs_review`. test: `tests/dispatch/test_stage_advance.py::test_advance_stage_submits_for_review`.
- 1.1.3 - A real heartbeat takes an `in_progress` `pr` stage to `needs_review` without spawning an agent. test: `tests/dispatch/test_dispatcher.py::test_real_heartbeat_pr_stage_submits_for_review_without_agent`.
- 1.1.4 - The bundled `pr` row has no `default_agent` and keeps trajectory-monitor as its required reviewer. file: `src/gobby/install/shared/registry/stages.yaml`.

### 1.2 Merge stage lands, completes, or escalates without an agent (depends: 1.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/registry/stages.yaml::stages`
- `src/gobby/dispatch/rules.py::merge_rule`
- `src/gobby/dispatch/rules.py::auto_advance_ready_rule`
- `src/gobby/dispatch/rules.py::_AUTO_ADVANCE_NON_AGENT_STAGES`
- `src/gobby/dispatch/_rule_merge.py::_workspace_merge_action`
- `src/gobby/dispatch/_rule_merge.py::_has_workspace_merge_source`
- `src/gobby/dispatch/_rule_state.py::_has_merge_agent`
- `src/gobby/dispatch/_rule_actions.py::_STAGE_AGENT_SLUGS`
- `src/gobby/dispatch/_rule_actions.py::_spawn_required_stage_agent`
- `src/gobby/dispatch/workspace_merge.py::_execute_merge_workspace`
- `src/gobby/dispatch/workspace_merge.py::_mark_workspace_merge_conflict_for_recovery`
- `src/gobby/dispatch/merge_recovery.py::*` — operation: delete — scope-reason: retire the conflict-recovery label module
- `tests/dispatch/test_merge_rule.py::*` — scope-reason: replace merge-agent spawn and no-agent cases
- `tests/dispatch/test_rules.py::*` — scope-reason: replace merge routing, conflict-label, and root-merge cases
- `tests/dispatch/test_workspace_merge.py::*` — scope-reason: assert conflict escalation without the recovery label
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: replace the merge heartbeat spawn test and the disabled-agent override fixture
- `tests/dispatch/test_delivery_chain.py::*` — scope-reason: drive the merge segment through the deterministic paths
- `tests/dispatch/test_pr_to_merge_advance.py::*` — scope-reason: the fake registry's merge row has no default agent
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — scope-reason: the merge stage no longer spawns merge-orchestrator

Rewrite `merge_rule` in `src/gobby/dispatch/rules.py` for a `merge` stage in state
`in_progress`, in this order:

1. `_workspace_merge_action` returns an action: return it.
2. `_isolation(task) == "none"`: return
   `AdvanceStageAction(task_id=..., stage_name="merge", method="complete_stage")`.
   Nothing needs landing; completing the stage closes the task with
   `manifest_exhausted` and cascades descendant closure.
3. Otherwise: return `EscalateAction(task_id=..., reason="merge_no_workspace_source")`.

Add `"merge"` to `_AUTO_ADVANCE_NON_AGENT_STAGES` and delete the
`stage_name == "merge" and _has_workspace_merge_source(...)` branch from
`auto_advance_ready_rule`, which the set membership replaces; without this a
`merge` stage with no default agent never leaves `ready`. Remove the
`_has_merge_agent` import and use from `rules.py`, delete `_has_merge_agent` from
`src/gobby/dispatch/_rule_state.py` and its `__all__`, delete the
`("merge", "in_progress")` entry from `_STAGE_AGENT_SLUGS`, and delete
`_spawn_required_stage_agent` from `src/gobby/dispatch/_rule_actions.py` and its
`__all__` and import, because deliverable 1.1 removed its other caller.

In `src/gobby/dispatch/_rule_merge.py`, drop the recovery-label check from
`_workspace_merge_action`, and make `_has_workspace_merge_source` accept
`worktree_id` and `clone_id` for root tasks as it already does for child tasks
(remove the `has_parent` split). In `src/gobby/dispatch/workspace_merge.py`,
replace both `_mark_workspace_merge_conflict_for_recovery` calls in
`_execute_merge_workspace` with
`_append_merge_failure_audit(db, action.task_id, reason)` followed by
`_fail_merge_stage(db, action.task_id, reason)` (default `needs_human=True`),
delete `_mark_workspace_merge_conflict_for_recovery`, and drop the
`WORKSPACE_MERGE_CONFLICT_LABEL` import. Delete `src/gobby/dispatch/merge_recovery.py`.

In `src/gobby/install/shared/registry/stages.yaml`, remove `default_agent:
merge-orchestrator` from the `merge` row and set its description to "Land the
task workspace into its target branch; close the terminal task."

**Research context:** `_workspace_merge_action` already builds
`MergeWorkspaceAction` from integration or own workspace artifacts.
`_resolve_paths` (`workspace_merge.py`) resolves a root task's own worktree or
clone: `ensure_task_parent_integration_workspace` (`src/gobby/build/workspaces.py`)
returns `None` for a task with no open ancestor epic, so the existing
`_local_target_path_if_checked_out` fallback lands into the main checkout when the
target branch is checked out there, the same local landing #14360 added for root
integration workspaces; otherwise it raises and `_execute_merge_workspace` fails
the stage with `workspace_merge_failed` and `needs_human=True`.
`_ensure_target_merge_safe` and `_recover_stale_merge_state` guard the target as
before. `_isolation` is defined in `_rule_state.py` and already used by
`development_isolation_rule`. `fail_stage` with `needs_human=True` escalates
through `StageStatesManager`; a human resolves the conflict and records the result
with `gobby-tasks-ops:record_merge_result`. `_close_task_in_txn` cascades
descendants only when the completing stage is `merge`
(`_stage_state_transitions.py`). Consumer sweep (`grep -rlw` over `src tests`):
`merge_rule` in `rules.py`, `test_delivery_chain.py`, `test_merge_rule.py`,
`test_pr_rules.py` (rule-order list only, unchanged), `test_rules.py`;
`auto_advance_ready_rule` in `rules.py`, `test_delivery_chain.py`,
`test_pr_full_walk.py`, `test_pr_to_merge_advance.py`, `test_rules.py`,
`test_rules_stage_native.py`; `_workspace_merge_action` and
`_has_workspace_merge_source` in `_rule_merge.py` and `rules.py`;
`_has_merge_agent` in `_rule_state.py` and `rules.py`;
`_spawn_required_stage_agent` in `_rule_actions.py` and `rules.py`;
`_mark_workspace_merge_conflict_for_recovery` in `workspace_merge.py` only;
`WORKSPACE_MERGE_CONFLICT_LABEL` in `_rule_merge.py`, `workspace_merge.py`,
`test_rules.py`, `test_workspace_merge.py`. Escalation reasons `merge_no_agent`
and `pr_no_agent` appear only in `rules.py` and tests. Tests to replace in
`test_rules.py`: `test_workspace_merge_conflict_label_routes_to_merge_orchestrator`,
`test_root_merge_still_routes_to_merge_orchestrator`,
`test_merge_rule_routes_on_merge_stage`, `test_merge_rule_escalates_when_merge_agent_missing`,
the `RULES` merge row of `test_review_dispatch_remains_single_existing_agent_per_stage`,
and the `"merge"` entry of the stage-agent mapping. In `test_dispatcher.py`,
replace `test_real_heartbeat_merge_ready_starts_then_spawns_merge_orchestrator`
and repoint `test_build_context_project_disabled_agent_override_wins` at
`trajectory-monitor`. Planned checks:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/dispatch/test_merge_rule.py tests/dispatch/test_rules.py tests/dispatch/test_workspace_merge.py tests/dispatch/test_dispatcher.py tests/dispatch/test_delivery_chain.py tests/dispatch/test_pr_to_merge_advance.py tests/e2e/test_build_dispatcher_autonomy.py -q`,
then ruff, format check, and mypy on the changed files.

**Acceptance:**

- 1.2.1 - A `ready` `merge` stage whose previous stage is done starts without a default agent. test: `tests/dispatch/test_rules.py::test_merge_stage_auto_starts_without_agent`.
- 1.2.2 - A root task with only its own `worktree_id` gets a `MergeWorkspaceAction`. test: `tests/dispatch/test_rules.py::test_root_own_worktree_uses_workspace_merge_action`.
- 1.2.3 - A merge stage of an `isolation: none` task with no workspace yields `complete_stage`. test: `tests/dispatch/test_merge_rule.py::test_merge_rule_completes_isolation_none_stage`.
- 1.2.4 - A merge stage with isolation and no usable workspace source escalates `merge_no_workspace_source`. test: `tests/dispatch/test_merge_rule.py::test_merge_rule_escalates_missing_workspace_source`.
- 1.2.5 - A workspace merge conflict fails the stage with `needs_human=True`, records the conflicted files, adds no label, and spawns nothing. test: `tests/dispatch/test_workspace_merge.py::test_conflict_fails_merge_stage_for_human`.
- 1.2.6 - A real heartbeat lands an isolated child task's merge stage without spawning an agent. test: `tests/dispatch/test_dispatcher.py::test_real_heartbeat_merge_lands_workspace_without_agent`.
- 1.2.7 - The bundled `merge` row has no `default_agent`, and `merge_recovery.py` is gone. file: `src/gobby/install/shared/registry/stages.yaml`.

## P2: Definition retirement
`kind: framing`

With no stage spawning it, the definition and its references go.

### 2.1 Retire the merge-orchestrator definition and its references (depends: 1.1, 1.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — operation: delete — scope-reason: retire the bundled definition
- `src/gobby/install/shared/workflows/agents/merge-worker.yaml::*` — scope-reason: rewrite the top-level description to describe the worker as standalone
- `src/gobby/dispatch/prompts.py::PROMPT_BUILDERS`
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::*` — scope-reason: delete the parent-orchestrator exemption and its call site
- `src/gobby/mcp_proxy/tools/tasks/_dispatch_mutex_release.py::_current_agent_mutex_owner`
- `src/gobby/mcp_proxy/tools/merge_landscape.py::*` — scope-reason: rewrite the module docstring and two tool descriptions, and move the verification-command helpers out
- `src/gobby/mcp_proxy/tools/merge_verification.py`
- `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md`
- `src/gobby/dispatch/AGENTS.md`
- `tests/agents/test_merge_orchestrator_contract.py::*` — operation: delete — scope-reason: the definition it pins is gone
- `tests/integration/test_merge_orchestrator.py::*` — operation: delete — scope-reason: rename to the landscape-primitives file below
- `tests/integration/test_merge_landscape_primitives.py`
- `tests/agents/test_merge_lifecycle.py::*` — scope-reason: delete the two merge-orchestrator definition tests
- `tests/dispatch/test_dispatch_prompts.py::*` — scope-reason: drop merge-orchestrator from the builder inventory
- `tests/dispatch/test_rules_stage_native.py::*` — scope-reason: drop the deleted YAML from the scanned list
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py::*` — scope-reason: replace the parent-orchestrator exemption test with a duplicate-refusal test
- `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::*` — scope-reason: delete the parent-orchestrator mutex release test
- `tests/skills/test_removed_wait_tool_guidance.py::*` — scope-reason: drop the deleted YAML parameter
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: drop the merge-orchestrator mapping entry
- `tests/workflows/test_spawn_scope_rules.py::*` — scope-reason: delete the bundled merge-orchestrator spawn-scope test
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: delete the merge-orchestrator no-work close test

Consumers unchanged:
- `src/gobby/dispatch/_planning_enhancement.py` — no-edit-reason: looks up planning-enhancer builders only; removing the merge-orchestrator key does not affect it.

Delete `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`. Remove
the `"merge-orchestrator"` entry from `PROMPT_BUILDERS` in
`src/gobby/dispatch/prompts.py`; keep `"merge-worker": _merge_runner`. In
`src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py`, delete
`_is_parent_merge_orchestrator_run` and the loop branch that skips it, so a second
active run on the same task is always reported. In
`src/gobby/mcp_proxy/tools/tasks/_dispatch_mutex_release.py`, delete the second
query in `_current_agent_mutex_owner` (the owner/child join on
`merge-orchestrator` and `merge-worker`) and return the direct-run result.

Rewrite the `merge-worker.yaml` description to say it lands one worktree or clone
into its target branch when an operator or seat spawns it; drop "the
merge-orchestrator dispatches". In `merge-campaigns.md` step 3, keep "Dispatch the
installed `merge-worker` for one workspace" and delete the campaign clause. In
`src/gobby/dispatch/AGENTS.md`, replace "PR/merge stages use the merge-orchestrator
and local git merge tools." with "The dispatcher submits the PR stage for review
and lands the merge stage with a deterministic local git merge; conflicts and
missing workspaces escalate to a human."

Split `src/gobby/mcp_proxy/tools/merge_landscape.py` (907 lines): move the
verification-command helpers (`_verification_environment`, `_is_env_assignment`,
`_reject_verification_env_key`, `_consume_env_assignments`,
`_normalize_verification_command`, `_reject_git_verification_args`,
`_looks_like_test_execution`, `_has_test_scope`, `_reject_unscoped_test_command`,
`_reject_verification_command`) into the new module
`src/gobby/mcp_proxy/tools/merge_verification.py`. Expose the three entry points
that `verify_in_worktree` calls as public names `verification_environment`,
`normalize_verification_command`, and `reject_verification_command`; the rest stay
private there. Then rewrite the `merge_landscape.py` module docstring and the
`analyze_merge_landscape` and `verify_in_worktree` descriptions without the
removed agent ("Used to survey a merge campaign before planning", "Used as a
post-merge verification gate").

Rename `tests/integration/test_merge_orchestrator.py` to
`tests/integration/test_merge_landscape_primitives.py` with `git mv`, delete
`test_orchestrator_yaml_loads`, and rewrite the module docstring. Delete
`tests/agents/test_merge_orchestrator_contract.py`.

**Granularity:** This section has more than six Target files because retiring one
definition must remove its executable references, guidance, and tests in one
change; split leaves would each leave the tree advertising or testing a deleted
agent. The `merge_landscape.py` split is the size lint's required move, kept here
because the same file needs the description edits.

**Research context:** Sync removal: `sync_bundled_agents` soft-deletes installed
Gobby-tagged rows missing from disk (`manager.delete(existing.id,
sync_orphan=True)`). `merge-worker.yaml` names `merge-orchestrator` only in its
description; its `spawnable_agents`, steps, and tools do not depend on it.
`_is_parent_merge_orchestrator_run` and the `_current_agent_mutex_owner` join are
the only code paths keyed to the orchestrator/worker parent-child pair; a
`merge-worker` spawned by an operator holds no dispatcher mutex, so the direct-run
query covers it. `merge_landscape.py` helpers are used only inside that file
(`grep -rlw` over `src tests`); `tests/mcp_proxy/tools/test_merge_landscape.py`
imports only `_active_merge_resolution_payload` and
`register_merge_landscape_tools`, and patches only
`gobby.mcp_proxy.tools.merge_landscape.os.killpg`, which stays valid because
`verify_in_worktree` stays. `src/gobby/hooks/_normalization_bindings.py` and
`_normalization_shell.py` define their own `_is_env_assignment` and are unrelated.
`tests/workflows/test_agent_workflow_completion.py` uses
`workflow_name="merge-orchestrator-test"` as an arbitrary fixture string and needs
no edit. Literal sweep for the closing check:
`gcode grep -E "merge[-_ ]orchestrator" src tests -m 200` must return no active
hits. Planned checks:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_merge_landscape.py tests/integration/test_merge_landscape_primitives.py tests/agents/test_merge_lifecycle.py tests/dispatch/test_dispatch_prompts.py tests/dispatch/test_rules_stage_native.py tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py tests/mcp_proxy/tools/tasks/test_record_merge_result.py tests/skills/test_removed_wait_tool_guidance.py tests/workflows/test_planner_grammar_prompt.py tests/workflows/test_spawn_scope_rules.py tests/workflows/test_workflows_agent_definitions.py -q`,
then ruff, format check, and mypy on the changed files.

**Acceptance:**

- 2.1.1 - The bundled merge-orchestrator definition and its prompt builder are gone; the installed-row removal is evidenced in section 3. test: `tests/dispatch/test_dispatch_prompts.py::test_merge_orchestrator_prompt_builder_absent`.
- 2.1.2 - A second active run on the same task is refused even when the requester is `merge-worker`. test: `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py::test_merge_worker_spawn_refuses_active_same_task_run`.
- 2.1.3 - `verify_in_worktree` still rejects unscoped and git verification commands after the helper move. test: `tests/mcp_proxy/tools/test_merge_landscape.py::test_verify_in_worktree_rejects_unscoped_pytest`.
- 2.1.4 - Active source, tests, guidance, and the merge-worker description contain no merge-orchestrator reference. behavior: "no active merge-orchestrator references" in `src/gobby/dispatch/AGENTS.md`.

## 3 End-to-end verification
`kind: verification`

After all three deliverables (1.1, 1.2, 2.1) land on 0.5.0, the Orchestrator runs
its coordinated restart from the main checkout. Then:

- `uv run gobby agents show merge-orchestrator` reports no active installed row.
  This is the evidence spawn-network-override D1 waits for; the Orchestrator lifts
  D1's hold only after it.
- `uv run gobby sync` or startup logs show the stage registry rewrote the `pr` and
  `merge` rows without `default_agent`.
- `gcode grep -E "merge[-_ ]orchestrator" src tests -m 200` returns no hits.
- The focused pytest commands in each deliverable pass against the isolated test hub.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05, Plan Writer gobby#15429 draft. The Orchestrator gobby#14972 accepted
  the five recommendations recorded in the Decision Record and will put the
  rejected `pr`-stage retirement alternative to Josh at approval.

# apply_agent_definition: Full Definition Activation For Interactive Sessions

Plan artifact: `.gobby/plans/apply-agent-definition.md`

**Plan ID:** apply-agent-definition

## Overview
`kind: framing`

Task #22903 (Plan apply_agent_definition activation contract) under epic #22691
(Agent definitions and deploy_runbook: incremental planning and delivery).

Josh's requirement of record, verbatim: "apply_persona doesn't apply rules or
workflows, apply_agent_definition will". This plan builds
`gobby-agents:apply_agent_definition`. It activates every orchestration field of
an agent definition on the caller's interactive session:
- identity and prompt;
- rules and skills;
- variables;
- tool blocks;
- the step workflow.

The activation survives compaction, resume and `/clear`. `apply_persona` is
deleted with no alias, and every caller migrates. This is a functional
expansion, not a rename.

Josh's execution-model ruling governs the target (memory 556ec801). Every session
is an interactive agent in a pane except one-shots, and every agent must be able
to execute a step workflow. The spawned-only step gates are as-is gaps, not
non-goals.

The #22902 plan (`.gobby/plans/agent-definition-profiles.md`) owns what a seat
definition declares. This plan owns how a session receives, rehydrates and
enforces it (#22902 Constraints, boundary paragraph).

## Decision Record
`kind: framing`

1. **One activation tool, self-session only.**
   - Tool: `gobby-agents:apply_agent_definition(agent, variables=None,
     task_id=None)`.
   - It acts on the caller's own session, resolved from session context. It
     takes no `session_id` parameter, because a session is bound to its pane.
   - The Python implementation takes an explicit `session_id` for one
     in-process caller: the web-chat launch, which activates a session it has
     just created.
   - Rejected: PD-targeted activation of another session. No flow in #22902,
     #22904 or #22895 needs it. It would also change another agent's rules and
     tool blocks mid-turn with no receipt.
   - `apply_persona` and its module are deleted, with no alias (AGENTS.md rule
     10).
2. **The delta is the SessionStart delta.**
   - Activation reuses `build_persona_changes`, renamed `build_definition_changes`
     and moved to the new module.
   - It writes, in one `SessionVariableManager.merge_variables` call:
     - `_agent_type`, `_active_rule_names`, `_active_skill_names` and
       `_skill_format`;
     - `_agent_blocked_tools` and `_agent_blocked_mcp_tools`, always written, as
       an empty list when the definition declares none;
     - `is_spawned_agent=False`;
     - `workflows.variables` and selector-filtered defaults;
     - the reinjection flags;
     - the pin (Decision 4);
     - the lifecycle declaration (Decision 9).
   - Why this closes the gaps:
     - `RuleEngine` resolves selectors from `_agent_type`, so writing it is what
       makes rules follow the seat.
     - `resolve_agent_name` returns a stored non-default `_agent_type`, so every
       later SessionStart re-activation resolves the same seat. That closes the
       compaction hazard: today a persona session reverts to the `default`
       skills on its first compact.
   - Rejected: a second, narrower persona delta. That is today's
     `build_session_persona_changes`, and it is the source of the hazard.
3. **`_persona_name` is retired.**
   - There are two readers. `_agent.py::_inject_agent_instructions_if_needed`
     chooses the prompt, and `skills/discovery.py::get_session_skill_exclusions`
     chooses skill exclusions. Both read `_agent_type`.
   - The prompt surface stays keyed on `is_spawned_agent`: `persona` for
     interactive sessions, `agent` for spawned ones.
   - Rejected: keeping `_persona_name` as a mirror of `_agent_type`. Two
     variables for one identity is the drift this plan removes.
4. **Pin and drift.**
   - Activation stores the pin `_agent_definition_hash`. Its value is
     `compute_definition_hash(agent_body.model_dump_json())` over the resolved
     body it applies, which is the same form `template_hashes.py` uses for agent
     templates. The resolved body is stable for a session because its CLI
     source does not change.
   - The pin is identity, not a freeze. On any later activation of the same seat
     (SessionStart on compact, resume or a `/clear` successor) with a different
     hash:
     - the current row applies;
     - the new hash is stored;
     - one drift line is injected into the next turn.
   - A running step instance keeps its snapshot until its unit of work ends.
   - #22902 D4's human `version` marker is not part of the pin, because hash
     identity is sufficient. That also leaves no external dependency on #22902
     1.1.
   - Rejected: refusing on drift. A bundled sync or `reload_cache` (#22902 D13)
     would strand every live seat.
   - Confirmed by the PD (gobby#14730, 2026-09-28): the pin is the content hash
     only.
5. **Idempotency and switching.**
   - The **base agent** is the session's configured default: `_agent_type` is
     absent, `default`, or equal to config `default_agent`, read with
     `ConfigRepository(db)` as `resolve_agent_name` does.
   - From the base agent, activation applies any persona-surface definition.
   - The same agent with the same pin is a no-op receipt (`status: unchanged`)
     that writes nothing and reinjects nothing.
   - Any other change is refused with the typed error
     `role_change_requires_relaunch`: seat X to seat Y, or a seat back to the
     base agent. This follows runbooks decision 20, "a role change is a
     relaunch". Rollback is therefore a pane relaunch, and there is no in-place
     revert path.
   - A spawned session (`is_spawned_agent` true, or an agent run bound) is
     refused with `spawned_session_definition_fixed`, because its definition is
     fixed at spawn. As a result, spawned-run resume (`resume_executor.py`, which
     reuses the existing session row) never meets an activation it would have to
     replay, and needs no change.
6. **Refusals write nothing.** Typed errors, each checked before any write:
   - `unknown_agent_definition` (resolution fails);
   - `persona_surface_missing` (the check exists today in `apply_persona_impl`
     and is kept);
   - `pipeline_requires_spawn`;
   - `role_change_requires_relaunch`;
   - `spawned_session_definition_fixed`;
   - `task_unresolved`;
   - `variable_collision` (`colliding_persona_variable_error`, renamed).

   The only tool parameters are `agent`, `variables` and `task_id`.
7. **`workflows.pipeline` is refused on interactive activation.**
   - Spawn turns it into `_assigned_pipeline`, which the rule
     `pipeline-enforcement/auto-run-pipeline.yaml` auto-runs. No seat declares a
     pipeline, and auto-running one inside a human's pane has no consumer.
   - A definition with a non-null `workflows.pipeline` is refused with
     `pipeline_requires_spawn`.
   - Rejected: auto-running it as spawn does, because that is mechanism without
     a caller.
   - Confirmed by the PD (gobby#14730, 2026-09-28): no automatic pipeline run.
8. **Step workflows run in interactive sessions.**
   - The step gates keyed on spawned-ness and task ownership are lifted:
     - `build_persona_changes` (the `step_workflow_complete` seed);
     - `session_activation.py::_missing_step_state` and `_ensure_step_instance`.
   - The single predicate is now "the resolved definition declares
     `step_workflow.steps`".
   - Activation materializes the instance at the first step, right after the
     variable merge. The merge and the step-instance save are separate
     transactions (`merge_variables` runs under its own `transaction_immediate`;
     so does `_ensure_step_instance`). The order is therefore variables first,
     instance second.
   - If the instance save fails, the session carries the new `_agent_type`, and
     the per-event reconciler `_reconcile_session_activation` materializes the
     missing instance on the next hook event. The reconciler is the retry. No
     cross-table transaction is added.
   - Scope of the change:
     - `default.yaml` declares no `step_workflow`, so plain sessions are
       unaffected.
     - Seats without one (assistant, program-director, lane-manager, archivist)
       get none.
     - Spawned sessions already get their instance at spawn
       (`spawn_agent/_step_state.py::persist_initial_step_instance`), so the lift
       only widens recovery.
9. **Lifecycle is declared on the definition. Runtime enforcement is separate
   (R4).**
   - `AgentDefinitionBody` gains an optional
     `lifecycle: {mode: long_lived | one_task | follow_up, idle_ttl_seconds: int
     | null}`.
     - `one_task` clears between deliverables and keeps the pane.
     - `long_lived` compacts and never clears.
     - `follow_up` requires `idle_ttl_seconds`.
   - Activation writes it to `_agent_lifecycle`. That session variable is the
     contract the runtime work reads.
   - This replaces #22902 D10's "Continuity is prose, not schema" (corrected in
     the #22902 revision below).
   - A model or provider change is a new agent. No in-place switch exists for
     terminal or spawned sessions, and this plan adds none.
   - Rework after rejection goes to a fresh agent (R3).
   - Seat values land in D1 once the #22902 seats exist.
10. **Continuity per boundary.**

    | Boundary | Behavior | Change |
    | --- | --- | --- |
    | Compaction | Same session; SessionStart `compact` re-activates by `_agent_type`; the step instance survives | none beyond Decision 2 |
    | Pane resume | Same as compaction (`source="resume"` on the existing row) | none beyond Decision 2 |
    | `/clear` | Successor copies `_agent_type` and `_agent_definition_hash` before its activation; gets a fresh step instance at the first step | 1.4 |
    | Spawned-run resume | Reuses the session row; spawned sessions cannot change definition (Decision 5) | none |

11. **Hand launch and lane carrier (R1, R2).**
    - R1: a hand-launched pane selects a definition and calls
      `apply_agent_definition(<definition>)`. Placed launch (#22904) passes the
      agent at launch through `agent_name_override`.
    - R2: once the role files retire (D2), a lane derives from task and queue
      ownership: the claimed task's epic, or the lane queue that assigned it.
      There is no lane session variable.
12. **The `/gobby persona` command word stays.**
    - The web UI's attached-terminal `set_agent` sends `/gobby persona <name>`
      (`servers/websocket/handlers/session_config.py::_set_attached_session_agent`,
      pinned by `tests/servers/websocket/test_set_agent.py`).
    - The command routes to the `references/agents/personas.md` skill reference.
      This plan rewrites that reference to call `apply_agent_definition`, and
      neither the websocket handler nor the reference path changes. `persona`
      stays the name of the prompt surface.

## As-Is Facts
`kind: framing`

Verified on 0.5.0 at 2aaa0b9ecc (Writer, 2026-09-28) unless marked:

- `mcp_proxy/tools/apply_persona.py` (305 lines) is the only way to activate a
  definition on an existing session.
  - `apply_persona_impl` resolves the name with `resolve_agent_with_row`, refuses
    a definition without the persona surface, and computes
    `build_session_persona_changes`: `_persona_name`, `_active_skill_names`,
    `_skill_format` and the two reinjection flags.
  - It then runs one `merge_variables`.
  - It writes no `_agent_type`, rules, tool blocks, variables or step instance.
  - It is registered as a nested tool inside
    `agents_spawn_tools.py::register_agent_spawn_tools`.
- `build_persona_changes` (in the same file) is the full SessionStart delta.
  - `_session_start/agents.py::build_agent_changes` calls it for
    `activate_default_agent`.
  - It seeds `step_workflow_complete=False` only when the definition has a step
    workflow, the session is spawned, and the session has an assigned or active
    task.
  - It writes the blocked-tools keys only when they are non-empty.
- `activate_default_agent` handles existing sessions as follows.
  - It always re-applies seven keys: `_agent_type`, `_active_rule_names`,
    `_active_skill_names`, `_skill_format`, the two blocked-tools keys and
    `is_spawned_agent`. All other keys are set only when absent.
  - `resolve_agent_name` returns the override if one is given. Otherwise it
    returns the stored `_agent_type` unless that is `default`, and otherwise
    config `default_agent`.
- Compaction hazard (the Researcher's brief, fact 4, verified by code path).
  - SessionStart `compact` or `resume` on a live session routes through
    `handle_pre_created_session` to `_activate_default_agent`.
  - `apply_persona` never set `_agent_type`, so that re-activation resolves
    `default` and re-applies the default skills over the persona's.
  - `_persona_name` survives, so the persona prompt still re-injects while the
    skill set is the default one.
- Step instances:
  - `session_activation.py::_ensure_step_instance` and `_missing_step_state` both
    require (`is_spawned_agent` or a spawned session) and an assigned or active
    task.
  - The comment in `_ensure_step_instance` states the old non-goal: a
    persona-bound session "must never have one materialized".
  - Enforcement (`_get_step_for_session`) has no spawned check.
- `/clear` successor:
  - `materialize.py::_bind_clear_successor` copies the task claim, the
    handoff-pull flag and the title.
  - `activate_materialized_session` calls the bind (line hint 352) before
    `_activate_default_agent` (line hint 402). The successor has no
    `_agent_type`, so it resolves `default` and the seat is lost.
- Spawned-run resume (`agents/resume_executor.py`) reuses the existing session
  (`existing_session_id`) and merges the spawn-time `initial_variables`.
- Web chat:
  - `chat/_session_launch.py::start_hydrated_session` calls `apply_persona_impl`
    for a persona-selected launch.
  - `chat/_session.py::ChatSessionMixin._create_chat_session_inner` builds the
    system prompt with `build_session_persona_context`.
- `compute_definition_hash(definition_json: str)`
  (`storage/definitions/_shared.py`) feeds template drift only today.
- `AgentDefinitionBody` has no `version` or `lifecycle` field. `timeout` is a
  wall-clock run limit, not an idle limit.

## Constraints
`kind: framing`

- No code in this planning task. The leaves below are the implementation.
- Consumer sweep evidence.
  - Run from the #22903 worktree. The code index does not cover the worktree
    overlay (#20664), and `gcode grep` groups hits by file, so the file list
    came from ripgrep:
    `rg -n -w 'apply_persona|apply_persona_impl|build_persona_changes|build_session_persona_changes|build_session_persona_context|colliding_persona_variable_error|_session_has_assigned_or_active_task|_persona_name' src tests docs`.
  - Code hits:
    - `agents_spawn_tools.py` (registration);
    - `apply_persona.py`;
    - `_session_start/agents.py` (`build_agent_changes`);
    - `chat/_session_launch.py` (`start_hydrated_session`);
    - `chat/_session.py` (`_create_chat_session_inner`);
    - `_agent.py` (`_inject_agent_instructions_if_needed`);
    - `skills/discovery.py`.
  - Bundled hits:
    - `skills/gobby/references/agents/personas.md`;
    - `skills/gobby/references/review/epic.md`;
    - `workflows/review.yaml`.
  - Doc hits:
    - `docs/guides/agents.md`;
    - `docs/guides/workflows-overview.md`;
    - `docs/reference-audit/agents.json`;
    - `docs/reference-audit/variables.json`.
    - Historical review notes under `docs/reviews/` are records and are left
      unchanged.
  - Test hits:
    - `tests/mcp_proxy/tools/test_apply_persona.py`;
    - `tests/workflows/test_step_snapshot_semantics.py`;
    - `tests/mcp_proxy/tools/skills/test_list_skills.py`;
    - `tests/servers/websocket/chat/test_servers_websocket_chat_session.py`;
    - `tests/workflows/test_session_defaults.py`;
    - `tests/skills/test_review_skill.py`;
    - `tests/hooks/test_agent_events_coverage.py` and
      `tests/hooks/test_session_activation_reconciliation.py`, whose fixtures
      set `_persona_name`.
  - `tests/servers/websocket/test_set_agent.py` names only
    `test_rejects_unsafe_persona_name_before_tmux_send` and the `/gobby persona`
    string, and stays unchanged (Decision 12).
- Production files stay under 1,000 lines.
  - Current line counts of the targeted production files:
    - `_session.py` 770;
    - `_agent.py` 699;
    - `session_activation.py` 698;
    - `materialize.py` 493;
    - `_session_launch.py` 345;
    - `_session_start/agents.py` 241;
    - `agent_models.py` 219;
    - `agents_spawn_tools.py` 122;
    - `skills/discovery.py` 44.
  - None reaches 850 lines. `resume_executor.py` (972) is not targeted
    (Decision 5).
- Plan-wide target scope form per file: `session_activation.py`, `_agent.py`,
  `_session_start/agents.py` and `materialize.py` take exact symbols only.
- Boundaries:
  - #22902 owns seat definitions and their bundle validation.
  - #22899 owns `sandbox_profile`. Activation copies no sandbox field: a
    profile applies at spawn or launch, and an interactive pane keeps the
    sandbox it launched with.
  - #22904 owns placed launch, which passes `agent_name_override` at session
    start and so reaches the same core through `activate_default_agent`.
  - #22895 owns runbook deployment.
- Rollout is PD-owned (see V1): the leaves change imported Python, so a daemon
  restart from the main checkout, with global notices outside quiet hours
  (04:45–06:45 CT), precedes any live activation.

## Runtime Lifecycle Boundary (R4)
`kind: framing`

The PD's ruling R4 keeps the activation and lifecycle declaration fields in this
plan (Decision 9, deliverable 2.1). Runtime lifecycle work that is independently
testable is carried as **#22691 sibling tasks for the PD to file**. This plan
does not defer it. Its only shared contract is the `_agent_lifecycle` session
variable that 2.1 writes.

| Item | Runtime work | Evidence |
| --- | --- | --- |
| L2 | Daemon idle-TTL sweep for `follow_up` sessions: wrap up, save, `end_agent_run` | No idle field today; `idle_check_handler.py` covers spawned runs only |
| L3 | `end_agent_run` closes the pane and terminal | It terminates the runtime but never calls `pane_close`; `sweep_dead_panes` is lazy |
| P1 | Orphan terminal reaper for live terminals whose session ended | No sweep covers them |
| P2 | Failed Stop leaves a seat `active` for 30–90 minutes | `handle_stop` pauses only when `turn_disposition != "unknown"` |
| P3 | Stale `set_handoff_pending` markers reconciled | Cleared only on consume or failed clear |
| L7 | Load-gated lane slots | Scheduler scope; listed so it is not lost |

Confirmed by the PD (gobby#14730, 2026-09-28): "separate" in R4 means sibling
tasks under #22691, which the PD files at the appropriate stage. This plan
carries only the shared `_agent_lifecycle` declaration.

## Cross-Plan Correction: #22902
`kind: framing`

The approved #22902 plan names `apply_persona` and conflicts with R2 and R3. The
Writer corrects it under #22903 after Josh approves this plan. The correction
goes through a fresh #22902 review round (#22902 Decision 14): the Adversary
re-derives M1, and the PD applies it.

Timing is PD-coordinated. `update_plan_hash` writes through the main checkout,
which is frozen during the cutover preflight.

Corrections (line hints are against 45e6be4254):

- Line 86: the skill-selector readers become `resolve_skills_for_agent` and
  `apply_agent_definition`.
- D2 (lane text) and 4.1 (lane files): the lane derives from task and queue
  ownership (R2). The lane line in each lane role file lasts only until D2 of
  this plan retires the files.
- D10: continuity is declared by `lifecycle` (this plan's 2.1). The prose
  catalogue becomes each seat's `lifecycle` value, which D1 of this plan sets.
- D11: role files point at `apply_agent_definition(agent=<seat>)`.
- As-Is (the `apply_persona_impl` bullet): the text is restated as the
  pre-#22903 state.
- 2.1 rule condition: `variables.get('_agent_type') in SEATS`. The
  `_persona_name` clause is removed (Decision 3).
- P3 framing: "because `apply_persona` replaces the default persona" becomes
  "because definition activation replaces the default prompt".
- 3.2 (developer loop): the same-session BOUNCE loop (`bounced`, `submit` →
  `implement`) is removed. A BOUNCE ends the unit, and rework goes to a fresh
  agent (R3).
- P4 goal, 4.1 research context, pointer text and rollout; acceptance 4.1.1 and
  4.1.2; V1 live check; M1 validation criteria for 4.1: every `apply_persona`
  becomes `apply_agent_definition`.
- Seat rename (#23038; the Orchestrator's ruling on 2026-09-28): `program-director` becomes
  `orchestrator` everywhere, so the plan matches the SEATS list #22994 committed at
  59255a9. That covers the Decision 2 catalogue, Decisions 7 and 10, the 2.2
  `seat-no-spawn` condition, 3.1's title, Target (`orchestrator.yaml`),
  role text and 3.1.2, the 4.1 role-file Target (`.gobby/roles/orchestrator.md`),
  and the deferral owners. The Adversary re-derives M1's 3.1 entry; nobody
  hand-edits it. The Orchestrator updates the expanded 3.1 leaf's title and
  criteria when the corrected plan is ready.

## P1: Activation
`kind: framing`

**Goal:** one tool activates a whole definition on the caller's session, and
SessionStart re-activation keeps it.

### 1.1 apply_agent_definition tool and shared activation core [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py`
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — operation: delete — scope-reason: the module is replaced by apply_agent_definition.py with no alias
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py::*` — scope-reason: replace the nested apply_persona registration with apply_agent_definition
- `src/gobby/hooks/event_handlers/_session_start/agents.py::build_agent_changes`
- `src/gobby/hooks/event_handlers/_session_start/agents.py::activate_default_agent`
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`
- `src/gobby/skills/discovery.py::get_session_skill_exclusions`
- `src/gobby/servers/websocket/chat/_session_launch.py::start_hydrated_session`
- `src/gobby/servers/websocket/chat/_session.py::ChatSessionMixin._create_chat_session_inner`
- `tests/mcp_proxy/tools/test_apply_agent_definition.py`
- `tests/hooks/test_session_start_reactivation.py`
- `tests/mcp_proxy/tools/test_apply_persona.py::*` — operation: delete — scope-reason: replaced by test_apply_agent_definition.py
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: retarget module paths; persona switch tests become refusal and no-op tests
- `tests/mcp_proxy/tools/skills/test_list_skills.py::*` — scope-reason: import the renamed delta builder
- `tests/workflows/test_session_defaults.py::*` — scope-reason: import the renamed delta builder
- `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::*` — scope-reason: patch targets move to the new module
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: fixtures set _agent_type instead of the retired _persona_name
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: fixtures set _agent_type instead of the retired _persona_name
- `tests/hooks/event_handlers/test_session_variable_preservation.py::*` — scope-reason: the always-reapply set gains the pin key

**Granularity:** nine production files, one behavior. The tool, its shared core,
the two web-chat callers and the two `_persona_name` readers change together.
Deleting `apply_persona.py` breaks every importer in the same commit, so they
cannot be split without a red tree.

**Research context:** the current code:
- `apply_persona.py` (305 lines) holds:
  - `build_persona_changes`, the full delta;
  - `build_session_persona_changes`, the narrow persona delta;
  - `build_session_persona_context`, which returns
    `agent_body.prompt_for("persona")` and the active skills;
  - `colliding_persona_variable_error`;
  - `_resolve_session_identity`;
  - `_session_has_assigned_or_active_task`;
  - `apply_persona_impl`.
- `apply_persona_impl(agent, db, session_id, variables, task_id, task_manager,
  cli_source)`:
  1. resolves the session from `get_session_context()` when `session_id` is
     None;
  2. resolves the definition with `resolve_agent_with_row`;
  3. refuses a missing persona surface;
  4. resolves `task_id` to `assigned_task_id` and refuses when that fails
     (#22402);
  5. runs the collision check;
  6. runs one `merge_variables`.
- Registration is the nested `apply_persona` inside
  `agents_spawn_tools.py::register_agent_spawn_tools`, which passes
  `ctx.db` and `ctx.task_manager`.

Implementation:
- New module `mcp_proxy/tools/apply_agent_definition.py`. Every symbol in it is
  new or moved:
  - `build_definition_changes`: moved from `build_persona_changes`, with the
    same signature and body. It always writes both blocked-tools keys, as an
    empty list when absent, so a stale block cannot survive. It also writes
    `_agent_definition_hash`.
  - `definition_pin(agent_body) -> str`: returns
    `compute_definition_hash(agent_body.model_dump_json())`.
  - `build_persona_prompt_context`: moved from `build_session_persona_context`.
  - `colliding_definition_variable_error`: moved from
    `colliding_persona_variable_error`.
  - `_resolve_session_identity`: moved unchanged.
  - `apply_agent_definition_impl`: see below.
- `_session_has_assigned_or_active_task` moves with `build_definition_changes`
  and is deleted in 1.2 when its gate lifts.
- `build_session_persona_changes` is deleted.

`apply_agent_definition_impl(agent, db, session_id=None, variables=None,
task_id=None, task_manager=None, cli_source=None)`:
1. Resolve the session as today.
2. Read the session variables and the session row.
3. Refuse `spawned_session_definition_fixed` when `is_spawned_agent` is true or
   `hooks/session_activation.py::_session_is_spawned(session)` holds.
4. Resolve the definition, or refuse `unknown_agent_definition`.
5. Refuse `persona_surface_missing`.
6. Refuse `pipeline_requires_spawn` when `workflows.pipeline` is set.
7. Compute the pin.
   - If the current `_agent_type` equals `agent` and the stored pin equals the
     new one, return `{success: true, status: "unchanged"}` without writing.
   - If the current `_agent_type` is not the base agent (absent, `default`, or
     `ConfigRepository(db).read(resolve_secrets=False).values["default_agent"]`)
     and differs from `agent`, refuse `role_change_requires_relaunch`, naming
     both.
   - The same agent with a different pin re-applies, as drift.
8. Resolve `task_id`, or refuse `task_unresolved`.
9. Build the changes with `build_definition_changes(is_spawned=False)` and add
   `_agent_context_injected=False` and `_agent_identity_reinject=True`.
10. Refuse `variable_collision`.
11. Run one `merge_variables`.

Every refusal is `{success: false, error_code, error}`, returned before any
write.

The success receipt carries:
- `status: "applied"`;
- `agent`;
- `definition_hash`;
- `rules_count`, `skills_count` and `blocked_tools_count`;
- `step_workflow` (the first step name, or null; 1.2 fills it).

Callers:
- The tool is registered as `apply_agent_definition` with the description
  "Activate an agent definition on the current session: prompt, rules, skills,
  variables, tool restrictions and step workflow. A role change requires a
  relaunch."
- `build_agent_changes` imports `build_definition_changes`.
- `activate_default_agent` adds `_agent_definition_hash` to both its
  `internal_keys` and `always_reapply` sets, so every re-activation refreshes
  the pin (1.3 compares it first).
- `start_hydrated_session` calls `apply_agent_definition_impl` with the
  explicit session id. `_create_chat_session_inner` calls
  `build_persona_prompt_context`.
- `_inject_agent_instructions_if_needed` resolves `agent_name` from
  `_agent_type` only. Its docstring bullet for `_persona_name` is removed.
- `get_session_skill_exclusions` reads `_agent_type` only.

Tests: `test_apply_agent_definition.py` uses the `HubDatabase` fixtures of
`test_apply_persona.py`.
- `test_step_snapshot_semantics.py`:
  - `test_persona_switch_leaves_existing_instance_untouched` and
    `test_persona_switch_never_calls_step_instance_writers` become the refusal
    test `test_definition_switch_refused_leaves_instance_untouched`.
  - `test_persona_same_agent_preserves_step_position` keeps its assertion under
    the no-op receipt.
  - `test_persona_switch_is_atomic_across_rows` and
    `test_persona_stepless_switch_is_atomic_across_rows` become base-to-seat
    activations.
  - The web-chat failure test retargets its patch paths.
- `test_session_start_reactivation.py` drives `handle_pre_created_session` with
  `source="compact"` on a session activated as a seat. It asserts that
  `_agent_type`, `_active_skill_names` and `_active_rule_names` still carry the
  seat's values, which is the compaction-hazard regression.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_apply_agent_definition.py tests/hooks/test_session_start_reactivation.py tests/workflows/test_step_snapshot_semantics.py tests/mcp_proxy/tools/skills/test_list_skills.py tests/workflows/test_session_defaults.py tests/servers/websocket/chat/test_servers_websocket_chat_session.py tests/hooks/test_agent_events_coverage.py tests/hooks/test_session_activation_reconciliation.py tests/hooks/event_handlers/test_session_variable_preservation.py -q`.
Then run `uv run ruff check` and `uv run mypy` on the changed files, and
`rg -w 'apply_persona|_persona_name|build_session_persona_changes' src tests`,
which must print nothing.

Consumers unchanged:
- `src/gobby/hooks/event_handlers/_base.py` — no-edit-reason: calls get_session_skill_exclusions by name; the signature and return type are unchanged, only the variable it reads changes.
- `src/gobby/mcp_proxy/tools/skills/_context.py` — no-edit-reason: calls get_session_skill_exclusions by name with the same signature.
- `tests/servers/test_fire_lifecycle_parity.py` — no-edit-reason: drives start_hydrated_session and patches no apply_persona path; the renamed call is internal to the function.
- `tests/servers/websocket/chat/test_launch_contracts.py` — no-edit-reason: exercises start_hydrated_session launch contracts and patches no apply_persona path (ripgrep sweep in Constraints).
- `tests/servers/websocket/chat/test_launch_contracts_codex.py` — no-edit-reason: exercises start_hydrated_session launch contracts and patches no apply_persona path.
- `tests/servers/websocket/chat/test_session_launch.py` — no-edit-reason: exercises start_hydrated_session and patches no apply_persona path.
- `tests/servers/websocket/chat/test_launch_contracts_identity.py` — no-edit-reason: exercises _create_chat_session_inner identity and patches no apply_persona path.

**Acceptance:**

- 1.1.1 - Activation from the base agent writes `_agent_type`, the rule, skill,
  variable and blocked-tools keys and `_agent_definition_hash` in one merge.
  test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_writes_full_delta_and_agent_type`.
- 1.1.2 - Unknown name, missing persona surface, declared pipeline, spawned
  session, unresolved task and colliding variables each return their typed
  `error_code` and write nothing. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_refusals_write_nothing`.
- 1.1.3 - The same agent with the same pin returns `status: unchanged` without a
  write, and a seat-to-seat or seat-to-base change returns
  `role_change_requires_relaunch`. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_same_seat_noop_and_role_change_refused`.
- 1.1.4 - A tool the seat blocks is refused by `_check_agent_tool_enforcement`
  after activation, and skill exclusions follow `_agent_type`. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_blocked_tools_and_skill_exclusions_follow_seat`.
- 1.1.5 - A compact SessionStart on an activated seat keeps its skills, rules and
  identity. test:
  `tests/hooks/test_session_start_reactivation.py::test_compact_sessionstart_keeps_seat_skills_and_rules`.
- 1.1.6 - Web-chat persona launch activates through
  `apply_agent_definition_impl`. test:
  `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::test_web_chat_launch_uses_apply_agent_definition`.
- 1.1.7 - `apply_persona.py` no longer exists and the tool registry exposes
  `apply_agent_definition` and no `apply_persona`. symbol:
  `register_agent_spawn_tools`. file:
  `src/gobby/mcp_proxy/tools/agents_spawn_tools.py`.

### 1.2 Step workflows on interactive sessions [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py`
- `src/gobby/hooks/session_activation.py::_missing_step_state`
- `src/gobby/hooks/session_activation.py::_ensure_step_instance`
- `tests/hooks/test_interactive_step_instance.py`
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: invert the tests that pinned the no-instance non-goal
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: step recovery no longer requires a spawned session or task

**Research context:** today's gates:
- `session_activation.py::_ensure_step_instance` returns without creating
  anything unless `is_spawned_agent` or `_session_is_spawned(session)` holds and
  `_has_assigned_or_active_task(variables)` holds.
- `_missing_step_state` uses the same predicate, and the comment in
  `_ensure_step_instance` says so.
- Otherwise the function resolves `_resolved_agent_name(variables, None)` and
  opens `db.transaction_immediate(AgentStepInstanceMutation(session_id=...))`.
  It returns when an instance exists, resolves the definition with
  `resolve_agent_with_row`, and saves
  `build_step_instance(body, session_id, step_workflow_id=row.step_workflow_id)`.
- `build_definition_changes` (moved in 1.1) seeds `step_workflow_complete=False`
  only when the definition has a step workflow, the session is spawned, and it
  has an assigned or active task.
- `_reconcile_session_activation` calls `_ensure_step_instance` on every hook
  event except pipeline sessions. That is the self-healing path.
- Spawn persists its own instance
  (`spawn_agent/_step_state.py::persist_initial_step_instance`).

Implementation:
- In both functions, drop the spawned and task clauses. The predicate becomes
  "the resolved definition has `step_workflow.steps`", which
  `_ensure_step_instance` already checks inside its transaction, and which
  `_missing_step_state` checks through `_agent_has_step_workflow`.
- Replace the non-goal comment with one line: every agent whose definition
  declares a step workflow gets an instance (Josh ruling, memory 556ec801).
- In `build_definition_changes`, seed `step_workflow_complete=False` whenever
  `agent_body.step_workflow` has steps, and delete
  `_session_has_assigned_or_active_task`.
- In `apply_agent_definition_impl`, after the merge, call the session-activation
  helper `_ensure_step_instance(db, session_id, merged_variables, session)` and
  report its first step in the receipt.
  - A failure is logged and returned as `step_workflow_pending: true`. The
    variables stand, and the reconciler repairs the instance on the next hook
    event (Decision 8).
  - The helper stays in `session_activation.py`, so there is one step-instance
    writer for non-spawn paths.
- Scope:
  - `default.yaml` declares no `step_workflow`, so a plain session still gets
    none.
  - Seats without a step workflow get none.
  - A spawned session already has its spawn-time instance, so
    `_ensure_step_instance` returns early.

Tests in `test_step_snapshot_semantics.py`:
- `test_persona_without_agent_run_creates_no_step_instance` inverts to
  `test_definition_activation_materializes_step_instance`.
- The atomic-pair tests assert that an injected instance-save failure leaves
  the variables written and that the next reconcile creates the instance.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_interactive_step_instance.py tests/workflows/test_step_snapshot_semantics.py tests/hooks/test_session_activation_reconciliation.py tests/workflows/test_step_runtime_transitions.py -q`.

**Acceptance:**

- 1.2.1 - An interactive seat whose definition declares steps gets an instance
  at the first step on activation, with no task claimed. test:
  `tests/hooks/test_interactive_step_instance.py::test_interactive_seat_with_step_workflow_gets_instance_without_task`.
- 1.2.2 - A seat or plain session whose definition has no step workflow gets no
  instance. test:
  `tests/hooks/test_interactive_step_instance.py::test_seat_without_step_workflow_gets_none`.
- 1.2.3 - A failed instance save leaves the activation variables in place and the
  next hook event's reconcile materializes the instance. test:
  `tests/hooks/test_interactive_step_instance.py::test_reconcile_repairs_missing_instance_after_failed_save`.
- 1.2.4 - A spawned session's spawn-time instance is untouched. test:
  `tests/hooks/test_interactive_step_instance.py::test_spawned_step_instance_unchanged`.
- 1.2.5 - The old no-instance non-goal test is inverted. test:
  `tests/workflows/test_step_snapshot_semantics.py::test_definition_activation_materializes_step_instance`.

### 1.3 Definition drift receipt on re-activation [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/agents.py::activate_default_agent`
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`
- `tests/hooks/test_session_start_reactivation.py`

**Research context:** existing behavior:
- `activate_default_agent` reads `existing = sv_mgr.get_variables(session_id)`
  before its merge.
- After 1.1, `_agent_definition_hash` is in `always_reapply`, so the merge
  overwrites the stored pin with the current one.
- `AgentActivationResult` has no context field, and the prompt reaches the model
  only through `_inject_agent_instructions_if_needed`. That method prepends
  `agent_body.prompt_for(surface)` when `_agent_context_injected` is false or
  `_agent_identity_reinject` or `_agent_context_rehydrate_pending` is set. It
  then stages the three flags back to their idle values.

Implementation:
- Before the merge in `activate_default_agent`, compare `existing` against the
  new changes.
- When the stored `_agent_type` equals the activated name and the stored
  `_agent_definition_hash` is non-null and differs from the new one, add to the
  changes:
  - `_agent_definition_drift`: a single line of the form "Definition
    `<agent>` changed since this session activated it (`<old[:12]>` →
    `<new[:12]>`); the current definition now applies.";
  - `_agent_identity_reinject: True`.
- In `_inject_agent_instructions_if_needed`, when `_agent_definition_drift` is a
  non-empty string, add it after the preamble and stage it back to `None` with
  the other flags.
- No drift line is produced in these cases:
  - no stored pin (the first activation);
  - the same pin;
  - a different agent. A `/clear` successor carrying the predecessor's pin is
    the same agent, so 1.4 reaches this path.

Rejected: a new context channel on `AgentActivationResult`. It was removed by
the Claude 5 prompt cleanup, and the reinjection flag path already delivers
exactly one line on the next turn.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_session_start_reactivation.py tests/hooks/event_handlers/test_session_variable_preservation.py -q`.

**Acceptance:**

- 1.3.1 - A resume or compact SessionStart whose seat definition changed applies
  the current row, stores the new pin, and injects the drift line exactly once.
  test:
  `tests/hooks/test_session_start_reactivation.py::test_reactivation_reports_definition_drift_once`.
- 1.3.2 - An unchanged pin injects no drift line. test:
  `tests/hooks/test_session_start_reactivation.py::test_unchanged_pin_injects_no_drift_line`.

### 1.4 /clear successor keeps its seat [category: code] (depends: 1.2, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::_bind_clear_successor`
- `tests/hooks/test_clear_successor_seat.py`

**Research context:** today's behavior:
- `_bind_clear_successor` takes the clear marker (`take_clear_handoff_marker`).
- It copies the task claim (`preserve_task_claim_state`), merges
  `HANDOFF_PULL_PENDING_VARIABLE: True` into the successor, and applies the
  successor title.
- `activate_materialized_session` runs the bind (line hint 352) before
  `_activate_default_agent` (line hint 402).
- The successor has no `_agent_type`, so `resolve_agent_name` returns config
  `default_agent`, and the seat is lost.
- `/clear` is the routine boundary for `one_task` seats: developer,
  code-reviewer, plan-writer, plan-adversary and researcher (#22902 D10).

Implementation:
- In the same `merge_variables` call that sets `HANDOFF_PULL_PENDING_VARIABLE`,
  copy the predecessor's `_agent_type` and `_agent_definition_hash` when
  `_agent_type` is set and is not the base agent.
- `_activate_default_agent` then resolves the seat and applies the full delta to
  the fresh session. 1.3 compares the carried pin.
- The successor is a new session with no step instance, so `_ensure_step_instance`
  (lifted in 1.2) creates a fresh one at the first step on the first hook event:
  a clear ends a unit of work.
- The predecessor's variables are already read into `predecessor_vars`, so no
  extra query is needed.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_clear_successor_seat.py tests/hooks/test_session_start_handlers.py -q`.

**Acceptance:**

- 1.4.1 - A `/clear` successor of an activated seat carries `_agent_type` and the
  pin and re-activates as that seat. test:
  `tests/hooks/test_clear_successor_seat.py::test_clear_successor_inherits_agent_type_and_pin`.
- 1.4.2 - The successor gets a fresh step instance at the first step. test:
  `tests/hooks/test_clear_successor_seat.py::test_clear_successor_gets_fresh_step_instance`.
- 1.4.3 - A base-agent predecessor carries nothing. test:
  `tests/hooks/test_clear_successor_seat.py::test_base_agent_clear_successor_unchanged`.

## P2: Lifecycle Declaration
`kind: framing`

**Goal:** a definition declares how its agent lives, and activation records it
for the runtime work that enforces it.

### 2.1 Lifecycle field on agent definitions [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py::*` — scope-reason: add the AgentLifecycle model and the lifecycle field to AgentDefinitionBody
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py`
- `tests/workflows/test_agent_definitions_v2.py::*` — scope-reason: cover lifecycle validation
- `tests/mcp_proxy/tools/test_apply_agent_definition.py`

**Research context:** existing model and inputs:
- `AgentDefinitionBody` (`workflows/agent_models.py`, 219 lines) is
  `extra="ignore"` for bundled sync, and `reject_legacy_step_keys` rejects
  retired keys.
- `timeout` is a wall-clock run limit (`agent_health.py`), not an idle limit.
- Josh's input (PD scope add, memory 7faa183d):
  - each agent lives in a pane with one task at a time;
  - developers `/clear` between tasks and keep the pane;
  - follow-up agents such as the researcher get an idle TTL of about 15
    minutes, then wrap up, save and `end_agent_run`;
  - coordinators are long-lived;
  - the TTL is declared on the definition.
- #22902 D10 has the same split in prose, with no field.

Implementation:
- New model `AgentLifecycle(BaseModel, extra="forbid")`:
  - `mode: Literal["long_lived", "one_task", "follow_up"]`;
  - `idle_ttl_seconds: int | None = None`.
- A model validator:
  - requires `idle_ttl_seconds` greater than 0 for `follow_up`;
  - forbids it for `long_lived`;
  - allows it for `one_task`.
- `AgentDefinitionBody.lifecycle: AgentLifecycle | None = None`.
- `build_definition_changes` writes
  `_agent_lifecycle = body.lifecycle.model_dump()` when the field is declared,
  and `None` otherwise.
- No runtime reader is added here. The PD's sibling tasks read
  `_agent_lifecycle` (see "Runtime Lifecycle Boundary").
- #22902 1.1 also edits `agent_models.py` (`version`, `skills`). The two changes
  touch different fields, and whichever lands second rebases.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_agent_definitions_v2.py tests/mcp_proxy/tools/test_apply_agent_definition.py tests/agents/test_agents_sync.py -q`.

**Acceptance:**

- 2.1.1 - `lifecycle` validates the three modes and rejects a `follow_up` without
  a positive TTL and a `long_lived` with one. test:
  `tests/workflows/test_agent_definitions_v2.py::test_lifecycle_declaration_validates`.
- 2.1.2 - Activation records the declaration as `_agent_lifecycle`. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_writes_lifecycle_declaration`.

## P3: Migration
`kind: framing`

**Goal:** every document and bundled skill names the tool that exists.

### 3.1 Bundled references and guides [category: docs] (depends: 1.2, 1.4, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/agents/personas.md`
- `src/gobby/install/shared/skills/gobby/references/review/epic.md`
- `src/gobby/install/shared/workflows/review.yaml::*` — scope-reason: the mode input description names the retired tool
- `docs/guides/agents.md`
- `docs/guides/workflows-overview.md`
- `docs/reference-audit/agents.json::*` — scope-reason: the tool inventory entry names the retired tool
- `docs/reference-audit/variables.json::*` — scope-reason: the implementation pointer names the retired module
- `tests/skills/test_review_skill.py::*` — scope-reason: the pinned epic-review phrase names the retired tool

**Research context:** what each file says today:
- `personas.md` routes `/gobby persona <name>` (sent by the web UI's
  `_set_attached_session_agent`). It tells the agent to call
  `gobby-agents:apply_persona`, says `agent="default"` restores the default, and
  says activation installs no rules, tool restrictions or step instances.
- `review/epic.md` (the interactive branch) says
  `apply_persona(agent="epic-reviewer")`, and
  `test_review_skill.py::test_epic_review_references_pin_routing_and_verdict_mapping`
  pins that phrase.
- `review.yaml` input `mode` says "the skill's in-line mode uses apply_persona".
- `docs/guides/agents.md`:
  - the surface table row names `gobby-agents:apply_persona`;
  - a paragraph says it is "intentionally narrow";
  - the run-tools list names it.
- `workflows-overview.md` names it once.
- The reference-audit JSONs name the tool and the module path.

Edits:
- `personas.md`:
  - It calls `gobby-agents:apply_agent_definition`.
  - It states that activation applies prompt, rules, skills, variables, tool
    blocks and the step workflow.
  - It states that a session already bound to a non-default definition refuses
    another with `role_change_requires_relaunch`, and that the user relaunches
    the pane instead. The `agent="default"` restore sentence is deleted.
  - It lists the typed refusals.
- `epic.md` and the test phrase: `apply_agent_definition(agent="epic-reviewer")`.
- `review.yaml`: "uses apply_agent_definition".
- `agents.md`:
  - The surface row names the new tool.
  - The "intentionally narrow" paragraph becomes the activation contract:
    Decisions 2, 4, 5, 7 and 8, the continuity table from Decision 10, and the
    `lifecycle` field.
  - The run-tools list names `apply_agent_definition`.
- `workflows-overview.md`: the tool name.
- The JSONs: the tool name and `apply_agent_definition.py::build_definition_changes`.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_review_skill.py -q`, and
`rg -w apply_persona src docs/guides docs/reference-audit tests`, which must print
nothing.

**Acceptance:**

- 3.1.1 - The persona reference calls `apply_agent_definition` and names the
  relaunch refusal. behavior: "apply_agent_definition" in
  `src/gobby/install/shared/skills/gobby/references/agents/personas.md`.
- 3.1.2 - The epic-review reference and its test name the new tool. test:
  `tests/skills/test_review_skill.py::test_epic_review_references_pin_routing_and_verdict_mapping`.
- 3.1.3 - The agents guide documents the activation contract, continuity table
  and `lifecycle`. behavior: "role_change_requires_relaunch" in
  `docs/guides/agents.md`.
- 3.1.4 - The workflows overview, the review pipeline, and both reference-audit
  files name `apply_agent_definition` and no `apply_persona`. file:
  `docs/guides/workflows-overview.md`.

## D1 Seat lifecycle values (depends: 2.1)
`kind: deferred`

Once #22902's seat definitions exist (its P3), each seat declares `lifecycle`:
- `long_lived`: assistant, program-director, lane-manager, archivist and
  log-monitor.
- `one_task`: developer, code-reviewer, plan-writer and plan-adversary.
- `follow_up` with `idle_ttl_seconds: 900`: researcher.

The #22902 bundle contract test (`tests/workflows/test_seat_definitions.py`)
asserts the mapping. Before the seats land, there is nothing to annotate.

```yaml
deferral:
  task_ref: "TBD-after-22902-P3"
  reason: "External prerequisite: the seat YAML files are created by #22902 P3 leaves under root #22988."
  owner: "program-director"
  original_acceptance_items:
    - D1.1
```

- D1.1 - Every seat definition declares the lifecycle mode above, and the seat
  contract test pins it.

## D2 Roster and role-file retirement (depends: 1.4)
`kind: deferred`

After durable activation (1.1 to 1.4) and one activation receipt from every live
seat, the prompt-only lookup retires:
- `.gobby/roles/_common.md`, whose content moved to the #22902 D5 `roles` rule;
- `roster.md` and every role file;
- the roster-lookup text in `src/gobby/install/shared/workflows/agents/default.yaml`;
- the contract pins `tests/workflows/test_default_agent_role_contract.py::_probe_documented_lookup`
  and `test_default_profile_describes_all_lookup_guards`.

Seats are then selected by R1: a hand launch calls `apply_agent_definition`, and
placed launch passes `agent_name_override`. Lanes derive from task and queue
ownership (R2).

The gates:
- #22894 closes, because its acceptance pins the roster, the role files and the
  lookup phrases;
- #22902 4.1 lands with the corrected pointer text.

```yaml
deferral:
  task_ref: "TBD-after-22894"
  reason: "External prerequisite: #22894 acceptance pins the roster, role files and default.yaml lookup phrases until it closes."
  owner: "program-director"
  original_acceptance_items:
    - D2.1
    - D2.2
```

- D2.1 - `.gobby/roles/` and the `default.yaml` roster lookup are removed, and
  the role-contract tests are deleted with them.
- D2.2 - A hand-launched pane that calls `apply_agent_definition` for its seat
  runs with no role file.

## V1: Verification
`kind: verification`

Run after the final edit of each leaf and again before the PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_apply_agent_definition.py tests/hooks/test_session_start_reactivation.py tests/hooks/test_interactive_step_instance.py tests/hooks/test_clear_successor_seat.py tests/workflows/test_step_snapshot_semantics.py tests/workflows/test_step_runtime_transitions.py tests/workflows/test_agent_definitions_v2.py tests/workflows/test_session_defaults.py tests/mcp_proxy/tools/skills/test_list_skills.py tests/servers/websocket/chat/test_servers_websocket_chat_session.py tests/servers/websocket/test_set_agent.py tests/hooks/test_agent_events_coverage.py tests/hooks/test_session_activation_reconciliation.py tests/hooks/event_handlers/test_session_variable_preservation.py tests/hooks/test_session_start_handlers.py tests/skills/test_review_skill.py tests/agents/test_agents_sync.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
rg -w 'apply_persona|_persona_name|build_session_persona_changes' src tests docs/guides docs/reference-audit
uv run gobby plans validate /Users/josh/.gobby/worktrees/gobby/task-22903-apply-agent-definition/.gobby/plans/apply-agent-definition.md -p /Users/josh/Projects/gobby
```

The `rg` must print nothing. Do not run the full pytest suite.

Live check after the PD-owned restart, which is announced globally before and
after and happens outside quiet hours:
1. A hand-launched default pane calls
   `apply_agent_definition(agent="plan-writer")`. The receipt is `applied`, and
   the next turn carries the seat prompt.
2. `get_variable("_agent_type")` returns `plan-writer`.
3. A repeat call returns `unchanged`.
4. `apply_agent_definition(agent="researcher")` returns
   `role_change_requires_relaunch`.
5. After a compaction the session still reports the seat's skills.
6. After a `/clear`, the successor reports `_agent_type: plan-writer` and a
   fresh step instance once the seat's step workflow exists.

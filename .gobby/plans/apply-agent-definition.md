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
   - Rejected: Orchestrator-targeted activation of another session. No flow in #22902,
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
     - the pin (Decision 4).

     Run lifetime is not part of the delta (Decision 9).
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
   - There is no separate `version` pin. #22902 D4's human `version` marker is
     a body field, so `compute_definition_hash` (which hashes every key of the
     dumped body) already covers it: a version bump changes the pin, as any
     other edit does. Nothing in this plan reads `version` on its own.
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
     - Seats without one (assistant, orchestrator, lane-manager, archivist)
       get none.
     - Spawned sessions already get their instance at spawn
       (`spawn_agent/_step_state.py::persist_initial_step_instance`), so the lift
       only widens recovery.
9. **Run lifetime is `execution_mode`. The only addition is an idle TTL.
   Runtime enforcement is separate (R4).**
   - Superseded on 2026-10-05 (Orchestrator ruling, relayed by the Lane Manager
     gobby#15389): the 2026-09-28 draft's
     `lifecycle: {mode: long_lived | one_task | follow_up, idle_ttl_seconds}`
     field and its `_agent_lifecycle` session variable. Reason: one mechanism
     per concern. `execution_mode` (#23442, Standing seats launched with
     spawn_agent are reprompted to end their run when idle, then completed by
     the watchdog) already owns run lifetime.
   - `AgentDefinitionBody.execution_mode` (`one_shot | interactive`) stays the
     single run-lifetime field.
     - Spawn resolves the effective mode; a spawn-time `execution_mode`
       overrides the definition.
     - Spawn stores the mode in the run's `resume_metadata`, which
       `AgentRun.is_interactive` reads.
   - The only new field is `idle_ttl_seconds`, a positive integer that is valid
     only with `execution_mode: interactive`.
     - It ends an interactive follow-up run that has sat idle that long.
     - Spawn stores it in `resume_metadata` next to `execution_mode`, and only
       when the effective mode is `interactive`.
   - Mapping of the superseded values:

     | Superseded `lifecycle.mode` | Declaration now |
     | --- | --- |
     | `long_lived` | `execution_mode: interactive`, no `idle_ttl_seconds` |
     | `one_task` | `execution_mode: interactive`, no `idle_ttl_seconds` |
     | `follow_up` | `execution_mode: interactive` with `idle_ttl_seconds` |

     - `long_lived` and `one_task` collapse into one value, because both keep
       their pane between tasks. Whether a seat compacts or clears between
       deliverables is a context-boundary practice, not run lifetime. It stays
       prose in each seat prompt (#22902 D10), and 1.4 carries the seat across
       `/clear`.
     - `one_shot` is not a seat lifecycle. It is for single-pass spawned
       workers such as plan-enhancer and the close reviewer.
   - Both fields act on agent runs. A hand-launched pane has no run, so
     activation writes nothing about run lifetime.
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

Verified on 0.5.0 at 2aaa0b9ecc (Writer, 2026-09-28) and re-verified at
48071323c9 (Writer, 2026-10-05) unless marked:

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
    handoff-pull flag, the MCP proxy readiness (`inherited_mcp_proxy_ready`)
    and the title.
  - `activate_materialized_session` calls the bind (line hint 356) before
    `_activate_default_agent` (line hint 406). The successor has no
    `_agent_type`, so it resolves `default` and the seat is lost.
  - The bind does not rebind an agent run. Only spawn
    (`spawn_agent/_runtime.py::_persist_spawn_runtime`) and the admin test
    route call `update_child_session`.
- Spawned-run resume (`agents/resume_executor.py`) reuses the existing session
  (`existing_session_id`) and merges the spawn-time `initial_variables`.
- Web chat:
  - `chat/_session_launch.py::start_hydrated_session` calls `apply_persona_impl`
    for a persona-selected launch.
  - `chat/_session.py::ChatSessionMixin._create_chat_session_inner` builds the
    system prompt with `build_session_persona_context`.
- `compute_definition_hash(definition_json: str)`
  (`storage/definitions/_shared.py`) feeds template drift only today.
- `AgentDefinitionBody` (`workflows/agent_models.py`, 270 lines):
  - `version: StrictStr | None` (line 112) landed with #22993 (Retire the
    skills map and store version, #22902 1.1, closed 2026-09-28). As a body
    field it is inside the content-hash pin (Decision 4).
  - `execution_mode: Literal["one_shot", "interactive"] = "one_shot"`
    (line 120) landed in #23442's commits a95e39fdad, 80ecc84f10 and
    dbfdc43f42. #23442 is still open.
    - `spawn_agent_impl` resolves the effective mode (`_implementation.py:137`):
      the spawn argument wins, otherwise the definition's value.
    - `build_spawn_context` writes it to the run's initial variables
      (`_runtime.py:62`) and `resume_metadata` (`_runtime.py:134`).
    - `AgentRun.is_interactive` (`storage/agents/_models.py:83`) reads
      `resume_metadata`. The idle check, health check, lifecycle monitor,
      lifecycle reconciliation and completed-turn recovery branch on it.
    - `_handle_idle_check` returns early for every interactive run
      (`idle_check_handler.py:481`), so nothing ends an idle interactive run
      today.
    - Resume passes the stored `resume_metadata` through whole
      (`tests/agents/watchdog/test_interactive_lifecycle_cleanup.py` asserts
      that `execution_mode` survives).
  - It has no idle-TTL field. `timeout` (line 149) is a wall-clock run limit,
    not an idle limit.
- The #22902 seat rules landed with `_persona_name` clauses: #22994 (Roles rule
  group with shared seat guidance, #22902 2.1) and #22995 (Seat spawn and write
  policy with the Plan Writer enhancer exception, #22902 2.2). Both are closed.
  - The conditions read `_agent_type in SEATS or _persona_name in SEATS`, or
    `'<seat>' in [_agent_type, _persona_name]`.
  - The files are `rules/roles/inject-seat-common.yaml` (3 hits),
    `seat-spawn-policy.yaml` (9) and `seat-write-scope.yaml` (2).
  - `tests/workflows/test_seat_rules.py` fixtures set `_persona_name`.
- Seat definitions: `plan-writer.yaml`, `plan-adversary.yaml` and
  `plan-enhancer.yaml` landed with #23339, and `researcher.yaml` predates the
  seats. None declares `execution_mode`, so each takes `one_shot`. The other
  seats (#22996, #22997, #22998) and the planning-council rewrite (#22999) are
  open.

## Constraints
`kind: framing`

- No code in this planning task. The leaves below are the implementation.
- Consumer sweep evidence.
  - Re-run on main at 48071323c9 (2026-10-05). `gcode grep` groups hits by
    file, so the file list came from ripgrep:
    `rg -l -w 'apply_persona|apply_persona_impl|build_persona_changes|build_session_persona_changes|build_session_persona_context|colliding_persona_variable_error|_session_has_assigned_or_active_task|_persona_name' src tests docs`.
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
    - `workflows/review.yaml`;
    - `workflows/rules/roles/inject-seat-common.yaml`,
      `seat-spawn-policy.yaml` and `seat-write-scope.yaml` (new since the
      2026-09-28 sweep; 1.5).
  - Doc hits:
    - `docs/guides/agents.md`;
    - `docs/guides/workflows-overview.md`;
    - `docs/reference-audit/agents.json`;
    - `docs/reference-audit/variables.json`.
    - Historical records are left unchanged: review notes under
      `docs/reviews/`, `docs/audits/post-push-review-ae78ebdf00.md` and
      `docs/plans/completed/provider-agnostic-resume.md`.
  - Test hits:
    - `tests/mcp_proxy/tools/test_apply_persona.py`;
    - `tests/workflows/test_step_snapshot_semantics.py`;
    - `tests/mcp_proxy/tools/skills/test_list_skills.py`;
    - `tests/servers/websocket/chat/test_servers_websocket_chat_session.py`;
    - `tests/workflows/test_session_defaults.py`;
    - `tests/skills/test_review_skill.py`;
    - `tests/hooks/test_agent_events_coverage.py` and
      `tests/hooks/test_session_activation_reconciliation.py`, whose fixtures
      set `_persona_name`;
    - `tests/workflows/test_seat_rules.py` (new since the 2026-09-28 sweep;
      1.5).
  - `tests/servers/websocket/test_set_agent.py` names only
    `test_rejects_unsafe_persona_name_before_tmux_send` and the `/gobby persona`
    string, and stays unchanged (Decision 12).
- Production files stay under 1,000 lines.
  - Current line counts of the targeted production files (2026-10-05):
    - `spawn_agent/_implementation.py` 926;
    - `_session.py` 776;
    - `_agent.py` 721;
    - `session_activation.py` 698;
    - `materialize.py` 497;
    - `_session_launch.py` 345;
    - `agent_models.py` 270;
    - `_session_start/agents.py` 241;
    - `spawn_agent/_runtime.py` 195;
    - `agents_spawn_tools.py` 123;
    - `skills/discovery.py` 44.
  - Only `spawn_agent/_implementation.py` is above 850 lines. 2.1 moves its
    run-lifetime resolution into the new `spawn_agent/_run_lifetime.py`, so it
    does not grow. `resume_executor.py` (711) is not targeted (Decision 5).
- Plan-wide target scope form per file: `session_activation.py`, `_agent.py`,
  `_session_start/agents.py`, `materialize.py`, `spawn_agent/_implementation.py`
  and `spawn_agent/_runtime.py` take exact symbols only.
- Boundaries:
  - #22902 owns seat definitions and their bundle validation.
  - #22899 owns `sandbox_profile`. Activation copies no sandbox field: a
    profile applies at spawn or launch, and an interactive pane keeps the
    sandbox it launched with.
  - #22904 owns placed launch, which passes `agent_name_override` at session
    start and so reaches the same core through `activate_default_agent`.
  - #22895 owns runbook deployment.
- Rollout is Orchestrator-owned (see V1): the leaves change imported Python, so a daemon
  restart from the main checkout, with global notices outside quiet hours
  (04:45–06:45 CT), precedes any live activation.

## Runtime Lifecycle Boundary (R4)
`kind: framing`

The PD's ruling R4 keeps activation and the run-lifetime declaration in this
plan (Decision 9, deliverable 2.1). Runtime lifecycle work that is independently
testable is carried as **#22691 sibling tasks for the Orchestrator to file**.
This plan does not defer it. The shared contract is the run's `resume_metadata`:
`execution_mode` (#23442) and the `idle_ttl_seconds` key that 2.1 writes at
spawn.

| Item | Runtime work | Evidence |
| --- | --- | --- |
| L2 | Idle-TTL enforcement for interactive runs whose `resume_metadata` carries `idle_ttl_seconds`: wrap up, save, `end_agent_run`. Interactive runs without the key keep #23442's no-idle-end behavior | `_handle_idle_check` returns early for every interactive run (`idle_check_handler.py:481`); `last_session_activity(session_id)` and `session.updated_at` already give the idle clock there |
| L3 | `end_agent_run` closes the pane and terminal | It terminates the runtime but never calls `pane_close`; `sweep_dead_panes` is lazy |
| P1 | Orphan terminal reaper for live terminals whose session ended | No sweep covers them |
| P2 | Failed Stop leaves a seat `active` for 30–90 minutes | `handle_stop` pauses only when `turn_disposition != "unknown"` |
| P3 | Stale `set_handoff_pending` markers reconciled | Cleared only on consume or failed clear |
| L7 | Load-gated lane slots | Scheduler scope; listed so it is not lost |

Confirmed by the PD (gobby#14730, 2026-09-28): "separate" in R4 means sibling
tasks under #22691, which the PD files at the appropriate stage. Since the
2026-10-05 revision (Decision 9), this plan carries only the `idle_ttl_seconds`
declaration and its spawn-time persistence.

## Cross-Plan Correction: #22902
`kind: framing`

The approved #22902 plan names `apply_persona` and conflicts with R2 and R3. The
Writer corrects it under #22903 after Josh approves this plan. The correction
goes through a fresh #22902 review round (#22902 Decision 14): the Adversary
re-derives and applies M1, and the Orchestrator reviews the stamped plan.

Timing is Orchestrator-coordinated, because `update_plan_hash` writes into the
shared main checkout.

Corrections (line hints are against 45e6be4254, which is still the plan's last
commit on 2026-10-05):

- Line 86: the skill-selector readers become `resolve_skills_for_agent` and
  `apply_agent_definition`.
- D2 (lane text) and 4.1 (lane files): the lane derives from task and queue
  ownership (R2). The lane line in each lane role file lasts only until D2 of
  this plan retires the files.
- D10: run lifetime is declared by `execution_mode` and `idle_ttl_seconds`
  (this plan's Decision 9 and 2.1), and D1 of this plan sets each seat's
  values. Compact versus clear stays prose. "No `continuity` field: nothing
  reads it" is restated, because spawn and the watchdog read `execution_mode`.
  The researcher moves from clear-between-deliverables to a follow-up seat with
  an idle TTL.
- D11: role files point at `apply_agent_definition(agent=<seat>)`.
- As-Is (the `apply_persona_impl` bullet): the text is restated as the
  pre-#22903 state.
- 2.1 and 2.2 rule conditions: `variables.get('_agent_type') in SEATS`. Both
  leaves are closed (#22994, #22995), so the `_persona_name` clauses are
  landed code, which this plan's 1.5 removes. The #22902 text is restated as
  the pre-#22903 state.
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
  before its merge. For an existing session it then filters the changes to keys
  in `always_reapply` or absent from `existing`, and merges the result.
- After 1.1, `_agent_definition_hash` is in `always_reapply`, so the merge
  overwrites the stored pin with the current one.
- `_agent_identity_reinject` is stored on any session that has injected once
  (staged back to `False`), and `_agent_definition_drift` stays stored as
  `None` after its first delivery. Both keys are therefore present in
  `existing` and are dropped if they enter the changes before the filter
  (enhancer note, gobby#15446, verified at
  `_session_start/agents.py::activate_default_agent`).
- `AgentActivationResult` has no context field, and the prompt reaches the model
  only through `_inject_agent_instructions_if_needed`. That method prepends
  `agent_body.prompt_for(surface)` when `_agent_context_injected` is false or
  `_agent_identity_reinject` or `_agent_context_rehydrate_pending` is set. It
  then stages the three flags back to their idle values.

Implementation:
- In `activate_default_agent`, compare `existing` against the new changes after
  the `always_reapply` filter, immediately before `merge_variables`, and add the
  drift keys at that point so the filter cannot drop them.
- When the stored `_agent_type` equals the activated name and the stored
  `_agent_definition_hash` is non-null and differs from the new one, add to the
  filtered changes:
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
  the current row, stores the new pin, and injects the drift line exactly once,
  including on a session that already stores `_agent_identity_reinject: False`
  and `_agent_definition_drift: None`. test:
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
  `HANDOFF_PULL_PENDING_VARIABLE: True` and the predecessor's MCP proxy
  readiness (`inherited_mcp_proxy_ready`) into the successor, and applies the
  successor title.
- `activate_materialized_session` runs the bind (line hint 356) before
  `_activate_default_agent` (line hint 406).
- The successor has no `_agent_type`, so `resolve_agent_name` returns config
  `default_agent`, and the seat is lost.
- `/clear` is the routine boundary for the seats #22902 D10 lists as
  clear-between-deliverables: developer, researcher, code-reviewer,
  plan-writer and plan-adversary.
- The bind does not rebind an agent run (As-Is Facts). This deliverable carries
  seat identity only and does not change run binding.

Implementation:
- In the same `merge_variables` call that sets `HANDOFF_PULL_PENDING_VARIABLE`
  and the inherited proxy readiness, copy the predecessor's `_agent_type` and
  `_agent_definition_hash` when
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

### 1.5 Seat rules match `_agent_type` only [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml::*` — scope-reason: drop the retired _persona_name clause, its comment and the seat-identity guidance wording
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml::*` — scope-reason: drop the retired _persona_name clauses from every seat condition and the header comment
- `src/gobby/install/shared/workflows/rules/roles/seat-write-scope.yaml::*` — scope-reason: drop the retired _persona_name clauses from both write-scope conditions
- `tests/workflows/test_seat_rules.py::*` — scope-reason: fixtures set _agent_type instead of the retired _persona_name, plus one stale-variable regression

**Research context:** the `rules/roles/` group landed with #22994 (Roles rule
group with shared seat guidance) and #22995 (Seat spawn and write policy with
the Plan Writer enhancer exception). Its conditions match a seat through both
variables, because `apply_persona` writes only `_persona_name` and spawn writes
only `_agent_type` (#22902 2.1: "`agent_scope` alone would miss persona
sessions").
- `inject-seat-common.yaml`:
  - the condition is `(_agent_type in SEATS or _persona_name in SEATS) and not
    _seat_common_injected`;
  - a comment explains the dual match;
  - the guidance line reads "Your seat is the definition named by your
    `_persona_name` or `_agent_type`."
- `seat-spawn-policy.yaml` (9 hits):
  - the same disjunction appears in four conditions;
  - `'<seat>' [not] in [_agent_type, _persona_name]` carries the plan-writer
    and orchestrator exceptions;
  - the header comment explains the dual match.
- `seat-write-scope.yaml` has `'assistant' in [...]` and `'archivist' in
  [...]`.
- `reset-seat-common-on-context-loss.yaml` reads neither variable.

After 1.1, activation writes `_agent_type` for every seat. The `_persona_name`
clauses are then dead, and the V1 `rg` cannot pass while they remain. The edit
cannot land before 1.1, because until then a persona session carries only
`_persona_name`. Bundled rule templates sync to the rule registry (AGENTS.md
rule 8).

`tests/workflows/test_seat_rules.py` drives a `RuleEngine` fixture with variable
dicts:
- `ASSISTANT`, `PLAN_WRITER` and `ORCHESTRATOR` set
  `{"_agent_type": "default", "_persona_name": <seat>}`;
- `test_seat_common_injected_once_per_epoch`,
  `test_seat_common_matches_spawned_and_skips_non_seats` and
  `test_seat_common_rearms_after_compact` build `{"_persona_name": ...}`
  contexts.

Implementation:
- Each condition keeps only its `_agent_type` side. The disjunctions become
  `variables.get('_agent_type') in [<SEATS>]`, and each two-variable list
  membership becomes `variables.get('_agent_type') == '<seat>'` (or `!=`).
- Both comments say that activation and spawn both set `_agent_type`.
- The guidance line becomes "Your seat is the definition named by your
  `_agent_type`."
- Test fixtures set `{"_agent_type": <seat>}`. A new test asserts that a
  context carrying only `_persona_name: plan-writer` matches no seat rule.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_seat_rules.py -q`, and
`rg -w _persona_name src/gobby/install/shared/workflows/rules tests/workflows/test_seat_rules.py`,
which must print nothing.

**Acceptance:**

- 1.5.1 - Every `rules/roles/` condition matches a seat through `_agent_type`
  alone, and a context that carries only `_persona_name` matches none of them.
  test: `tests/workflows/test_seat_rules.py::test_persona_name_alone_matches_no_seat_rule`.
- 1.5.2 - Seat guidance injection still matches a session whose `_agent_type`
  names a seat and skips one that names no seat. test:
  `tests/workflows/test_seat_rules.py::test_seat_common_matches_spawned_and_skips_non_seats`.

## P2: Run Lifetime Declaration
`kind: framing`

**Goal:** a definition declares when an idle interactive run ends, and spawn
records it for the runtime work that enforces it.

### 2.1 Idle TTL on agent definitions [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py::*` — scope-reason: add the optional idle_ttl_seconds field and its interactive-only validator to AgentDefinitionBody; every existing reader of the body is unaffected by a defaulted field
- `src/gobby/mcp_proxy/tools/spawn_agent/_run_lifetime.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::build_spawn_context`
- `tests/workflows/test_agent_definitions_v2.py::*` — scope-reason: cover idle_ttl_seconds validation
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: cover idle_ttl_seconds persistence beside execution_mode

**Research context:** existing model and inputs:
- `AgentDefinitionBody` (`workflows/agent_models.py`, 270 lines) has
  `model_config = ConfigDict(extra="ignore")` for stale YAML.
  `reject_legacy_step_keys` rejects retired keys with migration hints.
- `execution_mode` (line 120) defaults to `one_shot`. `timeout` (line 149) is
  a wall-clock run limit (`agent_health.py`), not an idle limit.
- `spawn_agent_impl` resolves `effective_execution_mode` (`_implementation.py:137`):
  the spawn argument wins, otherwise the definition's value. It passes the
  result to `build_spawn_context` next to `prewarm_pre_commit_store`, which it
  reads from `agent_body` the same way.
- `build_spawn_context` writes `execution_mode` to the initial variables
  (`_runtime.py:62`) and `resume_metadata` (`_runtime.py:134`). Resume passes
  `resume_metadata` through whole.
- `test_factory.py::test_execution_mode_is_persisted_for_watchdog_and_resume`
  is parametrized over the definition mode and the spawn override, and it
  asserts both stores. It is the pattern to extend.
- `template_hashes.py` hashes the parent body, so a new defaulted field changes
  every bundled hash once. Sync refreshes that drift, as it did for #22993's
  `version`.
- Josh's input (PD scope add, memory 7faa183d):
  - follow-up agents such as the researcher get an idle TTL of about 15
    minutes, then wrap up, save and `end_agent_run`;
  - the TTL is declared on the definition.
- #23442's description records Josh's goal: every seat except the Orchestrator
  and the Assistant launches with sandboxed `spawn_agent` as a long-lived
  interactive run.

Implementation:
- `AgentDefinitionBody.idle_ttl_seconds: PositiveInt | None = None`, placed
  after `execution_mode`, with the description "Idle seconds after which an
  interactive run ends."
- A model validator rejects `idle_ttl_seconds` unless `execution_mode` is
  `interactive`.
- `spawn_agent/_implementation.py` is at 926 lines, so the run-lifetime
  resolution moves out of it into the new `spawn_agent/_run_lifetime.py`
  rather than growing it. The move takes the effective-mode resolution and its
  invalid-mode refusal (`_implementation.py:137-141`).
  - The new module holds a frozen `RunLifetime(execution_mode,
    idle_ttl_seconds)` and `resolve_run_lifetime(agent_body, execution_mode)`.
  - The function returns the effective mode as today, and the definition's
    `idle_ttl_seconds` only when that mode is `interactive`. A spawn-time
    `one_shot` override therefore drops the definition's TTL.
  - `spawn_agent_impl` calls it in place of the moved lines, keeps the same
    `execution_mode must be one_shot or interactive` refusal, and passes both
    values to `build_spawn_context`. Its other `effective_execution_mode` reads
    (the standing-seat prompt suffix) use `RunLifetime.execution_mode`, so the
    file does not grow.
- `build_spawn_context` gains `idle_ttl_seconds: int | None = None` and writes
  `resume_metadata["idle_ttl_seconds"]` when it is set. It writes no session
  variable, because the reader is the watchdog, and the watchdog reads the run.
- No reader is added here. The L2 sibling reads the key (see "Runtime Lifecycle
  Boundary").
- No spawn-time TTL override is added: no caller needs one.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_agent_definitions_v2.py tests/mcp_proxy/tools/spawn_agent/test_factory.py tests/agents/test_agents_sync.py -q`.
Then run `uv run ruff check` and `uv run mypy` on the changed files.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` — no-edit-reason: passes the spawn-time execution_mode through unchanged; the TTL comes from the definition only.
- `src/gobby/storage/agents/_models.py` — no-edit-reason: is_interactive keeps reading execution_mode; the TTL reader belongs to the L2 sibling.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: calls spawn_agent_impl, whose signature and result shape are unchanged.
- `src/gobby/feedback/agent.py` — no-edit-reason: calls spawn_agent_impl, whose signature and result shape are unchanged.
- `src/gobby/scheduler/executor.py` — no-edit-reason: calls spawn_agent_impl, whose signature and result shape are unchanged.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: calls spawn_agent_impl, whose signature and result shape are unchanged.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/agents/test_local_context_setup.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/agents/test_sandbox_network.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py` — no-edit-reason: drives spawn_agent_impl error paths that the move does not touch.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: initial variables gain no key; the TTL goes only to resume_metadata.
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.
- `tests/tasks/test_plan_gate.py` — no-edit-reason: drives spawn_agent_impl without an idle_ttl_seconds definition; its assertions are unchanged.

**Acceptance:**

- 2.1.1 - `idle_ttl_seconds` accepts a positive integer with
  `execution_mode: interactive`, and rejects zero, a negative value, and any
  value on a `one_shot` definition. test:
  `tests/workflows/test_agent_definitions_v2.py::test_idle_ttl_requires_interactive_execution_mode`.
- 2.1.2 - An interactive spawn of a definition with a TTL stores
  `idle_ttl_seconds` in `resume_metadata`, and a spawn-time `one_shot` override
  stores none. test:
  `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_idle_ttl_is_persisted_only_for_interactive_runs`.

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
    run-lifetime fields `execution_mode` and `idle_ttl_seconds` (Decision 9).
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
  and `idle_ttl_seconds`. behavior: "role_change_requires_relaunch" in
  `docs/guides/agents.md`.
- 3.1.4 - The workflows overview, the review pipeline, and both reference-audit
  files name `apply_agent_definition` and no `apply_persona`. file:
  `docs/guides/workflows-overview.md`.

## D1 Seat run-lifetime values (depends: 2.1)
`kind: deferred`

Once #22902's seat definitions exist (its P3), each seat declares its run
lifetime (Decision 9):
- `execution_mode: interactive`, no `idle_ttl_seconds`: assistant,
  orchestrator, lane-manager, archivist, log-monitor, developer,
  code-reviewer, plan-writer and plan-adversary.
- `execution_mode: interactive` with `idle_ttl_seconds: 900`: researcher.
- `execution_mode: one_shot` (the default, so nothing to declare):
  plan-enhancer.

The #22902 bundle contract test (`tests/workflows/test_seat_definitions.py`)
asserts the mapping.

`plan-writer.yaml`, `plan-adversary.yaml` and `plan-enhancer.yaml` (#23339) and
`researcher.yaml` exist today and take the `one_shot` default (As-Is Facts).
The other seats do not exist yet. #22902's open P3 leaves own every seat YAML:
#22996 (Coordination seats: assistant, orchestrator, lane-manager), #22997
(One developer definition with task-routed skills), #22998 (Review and
observation seats: code-reviewer, archivist, log-monitor, researcher) and
#22999 (Planning council seats and the review flow). Editing those files here
would collide with those leaves.

```yaml
deferral:
  task_ref: "TBD-after-22902-P3"
  reason: "External prerequisite: the seat YAML files are created or rewritten by the open #22902 P3 leaves (#22996-#22999) under root #22988."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

- D1.1 - Every seat definition declares the run lifetime above, and the seat
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
  owner: "orchestrator"
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

Run after the final edit of each leaf and again after the last leaf lands:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_apply_agent_definition.py tests/hooks/test_session_start_reactivation.py tests/hooks/test_interactive_step_instance.py tests/hooks/test_clear_successor_seat.py tests/workflows/test_step_snapshot_semantics.py tests/workflows/test_step_runtime_transitions.py tests/workflows/test_agent_definitions_v2.py tests/workflows/test_session_defaults.py tests/workflows/test_seat_rules.py tests/mcp_proxy/tools/skills/test_list_skills.py tests/mcp_proxy/tools/spawn_agent/test_factory.py tests/servers/websocket/chat/test_servers_websocket_chat_session.py tests/servers/websocket/test_set_agent.py tests/hooks/test_agent_events_coverage.py tests/hooks/test_session_activation_reconciliation.py tests/hooks/event_handlers/test_session_variable_preservation.py tests/hooks/test_session_start_handlers.py tests/skills/test_review_skill.py tests/agents/test_agents_sync.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
rg -w 'apply_persona|_persona_name|build_session_persona_changes' src tests docs/guides docs/reference-audit
uv run gobby plans validate .gobby/plans/apply-agent-definition.md -p /Users/josh/Projects/gobby
```

The `rg` must print nothing. Do not run the full pytest suite.

Live check after the Orchestrator-owned restart, which is announced globally
before and after and happens outside quiet hours:
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

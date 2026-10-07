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

The activation survives compaction, resume and `/clear`. `apply_persona`
stays beside it as the lightweight persona switch: it overlays a definition's
persona prompt and skills on the session, live, and activates nothing else
(Decision 3). Josh's ruling on 2026-10-06 keeps both tools, and #23647 restores
`apply_persona` after 1.1 deleted it. This is a functional expansion, not a
rename.

What users will see:
- In a terminal pane, picking an agent for an attached terminal in the web UI
  switches the pane's persona live through `apply_persona`, with no relaunch.
  The prompt and skills change, and an active seat's rules, tool blocks and
  step workflow stay in force. Picking `default` returns the pane to its own
  definition's prompt and skills.
- Changing a terminal pane's agent definition, from one seat to another or
  from a seat back to the default agent, needs a relaunch: start a new
  terminal session with that agent. `apply_agent_definition` refuses the
  change in place with `role_change_requires_relaunch`.
- In web chat, switching agents keeps working. The switch restarts the chat's
  CLI process with the new agent, and the conversation continues on the same
  session.

Josh's execution-model ruling governs the target (memory 556ec801). Every session
is an interactive agent in a pane except one-shots, and every agent must be able
to execute a step workflow. The spawned-only step gates are as-is gaps, not
non-goals.

The #22902 plan (`.gobby/plans/agent-definition-profiles.md`) owns what a seat
definition declares. This plan owns how a session receives, rehydrates and
enforces it (#22902 Constraints, boundary paragraph).

## Decision Record
`kind: framing`

1. **One definition-activation tool, self-session only.**
   - Tool: `gobby-agents:apply_agent_definition(agent, variables=None,
     task_id=None)`.
   - It acts on the caller's own session, resolved from session context. It
     takes no `session_id` parameter, because a session is bound to its pane.
   - The Python implementation takes an explicit `session_id` for one
     in-process caller: the web-chat launch. It activates the conversation's
     row, which is a new row on the first launch and the same row after a
     `set_agent` switch (Decision 5).
   - Rejected: Orchestrator-targeted activation of another session. No flow in #22902,
     #22904 or #22895 needs it. It would also change another agent's rules and
     tool blocks mid-turn with no receipt.
   - `gobby-agents:apply_persona` stays beside it as the lightweight persona
     switch (Josh, 2026-10-06 00:48 CT: "Keep both."). The two tools have
     distinct roles:
     - `apply_agent_definition` activates a whole definition: identity,
       prompt, rules, skills, variables, tool blocks and the step workflow. A
       role change needs a relaunch (Decision 5).
     - `apply_persona` overlays a definition's persona prompt, skill set and
       skill format on the caller's session. It switches live with no
       relaunch, and changes no rules, variables, tool blocks or step workflow
       (Decision 3).
   - Amended 2026-10-06 (#23648): the earlier draft deleted `apply_persona`,
     and 1.1 did so at e8f43fb8ab. #23647 restores it.
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
       compaction hazard for an activated definition: before 1.1, a persona
       session reverted to the `default` skills on its first compact. A
       persona overlay keeps its skills through #23647 (Decision 3).
   - Rejected: activating a definition through the narrower persona delta
     (`build_session_persona_changes` before e8f43fb8ab). It writes no
     `_agent_type`, which was the source of the hazard. That delta serves only
     `apply_persona`'s overlay (Decision 3).
3. **`_persona_name` is `apply_persona`'s overlay, never a definition
   identity.** Amended 2026-10-06 (#23648): the earlier draft retired it, and
   1.1 removed its readers at e8f43fb8ab. #23647 restores them.
   - `apply_persona` writes `_persona_name` with the persona's skill set and
     skill format, and requests identity reinjection. It writes no
     `_agent_type`, rules, variables, tool blocks or step instance.
   - There are two readers. `_agent.py::_inject_agent_instructions_if_needed`
     chooses the prompt, and `skills/discovery.py::get_session_skill_exclusions`
     chooses skill exclusions. On an interactive session both take
     `_persona_name` first, then `_agent_type`. A spawned session reads
     `_agent_type` only.
   - Seat rules, tool blocks and step workflows are keyed on `_agent_type`
     only (1.5). A persona overlay never makes a session a seat.
   - Compact or resume re-activation keeps the overlay. While `_persona_name`
     is set, the session keeps the persona's prompt, skill set and skill
     format. A seated session also keeps its seat's rules, variables, tool
     blocks and step workflow (#23647).
   - `apply_persona(agent="default")` removes the overlay. A base-agent
     session returns to the default persona, and a seated session returns to
     its definition's own prompt and skill set (#23647).
   - A definition activation that writes, through the tool or a web-chat
     relaunch, clears `_persona_name`, so the activated definition's own
     prompt and skills apply. `apply_agent_definition_impl` writes
     `_persona_name: None` beside the reinjection flags (#23647). SessionStart
     re-activation leaves the overlay as it is, and so do a refusal and an
     `unchanged` receipt, which write nothing.
   - The prompt surface stays keyed on `is_spawned_agent`: `persona` for
     interactive sessions, `agent` for spawned ones.
   - Rejected: retiring `_persona_name`, the earlier draft. Josh, 2026-10-06
     00:27 CT, on the deletion: "They served two different roles."
   - Rejected: keeping `_persona_name` as a mirror of `_agent_type`. It names
     an overlay. A second variable for the same identity would drift.
4. **Pin and drift.**
   - Activation stores the pin `_agent_definition_hash`. Its value is
     `compute_definition_hash(agent_body.model_dump_json())` over the resolved
     body it applies, which is the same form `template_hashes.py` uses for agent
     templates. The resolved body is stable for a session because its CLI
     source does not change, and both entry points (the tool and SessionStart)
     resolve `provider: inherit` with that source (1.1).
   - Every activation path delivers the same receipt. A same-seat activation
     with a different pin injects one drift line, whether it is a SessionStart
     re-activation or a repeat tool call (1.3).
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
   - `variables` and `task_id` apply only when activation occurs. An
     `unchanged` call ignores them: it resolves no task, merges no variables
     and leaves the session's bindings as they are. There is no update mode.
   - Any other change is refused with the typed error
     `role_change_requires_relaunch`: seat X to seat Y, or a seat back to the
     base agent. This follows runbooks decision 20, "a role change is a
     relaunch". Rollback is therefore a relaunch, and there is no in-place
     revert path inside a running CLI process.
   - A relaunch starts a new CLI process for the role. A terminal pane relaunch
     creates a new session. A web-chat `set_agent` switch stops the
     conversation's CLI process, and the next launch starts a new one on the
     conversation's row. That launch is the only caller that changes roles. It
     calls `apply_agent_definition_impl(..., relaunch=True)` for seat X to seat Y
     and for seat X to the base agent alike. The relaunch removes the previous
     definition's recorded variables, ends its step instance (1.2), and applies
     the new delta, so the row carries what a new row would. Variables written
     at runtime stay with the conversation, as they do today.
   - Permission and cleanup are separate tests. `is_role_change` decides only
     whether an activation may proceed. Cleanup runs on every identity change:
     a stored `_agent_type` that differs from the new agent, on a row that is
     not spawned. That includes the permitted first activation over a
     configured base agent, which can declare variables and steps like any
     agent (adversary finding F-B1-base-cleanup). At the write, under the
     session's step lock (1.1), an activation commits only while the stored
     `_agent_type` still equals the one it was prepared against, and then both
     tests run again. A delta prepared from an earlier read never commits over
     a newer seat, the base agent included.
   - Rejected: a new row per web-chat switch. The launch binds the row by
     conversation id (`chat/_session.py:314-330`), and `set_agent` keeps that id
     stable. A new row would need a second successor-commit flow beside the
     `/clear` one. Rejected: refusing the web-chat switch, which would regress
     a working control (Orchestrator ruling, 2026-10-05 09:00 CT).
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
   - `activation_superseded` (the session's agent changed between the
     activation's read and its write);
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
    | `/clear` | Successor copies `_agent_type` and `_agent_definition_hash` before its activation; gets a fresh step instance at the first step; a spawned seat's live run, its terminal and its back-pointer move to the successor after a staged clear; the pane's frozen managed identity then speaks for the successor | 1.4, 1.6 |
    | Spawned-run resume | Reuses the session row; spawned sessions cannot change definition (Decision 5) | none |

11. **Hand launch and lane carrier (R1, R2).**
    - R1: a hand-launched pane selects a definition and calls
      `apply_agent_definition(<definition>)`. Placed launch (#22904) passes the
      agent at launch through `agent_name_override`.
    - R2: once the role files retire (D2), a lane derives from task and queue
      ownership: the claimed task's epic, or the lane queue that assigned it.
      There is no lane session variable.
12. **The `/gobby persona` command routes to `apply_persona`.**
    - The web UI's attached-terminal `set_agent` sends `/gobby persona <name>`
      (`servers/websocket/handlers/session_config.py::_set_attached_session_agent`,
      pinned by `tests/servers/websocket/test_set_agent.py`).
    - The command routes to the `references/agents/personas.md` skill
      reference, which calls `apply_persona`. The picker therefore switches the
      terminal's persona live, and #23647 restores that switch. 3.1 adds the
      definition-activation route to the reference. #23647 returns the
      websocket handler to its pre-#23503 switch, and the reference path does
      not change. `persona` stays the name of the prompt surface.
    - Amended 2026-10-06 (#23648): the earlier draft rewrote the reference to
      call `apply_agent_definition`.

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
  - It writes the blocked-tools keys only when they are non-empty,
    `_active_skill_names` only for a restricted skill selection, and
    `_skill_format` only when the definition sets one.
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
  - Earlier in the same `/clear`, SessionEnd `clear` on a spawned seat
    completes its run and marks its terminal exited, even after
    `set_handoff(clear_session=true)` staged the attempt (1.4 research
    context).
  - A spawned pane keeps its launch environment across `/clear`. Its hooks and
    its MCP proxy send `GOBBY_SESSION_ID`, the predecessor, as
    `X-Gobby-Session-Id`. After the take, the daemon refuses the successor's
    MCP tool calls (403), and attributes its hook events, variable calls and
    LLM calls to the expired predecessor (1.6 research context).
- Spawned-run resume (`agents/resume_executor.py`) reuses the existing session
  (`existing_session_id`) and merges the spawn-time `initial_variables`.
- Web chat:
  - `handlers/session_config.py::handle_set_agent` cancels the active chat,
    marks the row `paused`, stops the CLI process, and stores
    `_pending_agents[conversation_id]`. It resets no variables (lines 546-655).
  - The next `_create_chat_session_inner` rebinds the same `web_chat` row by
    conversation id (`chat/_session.py:314-330` and 549-552). A pending
    non-default agent with the persona surface sets `persona_selected`
    (374-395).
  - `chat/_session_launch.py::start_hydrated_session` calls `apply_persona_impl`
    for a persona-selected launch, raises when it returns `success: false`
    (lines 255-276), and then sets `skip_default_agent_activation`. A pending
    `default` instead becomes the SessionStart input `agent_name_override`
    (287-288), which `_session_start/flow.py:669` and `materialize.py:405`
    pass to `activate_default_agent` with no role-change check.
  - So under 1.1 as first drafted, a switch from seat X to seat Y would be
    refused `role_change_requires_relaunch` and fail the launch, and a switch
    from X to `default` would revert in place. Today both work, because
    `apply_persona` writes only `_persona_name` (reviewer gobby#15396, B1).
  - Today the web-chat launch is the only producer of the SessionStart input
    `agent_name_override`. #22904's placed launch will pass it for a new
    session (Constraints). The reconciler passes its own override to
    `activate_default_agent` directly (`hooks/session_activation.py:201`).
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
    - `sessions/clear_continuation.py` 860;
    - `_session.py` 776;
    - `routes/mcp/hooks.py` 775;
    - `handlers/session_config.py` 723;
    - `_agent.py` 721;
    - `session_activation.py` 698;
    - `routes/llm.py` 575;
    - `request_context.py` 515;
    - `materialize.py` 497;
    - `_session_launch.py` 345;
    - `agent_models.py` 270;
    - `_session_start/agents.py` 241;
    - `_session_end.py` 217;
    - `spawn_agent/_runtime.py` 195;
    - `routes/sessions/variables.py` 126;
    - `agents_spawn_tools.py` 123;
    - `skills/discovery.py` 44.
  - Two targeted files are above 850 lines, and neither grows.
    - 2.1 moves the run-lifetime resolution of `spawn_agent/_implementation.py`
      into the new `spawn_agent/_run_lifetime.py`.
    - 1.4 moves the run-lineage writes of `sessions/clear_continuation.py`
      into the new `sessions/clear_run_lineage.py`.
  - `resume_executor.py` (711) is not targeted (Decision 5).
- Plan-wide target scope form per file: `session_activation.py`, `_agent.py`,
  `_session_start/agents.py`, `materialize.py`, `_session_end.py`,
  `clear_continuation.py`, `spawn_agent/_implementation.py`,
  `spawn_agent/_runtime.py`, `request_context.py`, `routes/mcp/hooks.py`,
  `routes/sessions/variables.py` and `routes/llm.py` take exact symbols only.
- Boundaries:
  - #22902 owns seat definitions and their bundle validation.
  - #22899 owns `sandbox_profile`. Activation copies no sandbox field: a
    profile applies at spawn or launch, and an interactive pane keeps the
    sandbox it launched with.
  - #22904 owns placed launch, which passes `agent_name_override` at session
    start and so reaches the same core through `activate_default_agent`.
  - #22895 owns runbook deployment.
- Rollout is Orchestrator-owned (see V2): the leaves change imported Python, so a daemon
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
| L2 | Idle-TTL enforcement for interactive runs whose `resume_metadata` carries `idle_ttl_seconds`. The reader is `_handle_idle_check`'s interactive branch: once the run's idle time reaches the TTL, the run wraps up, saves and calls `end_agent_run`. Interactive runs without the key keep #23442's no-idle-end behavior. After 1.4, a seat's run points at its current `/clear` successor, so the idle clock follows the live session. Carried as deferral D3. The Orchestrator files its task under #22691 after Josh approves this plan and before expansion, and D3 states the numeric-ref route | `_handle_idle_check` returns early for every interactive run (`idle_check_handler.py:481`); `last_session_activity(session_id)` and `session.updated_at` already give the idle clock there |
| L3 | `end_agent_run` closes the pane and terminal | It terminates the runtime but never calls `pane_close`; `sweep_dead_panes` is lazy |
| P1 | Orphan terminal reaper for live terminals whose session ended | No sweep covers them |
| P2 | Failed Stop leaves a seat `active` for 30–90 minutes | `handle_stop` pauses only when `turn_disposition != "unknown"` |
| P3 | Stale `set_handoff_pending` markers reconciled | Cleared only on consume or failed clear |
| L7 | Load-gated lane slots | Scheduler scope; listed so it is not lost |

Confirmed by the PD (gobby#14730, 2026-09-28): "separate" in R4 means sibling
tasks under #22691, which the PD files at the appropriate stage. Since the
2026-10-05 revision (Decision 9), this plan carries the `idle_ttl_seconds`
declaration and its spawn-time persistence (2.1), and carries L2 as deferral D3
(Orchestrator ruling, 2026-10-05). The other items stay sibling tasks.

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

- Line 86: the skill-selector readers become `resolve_skills_for_agent`,
  `apply_agent_definition` and, once #23647 restores it, `apply_persona`.
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
  - 4.1.1 (#23001, "Role files point at seat definitions"): each role file
    points its seat at `apply_agent_definition(agent=<seat>)`, not
    `apply_persona`. Amended 2026-10-06 (#23671): keeping `apply_persona`
    (Decision 1) does not change this, because after #23507 ("Seat rules
    match `_agent_type` only", 1.5) `apply_persona` gives a session no seat
    rules and no activation receipt. The Orchestrator updates #23001's
    description and criteria when the corrected plan is ready.
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
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py::*` — scope-reason: 1.1 creates the module: the shared activation core (build_definition_changes, activation_decision, commit_definition_changes) and apply_agent_definition_impl
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py::*` — scope-reason: 1.1 replaced the nested apply_persona registration with apply_agent_definition; #23647 registers apply_persona again beside it
- `src/gobby/hooks/event_handlers/_session_start/agents.py::build_agent_changes`
- `src/gobby/hooks/event_handlers/_session_start/agents.py::activate_default_agent`
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`
- `src/gobby/skills/discovery.py::get_session_skill_exclusions`
- `src/gobby/servers/websocket/chat/_session_launch.py::start_hydrated_session`
- `src/gobby/servers/websocket/chat/_session.py::ChatSessionMixin._create_chat_session_inner`
- `src/gobby/servers/websocket/handlers/session_config.py::_set_attached_session_agent`
- `tests/mcp_proxy/tools/test_apply_agent_definition.py::*` — scope-reason: 1.1 creates the tool's activation, refusal, seat, relaunch and concurrency tests
- `tests/hooks/test_session_start_reactivation.py::*` — scope-reason: 1.1 creates the SessionStart compact, provider-pin, dropped-skill and drift-relaunch reactivation tests
- `tests/servers/websocket/test_attached_session_agent.py::*` — scope-reason: the fixture seeds the target's variables at the base agent, plus the role-change refusal and current-agent echo case
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: retarget module paths; persona switch tests become refusal and no-op tests
- `tests/mcp_proxy/tools/skills/test_list_skills.py::*` — scope-reason: import the renamed delta builder
- `tests/workflows/test_session_defaults.py::*` — scope-reason: import the renamed delta builder
- `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::*` — scope-reason: patch targets move to the new module
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: fixtures set the seat identity in _agent_type instead of _persona_name
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: fixtures set the seat identity in _agent_type instead of _persona_name
- `tests/hooks/event_handlers/test_session_variable_preservation.py::*` — scope-reason: the always-reapply set gains the pin and key-list keys
- `tests/hooks/event_handlers/test_activate_agent_override.py::*` — scope-reason: an override that is a role change on a seat row keeps the stored seat

**Amendment, 2026-10-06 (#23648):** Josh's ruling on `apply_persona` (00:48
CT, "Keep both.") reverses part of what #23503 delivered at e8f43fb8ab.
#23647 restores, beside `apply_agent_definition`:
- the `apply_persona` tool, its registration and its persona-switch helpers,
  adapted to the shared core, and `test_apply_persona.py`;
- the `_persona_name`-first reads in `_inject_agent_instructions_if_needed`
  and `get_session_skill_exclusions` (Decision 3);
- the attached-terminal picker's live persona switch. `_set_attached_session_agent`
  sends `/gobby persona <name>` again in place of the
  `ROLE_CHANGE_REQUIRES_RELAUNCH` refusal that #23503 delivered under 1.1.15,
  which #23647 criterion 3 reverses.

#23647 also removes the registry test's assertion that `apply_persona` is
absent (1.1.7). 1.1.7 and 1.1.15 leave the acceptance items, and the item ids
of the rest stay. The zero-persona `rg` sweep that 1.1 ran is withdrawn as a
standing check. 1.1's Targets no longer list `apply_persona.py` or
`test_apply_persona.py`: #23503 removed both at e8f43fb8ab, and #23647
restores them. The rest of this section records 1.1 as delivered. Where it
deletes or retires a persona symbol, or refuses the attached-terminal switch,
this amendment governs.

**Granularity:** ten production files, one behavior. The tool, its shared
core, the two web-chat callers, the attached-terminal handler and the two
`_persona_name` readers change together.
At e8f43fb8ab, removing `apply_persona.py` broke every importer in the same
commit, so they could not be split without a red tree. The switch paths
change in the same commit as the role-change refusal, because the refusal
alone would break today's web-chat switch.

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
- The two entry points resolve `provider: inherit` differently today
  (adversary finding F-pin-provider-identity, gobby#15414, verified at
  921ecab10a).
  - `agent_resolver.py::_resolve_inherit` sets the provider to `cli_source`,
    or to `"claude"` when it is omitted (lines 31-33).
  - The tool passes the session's source (`apply_persona.py:237`, through
    `_resolve_session_identity`).
  - `activate_default_agent` receives `cli_source` (`event.source.value` at
    both callers, `flow.py:121` and `materialize.py:308`) but calls
    `resolve_agent` without it (`_session_start/agents.py:120-124`).
  - The pin hashes every dumped field, so a Codex seat would pin
    `provider: codex` through the tool and `provider: claude` at its next
    SessionStart, which is false drift.
- `build_persona_changes` writes `_active_skill_names` only when the selection
  is restricted (line 87) and `_skill_format` only when it is set (line 90).
  The narrow builder that 1.1 deletes wrote `None` for both (lines 133-134).
  `search_skills.py:120-125` treats `None` as every skill, and
  `activate_default_agent` re-applies both keys on every re-activation (adversary
  finding F-full-delta-stale-skills).

Implementation:
- New module `mcp_proxy/tools/apply_agent_definition.py`. Every symbol in it is
  new or moved:
  - `build_definition_changes`: moved from `build_persona_changes`, with the
    same signature and body, except that a full activation replaces every
    definition-owned key, so no restriction, block or format from an earlier
    activation survives:
    - it always writes both blocked-tools keys, as an empty list when absent;
    - it always writes `_active_skill_names` (`None` when the definition
      selects every skill) and `_skill_format` (`None` when it sets none);
    - it writes `_agent_definition_hash`;
    - it writes `_agent_definition_keys`, the sorted names of its definition
      variables: the `workflows.variables` keys, the selector-filtered default
      keys, and `step_workflow_complete` when seeded.
      `commit_definition_changes` finalizes the list.
  - `definition_pin(agent_body) -> str`: returns
    `compute_definition_hash(agent_body.model_dump_json())`.
  - `is_role_change(db, variables, agent) -> bool`: true when the stored
    `_agent_type` is not the base agent (absent, `default`, or
    `ConfigRepository(db).read(resolve_secrets=False).values["default_agent"]`)
    and differs from `agent`. Step 7, the attached-terminal handler and the
    override guard share it. It decides permission only.
  - `activation_decision(db, variables, agent, pin, *, relaunch, same_pin_noop)
    -> str`:
    - It returns `unchanged` when `same_pin_noop` is true, the stored
      `_agent_type` equals `agent`, and the stored pin equals `pin`.
    - It returns `role_change_requires_relaunch` when `is_role_change` holds
      and `relaunch` is false.
    - Otherwise it returns `apply`.
    Step 7 and `commit_definition_changes` share it, so the early check and
    the locked check cannot diverge.
  - `commit_definition_changes(db, session_id, agent, changes, *,
    expected_agent_type, relaunch, same_pin_noop) -> dict`: the one write path
    for both activation entries, the tool and `activate_default_agent`.
    - It opens `db.transaction_immediate(AgentStepInstanceMutation(session_id=
      session_id))` and rereads the session's variables under that lock.
    - It compares the reread `_agent_type` with `expected_agent_type`, the
      stored value the caller read before choosing its agent and building its
      delta. On a mismatch it returns `superseded` with the current
      `_agent_type` and writes nothing, whatever the current identity is, base
      agent included.
    - Otherwise it reruns `activation_decision` on the reread, with the pin
      from `changes["_agent_definition_hash"]`, which still catches a pin that
      a concurrent same-agent activation changed. A result other than `apply`
      is returned with the current `_agent_type`, and nothing is written.
    - A delta prepared from an earlier read therefore never commits over a
      newer seat (adversary finding F-B1-locked-permission).
    - The variable lock, at priority 950, nests inside the step lock at 875,
      as `_acquire_lock` requires (`postgres_pool.py:707-715`). Ambient
      transaction reuse is keyed by the adapter (`_ambient.py:28-56`), so the
      helper builds every manager from its one `db`. No caller of either entry
      holds an immediate transaction.
    - It finds an identity change when the stored `_agent_type` is set and
      differs from `agent`, and `is_spawned_agent` is not true. The spawned
      exclusion matters because the reconciler passes the run agent when the
      stored `_agent_type` is `default` (`session_activation.py:532-541`).
    - On an identity change, it sets to `None` every name in the stored
      `_agent_definition_keys` that the new list,
      `changes["_agent_definition_keys"]`, does not name. It compares lists,
      never the keys of `changes`, because `activate_default_agent` drops
      existing keys from `changes` before the call. Rules read variables with
      `get`, so a cleared key reads as absent.
    - Without an identity change, it writes the sorted union of the stored and
      new lists. A key that a drifted definition no longer declares keeps its
      value, as runtime values do, and stays listed until the next identity
      change clears it (adversary finding F-B1-key-ledger).
    - It runs one `merge_variables` inside the held transaction and returns
      `status: "applied"` with the merged variables. 1.2 adds the step
      instance delete to the same transaction.
  - `build_persona_prompt_context`: moved from `build_session_persona_context`.
  - `colliding_definition_variable_error`: moved from
    `colliding_persona_variable_error`.
  - `_resolve_session_identity`: moved unchanged.
  - `apply_agent_definition_impl`: see below.
- `_session_has_assigned_or_active_task` moves with `build_definition_changes`
  and is deleted in 1.2 when its gate lifts.
- `build_session_persona_changes` is deleted.

`apply_agent_definition_impl(agent, db, session_id=None, variables=None,
task_id=None, task_manager=None, cli_source=None, *, relaunch=False)`. Only the
web-chat launch passes `relaunch=True`. The registered tool never does.
1. Resolve the session as today.
2. Read the session variables and the session row.
3. Refuse `spawned_session_definition_fixed` when `is_spawned_agent` is true or
   `hooks/session_activation.py::_session_is_spawned(session)` holds.
4. Resolve the definition, or refuse `unknown_agent_definition`.
5. Refuse `persona_surface_missing`.
6. Refuse `pipeline_requires_spawn` when `workflows.pipeline` is set.
7. Compute the pin, and run `activation_decision(db, variables, agent, pin,
   relaunch=relaunch, same_pin_noop=True)` on the step 2 read.
   - `unchanged` returns `{success: true, status: "unchanged"}` without
     writing. This return comes before step 8, so `variables` and `task_id`
     are ignored (Decision 5).
   - `role_change_requires_relaunch` refuses, naming both. With `relaunch`
     true, the role change proceeds.
   - The same agent with a different pin re-applies, as drift.
8. Resolve `task_id`, or refuse `task_unresolved`.
9. Build the changes with `build_definition_changes(is_spawned=False)` and add
   `_agent_context_injected=False` and `_agent_identity_reinject=True`.
10. Refuse `variable_collision`.
11. Write through `commit_definition_changes(..., expected_agent_type=<the
    step 2 _agent_type>, relaunch=relaunch, same_pin_noop=True)`.
    - `superseded` refuses `activation_superseded`, naming the current agent.
    - `unchanged` and `role_change_requires_relaunch` return the same receipts
      as step 7.
    - An activation that committed first wins, and the other writes nothing.
    - On an identity change the helper clears the previous definition's keys,
      and from 1.2 it ends the previous step instance.

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
- `activate_default_agent` adds `_agent_definition_hash` and
  `_agent_definition_keys` to both its `internal_keys` and `always_reapply`
  sets, so every re-activation refreshes the pin (1.3 compares it first) and
  the key list. It writes through `commit_definition_changes(...,
  expected_agent_type=..., relaunch=False, same_pin_noop=False)` in place of
  its own `merge_variables`.
  - It reads the session's variables once, before `_resolve_agent_name`, and
    passes that read's `_agent_type` as `expected_agent_type`. Any identity
    change committed after that read, including a relaunch to the base agent,
    is caught at the write.
  - A same-agent re-activation still refreshes every always-reapply key and
    keeps the union of key lists.
  - An override on a base-agent row clears the base agent's keys.
  - If the locked check returns `superseded` or
    `role_change_requires_relaunch`, a relaunch committed while it prepared
    its delta. It writes nothing, logs a warning naming the session, the
    current seat and the stale agent, and returns `None`. The relaunch already wrote the current seat's full delta and
    requested reinjection. The reconciler reports `activation_failed` for that
    one event and resolves the current seat on the next.
- `activate_default_agent` passes its `cli_source` to `resolve_agent`, so both
  entry points resolve `provider: inherit` to the session's CLI and compute the
  same pin for the same row.
- `start_hydrated_session` calls `apply_agent_definition_impl` with the
  explicit session id and `relaunch=True` for every pending agent, including
  the base agent. Whenever it applied one, it sets
  `skip_default_agent_activation`, and it never sets `agent_name_override`.
  `_create_chat_session_inner` sets `persona_selected` for a pending `default`
  too (`default.yaml` declares `surfaces: [spawn, persona]`), and it calls
  `build_persona_prompt_context`.
- `activate_default_agent` applies Decision 5 to its `agent_name_override`.
  When `is_role_change` holds for the override, it logs a warning naming the
  session, the stored seat and the ignored override, and activates the stored
  seat instead, so SessionStart never changes roles. An
  override on a base-agent row stays a first activation: that is #22904's
  placed launch (Constraints). The reconciler passes the stored or run agent,
  which is never a role change.
- `_set_attached_session_agent`: after `_validate_persona_agent`, it reads the
  target session's variables. When `is_role_change` holds, it writes no
  command. It sends error code `ROLE_CHANGE_REQUIRES_RELAUNCH` with the text
  "This terminal runs agent '<X>'. Switching to '<Y>' needs a relaunch: start
  a new terminal session and select agent '<Y>' there." It then sends
  `agent_changed` naming X for that `target_session_id`. Both reach the user
  through existing UI paths, so web/ needs no edit. `handleTransportError`
  (`web/src/hooks/useChat/transportConversationEvents.ts:364-391`) appends the
  error text to the chat as a system message, which shows the relaunch
  instruction. `handleAgentChanged` (lines 271-277) restores the label that
  `sendAgentChange` set optimistically (`web/src/hooks/useChat/actionControls.ts`). From the
  base agent, or for the same agent, it sends `/gobby persona <name>` as
  today.
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
- The pin-identity case seeds a session whose `source` is `codex` and a seat row
  with `provider: inherit`. It activates through the tool, drives SessionStart
  `compact` with `cli_source="codex"`, and repeats the tool call.
- The skill-reset cases configure a base agent with restricted skill selectors
  and a `skill_format`, and a seat with neither. The reactivation case patches
  definition resolution to drop the seat's selectors and format before
  SessionStart `compact`.
- The relaunch case seeds two seat rows, X declaring `workflows.variables`
  `{x_only: 1}` and Y declaring none, activates X, and relaunches to Y and,
  separately, to `default`.
- The drift-then-switch case activates X with `x_only`, patches definition
  resolution so X no longer declares it, drives SessionStart `compact`, and
  then relaunches to Y.
- The configured-base case sets `default_agent` to a definition declaring
  `{base_only: 1}`, activates the base agent on the row, and then activates Y
  through the tool without `relaunch`.
- The concurrent-activation case runs two threads on a base-agent row, one
  activating Y and one Z. A `threading.Barrier` patched into step 8's task
  resolution holds both past step 7 until both arrive. Each join is bounded at
  10 seconds.
- The stale-SessionStart case starts from a row at seat X and patches
  `build_agent_changes` to wait on a `threading.Event` after building X's
  delta. While it waits, a `relaunch=True` activation commits. The event is
  then set, with joins bounded at 10 seconds. The case is parametrized over a
  relaunch to Y and a relaunch to `default`.
- The web-chat switch case mirrors the existing launch tests: it patches
  `apply_agent_definition_impl` and asserts the call and `start_data`.
- The override case extends `test_activate_agent_override.py`: a row stored at
  seat X with an override naming seat Y keeps X, and a base-agent row with the
  same override activates Y.
- The attached-terminal case reuses `test_attached_session_agent.py`'s
  `_attached_target` terminal and write-coordinator mocks. That fixture now
  also seeds the target's variables, at the base agent for the existing
  command cases and at seat X for the refusal.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_apply_agent_definition.py tests/hooks/test_session_start_reactivation.py tests/workflows/test_step_snapshot_semantics.py tests/mcp_proxy/tools/skills/test_list_skills.py tests/workflows/test_session_defaults.py tests/servers/websocket/chat/test_servers_websocket_chat_session.py tests/servers/websocket/test_set_agent.py tests/servers/websocket/test_attached_session_agent.py tests/hooks/test_agent_events_coverage.py tests/hooks/test_session_activation_reconciliation.py tests/hooks/event_handlers/test_session_variable_preservation.py tests/hooks/event_handlers/test_activate_agent_override.py tests/hooks/test_session_start_handlers.py tests/hooks/test_session_materialize.py -q`.
Then run `uv run ruff check` and `uv run mypy` on the changed files. 1.1 also
ran a zero-persona `rg` sweep at e8f43fb8ab. The 2026-10-06 amendment above
withdraws it, because #23647 restores `apply_persona` and the `_persona_name`
reads.

Consumers unchanged:
- `src/gobby/hooks/event_handlers/_base.py` — no-edit-reason: calls get_session_skill_exclusions by name; the signature and return type are unchanged, only the variable it reads changes.
- `src/gobby/mcp_proxy/tools/skills/_context.py` — no-edit-reason: calls get_session_skill_exclusions by name with the same signature.
- `tests/mcp_proxy/tools/test_apply_persona.py` — no-edit-reason: #23647 restored it at ef7886df52 after 1.1 removed it; it calls get_session_skill_exclusions by name with the same signature.
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
  write, even when the repeat call passes changed `variables` and a `task_id`
  (no task resolution, merge or reinjection). A seat-to-seat or seat-to-base
  change through the tool returns `role_change_requires_relaunch`. test:
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
- 1.1.8 - A `provider: inherit` seat on a `codex` session keeps one pin across
  tool activation, a `compact` SessionStart and a repeat tool call. The
  SessionStart stores the same pin, and the repeat call returns `unchanged`
  without a write. test:
  `tests/hooks/test_session_start_reactivation.py::test_inherit_provider_pin_is_stable_across_entry_points`.
- 1.1.9 - Activating a seat that selects every skill and sets no skill format,
  over a base agent that restricted both, stores `_active_skill_names: None`
  and `_skill_format: None`. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_clears_inherited_skill_restriction`.
- 1.1.10 - A `compact` SessionStart after the seat's definition drops its skill
  selectors and format stores `_active_skill_names: None` and
  `_skill_format: None`. test:
  `tests/hooks/test_session_start_reactivation.py::test_reactivation_clears_dropped_skill_restriction`.
- 1.1.11 - A hand-launched pane that activates a seat whose definition blocks
  `gobby-worktrees:create_worktree` (`blocked_mcp_tools`) and `EnterWorktree`
  (`blocked_tools`) is refused both by `_check_agent_tool_enforcement`, and
  stays refused after a `compact` SessionStart. #23477 relies on this for the
  plan-family seats (Orchestrator, via gobby#15389, 2026-10-05). test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activated_seat_refuses_worktree_tools`.
- 1.1.12 - On a row at seat X whose definition declares a variable that seat Y
  does not, `relaunch=True` to Y writes Y's delta and key list and sets that
  variable to `None`. `relaunch=True` from X to `default` writes the base
  agent's delta the same way. The same calls without `relaunch` are refused
  `role_change_requires_relaunch` and write nothing. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_relaunch_switches_seat_and_clears_previous_keys`.
- 1.1.13 - A web-chat launch after `set_agent` from X to Y, and from X to
  `default`, calls `apply_agent_definition_impl` with `relaunch=True`, sets
  `skip_default_agent_activation`, sends no `agent_name_override`, and
  completes. test:
  `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::test_agent_switch_relaunches_through_activation`.
- 1.1.14 - `activate_default_agent` with an `agent_name_override` naming seat Y
  keeps seat X on a row stored at X and logs one warning naming the session, X
  and Y. It activates Y on a base-agent row.
  test:
  `tests/hooks/event_handlers/test_activate_agent_override.py::test_override_never_changes_role`.
- 1.1.16 - After seat X's definition drops `x_only` and a `compact`
  SessionStart re-activates X, `x_only` keeps its value and
  `_agent_definition_keys` still names it. A later `relaunch=True` to Y sets
  `x_only` to `None`. test:
  `tests/hooks/test_session_start_reactivation.py::test_drift_then_relaunch_clears_retired_definition_key`.
- 1.1.17 - With `default_agent` configured as a definition that declares
  `base_only`, activating seat Y through the tool without `relaunch` succeeds
  and sets `base_only` to `None`. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_over_configured_base_clears_base_keys`.
- 1.1.18 - Two concurrent tool activations from the base agent, to Y and to Z,
  both pass step 7. Exactly one returns `applied`, the other returns
  `activation_superseded`, and the row holds only the winner's delta.
  test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_concurrent_activations_recheck_permission_under_lock`.
- 1.1.19 - On a row at seat X, a SessionStart activation of X whose delta was
  built before a `relaunch=True` committed writes nothing. This holds for a
  relaunch to Y and for a relaunch to `default`. It returns `None` and logs
  one warning naming the session, the new agent and X. The row keeps the
  relaunched agent's delta. test:
  `tests/hooks/test_session_start_reactivation.py::test_stale_sessionstart_behind_relaunch_keeps_current_seat`.

### 1.2 Step workflows on interactive sessions [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py::*` — scope-reason: commit_definition_changes ends the previous step instance in its transaction, build_definition_changes seeds the step gate for every declared step workflow, _session_has_assigned_or_active_task is deleted, and apply_agent_definition_impl fills the receipt's step_workflow
- `src/gobby/hooks/session_activation.py::_missing_step_state`
- `src/gobby/hooks/session_activation.py::_ensure_step_instance`
- `tests/hooks/test_interactive_step_instance.py::*` — scope-reason: 1.2 creates the interactive step-instance creation, reconcile repair, spawned preservation and replacement tests
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: invert the tests that pinned the no-instance non-goal
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: step recovery no longer requires a spawned session or task

**Granularity:** eight acceptance items, one behavior: the lifecycle of a
session's single step instance. Creation on activation (1.2.1, 1.2.2, 1.2.5),
recovery by the reconciler (1.2.3), preservation for spawned runs (1.2.4) and
replacement on an identity change (1.2.6-1.2.8) all go through the same two
writers, `_ensure_step_instance` and the transition in
`commit_definition_changes`, under the same step lock. Shipping creation
without replacement would leave seat X's steps on a row switched to Y.
Shipping replacement without the locked reread would reopen the stale
reconcile race. No split is warranted.

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
- `commit_definition_changes` (1.1) already holds the step lock and has
  rechecked the decision. On an identity change, it now also deletes the
  previous instance with `AgentStepInstanceManager.delete_for_session` inside
  that transaction, before the merge (adversary finding F-B1-step-race).
  - `_ensure_step_instance` returns early on any existing instance
    (`session_activation.py:660-661`), so without the delete seat X's steps
    would outlive X.
  - The delete and the merge commit together, because `delete_for_session`
    re-enters the held step lock as a no-op (`postgres_pool.py:394-405`).
  - A merge failure rolls back the delete, so the row stays X with X's
    instance.
- `_ensure_step_instance` takes the step lock, rereads the session's
  variables, and resolves `agent_name` from that read. It no longer resolves
  from the caller's snapshot. The snapshot only gates the lock: with no
  resolvable agent, the function returns without locking.
  - A reconcile holding a pre-switch snapshot waits for the transition. It
    then builds Y's instance, or returns on the one already there.
  - The instance save stays outside the transition, so it can still fail on
    its own and report `step_workflow_pending` (Decision 8).
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
- 1.2.4 - A spawned session's spawn-time instance is untouched, including
  when the reconciler activates its run agent over a stored `_agent_type` of
  `default`. test:
  `tests/hooks/test_interactive_step_instance.py::test_spawned_step_instance_unchanged`.
- 1.2.5 - The old no-instance non-goal test is inverted. test:
  `tests/workflows/test_step_snapshot_semantics.py::test_definition_activation_materializes_step_instance`.
- 1.2.6 - A relaunch from seat X with steps to seat Y with steps leaves one
  instance, Y's, at Y's first step. A relaunch from X to `default` leaves no
  instance. test:
  `tests/hooks/test_interactive_step_instance.py::test_relaunch_switch_replaces_step_instance`.
- 1.2.7 - With `default_agent` configured as a definition that declares steps,
  activating seat Y through the tool without `relaunch` leaves one instance,
  Y's, at Y's first step. test:
  `tests/hooks/test_interactive_step_instance.py::test_activation_over_configured_base_replaces_base_instance`.
- 1.2.8 - A relaunch from seat X to seat Y, both with steps, ends with one
  instance, Y's, in two cases:
  - A reconcile holding X's pre-switch snapshot runs after the transition
    commits, while Y's own instance save is injected to fail.
  - A reconcile thread starts while the transition is paused inside the merge
    on a `threading.Event`. Each join is bounded at 10 seconds.
  A merge failure injected inside the transition leaves X's variables and X's
  instance. test:
  `tests/hooks/test_interactive_step_instance.py::test_switch_serializes_with_stale_reconcile`.

### 1.3 Definition drift receipt on re-activation [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py::*` — scope-reason: adds definition_drift_line and stores the drift line from apply_agent_definition_impl's same-agent re-apply
- `src/gobby/hooks/event_handlers/_session_start/agents.py::activate_default_agent`
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`
- `tests/hooks/test_session_start_reactivation.py::*` — scope-reason: adds the SessionStart drift-line cases beside the 1.1 re-activation tests
- `tests/mcp_proxy/tools/test_apply_agent_definition.py::*` — scope-reason: adds the same-seat repeat-call drift case beside the 1.1 activation tests

**Granularity:** the drift comparison is shared by the tool and SessionStart,
so both entry points change with it. 1.3 depends on 1.2 because 1.2 also edits
`apply_agent_definition.py`.

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
- The tool re-applies a same-seat changed pin as drift (1.1 step 7) and already
  sets `_agent_identity_reinject`. Without its own drift line, the next
  SessionStart sees the new pin and cannot report the change (adversary finding
  F-tool-drift-receipt).

Implementation:
- New function in `apply_agent_definition.py`:
  `definition_drift_line(existing: Mapping[str, Any], agent: str, new_pin: str) -> str | None`.
  When `existing["_agent_type"]` equals `agent` and the stored
  `_agent_definition_hash` is non-null and differs from `new_pin`, it returns a
  single line of the form "Definition `<agent>` changed since this session
  activated it (`<old[:12]>` → `<new[:12]>`); the current definition now
  applies." Otherwise it returns `None`.
- In `activate_default_agent`, call it with `existing` after the
  `always_reapply` filter, immediately before `merge_variables`, so the filter
  cannot drop the keys. For a line, add to the filtered changes:
  - `_agent_definition_drift`: the line;
  - `_agent_identity_reinject: True`.
- In `apply_agent_definition_impl` step 9, call it with the variables read at
  step 2. For a line, add `_agent_definition_drift` to the changes; the
  reinjection flag is already set.
- In `_inject_agent_instructions_if_needed`, when `_agent_definition_drift` is a
  non-empty string, add it after the preamble and stage it back to `None` with
  the other flags.
- Neither path produces a drift line in these cases:
  - no stored pin (the first activation);
  - the same pin;
  - a different agent. A `/clear` successor carrying the predecessor's pin is
    the same agent, so 1.4 reaches this path.

Rejected: a new context channel on `AgentActivationResult`. It was removed by
the Claude 5 prompt cleanup, and the reinjection flag path already delivers
exactly one line on the next turn.

Tests: the step-instance drift case mirrors the `snap_db` and `_agent` helpers
of `tests/workflows/test_step_snapshot_semantics.py`. It seeds the instance with
`spawn_agent/_step_state.py::persist_initial_step_instance`, as
`test_definition_edit_does_not_mutate_running_snapshot` does. It activates the
seat, advances the instance to its second step, and then patches definition
resolution to a body with changed rules, tool blocks and step list before it
drives SessionStart `compact` and `resume`. The tool case activates a seat,
patches definition resolution to a changed row, calls the tool again for the
same seat, runs `_inject_agent_instructions_if_needed` twice, and then repeats
the tool call unchanged.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_session_start_reactivation.py tests/mcp_proxy/tools/test_apply_agent_definition.py tests/hooks/event_handlers/test_session_variable_preservation.py -q`.

**Acceptance:**

- 1.3.1 - A resume or compact SessionStart whose seat definition changed applies
  the current row, stores the new pin, and injects the drift line exactly once,
  including on a session that already stores `_agent_identity_reinject: False`
  and `_agent_definition_drift: None`. test:
  `tests/hooks/test_session_start_reactivation.py::test_reactivation_reports_definition_drift_once`.
- 1.3.2 - An unchanged pin injects no drift line, including for a
  `provider: inherit` seat on a `codex` session whose pin the tool wrote. test:
  `tests/hooks/test_session_start_reactivation.py::test_unchanged_pin_injects_no_drift_line`.
- 1.3.3 - On a stepped seat advanced past its first step, a compact or resume
  SessionStart after a change to the definition's rules, tool blocks and step
  list stores the new pin, rule set and blocked tools and injects one drift
  line. The step instance keeps its id, its current step and its snapshot.
  test:
  `tests/hooks/test_session_start_reactivation.py::test_drift_reactivation_keeps_running_step_instance`.
- 1.3.4 - A same-seat tool call after the seat's row changed stores the new pin
  and stages one drift line, which the next injection delivers once and the one
  after it does not. An unchanged repeat call stages none. test:
  `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_same_seat_changed_row_reports_drift_once`.

### 1.4 /clear successor keeps its seat and its run [category: code] (depends: 1.2, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::_bind_clear_successor`
- `src/gobby/sessions/clear_run_lineage.py`
- `src/gobby/sessions/clear_continuation.py::take_clear_handoff_marker`
- `src/gobby/sessions/clear_continuation.py::_commit_web_chat_clear_successor_rows`
- `src/gobby/hooks/event_handlers/_session_end.py::SessionEndMixin.handle_session_end`
- `tests/hooks/test_clear_successor_seat.py`
- `tests/hooks/test_session_end_handlers.py::*` — scope-reason: cover a staged clear end that hands its run to the successor

**Granularity:** the seat and the run cross the same `/clear` boundary, through
the same marker take, and the regression for each needs the other: a successor
that keeps its seat but loses its run is the bug the Orchestrator's Q3 ruling
(2026-10-05) folds in here.

**Research context:** today's behavior for the seat:
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
For the run of a spawned seat (Q3, verified with `gcode evidence` at
7c80404126; excerpt hashes are prefixes):
- `set_handoff(clear_session=true)` stages the clear attempt and moves the
  predecessor to `awaiting_handoff` (`clear_continuation.py::stage_clear_attempt`,
  lines 102-123, `00dedf6a`). `set_handoff` refuses only headless runs
  (`_terminal.py:561-566`, `a1298cce`), so a terminal-backed seat can clear.
- SessionEnd `clear` ends the run before the successor starts.
  `handle_session_end` derives `end_status = "expired"` from the `clear`
  reason for any session without a tmux target (`_session_end.py:67-85`,
  `58e023de`). Spawned seats run in Gobby terminals, so they take that branch.
  - The status write is skipped for an `awaiting_handoff` row (lines 180-190,
    `0628a269`), but `terminal_outcome` still comes from `end_status`.
  - With `session.agent_run_id` set, it calls
    `SessionCoordinator.complete_agent_run` (lines 109-112, `8c5b8f04`),
    which has no `/clear` exemption.
  - It also marks the run's terminal row exited (`mark_exited`, lines
    198-208).
- Nothing moves the run to the successor.
  - The take moves only `agent_runs.parent_session_id`
    (`clear_continuation.py:478-490`, `2e67a571`; the web-chat commit repeats
    it at line 779).
  - `update_child_session` has two callers, spawn (`_runtime.py:182`) and the
    admin test route (`admin/_testing.py:182`). The only other
    `child_session_id` writes set it to NULL (`_failure_cleanup.py:491`,
    `projects/purge.py:147`).
  - `TerminalManager.bind_session` never rebinds an agent terminal: its update
    requires `t.agent_run_id IS NULL` (`storage/terminals.py:715-727`,
    `928b2f6a`).
- A stale binding has concrete consumers.
  - `list_termination_candidates` returns a running run with a live terminal
    when any expired session has `id = child_session_id` or
    `agent_run_id = run` (`storage/agents/_termination.py:126-145`,
    `dd42756f`). `reconcile_pending_terminations` then terminalizes it.
  - External write grants refuse a session that is not the run's
    `child_session_id` (`external_write_grants.py:84-88`, `1e968128`).
- Spawned-ness at activation comes from the session row.
  - `build_agent_changes` re-reads the session and calls `_session_is_spawned`
    (`_session_start/agents.py:54-55`, `969fc702`).
  - `activate_materialized_session` refreshes `session_obj` after the bind and
    before `_activate_default_agent`.
  - The reconciler's `_backfill_terminal_pickup` sets `sessions.agent_run_id`
    from the pane's `GOBBY_AGENT_RUN_ID` (`session_activation.py:177-183`,
    `d9320690`), but only on a later hook event. Without a write in the take,
    the successor activates with `is_spawned_agent: False` and the `persona`
    prompt surface.
- `claimed_session_id` records the session that owned the task when the run was
  created (`agents/spawn.py:213`). The take never moved it, and this
  deliverable leaves it alone: it is creation-time provenance, and the seat's
  binding is `child_session_id`.
- Spawn depth does not cross a terminal `/clear` (adversary finding
  F-clear-spawn-depth, gobby#15414, verified at 9e79ef1).
  - The successor's SessionStart reads `agent_depth` from the hook payload and
    defaults to 0 when it is absent (`_session_start/flow.py:303` and `:322`).
    It then registers the row with that depth (lines 411-423).
  - `can_spawn_child` reads the stored depth (`agents/session.py:116`) against
    the depth limit (lines 133-141), so a depth-5 seat could spawn after a
    clear.
  - The web-chat clear already copies the predecessor's `agent_depth` and
    `spawned_by_agent_id` (`clear_continuation.py:724-725`).

Implementation, seat identity:
- In the same `merge_variables` call that sets `HANDOFF_PULL_PENDING_VARIABLE`
  and the inherited proxy readiness, copy the predecessor's `_agent_type` and
  `_agent_definition_hash` when
  `_agent_type` is set and is not the base agent.
- `_activate_default_agent` then resolves the seat and applies the full delta to
  the fresh session. 1.3 compares the carried pin.
- The successor is a new session with no step instance, so `_ensure_step_instance`
  (lifted in 1.2) creates a fresh one at the first step of the definition it
  resolves now, on the first hook event: a clear ends a unit of work.
- The predecessor's variables are already read into `predecessor_vars`, so no
  extra query is needed.

Implementation, run binding:
- SessionEnd: in `handle_session_end`, a `clear` end on a session whose row is
  already `awaiting_handoff` and has `agent_run_id` set hands its run to the
  successor. It skips `complete_agent_run` and the terminal `mark_exited`.
  Status handling and step-instance deletion are unchanged. A `clear` end
  without a staged attempt (`status` not `awaiting_handoff`) keeps today's
  behavior, and the run ends.
- `clear_continuation.py` is at 860 lines, so the run-lineage writes move out of
  it into the new `sessions/clear_run_lineage.py` rather than growing it. The
  move takes the `parent_session_id` updates from `take_clear_handoff_marker`
  (lines 482-490, including the supersede id) and from
  `_commit_web_chat_clear_successor_rows` (line 779). Both call one function,
  `move_clear_run_lineage(conn, *, predecessor_id, successor_id, session_ids)`,
  inside their existing transaction, so the web-chat clear follows the same
  lineage rule. It does nothing extra there, because a web-chat session is
  never a run's child and its successor row already carries the predecessor's
  depth. The function:
  1. moves `agent_runs.parent_session_id` from `session_ids` to the successor
     (the moved statements);
  2. moves `agent_runs.child_session_id` from `session_ids` to the successor
     for runs in `pending` or `running`;
  3. for the run it moved, sets `sessions.agent_run_id` to NULL on the
     sessions in `session_ids` that point at it, sets the successor's
     `agent_run_id` to it when null, and moves the run's live terminal row
     (`terminals.agent_run_id` = run, `state` in `pending` or `live`) to the
     successor;
  4. copies `agent_depth` and `spawned_by_agent_id` from `predecessor_id`, the
     session whose clear marker the take consumes, to the successor. The
     source is that stored row, never `parent_session_id` or hook input, and
     a superseding successor copies from the same predecessor.
- The take expires the predecessor and calls the function in one transaction,
  before `_activate_default_agent`. No reader sees a live run pointing at an
  expired session: before the take the predecessor is `awaiting_handoff`, and
  after it no expired session points at the run.
- A staged clear whose successor never starts leaves the run on an
  `awaiting_handoff` predecessor. The existing orphaned-handoff sweep
  (`sessions/lifecycle.py`, `expire_orphaned_handoff_sessions`) expires that
  row, and `reconcile_pending_terminations` then terminalizes the run. No new
  sweep is added.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_clear_successor_seat.py tests/hooks/test_session_start_handlers.py tests/hooks/test_session_end_handlers.py tests/hooks/test_session_events_coverage.py tests/hooks/test_session_materialize.py tests/sessions/test_clear_acknowledgment.py tests/sessions/test_handoff.py tests/sessions/test_mailbox.py tests/servers/websocket/chat/test_clear_session.py tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py -q`.
Then run `uv run ruff check` and `uv run mypy` on the changed files.
`test_execution_session_end_cleanup.py` drives `handle_session_end` without a
clear reason, so the staged-clear hand-off never applies there; 1.6 owns its
only edit.

Consumers unchanged:
- `tests/hooks/test_session_materialize.py` — no-edit-reason: calls take_clear_handoff_marker, whose signature and boolean result are unchanged; its assertions read sessions.parent_session_id, which the take does not move.
- `tests/sessions/test_clear_acknowledgment.py` — no-edit-reason: calls take_clear_handoff_marker with the same signature and result; its agent-run manager is a mock with no bound run.
- `tests/sessions/test_handoff.py` — no-edit-reason: calls take_clear_handoff_marker with the same signature and result; its runs are children of other sessions, so only their parent_session_id moves, as today.
- `tests/sessions/test_mailbox.py` — no-edit-reason: calls take_clear_handoff_marker with the same signature and result; it seeds no agent run.
- `tests/workflows/test_session_end_cleanup.py` — no-edit-reason: drives handle_session_end without a clear reason, so the staged-clear hand-off never applies.

**Acceptance:**

- 1.4.1 - A `/clear` successor of an activated seat carries `_agent_type` and the
  pin and re-activates as that seat. test:
  `tests/hooks/test_clear_successor_seat.py::test_clear_successor_inherits_agent_type_and_pin`.
- 1.4.2 - The successor gets a fresh step instance at the first step of the
  current definition. After a definition edit, that is the new step list. test:
  `tests/hooks/test_clear_successor_seat.py::test_clear_successor_gets_fresh_step_instance`.
- 1.4.3 - A base-agent predecessor with no run carries nothing: the successor
  has no `_agent_type`, and its `agent_run_id` stays null. test:
  `tests/hooks/test_clear_successor_seat.py::test_base_agent_clear_successor_unchanged`.
- 1.4.4 - After a staged `/clear`, a spawned interactive seat keeps its run.
  - After SessionEnd `clear`, the run is still `running` and its terminal is
    still live.
  - After the successor's SessionStart:
    - the run's `child_session_id`, the successor's `agent_run_id` and the
      run terminal's `session_id` all name the successor;
    - the predecessor's `agent_run_id` is null;
    - the successor activates with `is_spawned_agent: True`;
    - runs the predecessor spawned have the successor as
      `parent_session_id`;
    - `list_termination_candidates` does not return the run.

  test:
  `tests/hooks/test_clear_successor_seat.py::test_spawned_interactive_seat_keeps_run_binding_after_clear`.
- 1.4.5 - A `clear` end on a run-bound `awaiting_handoff` session neither
  completes the run nor marks its terminal exited. A `clear` end on a session
  with no staged attempt still does both. test:
  `tests/hooks/test_session_end_handlers.py::test_staged_clear_end_hands_run_to_successor`.
- 1.4.6 - When a newer successor supersedes a bound one, the run moves from the
  superseded successor to the newer one. test:
  `tests/hooks/test_clear_successor_seat.py::test_superseding_successor_takes_the_run`.
- 1.4.7 - A depth-5 seat's terminal clear successor, whose SessionStart payload
  carries no `agent_depth`, stores the predecessor's `agent_depth` and
  `spawned_by_agent_id`, and so does a superseding successor.
  `can_spawn_child` refuses both. test:
  `tests/hooks/test_clear_successor_seat.py::test_clear_successor_keeps_spawn_depth`.

### 1.5 Seat rules match `_agent_type` only [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml::*` — scope-reason: drop the persona-overlay _persona_name clause, its comment and the seat-identity guidance wording
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml::*` — scope-reason: drop the persona-overlay _persona_name clauses from every seat condition and the header comment
- `src/gobby/install/shared/workflows/rules/roles/seat-write-scope.yaml::*` — scope-reason: drop the persona-overlay _persona_name clauses from both write-scope conditions
- `tests/workflows/test_seat_rules.py::*` — scope-reason: fixtures set _agent_type instead of _persona_name, plus one persona-only regression

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
clauses then only let a persona overlay act as a seat, which Decision 3
forbids: `apply_persona` switches prompt and skills and never seat rules. The
V2 rule-directory `rg` cannot pass while they remain. The edit cannot land
before 1.1, because until then a seat session carried only `_persona_name`.
Bundled rule templates sync to the rule registry (AGENTS.md rule 8).

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
- Both comments say that activation and spawn both set `_agent_type`, and
  that a persona overlay (`_persona_name`) never matches a seat rule.
- The guidance line becomes "Your seat is the definition named by your
  `_agent_type`."
- Test fixtures set `{"_agent_type": <seat>}`. A new test asserts that a
  context carrying only `_persona_name: plan-writer` matches no seat rule.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_seat_rules.py -q`, and
`rg -w _persona_name src/gobby/install/shared/workflows/rules`, which must print
nothing. The test file keeps exactly one persona-only fixture, the 1.5.1
context: `rg -w -c _persona_name tests/workflows/test_seat_rules.py` must print
`1`.

**Acceptance:**

- 1.5.1 - Every `rules/roles/` condition matches a seat through `_agent_type`
  alone, and a context that carries only `_persona_name` matches none of them.
  test: `tests/workflows/test_seat_rules.py::test_persona_name_alone_matches_no_seat_rule`.
- 1.5.2 - Seat guidance injection still matches a session whose `_agent_type`
  names a seat and skips one that names no seat. test:
  `tests/workflows/test_seat_rules.py::test_seat_common_matches_spawned_and_skips_non_seats`.

### 1.6 Managed pane identity follows its run across /clear [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/sessions/clear_run_lineage.py`
- `src/gobby/servers/routes/mcp/endpoints/request_context.py::_set_context_for_request`
- `src/gobby/servers/routes/mcp/hooks.py::execute_hook`
- `src/gobby/servers/routes/sessions/variables.py::_bound_session_id`
- `src/gobby/servers/routes/llm.py::chat_completions`
- `tests/servers/test_managed_clear_identity.py`
- `tests/servers/routes/mcp_endpoints/test_execution_context.py::*` — scope-reason: the MagicMock server's auth_service.verified_agent_claims returns None, so the mock does not stand in for run claims
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py::*` — scope-reason: the MagicMock servers' auth_service.verified_agent_claims returns None
- `tests/servers/test_mcp_execution_context.py::*` — scope-reason: the MagicMock server's auth_service.verified_agent_claims returns None
- `tests/servers/routes/test_llm_routes.py::*` — scope-reason: the SimpleNamespace claims stub gains agent_run_id=None

**Granularity:** one rule, applied at the four places where a run-bound
capability names its caller session. 1.4 makes the run's binding authoritative,
and this deliverable makes requests follow it. It is split from 1.4 because it
changes server ingress, while 1.4 changes the clear lifecycle.

**Research context:** adversary finding F-clear-capability-continuity
(gobby#15414) reported that the successor is refused. The source shows that
auth accepts the successor everywhere. After that, MCP ingress refuses it, and
the other three ingress sites attribute it to the expired predecessor. Verified
at 9e79ef1, and the MCP refusal at b9afc3b:
- A spawned pane keeps its launch environment across `/clear`.
  - ghook sets `X-Gobby-Session-Id` from `GOBBY_SESSION_ID` before the payload
    session (`crates/ghook/src/dispatch.rs:402-413`).
  - The MCP stdio proxy sends `GOBBY_SESSION_ID` as the caller for managed runs
    (`mcp_proxy/stdio_proxy.py::DaemonProxy._request`).
  - `utils/local_token.py::daemon_auth_headers` sends the run token from
    `GOBBY_AGENT_API_TOKEN`, with `GOBBY_SESSION_ID` and `GOBBY_AGENT_RUN_ID`
    beside it.
  - The token's `session_id` claim is the session it was issued for
    (`issue_agent_api_token`, `local_token.py:96`).
- Auth passes after the clear. `auth_service.py::_agent_identity_matches` checks
  the header session against `claims.session_id` (lines 170-174), and both still
  name the predecessor.
- MCP ingress refuses after the take. `_set_context_for_request` seeds the
  header session (the predecessor) as `tokens.resolved_session_id` and calls
  `_bind_agent_run_context` (`request_context.py:312-363`, called at lines 230
  and 288). That check compares the run's `child_session_id`, which is now the
  successor, against the seeded session, and raises 403 "Invalid agent run
  identity" (lines 330-334). Before the take, the two match and the call is
  attributed to the predecessor.
- The other three sites attribute to the predecessor:
  - hook ingress puts the header into `_platform_session_id` (`hooks.py:284`),
    and `SessionLookupService.resolve` returns an explicit platform session as
    it is (`hooks/session_lookup.py:155-160`). Revival of a cleared row is
    suppressed (`storage/sessions/_terminal_revival.py`), but the id is still
    returned;
  - `routes/sessions/variables.py::_bound_session_id` returns
    `claims.session_id` (lines 24-38);
  - `routes/llm.py::chat_completions` takes `claims.session_id` (line 308) and
    passes it as `ToolChatRequest.session_id`. The route requires a `tool_chat`
    runtime grant whose principal matches the bearer's issued session
    (`auth_service.py:295`, `grant_auth.py::bearer_matches_grant`).
  - `utils/session_context.py` follows no clear chain.
- After 1.4's take, the run's `child_session_id` and the successor's
  `agent_run_id` name each other, and the predecessor's `agent_run_id` is null.
  Before the take, `child_session_id` is still the predecessor. That pair is the
  live binding.

Implementation:
- New function in `sessions/clear_run_lineage.py`:
  `current_run_session_id(db, *, agent_run_id: str | None, session_id: str, project_id: str) -> str`.
  It returns the run's `child_session_id` when all of these hold:
  - `agent_run_id` is set;
  - the run is `pending` or `running`;
  - the child session's `agent_run_id` is that run;
  - the child session's `project_id` equals `project_id`.

  Otherwise it returns `session_id`. It follows the run's binding and never
  walks the clear chain, so a predecessor with no run is never forwarded.
- Each call site passes the verified claims (`agent_run_id`, `session_id`,
  `project_id`):
  - `_set_context_for_request`: when `server.auth_service.verified_agent_claims(request)`
    returns claims, it seeds the returned session in place of the header
    session. Auth has already bound the header to `claims.session_id`. The
    swap happens before both `_bind_agent_run_context` calls, so the run check
    compares the run's child with the successor and admits the call.
  - `execute_hook`: it stores the returned session as `_platform_session_id`.
  - `_bound_session_id`: it accepts a requested session that resolves to
    `claims.session_id` or to the returned session, and returns the returned
    session.
  - `chat_completions`: it uses the returned session.
- Unchanged, with reasons:
  - `_agent_identity_matches`: the header still names the token's session.
  - `runtime_grants/handshake.py` (line 90 recomputes the token signature over
    its own claims; line 165 binds a grant principal to the token's session),
    `routes/runtime_handshake.py:165` and `grant_auth.py::bearer_matches_grant`
    (line 249). The grant and the token both name the issued session, as
    capability provenance, so a grant stays valid across the clear. Caller
    attribution is decided at the four sites above.
- Scope and revocation are unchanged. A token speaks only for its own run, in
  its own project, on its own machine. Once the run ends, `_managed_capability_is_live`
  rejects the token (`run_inactive`) before any of these sites runs.

Tests: `tests/servers/test_managed_clear_identity.py` uses the HTTP server
fixtures of `tests/servers/test_auth_service.py`. It seeds the post-take state
of 1.4: an expired predecessor with a consumed clear marker, a live successor,
and a `running` run whose `child_session_id` is the successor. It mints a token
for (run, predecessor, project) with `issue_agent_api_token` and sends the
headers a spawned pane sends. The 1.6.4 case also issues a `tool_chat` grant
for that principal through the fixture's grant service and stubs only the tool
chat service.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_managed_clear_identity.py tests/servers/test_auth_service.py tests/servers/test_grant_auth.py tests/servers/routes/mcp_endpoints/test_execution_context.py tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py tests/servers/test_mcp_execution_context.py tests/servers/routes/test_llm_routes.py tests/servers/routes/test_session_variables.py tests/servers/routes/mcp/test_hook_session_metadata.py -q`.
Then run `uv run ruff check` and `uv run mypy` on the changed files.

Consumers unchanged:
- `src/gobby/servers/routes/mcp/endpoints/execution.py` — no-edit-reason: calls _set_context_for_request(server, arguments, request) at four sites; the signature and the returned tokens are unchanged.
- `src/gobby/servers/routes/mcp/endpoints/bridge.py` — no-edit-reason: calls _set_context_for_request(server, {}, request) at line 27 and writes _mcp_proxy_ready to the returned resolved_session_id, which then names the successor with no edit.
- `tests/servers/routes/mcp_endpoints/test_bridge.py` — no-edit-reason: replaces _set_context_for_request with an AsyncMock (lines 63, 82), so the helper body never runs.
- `tests/mcp_proxy/services/test_scope_resolution_matrix.py` — no-edit-reason: patches _set_context_for_request by path with a seeding side effect (line 381), so the helper body never runs.

**Acceptance:**

- 1.6.1 - After a staged clear, a wrapper `call_tool` for a session-scoped tool
  that carries the pane's frozen headers and token is admitted, where today it
  is refused 403, and is attributed to the successor: a `set_variable` through
  it lands on the successor's row, and the predecessor's row is unchanged. test:
  `tests/servers/test_managed_clear_identity.py::test_successor_tool_call_is_attributed_to_successor`.
- 1.6.2 - A hook event with the frozen headers resolves `_platform_session_id`
  to the successor, and `POST /api/sessions/<predecessor>/variables/get`
  returns the successor's variables. test:
  `tests/servers/test_managed_clear_identity.py::test_successor_hook_and_variables_follow_run`.
- 1.6.3 - Nothing is forwarded in these cases:
  - before the take;
  - for a token whose run has ended, which is refused `run_inactive`;
  - for a token of another run;
  - for a session with no run.

  test:
  `tests/servers/test_managed_clear_identity.py::test_no_forwarding_without_live_run_binding`.
- 1.6.4 - After a staged clear, `POST /api/llm/chat/completions` with the frozen
  bearer and a valid `tool_chat` grant issued for the predecessor passes real
  auth and grant checks, and the stubbed tool chat service receives
  `ToolChatRequest.session_id` equal to the successor. Once the run ends, the
  same request is refused `run_inactive`. test:
  `tests/servers/test_managed_clear_identity.py::test_successor_chat_completion_keeps_issued_grant`.

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
- `tests/agents/watchdog/test_interactive_lifecycle_cleanup.py::*` — scope-reason: assert idle_ttl_seconds survives resume beside execution_mode

**Landing order:** the workspace index pin plan
(`.gobby/plans/workspace-index-pin.md`, reviewed under #23443 "Enhancer and
adversary passes on the workspace index pin plan") also moves code out of
`spawn_agent_impl` in its T1 and A1. It is Josh's priority 1, so its T1 and A1
land first, and this leaf rebases its `spawn_agent_impl` move onto them
(Orchestrator ruling, 2026-10-05 09:00 CT).

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
- No reader is added here. The reader is deferral D3 (R4 item L2):
  `_handle_idle_check`'s interactive branch (`idle_check_handler.py:481`) ends
  the run once its idle time reaches `idle_ttl_seconds`. Its task goes under
  #22691 at expansion, after Josh approves this plan.
- No spawn-time TTL override is added: no caller needs one.

Tests:
- The `test_factory.py` persistence matrix covers four cases:
  - an interactive definition with a TTL stores the key;
  - an interactive definition without a TTL stores no key;
  - an interactive definition with a TTL under a spawn-time `one_shot`
    override stores no key;
  - a `one_shot` definition under a spawn-time `interactive` override stores
    no key.
- Each case asserts that `idle_ttl_seconds` appears only at the top level of
  `resume_metadata` and never in the initial variables.
- `test_interactive_lifecycle_cleanup.py::test_missing_interactive_terminal_resumes_after_task_close`
  already asserts that resume carries `execution_mode`. It gains a TTL in
  the stored `resume_metadata` and asserts that the TTL survives resume.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_agent_definitions_v2.py tests/mcp_proxy/tools/spawn_agent/test_factory.py tests/agents/watchdog/test_interactive_lifecycle_cleanup.py tests/agents/test_agents_sync.py -q`.
Then run `uv run ruff check` and `uv run mypy` on the changed files.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` — no-edit-reason: passes the spawn-time execution_mode through unchanged; the TTL comes from the definition only.
- `src/gobby/storage/agents/_models.py` — no-edit-reason: is_interactive keeps reading execution_mode; the TTL reader belongs to deferral D3.
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
  `idle_ttl_seconds` at the top level of `resume_metadata`. An interactive
  definition without a TTL, a spawn-time `one_shot` override, and a `one_shot`
  definition overridden to `interactive` store none. test:
  `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_idle_ttl_is_persisted_only_for_interactive_runs`.
- 2.1.3 - Resume carries `idle_ttl_seconds` through with `execution_mode`.
  test:
  `tests/agents/watchdog/test_interactive_lifecycle_cleanup.py::test_missing_interactive_terminal_resumes_after_task_close`.

## P3: Migration
`kind: framing`

**Goal:** every document and bundled skill names the tool that exists.

### 3.1 Bundled references and guides [category: docs] (depends: 1.2, 1.4, 1.6, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/agents/personas.md`
- `docs/guides/agents.md`
- `docs/guides/workflows-overview.md`
- `docs/reference-audit/variables.json::*` — scope-reason: the selector finding's implementation pointer names build_persona_changes, which 1.1 moved to apply_agent_definition.py::build_definition_changes

**Research context:** what each file says today:
- `personas.md` routes `/gobby persona <name>` (sent by the web UI's
  `_set_attached_session_agent`). It tells the agent to call
  `gobby-agents:apply_persona`, says `agent="default"` restores the default, and
  says activation installs no rules, tool restrictions or step instances. It
  does not mention `apply_agent_definition`.
- `docs/guides/agents.md`:
  - the `persona` surface row names only `gobby-agents:apply_persona`;
  - a paragraph says `apply_persona` is "intentionally narrow";
  - the run-tools list names `apply_persona` and not `apply_agent_definition`.
- `workflows-overview.md` says a definition "can be applied to the current
  session with `gobby-agents:apply_persona`".
- `docs/reference-audit/variables.json`'s selector finding points at
  `apply_persona.py::build_persona_changes`.
- These stay as they are, because each describes a persona switch inside a
  running session, which is `apply_persona`'s role (Decision 1):
  `review/epic.md` (the in-line epic review calls
  `apply_persona(agent="epic-reviewer")`), the phrase that
  `test_review_skill.py::test_epic_review_references_pin_routing_and_verdict_mapping`
  pins, and `review.yaml`'s `mode` input.
- `docs/reference-audit/agents.json` stays as it is: #23647 restores its
  `apply_persona` inventory entry beside the `apply_agent_definition` entry,
  because `tests/skills/test_reference_library.py` maps every registered tool.
- #23647 (Restore apply_persona) blocks this leaf: the edits document the
  restored tool.

Edits:
- `personas.md` keeps calling `gobby-agents:apply_persona`, and keeps its
  `agent="default"` sentence and its no-rules, no-tool-restrictions,
  no-step-instance sentence. It adds:
  - On a session with an active non-default definition, `agent="default"`
    returns to that definition's own prompt and skill set, and the
    definition's rules, tool blocks and step workflow stay in force.
  - The persona's prompt, skill set and skill format survive compaction and
    resume.
  - To activate a whole definition (rules, variables, tool blocks and the
    step workflow as well), call `gobby-agents:apply_agent_definition`. An
    activation that writes clears any persona overlay. An `unchanged` receipt
    or a refusal writes nothing and keeps the overlay. To drop the overlay
    without activating, call `apply_persona(agent="default")`. A session
    already bound to a non-default definition refuses another with
    `role_change_requires_relaunch`, and the user relaunches the pane instead.
- `agents.md`:
  - The `persona` surface row names both tools: `apply_persona` switches the
    persona prompt and skill selection live, and `apply_agent_definition`
    activates the whole definition.
  - The "intentionally narrow" paragraph stays, describing `apply_persona`. A
    new paragraph after it gives the `apply_agent_definition` activation
    contract: Decisions 2, 3, 4, 5, 7 and 8, the continuity table from
    Decision 10, and the run-lifetime fields `execution_mode` and
    `idle_ttl_seconds` (Decision 9).
  - The run-tools list names `apply_agent_definition` beside `apply_persona`.
- `workflows-overview.md`: a definition is activated on the current session
  with `gobby-agents:apply_agent_definition`, its persona is switched live
  with `gobby-agents:apply_persona`, or it is used to spawn a child session.
- `variables.json`: the pointer becomes
  `src/gobby/mcp_proxy/tools/apply_agent_definition.py::build_definition_changes`.

Planned verification: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_review_skill.py tests/skills/test_reference_library.py -q`.
`rg -w -l apply_agent_definition src/gobby/install/shared/skills/gobby/references/agents/personas.md docs/guides/agents.md docs/guides/workflows-overview.md`
must print all three paths, and
`rg -w -l apply_persona src/gobby/install/shared/skills/gobby/references/agents/personas.md docs/guides/agents.md docs/guides/workflows-overview.md`
must print all three as well.

**Acceptance:**

- 3.1.1 - The persona reference keeps `apply_persona` for the live persona
  switch, and routes whole-definition activation to `apply_agent_definition`
  with its relaunch refusal. behavior: "role_change_requires_relaunch" in
  `src/gobby/install/shared/skills/gobby/references/agents/personas.md`.
- 3.1.2 - The epic-review reference, its pinned test phrase and the review
  pipeline's `mode` input keep `apply_persona` for the in-line review. test:
  `tests/skills/test_review_skill.py::test_epic_review_references_pin_routing_and_verdict_mapping`.
- 3.1.3 - The agents guide documents both tools, the activation contract, the
  continuity table and `idle_ttl_seconds`. behavior:
  "role_change_requires_relaunch" in `docs/guides/agents.md`.
- 3.1.4 - The workflows overview names both tools, and the variables audit
  points at `build_definition_changes`. file:
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

Amended 2026-10-06 (#23671): Josh's dedupe folded the D1 task #23511 and the D3
task #23497 into the 2.1 leaf #23509, "Run lifetime on agent definitions: idle
TTL field, watchdog enforcement and seat values", which carries both provenance
labels. D1 and D3 now name #23509.

```yaml
deferral:
  task_ref: "#23509"
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
  task_ref: "#23512"
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

## D3 Idle-TTL enforcement, R4 item L2 (depends: 2.1)
`kind: deferred`

Once 2.1 stores `idle_ttl_seconds` in the run's `resume_metadata`, the
watchdog enforces it:
- Reader: the interactive branch of `IdleCheckHandler._handle_idle_check`
  (`idle_check_handler.py:481`). Today that branch resets the idle detector and
  returns for every interactive run.
- Idle clock: the run's `child_session_id` session, through
  `last_session_activity(session_id)` and `session.updated_at`, which the
  handler already reads. After 1.4, that session is the seat's current `/clear`
  successor.
- Once the idle time reaches `idle_ttl_seconds`, the run takes the reprompt
  path that one-shot runs use today. The agent is reprompted to wrap up, save
  and call `end_agent_run`, and the existing completion path ends the run when
  the reprompts are exhausted.
- An interactive run without the key keeps #23442's behavior: idleness never
  ends it.

The Orchestrator's rulings (2026-10-05, 08:10 and 08:18 CT) parent this work
under #22691 (Lane 3 - Runbooks). Expansion creates a placeholder deferral's
task under the plan's root epic and leaves a numeric `task_ref` alone
(`tasks/expansion/_deferrals.py::create_placeholder_deferral_tasks`), so D3
reaches its parent through a numeric ref:
1. After Josh approves this plan and before expansion, the Orchestrator files
   the D3 task under #22691. Its labels are
   `deferred-from:<plan_id>:<D3 section_id>`, `needs-planning` and the plan
   provenance label. Its description cites this plan's path and section, and
   its validation criteria copy D3.1 and D3.2.
2. The Writer replaces the placeholder `task_ref` below with that `#N` in one
   mechanical path-only commit. The change needs no re-approval, and the
   Adversary verifies it.
3. Expansion runs and leaves `#N` alone. Expansion-qa proves it is not
   `task_missing`.
4. After expansion, the Orchestrator adds the edges that the placeholder path
   would have added: `#N` blocked by the 2.1 leaf, and the root epic blocked by
   `#N`.

Amended 2026-10-06 (#23671): the dedupe recorded in D1 moved D3 to #23509, the
2.1 leaf itself, so step 4's edge from the 2.1 leaf no longer applies.

```yaml
deferral:
  task_ref: "#23509"
  reason: "Orchestrator rulings (2026-10-05, 08:10 and 08:18 CT, R4): runtime lifecycle enforcement is parented under #22691 (Lane 3 - Runbooks). The Orchestrator files the task after Josh approves this plan, the Writer replaces this placeholder with its numeric ref before expansion, and the Orchestrator adds the 2.1 and root-epic edges after expansion."
  owner: "orchestrator"
  original_acceptance_items:
    - D3.1
    - D3.2
```

- D3.1 - An interactive run whose `resume_metadata` carries `idle_ttl_seconds`
  and whose session has been idle that long is reprompted to wrap up, save and
  call `end_agent_run`, and then ends.
- D3.2 - An interactive run without `idle_ttl_seconds` is never ended for
  idleness.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: Consensus between the Lane 7 Plan Writer gobby#15429 and the Plan
  Adversary gobby#15414 at `c89787fcc0`, after one sweep that raised eight
  blocking findings, all resolved:
  - F1: the pin records the resolved provider for `provider: inherit` at both
    entry points (1.1).
  - F2: activation always writes the skill and blocked-tool keys, so a narrower
    inherited restriction cannot survive (1.1).
  - F3: one drift helper serves SessionStart re-activation and a repeat tool
    call, so 1.3 depends on 1.1 and 1.2.
  - F4: the retirement `rg` has a bounded allowlist. Each leaf's check excludes
    only paths that a later leaf owns, and the V2 check runs after the last
    leaf.
  - F5: after a managed `/clear`, the successor is refused at MCP ingress and
    attributed to the predecessor at hook, variable and LLM ingress. The finding
    first claimed only refusal. New deliverable 1.6 makes all four sites follow
    the run's live binding. Tokens and grants keep the issued session as
    provenance.
  - F6: D3 routes through the Orchestrator's 08:18 CT ruling: it is filed under
    #22691 "Lane 3 - Runbooks" before expansion and carries a numeric ref, and
    its edges are added after expansion.
  - F7: the `/clear` successor keeps the run's spawn depth (1.4.7).
  - F-managed-consumer-inventory: the bridge and the two helper-mocking tests
    are declared as unchanged consumers of 1.6.

  The Orchestrator's request, relayed by the Lane Manager gobby#15389, added
  1.1.11 for #23477 "Block worktree creation in the plan-writer, plan-enhancer
  and plan-adversary agent definitions". The plan has 40 acceptance items over
  eight deliverables, and the original 30 keep their order. Base validation
  passes without warnings.

- 2026-10-05: Renewed consensus between the Plan Writer gobby#15429 and the
  Plan Adversary gobby#15414 at `0b65f09c28`. The reviewer gobby#15396 had
  bounced the stamped plan (`04de7d624d`) on blocker B1: 1.1's role-change
  refusal would break today's web-chat agent switch and the attached-terminal
  switch. The Orchestrator ruled on B1 three times:
  - 09:00 CT: web chat keeps switching through an in-process relaunch on the
    reused row. A new row per switch and a refusal were both rejected.
  - 09:05 CT: the relaunch design and an attached-terminal refusal with
    `ROLE_CHANGE_REQUIRES_RELAUNCH` were approved. Conditions: the error text
    tells the user how to switch, the Overview states the change in plain
    words, and both surfaces have tests (1.1.12-1.1.15, 1.2.6).
  - 09:12 CT: two deviations were accepted, each with a condition.
    - SessionStart keeps reading `agent_name_override` for #22904's placed
      launch. `activate_default_agent` keeps the stored seat over a
      role-change override and logs a warning naming both agents and the
      session.
    - web/ is not a target. `handleTransportError` shows the relaunch
      instruction as a system message, and `agent_changed` restores the label.

  The adversary's findings on the repair, all resolved:
  - F-B1-key-ledger: while the identity is unchanged, the definition key list
    is a union (1.1.16).
  - F-B1-base-cleanup: cleanup runs on every identity change, separately from
    the role-change permission, including over a configured base agent
    (1.1.17, 1.2.7).
  - F-B1-step-race: the old instance's delete and the merge commit in one
    transaction under the step lock. `_ensure_step_instance` rereads the
    identity under that lock (1.2.8).
  - F-B1-locked-permission: an activation commits only over the identity it
    was prepared against, base agent included, and its decision reruns under
    the lock (1.1.18, 1.1.19, `activation_superseded`).
  - 1.2 gained its granularity rationale.

  The adversary's nonblocking LOW (a) was declined. 2.1 lands after the
  workspace index pin plan's T1 and A1 (#23443 "Enhancer and adversary passes
  on the workspace index pin plan"). The plan has 51 acceptance items over
  eight deliverables, and the original 40 keep their order. The stale M1 was
  withdrawn for re-derivation (memory f5577ae0).

- 2026-10-06: Renewed consensus between the Lane 7 Plan Writer gobby#15528 and
  the Plan Adversary gobby#15471 under #23648 (Amend apply-agent-definition
  plan: keep apply_persona alongside apply_agent_definition). Josh ruled on the
  `apply_persona` deletion that 1.1 delivered at e8f43fb8ab: "Irritating. They
  served two different roles." (00:27 CT) and "Keep both." (00:48 CT). #23647
  restores the tool. The Orchestrator's 00:57 CT ruling set the scope: every
  forward-looking clause that contradicts the ruling is reconciled in this
  pass.
  - Overview and What users will see: the attached-terminal picker switches
    the persona live, and only a change of agent definition needs a relaunch.
  - Decisions 1, 2, 3 and 12 keep `apply_persona` as the persona overlay
    beside `apply_agent_definition`. Seat rules, tool blocks and step
    workflows stay keyed on `_agent_type` only.
  - 1.1 carries a dated amendment. 1.1.7 (the registry absence assertion)
    and 1.1.15 (the attached-terminal refusal) are withdrawn, and so is 1.1's
    zero-persona sweep.
  - 1.5 still drops the `_persona_name` seat-rule clauses, now because a
    persona overlay never makes a session a seat.
  - 3.1 documents both tools. `review/epic.md`, `review.yaml`, their test and
    `docs/reference-audit/agents.json` leave its Targets.
  - V2's sweep keeps only the rule-directory check.

  The Orchestrator's 01:04 CT ruling added two items to #23647 that the
  Writer raised. A definition activation that writes clears `_persona_name`
  (Decision 3). The `apply_persona` entry in the reference-audit inventory
  returns with the tool. The plan has 49 acceptance items over eight
  deliverables, and the remaining items keep their ids. The stale M1 was
  withdrawn for re-derivation (memory f5577ae0).
- 2026-10-06: Close-review repair under #23648 (CR7 gobby#15396 F1, the
  Orchestrator's ruling). With completed-section exemptions stubbed out, the
  close-review sandbox condition of #23620 (memory 1033f9db), validation
  failed on four bare Targets naming files that 1.1 and 1.2 created and the
  index now reports symbols for. 1.1's `apply_agent_definition.py`,
  `test_apply_agent_definition.py` and `test_session_start_reactivation.py`
  and 1.2's `test_interactive_step_instance.py` become `::*` Targets with a
  scope-reason naming what the section creates, the #23618 pattern
  (f39512cb1a). M1 inputs are unchanged. 1.1's two delete Targets stay bare
  paths while `apply_persona.py` and `test_apply_persona.py` are absent; the
  sandbox failure they meet once #23647 restores the files belongs to
  #23620's root fix.
- 2026-10-06: Second close-review repair under #23648 (review 20f093de). The
  reviewer read 1.1's `apply_persona.py — operation: delete` Target as a live
  deletion clause, against the task's no-deletion criterion. 1.1 drops both
  delete Targets (`apply_persona.py`, `test_apply_persona.py`), its amendment
  records that #23503 removed them at e8f43fb8ab and #23647 restores them,
  and the granularity note moves to the past tense. This supersedes the
  previous entry's last sentence: no delete Target remains, so none can fail
  once #23647 restores the files.
- 2026-10-06: Mechanical repoint under #23671 (the Orchestrator's request,
  relayed by the Lane Manager gobby#15389). Josh's dedupe folded #23511 (D1)
  and #23497 (D3) into the 2.1 leaf #23509, so both deferrals' `task_ref`
  values name #23509, and D1 and D3 carry dated amendments. The Cross-Plan
  Correction now states that #22902's 4.1.1 (#23001) points seats at
  `apply_agent_definition`, because after #23507 `apply_persona` gives no seat
  rules and no activation receipt. 1.1's Consumers unchanged inventory gains
  `tests/mcp_proxy/tools/test_apply_persona.py`, which #23647 restored at
  ef7886df52 and which failed consumer-coverage on the committed plan. No
  deliverable, acceptance item or M1 entry changed.

## V2: Verification
`kind: verification`

Each leaf runs its own planned verification after its final edit. Run this
block after the last leaf lands:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_apply_agent_definition.py tests/hooks/test_session_start_reactivation.py tests/hooks/test_interactive_step_instance.py tests/hooks/test_clear_successor_seat.py tests/workflows/test_step_snapshot_semantics.py tests/workflows/test_step_runtime_transitions.py tests/workflows/test_agent_definitions_v2.py tests/workflows/test_session_defaults.py tests/workflows/test_seat_rules.py tests/mcp_proxy/tools/skills/test_list_skills.py tests/mcp_proxy/tools/spawn_agent/test_factory.py tests/servers/websocket/chat/test_servers_websocket_chat_session.py tests/servers/websocket/test_set_agent.py tests/servers/websocket/test_attached_session_agent.py tests/hooks/test_agent_events_coverage.py tests/hooks/test_session_activation_reconciliation.py tests/hooks/event_handlers/test_session_variable_preservation.py tests/hooks/test_session_start_handlers.py tests/hooks/test_session_end_handlers.py tests/hooks/test_session_events_coverage.py tests/hooks/test_session_materialize.py tests/sessions/test_clear_acknowledgment.py tests/sessions/test_handoff.py tests/sessions/test_mailbox.py tests/servers/websocket/chat/test_clear_session.py tests/agents/watchdog/test_interactive_lifecycle_cleanup.py tests/servers/test_managed_clear_identity.py tests/servers/test_auth_service.py tests/servers/test_grant_auth.py tests/servers/routes/mcp_endpoints/test_execution_context.py tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py tests/servers/test_mcp_execution_context.py tests/servers/routes/test_llm_routes.py tests/servers/routes/test_session_variables.py tests/servers/routes/mcp/test_hook_session_metadata.py tests/skills/test_review_skill.py tests/skills/test_reference_library.py tests/mcp_proxy/tools/test_apply_persona.py tests/agents/test_agents_sync.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
rg -w _persona_name src/gobby/install/shared/workflows/rules
rg -w -c _persona_name tests/workflows/test_seat_rules.py
uv run gobby plans validate .gobby/plans/apply-agent-definition.md -p /Users/josh/Projects/gobby
```

The first `rg` must print nothing: no seat rule reads `_persona_name`, so a
persona overlay never matches one (Decision 3, 1.5). It can pass only after 1.5
lands. The second prints `1`, the 1.5.1 persona-only fixture. `apply_persona`
and `_persona_name` stay in production code, tests and guides (Decision 1).
`test_apply_persona.py` exists once #23647, which blocks 3.1, has landed. Do
not run the full pytest suite.

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
6. `apply_persona(agent="researcher")` succeeds without a relaunch:
   `_agent_type` stays `plan-writer`, and the next turn carries the researcher
   persona prompt. `apply_persona(agent="default")` then returns the next turn
   to the plan-writer prompt.
7. After a `/clear`, the successor reports `_agent_type: plan-writer` and a
   fresh step instance once the seat's step workflow exists.
8. A spawned interactive seat that stages `set_handoff(clear_session=true)`
   keeps its run: after the clear, `list_agents` shows the same run
   `running` with the successor as its session.
9. In that successor, `get_variable("_agent_type")` returns the seat, and a
   `set_variable` lands on the successor's row (the session `#N` the pane now
   reports), not on the predecessor's.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: apply_agent_definition tool and shared activation core
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Activation from the base agent writes `_agent_type`,
    the rule, skill, variable and blocked-tools keys and `_agent_definition_hash`
    in one merge. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_writes_full_delta_and_agent_type`.

    1.1.2: Unknown name, missing persona surface, declared pipeline, spawned session,
    unresolved task and colliding variables each return their typed `error_code` and
    write nothing. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_refusals_write_nothing`.

    1.1.3: The same agent with the same pin returns `status: unchanged` without a
    write, even when the repeat call passes changed `variables` and a `task_id` (no
    task resolution, merge or reinjection). A seat-to-seat or seat-to-base change
    through the tool returns `role_change_requires_relaunch`. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_same_seat_noop_and_role_change_refused`.

    1.1.4: A tool the seat blocks is refused by `_check_agent_tool_enforcement` after
    activation, and skill exclusions follow `_agent_type`. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_blocked_tools_and_skill_exclusions_follow_seat`.

    1.1.5: A compact SessionStart on an activated seat keeps its skills, rules and
    identity. test: `tests/hooks/test_session_start_reactivation.py::test_compact_sessionstart_keeps_seat_skills_and_rules`.

    1.1.6: Web-chat persona launch activates through `apply_agent_definition_impl`.
    test: `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::test_web_chat_launch_uses_apply_agent_definition`.

    1.1.8: A `provider: inherit` seat on a `codex` session keeps one pin across tool
    activation, a `compact` SessionStart and a repeat tool call. The SessionStart
    stores the same pin, and the repeat call returns `unchanged` without a write.
    test: `tests/hooks/test_session_start_reactivation.py::test_inherit_provider_pin_is_stable_across_entry_points`.

    1.1.9: Activating a seat that selects every skill and sets no skill format, over
    a base agent that restricted both, stores `_active_skill_names: None` and `_skill_format:
    None`. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_clears_inherited_skill_restriction`.

    1.1.10: A `compact` SessionStart after the seat''s definition drops its skill
    selectors and format stores `_active_skill_names: None` and `_skill_format: None`.
    test: `tests/hooks/test_session_start_reactivation.py::test_reactivation_clears_dropped_skill_restriction`.

    1.1.11: A hand-launched pane that activates a seat whose definition blocks `gobby-worktrees:create_worktree`
    (`blocked_mcp_tools`) and `EnterWorktree` (`blocked_tools`) is refused both by
    `_check_agent_tool_enforcement`, and stays refused after a `compact` SessionStart.
    #23477 relies on this for the plan-family seats (Orchestrator, via gobby#15389,
    2026-10-05). test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activated_seat_refuses_worktree_tools`.

    1.1.12: On a row at seat X whose definition declares a variable that seat Y does
    not, `relaunch=True` to Y writes Y''s delta and key list and sets that variable
    to `None`. `relaunch=True` from X to `default` writes the base agent''s delta
    the same way. The same calls without `relaunch` are refused `role_change_requires_relaunch`
    and write nothing. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_relaunch_switches_seat_and_clears_previous_keys`.

    1.1.13: A web-chat launch after `set_agent` from X to Y, and from X to `default`,
    calls `apply_agent_definition_impl` with `relaunch=True`, sets `skip_default_agent_activation`,
    sends no `agent_name_override`, and completes. test: `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::test_agent_switch_relaunches_through_activation`.

    1.1.14: `activate_default_agent` with an `agent_name_override` naming seat Y keeps
    seat X on a row stored at X and logs one warning naming the session, X and Y.
    It activates Y on a base-agent row. test: `tests/hooks/event_handlers/test_activate_agent_override.py::test_override_never_changes_role`.

    1.1.16: After seat X''s definition drops `x_only` and a `compact` SessionStart
    re-activates X, `x_only` keeps its value and `_agent_definition_keys` still names
    it. A later `relaunch=True` to Y sets `x_only` to `None`. test: `tests/hooks/test_session_start_reactivation.py::test_drift_then_relaunch_clears_retired_definition_key`.

    1.1.17: With `default_agent` configured as a definition that declares `base_only`,
    activating seat Y through the tool without `relaunch` succeeds and sets `base_only`
    to `None`. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_activation_over_configured_base_clears_base_keys`.

    1.1.18: Two concurrent tool activations from the base agent, to Y and to Z, both
    pass step 7. Exactly one returns `applied`, the other returns `activation_superseded`,
    and the row holds only the winner''s delta. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_concurrent_activations_recheck_permission_under_lock`.

    1.1.19: On a row at seat X, a SessionStart activation of X whose delta was built
    before a `relaunch=True` committed writes nothing. This holds for a relaunch to
    Y and for a relaunch to `default`. It returns `None` and logs one warning naming
    the session, the new agent and X. The row keeps the relaunched agent''s delta.
    test: `tests/hooks/test_session_start_reactivation.py::test_stale_sessionstart_behind_relaunch_keeps_current_seat`.'
  labels:
  - covers:apply-agent-definition:1.1:1.1.1
  - covers:apply-agent-definition:1.1:1.1.2
  - covers:apply-agent-definition:1.1:1.1.3
  - covers:apply-agent-definition:1.1:1.1.4
  - covers:apply-agent-definition:1.1:1.1.5
  - covers:apply-agent-definition:1.1:1.1.6
  - covers:apply-agent-definition:1.1:1.1.8
  - covers:apply-agent-definition:1.1:1.1.9
  - covers:apply-agent-definition:1.1:1.1.10
  - covers:apply-agent-definition:1.1:1.1.11
  - covers:apply-agent-definition:1.1:1.1.12
  - covers:apply-agent-definition:1.1:1.1.13
  - covers:apply-agent-definition:1.1:1.1.14
  - covers:apply-agent-definition:1.1:1.1.16
  - covers:apply-agent-definition:1.1:1.1.17
  - covers:apply-agent-definition:1.1:1.1.18
  - covers:apply-agent-definition:1.1:1.1.19
  tdd: false
  source_section: '1.1'
  implementation_domain: backend
- title: Step workflows on interactive sessions
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: An interactive seat whose definition declares steps
    gets an instance at the first step on activation, with no task claimed. test:
    `tests/hooks/test_interactive_step_instance.py::test_interactive_seat_with_step_workflow_gets_instance_without_task`.

    1.2.2: A seat or plain session whose definition has no step workflow gets no instance.
    test: `tests/hooks/test_interactive_step_instance.py::test_seat_without_step_workflow_gets_none`.

    1.2.3: A failed instance save leaves the activation variables in place and the
    next hook event''s reconcile materializes the instance. test: `tests/hooks/test_interactive_step_instance.py::test_reconcile_repairs_missing_instance_after_failed_save`.

    1.2.4: A spawned session''s spawn-time instance is untouched, including when the
    reconciler activates its run agent over a stored `_agent_type` of `default`. test:
    `tests/hooks/test_interactive_step_instance.py::test_spawned_step_instance_unchanged`.

    1.2.5: The old no-instance non-goal test is inverted. test: `tests/workflows/test_step_snapshot_semantics.py::test_definition_activation_materializes_step_instance`.

    1.2.6: A relaunch from seat X with steps to seat Y with steps leaves one instance,
    Y''s, at Y''s first step. A relaunch from X to `default` leaves no instance. test:
    `tests/hooks/test_interactive_step_instance.py::test_relaunch_switch_replaces_step_instance`.

    1.2.7: With `default_agent` configured as a definition that declares steps, activating
    seat Y through the tool without `relaunch` leaves one instance, Y''s, at Y''s
    first step. test: `tests/hooks/test_interactive_step_instance.py::test_activation_over_configured_base_replaces_base_instance`.

    1.2.8: A relaunch from seat X to seat Y, both with steps, ends with one instance,
    Y''s, in two cases: - A reconcile holding X''s pre-switch snapshot runs after
    the transition commits, while Y''s own instance save is injected to fail. - A
    reconcile thread starts while the transition is paused inside the merge on a `threading.Event`.
    Each join is bounded at 10 seconds. A merge failure injected inside the transition
    leaves X''s variables and X''s instance. test: `tests/hooks/test_interactive_step_instance.py::test_switch_serializes_with_stale_reconcile`.'
  labels:
  - covers:apply-agent-definition:1.2:1.2.1
  - covers:apply-agent-definition:1.2:1.2.2
  - covers:apply-agent-definition:1.2:1.2.3
  - covers:apply-agent-definition:1.2:1.2.4
  - covers:apply-agent-definition:1.2:1.2.5
  - covers:apply-agent-definition:1.2:1.2.6
  - covers:apply-agent-definition:1.2:1.2.7
  - covers:apply-agent-definition:1.2:1.2.8
  tdd: false
  source_section: '1.2'
  implementation_domain: backend
- title: Definition drift receipt on re-activation
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '1.3.1: A resume or compact SessionStart whose seat definition
    changed applies the current row, stores the new pin, and injects the drift line
    exactly once, including on a session that already stores `_agent_identity_reinject:
    False` and `_agent_definition_drift: None`. test: `tests/hooks/test_session_start_reactivation.py::test_reactivation_reports_definition_drift_once`.

    1.3.2: An unchanged pin injects no drift line, including for a `provider: inherit`
    seat on a `codex` session whose pin the tool wrote. test: `tests/hooks/test_session_start_reactivation.py::test_unchanged_pin_injects_no_drift_line`.

    1.3.3: On a stepped seat advanced past its first step, a compact or resume SessionStart
    after a change to the definition''s rules, tool blocks and step list stores the
    new pin, rule set and blocked tools and injects one drift line. The step instance
    keeps its id, its current step and its snapshot. test: `tests/hooks/test_session_start_reactivation.py::test_drift_reactivation_keeps_running_step_instance`.

    1.3.4: A same-seat tool call after the seat''s row changed stores the new pin
    and stages one drift line, which the next injection delivers once and the one
    after it does not. An unchanged repeat call stages none. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_same_seat_changed_row_reports_drift_once`.'
  labels:
  - covers:apply-agent-definition:1.3:1.3.1
  - covers:apply-agent-definition:1.3:1.3.2
  - covers:apply-agent-definition:1.3:1.3.3
  - covers:apply-agent-definition:1.3:1.3.4
  tdd: false
  source_section: '1.3'
  implementation_domain: backend
- title: /clear successor keeps its seat and its run
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  validation_criteria: '1.4.1: A `/clear` successor of an activated seat carries `_agent_type`
    and the pin and re-activates as that seat. test: `tests/hooks/test_clear_successor_seat.py::test_clear_successor_inherits_agent_type_and_pin`.

    1.4.2: The successor gets a fresh step instance at the first step of the current
    definition. After a definition edit, that is the new step list. test: `tests/hooks/test_clear_successor_seat.py::test_clear_successor_gets_fresh_step_instance`.

    1.4.3: A base-agent predecessor with no run carries nothing: the successor has
    no `_agent_type`, and its `agent_run_id` stays null. test: `tests/hooks/test_clear_successor_seat.py::test_base_agent_clear_successor_unchanged`.

    1.4.4: After a staged `/clear`, a spawned interactive seat keeps its run. - After
    SessionEnd `clear`, the run is still `running` and its terminal is still live.
    - After the successor''s SessionStart: - the run''s `child_session_id`, the successor''s
    `agent_run_id` and the run terminal''s `session_id` all name the successor; -
    the predecessor''s `agent_run_id` is null; - the successor activates with `is_spawned_agent:
    True`; - runs the predecessor spawned have the successor as `parent_session_id`;
    - `list_termination_candidates` does not return the run. test: `tests/hooks/test_clear_successor_seat.py::test_spawned_interactive_seat_keeps_run_binding_after_clear`.

    1.4.5: A `clear` end on a run-bound `awaiting_handoff` session neither completes
    the run nor marks its terminal exited. A `clear` end on a session with no staged
    attempt still does both. test: `tests/hooks/test_session_end_handlers.py::test_staged_clear_end_hands_run_to_successor`.

    1.4.6: When a newer successor supersedes a bound one, the run moves from the superseded
    successor to the newer one. test: `tests/hooks/test_clear_successor_seat.py::test_superseding_successor_takes_the_run`.

    1.4.7: A depth-5 seat''s terminal clear successor, whose SessionStart payload
    carries no `agent_depth`, stores the predecessor''s `agent_depth` and `spawned_by_agent_id`,
    and so does a superseding successor. `can_spawn_child` refuses both. test: `tests/hooks/test_clear_successor_seat.py::test_clear_successor_keeps_spawn_depth`.'
  labels:
  - covers:apply-agent-definition:1.4:1.4.1
  - covers:apply-agent-definition:1.4:1.4.2
  - covers:apply-agent-definition:1.4:1.4.3
  - covers:apply-agent-definition:1.4:1.4.4
  - covers:apply-agent-definition:1.4:1.4.5
  - covers:apply-agent-definition:1.4:1.4.6
  - covers:apply-agent-definition:1.4:1.4.7
  tdd: false
  source_section: '1.4'
  implementation_domain: backend
- title: Seat rules match `_agent_type` only
  category: config
  task_type: chore
  depends_on:
  - '1.1'
  validation_criteria: '1.5.1: Every `rules/roles/` condition matches a seat through
    `_agent_type` alone, and a context that carries only `_persona_name` matches none
    of them. test: `tests/workflows/test_seat_rules.py::test_persona_name_alone_matches_no_seat_rule`.

    1.5.2: Seat guidance injection still matches a session whose `_agent_type` names
    a seat and skips one that names no seat. test: `tests/workflows/test_seat_rules.py::test_seat_common_matches_spawned_and_skips_non_seats`.'
  labels:
  - covers:apply-agent-definition:1.5:1.5.1
  - covers:apply-agent-definition:1.5:1.5.2
  tdd: false
  source_section: '1.5'
  assigned_agent: backend-developer
- title: Managed pane identity follows its run across /clear
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '1.6.1: After a staged clear, a wrapper `call_tool` for a session-scoped
    tool that carries the pane''s frozen headers and token is admitted, where today
    it is refused 403, and is attributed to the successor: a `set_variable` through
    it lands on the successor''s row, and the predecessor''s row is unchanged. test:
    `tests/servers/test_managed_clear_identity.py::test_successor_tool_call_is_attributed_to_successor`.

    1.6.2: A hook event with the frozen headers resolves `_platform_session_id` to
    the successor, and `POST /api/sessions/<predecessor>/variables/get` returns the
    successor''s variables. test: `tests/servers/test_managed_clear_identity.py::test_successor_hook_and_variables_follow_run`.

    1.6.3: Nothing is forwarded in these cases: - before the take; - for a token whose
    run has ended, which is refused `run_inactive`; - for a token of another run;
    - for a session with no run. test: `tests/servers/test_managed_clear_identity.py::test_no_forwarding_without_live_run_binding`.

    1.6.4: After a staged clear, `POST /api/llm/chat/completions` with the frozen
    bearer and a valid `tool_chat` grant issued for the predecessor passes real auth
    and grant checks, and the stubbed tool chat service receives `ToolChatRequest.session_id`
    equal to the successor. Once the run ends, the same request is refused `run_inactive`.
    test: `tests/servers/test_managed_clear_identity.py::test_successor_chat_completion_keeps_issued_grant`.'
  labels:
  - covers:apply-agent-definition:1.6:1.6.1
  - covers:apply-agent-definition:1.6:1.6.2
  - covers:apply-agent-definition:1.6:1.6.3
  - covers:apply-agent-definition:1.6:1.6.4
  tdd: false
  source_section: '1.6'
  implementation_domain: backend
- title: Idle TTL on agent definitions
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: `idle_ttl_seconds` accepts a positive integer with
    `execution_mode: interactive`, and rejects zero, a negative value, and any value
    on a `one_shot` definition. test: `tests/workflows/test_agent_definitions_v2.py::test_idle_ttl_requires_interactive_execution_mode`.

    2.1.2: An interactive spawn of a definition with a TTL stores `idle_ttl_seconds`
    at the top level of `resume_metadata`. An interactive definition without a TTL,
    a spawn-time `one_shot` override, and a `one_shot` definition overridden to `interactive`
    store none. test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_idle_ttl_is_persisted_only_for_interactive_runs`.

    2.1.3: Resume carries `idle_ttl_seconds` through with `execution_mode`. test:
    `tests/agents/watchdog/test_interactive_lifecycle_cleanup.py::test_missing_interactive_terminal_resumes_after_task_close`.'
  labels:
  - covers:apply-agent-definition:2.1:2.1.1
  - covers:apply-agent-definition:2.1:2.1.2
  - covers:apply-agent-definition:2.1:2.1.3
  tdd: false
  source_section: '2.1'
  implementation_domain: backend
- title: Bundled references and guides
  category: docs
  task_type: chore
  depends_on:
  - '1.2'
  - '1.4'
  - '1.6'
  - '2.1'
  validation_criteria: '3.1.1: The persona reference keeps `apply_persona` for the
    live persona switch, and routes whole-definition activation to `apply_agent_definition`
    with its relaunch refusal. behavior: "role_change_requires_relaunch" in `src/gobby/install/shared/skills/gobby/references/agents/personas.md`.

    3.1.2: The epic-review reference, its pinned test phrase and the review pipeline''s
    `mode` input keep `apply_persona` for the in-line review. test: `tests/skills/test_review_skill.py::test_epic_review_references_pin_routing_and_verdict_mapping`.

    3.1.3: The agents guide documents both tools, the activation contract, the continuity
    table and `idle_ttl_seconds`. behavior: "role_change_requires_relaunch" in `docs/guides/agents.md`.

    3.1.4: The workflows overview names both tools, and the variables audit points
    at `build_definition_changes`. file: `docs/guides/workflows-overview.md`.'
  labels:
  - covers:apply-agent-definition:3.1:3.1.1
  - covers:apply-agent-definition:3.1:3.1.2
  - covers:apply-agent-definition:3.1:3.1.3
  - covers:apply-agent-definition:3.1:3.1.4
  tdd: false
  source_section: '3.1'
  assigned_agent: tech-writer
```

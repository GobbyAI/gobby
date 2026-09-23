Plan artifact: `.gobby/plans/handoff-stop-gates-and-pull-order.md`
**Plan ID:** handoff-stop-gates-and-pull-order

# Handoff Stop Gates And Pull Order

## Overview
`kind: framing`

Four defects in rule enforcement and test gating, reported by the repository owner.

A session that stages a handoff has no legal move. `set_handoff` returns, the
`before_tool` gate `block-tools-after-handoff-compact` orders the agent to stop
calling tools and end its turn so the queued `/compact` or `/clear` runs, and the
`turn_end` gate `require-task-close` refuses that turn end because a task is still
claimed. Only a context compact clears it. The owner's rule: `set_handoff` always
allows a stop, and the gates re-arm at the next session start while a task is
claimed. `require-epic-tree-close` is retired at the same time.

Nothing enforces pulling the handoff before loading skills. The continuation prompt
from `build_handoff_continue_prompt` is typed into the pane and is already first,
but the `turn_start` it creates fires sixteen rule files of which only three defer
on `handoff_pull_pending`, and no gate stops an agent from loading a skill on its
own before calling `get_handoff`.

The `auto-task` rule group is dead. Nothing in the daemon writes `auto_task_ref`,
stage-manifest dispatch through `gobby build` replaced it, and no bundled agent
selects its tag. It shares `task_tree_complete` with `require-epic-tree-close`, so
retiring both frees the helper.

One test runs two full `uv build` invocations on every local pytest run. Bundled
content tamper detection is a production concern served by `verify_bundled_integrity`
at runtime, not by a local test.

## Constraints
`kind: framing`

- Stop-gate suppression must stay daemon-owned. The settled invariant is that only
  daemon-owned state may suppress stop attempts and task close gates; an agent-set
  variable must never do it. The marker is therefore written inside
  `stage_handoff_attempt`, atomic with the attempt, so a `set_handoff` that returns
  `interrupt_unconfirmed` without staging suppresses nothing.
- Do not route the suppression through `force_allow_stop`. `RuleEngine.evaluate`
  deliberately refuses that override while `task_claimed` is true so claimed work
  cannot escape closure; that refusal stays exactly as it is.
- Do not clear `set_handoff_pending` on the re-arm. `_consume_candidate` pops it when
  `get_handoff` runs, and clearing it early breaks the pull.
- A handoff gate must leave its own prerequisites callable. A gate that blocks
  `set_handoff` while another gate holds `set_handoff` for its prerequisite deadlocks
  the session, and only a context compact clears it. The new `before_tool` gate
  allows `get_handoff`, the pending memory recall, and coordination messaging.
  `get_tool_schema` and `list_tools` need no clause: `is_unblockable_discovery_tool`
  already exempts them from every `before_tool` block.
- The epic auto-closer is out of scope and must keep working.
  `_close_eligible_ancestors` walks parents in SQL with its own hold-label,
  claimed-ancestor and parent-criteria guards. It never calls a condition helper, so
  deleting `task_tree_complete` cannot reach it.
- Retiring bundled rules needs no DB work. `sync_bundled_rules` skips `deprecated/`
  and soft-deletes installed bundled rows whose templates disappeared.
- Rule templates and engine code ship together. The daemon serving this checkout must
  be restarted after the merge for either half to be live; announce it with a
  `global` `send_message` and wait for a quiet window.
- Keep the `if task_manager:` block in `build_condition_helpers`.
  `all_tasks_have_label`, `task_needs_human_review`, `task_state_in` and
  `task_type_in` still need the manager, so the `LocalTaskManager` threading into
  `RuleEngine.__init__` stays.
- Rejected: adding `and not variables.get(...)` to each stop gate's `when`. The owner
  specified that `set_handoff` always allows a stop, which is an engine-level
  property; per-rule conditions would have to be repeated on every future turn-end
  gate and would silently miss one.
- Rejected: injecting the pull directive as a `session_start` rule. Session-start
  context does not resume an agent; the typed continuation prompt is what does, and
  it already arrives first.

## Granularity
`kind: framing`

Six deliverables across four phases. Two independently testable state machines are
in scope and are deliberately kept apart: the turn-end suppression and re-arm cycle
in P1, and the handoff-pull deferral and block cycle in P2. They share no variable,
no event and no test module. P3 is a pure retirement whose only coupling to P1 is the
shared helper, expressed as a dependency. P4 touches no runtime code. The rules
`AGENTS.md` table is a shared Target across 1.2, 2.2 and 3.1, so those three are
chained even though their symbol scopes differ.

## P1: Stop gates stand down for a staged handoff
`kind: framing`

**Goal**: `set_handoff` always allows the turn to end, and the gates re-arm on the
next session start while a task is still claimed.

### 1.1 Suppress turn-end block gates while a handoff is staged [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/handoff.py::stage_handoff_attempt`
- `src/gobby/workflows/engine/evaluation.py::EvaluationMixin._run_rule_loop`
- `src/gobby/workflows/engine/evaluation.py::EvaluationMixin._assemble_response`
- `src/gobby/workflows/reserved_variables.py::RESERVED_WORKFLOW_VARIABLES`
- `src/gobby/install/shared/workflows/rules/stop-gates/rearm-close-gates-on-session-start.yaml`
- `tests/workflows/test_stop_gates_rules.py::*` — scope-reason: adds the staged-handoff and re-arm cases to the bundled stop-gate suite

Research context. `stage_handoff_attempt` already builds a `marker_updates` dict and
writes `set_handoff_pending` for every attempt plus `handoff_pull_pending` on the
compact path only. The new marker goes beside them so it lands on both the clear and
compact paths inside the same transaction.

The engine precedent is `turn_interrupt_initiated`, handled in
`EvaluationMixin._run_rule_loop` and surfaced in `_assemble_response`, which drops
every turn-end block gate while leaving non-block effects live and audit-logs the
decision as the pseudo-rule `interrupt-initiated-turn`. Mirror that shape and name
the pseudo-rule for the staged handoff.

`RESERVED_WORKFLOW_VARIABLES` stops an agent forging the marker through MCP
`set_variable`. Bundled rules can still write it: `is_internal_rule` exempts
installed rows carrying the `gobby` tag and no `user` tag, checked in `EffectsMixin`
before the reserved-name refusal.

The re-arm rule is modelled on
`context-handoff/clear-pending-context-reset-on-start.yaml`: `event: session_start`,
one `set_variable` effect. It carries no `when` restriction. Every other reset rule
narrows to the clear and compact sources, but a stop gate must re-arm on every
boundary, and an unconditional reset cannot strand a session in a state where it
stops freely forever.

A one-shot manual-compact bypass already exists and strips the turn-end trigger for a
single turn end after a manual `pre_compact`. It does not solve this: it arms at
PreCompact, which happens after the turn end the gate blocks.

`RuleEngine.evaluate` is not a Target here and must not be edited. Its
`force_allow_stop` branch deliberately refuses to allow a stop while a task is
claimed, and the new suppression is a separate path that leaves that refusal intact.
Acceptance 1.1.7 re-verifies it through the existing test.

**Acceptance:**

- 1.1.1 - `stage_handoff_attempt` writes the suppression marker into `marker_updates` on both the clear and compact paths. symbol: `stage_handoff_attempt`. file: `src/gobby/sessions/handoff.py`.
- 1.1.2 - A staged handoff drops every turn-end block gate while non-block effects still run, audit-logged as a named pseudo-rule. symbol: `EvaluationMixin._run_rule_loop`. file: `src/gobby/workflows/engine/evaluation.py`.
- 1.1.3 - The marker name is refused for agent writes and accepted for bundled-rule writes. symbol: `RESERVED_WORKFLOW_VARIABLES`. file: `src/gobby/workflows/reserved_variables.py`.
- 1.1.4 - A session-start rule clears the marker with no `when` restriction. file: `src/gobby/install/shared/workflows/rules/stop-gates/rearm-close-gates-on-session-start.yaml`.
- 1.1.5 - A staged handoff allows the turn end while a task is claimed. test: `tests/workflows/test_stop_gates_rules.py::test_staged_handoff_allows_turn_end_with_claimed_task`.
- 1.1.6 - The following session start re-arms the gate and a claimed task blocks again, with the pull marker intact. test: `tests/workflows/test_stop_gates_rules.py::test_session_start_rearms_close_gate_and_preserves_pull_marker`.
- 1.1.7 - `force_allow_stop` still refuses to allow a stop while a task is claimed. test: `tests/workflows/test_stop_gates_rules.py::TestForceAllowStopWithTaskClaimed::test_force_allow_suppressed_when_task_claimed`.

### 1.2 Retire require-epic-tree-close [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml::*` — scope-reason: whole-file retirement; the rule moves under stop-gates/deprecated/, which sync excludes
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `tests/workflows/test_stop_gates_rules.py::*` — scope-reason: drops the retired rule from the bundled rule-name set and the two parametrized gate lists
- `tests/workflows/test_turn_interrupt_stop_gates.py::*` — scope-reason: drops the retired rule from the gate-name set the interrupt suite sweeps
- `tests/workflows/test_rule_engine_task_helper_wiring.py::test_require_epic_tree_close_uses_real_task_manager`
- `tests/workflows/test_rule_engine_task_helper_wiring.py::test_require_epic_tree_close_rejects_live_session_label`
- `tests/workflows/test_rule_engine_task_helper_wiring.py::test_unresolved_dependency_yields_claim_gates_and_rearms_after_close`

Research context. The rule is a turn-end gate at priority 51 that blocks when
`task_tree_complete` reports the claimed tree incomplete. Leaf closure already runs a
thirteen-gate checklist and ancestors auto-close when their last child closes, so the
gate re-asserts at turn end what task closure already enforces.

Moving the YAML under `deprecated/` is enough. `sync_bundled_rules` skips that
directory and soft-deletes the orphaned installed row on the next sync, so there is
no DB work and no migration.

Test references are a rule-name set, two parametrize lists, and an aggregate-reason
assertion in the stop-gates suite, plus a gate-name set in the interrupt suite. Two
wiring tests exist only for this rule and are deleted whole. The third,
`test_unresolved_dependency_yields_claim_gates_and_rearms_after_close`, asserts both
gate names appear in one aggregated block reason; it keeps the `require-task-close`
assertion and drops the other.

The `stop-gates` row in the rules reference keeps its count of 6: this retirement
removes one rule and 1.1 adds the re-arm rule, so only the purpose text changes.
`test_rule_reference_counts_match_yaml` parses that table but is parametrized only
over `task-enforcement` and `worker-safety`, so it cannot catch a stale count here.

**Acceptance:**

- 1.2.1 - No enabled bundled rule named require-epic-tree-close remains under the synced rules tree. file: `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml`.
- 1.2.2 - The stop-gates row records 6 rules with the retired gate absent from its purpose text. file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.
- 1.2.3 - The bundled stop-gate suite passes with the rule absent from its name set and both parametrized lists. test: `tests/workflows/test_stop_gates_rules.py::TestStopAttemptsPlumbing::test_stop_attempts_increments_even_when_force_allowed`.
- 1.2.4 - The interrupt suite passes with the retired gate absent from its gate-name set. test: `tests/workflows/test_turn_interrupt_stop_gates.py::test_interrupt_initiated_turn_suppresses_turn_end_block_effects`.
- 1.2.5 - The dependency re-arm test asserts only require-task-close in the aggregated reason. test: `tests/workflows/test_rule_engine_task_helper_wiring.py::test_unresolved_dependency_yields_claim_gates_and_rearms_after_close`.

## P2: The handoff pull precedes context and skills
`kind: framing`

**Goal**: the resume turn carries no injected payload until the handoff is pulled,
and no tool but the pull itself is callable until then.

### 2.1 Defer turn-start injections while a pull is pending [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-turn-start.yaml::*` — scope-reason: adds the pending-pull guard to the turn-start memory surfacing rule
- `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml::*` — scope-reason: adds the pending-pull guard to the lesson injection rule
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml::*` — scope-reason: adds the pending-pull guard to the lesson injection rule
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml::*` — scope-reason: adds the pending-pull guard to the lesson injection rule
- `src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml::*` — scope-reason: adds the pending-pull guard to the lesson injection rule
- `src/gobby/install/shared/workflows/rules/brevity/reinforce-brevity.yaml::*` — scope-reason: adds the guard to the turn-start reminder rules only, not the opt-out phrase handler
- `src/gobby/install/shared/workflows/rules/restraint/require-restraint-skill.yaml::*` — scope-reason: adds the guard to the turn-start reminder rule only, not the opt-out phrase handler
- `tests/workflows/test_reminder_cadence_rules.py::*` — scope-reason: the cadence suite asserts per-turn reminder injection and must cover a pending pull

Research context. The typed continuation prompt is an ordinary submission, so
`_resolve_rule_events` resolves it to the turn-start trigger. Sixteen rule files fire
there. `bootstrap-default-agent-core-skills` at priority 9,
`list-skill-hubs-once-per-session` at 15 and `nudge-compact-on-context-pressure` at 30
already carry the pending-pull guard. Every other injecting rule does not, so memory
hits, review lessons and the reminder one-liners all land on the resume turn ahead of
the pull.

`handoff_pull_pending` is set by `stage_handoff_attempt` on the compact path and
merged onto the successor by `_bind_clear_successor` on the clear path, then popped
inside the `get_handoff` transaction by `_finish_successor_pull`. The guard clause is
therefore already correct and self-clearing; this deliverable only widens its reach.

Leave the bookkeeping rules alone. `increment-parent-turn-seq`, `reset-subagent-flag`
and `reset-code-index-navigation` only set variables and must keep running on the
resume turn or the turn counters drift.

`auto-task/inject-autonomous-mode.yaml` also injects on turn start. It is deleted in
3.1 and gets no guard.

**Acceptance:**

- 2.1.1 - Turn-start memory surfacing does not fire while a handoff pull is pending. file: `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-turn-start.yaml`.
- 2.1.2 - No review-learning lesson injection fires while a handoff pull is pending. file: `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml`.
- 2.1.3 - The brevity and restraint turn-start reminders do not fire while a handoff pull is pending, and the opt-out phrase handlers still do. file: `src/gobby/install/shared/workflows/rules/brevity/reinforce-brevity.yaml`.
- 2.1.4 - The bookkeeping turn-start rules still run on a resume turn with a pending pull. behavior: "turn counters advance on the resume turn" in `src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml`.
- 2.1.5 - The reminder cadence suite proves a pending pull injects nothing and the following turn resumes the cadence. test: `tests/workflows/test_reminder_cadence_rules.py::test_pending_handoff_pull_suppresses_turn_start_reminders`.

### 2.2 Block tools until the handoff pull runs [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/workflows/handoff_conditions.py`
- `src/gobby/workflows/safe_evaluator.py::build_condition_helpers`
- `src/gobby/install/shared/workflows/rules/context-handoff/require-handoff-pull-before-tool.yaml`
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `tests/workflows/test_block_tools_after_handoff_compact.py::HANDOFF_PREREQUISITE_EVENTS`
- `tests/workflows/test_handoff_pull_gate.py`
- `src/gobby/workflows/enforcement/blocking.py::*` — scope-reason: consumer re-verification only; ARGUMENTLESS_PROXY_TOOLS and is_unblockable_discovery_tool must keep exempting the pull and schema discovery, no edit expected

Research context. Deferring Gobby's own injections in 2.1 does not stop an agent
loading a skill on its own initiative, which is the reported failure. The gate was
already specified in `.gobby/plans/handoff-tool.md` as
`require-handoff-pull-before-tool`, a before-tool rule at priority 10 with helpers in
a new `handoff_conditions.py`; neither the module nor the rule was ever built.

The exemption list is the load-bearing part. Both existing before-tool handoff gates
in `block-tools-after-handoff-compact.yaml` permit an identical prerequisite set, and
they must: a gate permitting only its own target tool deadlocks when another gate
holds that tool for a prerequisite of its own. Allow `gobby-sessions:get_handoff`,
the pending memory recall, and `gobby-agents` coordination messaging. Schema discovery
needs no YAML clause because `is_unblockable_discovery_tool` exempts `get_tool_schema`
and `list_tools` from every before-tool block, and `get_handoff` is in
`ARGUMENTLESS_PROXY_TOOLS` so the successor can call it as its literal first tool with
no schema round-trip.

`HANDOFF_PREREQUISITE_EVENTS` is the shared table the two existing gates are
parametrized over. Extending it to cover the new gate is what stops a future
prerequisite being added to one allowlist and forgotten in another.

The `context-handoff` group row in the rules reference goes from 14 rules to 15.

**Acceptance:**

- 2.2.1 - A new condition-helper module exposes the pending-pull and pull-call predicates and they are registered for rule conditions. symbol: `build_condition_helpers`. file: `src/gobby/workflows/safe_evaluator.py`.
- 2.2.2 - A before-tool gate blocks ordinary tools while a handoff pull is pending. file: `src/gobby/install/shared/workflows/rules/context-handoff/require-handoff-pull-before-tool.yaml`.
- 2.2.3 - The gate permits get_handoff, the pending memory recall and coordination messaging, proven over the shared prerequisite table. test: `tests/workflows/test_handoff_pull_gate.py::test_pull_gate_allows_handoff_prerequisite_tools`.
- 2.2.4 - An agent-initiated skill load is blocked until the pull runs and allowed afterwards. test: `tests/workflows/test_handoff_pull_gate.py::test_pull_gate_blocks_skill_load_until_pull`.
- 2.2.5 - The context-handoff row records 15 rules. file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.

## P3: Retire the auto-task group
`kind: framing`

**Goal**: no bundled rule, condition helper, or test depends on `auto_task_ref`.

### 3.1 Delete the auto-task rules and the tree-completeness helper [category: refactor] (depends: 2.2, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/auto-task/inject-autonomous-mode.yaml::*` — scope-reason: whole-file deletion; the group is retired outright, not preserved under deprecated/
- `src/gobby/install/shared/workflows/rules/auto-task/guide-task-continuation.yaml::*` — scope-reason: whole-file deletion; the group is retired outright
- `src/gobby/install/shared/workflows/rules/auto-task/notify-task-tree-complete.yaml::*` — scope-reason: whole-file deletion; the group is retired outright
- `src/gobby/install/shared/workflows/rules/context-handoff/preserve-context-on-compact.yaml::*` — scope-reason: drops the autonomous-mode reminder reset, dead once the injecting rule is gone
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `src/gobby/workflows/condition_helpers.py::task_tree_complete`
- `src/gobby/workflows/condition_helpers.py::_is_tree_complete`
- `src/gobby/workflows/condition_helpers_shell.py`
- `src/gobby/workflows/safe_evaluator.py::build_condition_helpers`
- `src/gobby/workflows/engine/core.py::RuleEngine.evaluate`
- `src/gobby/workflows/engine/core_overrides.py`
- `tests/workflows/test_auto_task_rules.py::*` — scope-reason: whole-file deletion; every test in it covers a retired rule
- `tests/workflows/test_condition_helpers.py::*` — scope-reason: drops the tree-completeness cases and their import
- `tests/workflows/test_reminder_cadence_rules.py::*` — scope-reason: deletes the autonomous-mode cadence case whose rule is retired here
- `tests/workflows/test_safe_evaluator.py::*` — scope-reason: drops the retired helper from the registered-helper expectations

Research context. `auto_task_ref` has no writer anywhere in the daemon. The only
setter on record is an agent calling `set_variable` by hand in a research transcript,
and stage-manifest dispatch through `gobby build` superseded the mechanism. No bundled
agent selects the group tag; the group's only reference outside its own directory is
the rules reference table row.

`task_tree_complete` has exactly three callers: the two auto-task rules and
`require-epic-tree-close`, retired in 1.2. Its private recursive helper
`_is_tree_complete` is called only from `task_tree_complete` and itself. Both go.
`_normalize_task_ids` and `_get_task` stay: `all_tasks_have_label`, `task_state_in`
and `task_type_in` still use them.

In `build_condition_helpers` remove the import, the live binding and the no-manager
fallback for the retired helper only. The `if task_manager:` block itself stays for
the four remaining task helpers, so the `LocalTaskManager` threading into
`RuleEngine.__init__` is unchanged. `dry_run_validation` derives its helper-name set
by calling `build_condition_helpers()` and needs no edit.

`RuleEngine.evaluate` copies `auto_task_ref` into a span attribute; that read goes.
`preserve-context-on-compact.yaml` resets `autonomous_mode_reminder_turn`, which only
the retired injecting rule ever read.

Two targeted files sit near the 1,000-line production ceiling and are decomposed
inside this deliverable, not deferred. `condition_helpers.py` is 988 lines; the
retirement removes about sixty, which is not enough. Move the shell-command group to
the new sibling `condition_helpers_shell.py`: `is_validation_command`,
`wrapped_validation_command`, `is_gobby_build_command`, `shell_command_invokes_gcode`,
`shell_command_runs_ocr_review`, `shell_command_consumes_code_review`,
`_strip_shell_command_prefixes`, `_git_subcommand`, `_segment_invokes_gobby_build`,
`_strip_env_assignments`, `_strip_uv_run_options`, `_python_module_tokens` and
`_executable_name`, with the imports they need. `condition_helpers.py` re-exports them
so `build_condition_helpers` and every rule condition resolve unchanged.

`core.py` is 899 lines and `RuleEngine.evaluate` alone spans 540 of them. Move
the hardcoded override block to the new sibling `core_overrides.py`: the
`force_allow_stop` branch with its refusal under a claimed task, and the
`tool_block_pending` and `edit_write_pending` branches, as functions taking the
evaluation context and returning an optional response. `evaluate` calls them in the
same order, before any rule-level block, so the documented turn-end assembly order is
preserved exactly.

The epic auto-closer is untouched. `_close_eligible_ancestors` in
`storage/tasks/_stage_utils.py` walks parents in SQL and checks for zero open children
with its own hold-label, claimed-ancestor and parent-criteria guards.

Sweep the remaining tests that mention the group or its variable and change only what
depends on the retired rules. Most use the strings as sample data for model,
definition and query tests and need no edit; confirm that in
`tests/workflows/test_rule_models.py`, `tests/workflows/test_definitions.py` and
`tests/mcp_proxy/tools/workflows/test_query.py`.

**Acceptance:**

- 3.1.1 - No bundled rule template references auto_task_ref and the auto-task directory is gone. file: `src/gobby/install/shared/workflows/rules/auto-task/inject-autonomous-mode.yaml`.
- 3.1.2 - The rules reference table has no auto-task row. file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.
- 3.1.3 - The condition helpers module defines neither the tree-completeness helper nor its private recursion. symbol: `task_tree_complete`. file: `src/gobby/workflows/condition_helpers.py`.
- 3.1.4 - `build_condition_helpers` registers no tree-completeness helper and still registers the four remaining task helpers under a task manager. symbol: `build_condition_helpers`. file: `src/gobby/workflows/safe_evaluator.py`.
- 3.1.5 - `RuleEngine.evaluate` performs no auto_task_ref read. symbol: `RuleEngine.evaluate`. file: `src/gobby/workflows/engine/core.py`.
- 3.1.6 - The compact preservation rule no longer resets the autonomous-mode reminder marker. file: `src/gobby/install/shared/workflows/rules/context-handoff/preserve-context-on-compact.yaml`.
- 3.1.7 - The safe-evaluator suite passes with the retired helper absent from the registered set. test: `tests/workflows/test_safe_evaluator.py::test_condition_helpers_without_task_manager_return_false`.
- 3.1.8 - The reminder cadence suite passes with the autonomous-mode case removed. test: `tests/workflows/test_reminder_cadence_rules.py::TestReminderCadence::test_ten_turn_session_caps_brevity_and_restraint_reminders`.
- 3.1.9 - The shell-command condition helpers live in a sibling module, are re-exported, and `condition_helpers.py` is under 850 lines. file: `src/gobby/workflows/condition_helpers_shell.py`.
- 3.1.10 - The hardcoded turn-end overrides live in a sibling module, run in their original order ahead of any rule-level block, and `engine/core.py` is under 850 lines. file: `src/gobby/workflows/engine/core_overrides.py`.
- 3.1.11 - The override extraction preserves the documented precedence and the claimed-task refusal. test: `tests/workflows/test_rule_engine.py::TestOverrideCollectsMcpCalls::test_force_allow_trumps_rule_evaluated_block`.

## P4: Gate the wheel-manifest parity test to CI
`kind: framing`

**Goal**: an ordinary local pytest run performs no package builds, and CI still proves
the generated manifest matches the packaged tree.

### 4.1 Put the wheel-manifest parity test behind an opt-in flag [category: test]
`kind: deliverable`

Targets:
- `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`
- `.github/workflows/ci.yml`
- `pre-push-test.sh::*` — scope-reason: adds one opt-in deselect branch to the pytest selection arguments
- `tests/ci/test_postgres_test_stack.py::test_pre_push_excludes_live_opt_in_tests_by_default`

Research context. The test copies every tracked file into a temp tree, runs
`uv build --sdist` and `uv build --wheel` with 300-second timeouts each, unzips the
wheel and compares the generated manifest to a freshly built one. Two full package
builds on every local run, with no marker and no guard.

The repository has no CI marker and no test reads a CI environment variable. The
established opt-in is a module-level `pytestmark` list combining a category marker
with a skipif on a `GOBBY_RUN_*` variable, evaluated before fixture setup.
`tests/packaging/test_installed_wheel_ui_smoke.py` is the canonical example; seven
other modules follow the same shape.

Each such gate is recorded in three places: the variable is exported in the test job
environment of the CI workflow, a deselect branch is added to the pre-push selection
arguments, and `test_pre_push_excludes_live_opt_in_tests_by_default` asserts the
literal variable name and deselect target appear in that script. The registry test
does not fail on its own when a gate is added, which is exactly why it must be
updated deliberately.

Nothing else needs gating and nothing else should be. The Rust schema-catalog test
compiles only under the postgres feature, off by default, and runs solely from an
explicit feature-enabled CI step. The definition-hash unit tests in
`tests/workflows/test_template_hashes.py`, `tests/sync/test_integrity.py` and
`tests/test_build_backend.py` are unit-marked, run against temporary directories, and
assert on no committed file. Rules, skills, agents and variables have no committed
hashes file at all: `TemplateHashCache` hashes them in memory at startup purely to
detect DB drift. Production tamper detection runs through `verify_bundled_integrity`
and the startup refusal `dirty_bundled_content_refusal`, neither of which depends on
a test.

**Acceptance:**

- 4.1.1 - The module carries a pytestmark list with a category marker and an opt-in skipif, and the test is skipped without the variable. test: `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.
- 4.1.2 - The CI test job exports the opt-in variable alongside the test-protection flag. file: `.github/workflows/ci.yml`.
- 4.1.3 - The pre-push script deselects the test unless the variable is set. file: `pre-push-test.sh`.
- 4.1.4 - The opt-in registry test names the new variable and its deselect target. test: `tests/ci/test_postgres_test_stack.py::test_pre_push_excludes_live_opt_in_tests_by_default`.

## E1 End-to-end verification
`kind: verification`

Scoped suites against the isolated test hub, covering every touched module:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_stop_gates_rules.py tests/workflows/test_turn_interrupt_stop_gates.py tests/workflows/test_rule_engine_task_helper_wiring.py tests/workflows/test_block_tools_after_handoff_compact.py tests/workflows/test_handoff_pull_gate.py tests/workflows/test_condition_helpers.py tests/workflows/test_safe_evaluator.py tests/workflows/test_reminder_cadence_rules.py tests/sessions/ tests/install/test_bundled_content_manifest.py tests/ci/test_postgres_test_stack.py -q`.
Then `uv run ruff format src/`, `uv run ruff check src/ tests/`, `uv run mypy src/`,
and `bash -n pre-push-test.sh`.

Gating proof: the manifest test reports skipped without the opt-in variable and runs
green with it set.

Live, from the main checkout after a merge and an announced daemon restart. Claim a
task, call set_handoff with clear_session false and end the turn: the turn end is
allowed with no require-task-close block and the compact lands. On resume the typed
continuation prompt arrives with no memory index, no review lessons and no skill-load
directives, and any tool call other than get_handoff is refused, including a skill
load the agent initiates itself. After the pull, the task is still claimed and
attempting to stop blocks on require-task-close again. The workflows surface lists no
installed row for require-epic-tree-close or any auto-task rule, and lists the two new
rules enabled. Closing the last open child of an epic still auto-closes the ancestor.
The daemon log carries the suppression pseudo-rule line for the staged turn end.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

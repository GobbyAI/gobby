# Staleness report: #22643 Handoff stop gates and pull order

**Verdict: MOSTLY DELIVERED.** P1, P2 and P3 have landed on 0.5.0. #22638 delivered the
staged-handoff stop bypass and its session-start re-arm. #24421 delivered the epic-gate
retirement, the turn-start injection guards, the handoff pull gate and the auto-task
retirement. The installed rule rows match. Only P4 is still needed: the wheel-manifest
parity test still runs two package builds on every local pytest run. It is joined by one
found defect in the same files: a dead `GOBBY_RUN_BUILD_CANARY` deselect branch.

Refreshed by the Plan Writer gobby#18340 for #23669 on 2026-10-28, against 0.5.0 HEAD
`e9ce2f79c7ce243db9c973c718a9cb81fe80c71b`. Canonical plan:
`.gobby/plans/handoff-stop-gates-and-pull-order.md`, which was unchanged since
`4b8f14d09c` (2026-09-20) before this pass.

This replaces the 2026-10-06 report by gobby#15401, which was pinned to `93169cf439` and
last committed at `c2ad231f46`. That report came eight days before #24421 landed, so most
of its "still open" rows are now done. The sibling
`handoff-stop-gates-staleness-2026-10.json` is still that snapshot's raw evidence packet.
It was not regenerated and does not describe today's tree.

## Method

- Source reads came from gcode navigation (`grep`, `outline`, `symbol-at`) on the main
  checkout at the HEAD above. The index was refreshing during the pass; every hit cited
  below was read back from the working file. Line numbers are current working-tree lines.
- Installed state came from `gobby-workflows:list_rules` and `get_rule` on the live daemon
  DB, not from template defaults.
- Task and commit provenance came from `gobby-tasks:get_task` and `git log`.
- No pytest, build, lint or runtime probe was run in this pass. "Done" means the code or
  rule is present and named tests exist; their pass state comes from the #24421 close
  review, which reported 142 passed across ten focused test files.

## Landing commits

| Commit | Date | Task | Content |
| --- | --- | --- | --- |
| `52dca8c848` | 2026-09-21 | #22638 | Staged-handoff turn-end suppression, reserved marker, re-arm rule |
| `055288e643` | 2026-09-21 | #22638 | Queued compact receipt recorded on completion |
| `5f328172bc` | 2026-10-14 | #24421 | Pull gate, injection guards, epic-gate and auto-task retirement |
| `69aa85a125` | 2026-10-14 | #24421 | Retarget retired epic-gate test dependents |
| `3994415127` | 2026-10-14 | #24421 | Merge 0.5.0, reconcile the rule inventory; landed as merge `ce42fd9e5d` |
| `dea47c35a4` | 2026-10-21 | #23854 | Deleted three of the four review-learning lesson injectors |

#24421 closed `done` on 2026-10-19 with review status `valid`.

## Installed rules (2026-10-28)

- `stop-gates`: 8 enabled rows, including `rearm-close-gates-on-session-start` and
  `require-task-close`. There is no `require-epic-tree-close` row; `get_rule` returns
  `Rule 'require-epic-tree-close' not found`.
- `context-handoff`: 16 enabled rows, including `require-handoff-pull-before-tool`
  (before_tool) and `block-tools-after-handoff-compact`.
- `auto-task`: 0 rows.
- `surface-memories-on-turn-start` (row `9173255a`) is enabled, and its `when` starts with
  `not variables.get('handoff_pull_pending')`.
- The rules reference agrees: `src/gobby/install/shared/workflows/rules/AGENTS.md:13`
  lists `stop-gates` with 8 rules and `:17` lists `context-handoff` with 16. There is no
  `auto-task` row.

## The 2026-10-06 block-tools-after-handoff-compact behaviour

The reported deadlock went like this. `set_handoff` staged an attempt. Then
`block-tools-after-handoff-compact` told the agent to end its turn so the compact could
run, and `require-task-close` refused that turn end because a task was claimed. That path
is now closed:

- `stage_handoff_attempt` writes `marker_updates[HANDOFF_TURN_END_PENDING_VARIABLE] = True`
  (`src/gobby/sessions/handoff.py:417`).
- `EvaluationMixin._run_rule_loop_pass` checks that marker on any turn-end event. While it
  is set, every block gate is suppressed under the pseudo-rule `pending-handoff-delivery`
  (`src/gobby/workflows/engine/evaluation.py:540`). Non-block effects still run.
- `rearm-close-gates-on-session-start` sets `_handoff_turn_end_pending` back to false at
  every session start, with no `when` clause
  (`src/gobby/install/shared/workflows/rules/stop-gates/rearm-close-gates-on-session-start.yaml:11`).

`block-tools-after-handoff-compact` is still what it was on 2026-10-06: a gate that runs
while the staged delivery is pending, and is not the resume-pull gate. Resume-pull
enforcement is now the separate `require-handoff-pull-before-tool`.

## Every deliverable

| Deliverable | Status | Delivered by |
| --- | --- | --- |
| 1.1 Suppress turn-end block gates while a handoff is staged | done | #22638 (`52dca8c848`, `055288e643`) |
| 1.2 Retire require-epic-tree-close | done | #24421 (`5f328172bc`, `69aa85a125`) |
| 2.1 Defer turn-start injections while a pull is pending | done | #24421 (`5f328172bc`); #23854 (`dea47c35a4`) deleted three injectors |
| 2.2 Block tools until the handoff pull runs | done | #24421 (`5f328172bc`) |
| 3.1 Delete the auto-task rules and the tree-completeness helper | partly done | #24421 (`5f328172bc`). Retirement is complete and `condition_helpers.py` no longer needs its split; the `engine/core.py` split did not happen and is no longer required (3.1.10). |
| 4.1 Put the wheel-manifest parity test behind an opt-in flag | still needed | nothing landed |

## Every acceptance item

37 items: 27 done, 5 done by a different test or mechanism, 1 with no remaining
obligation, and 4 still needed. The revised 4.1 adds one obligation for the dead canary
branch found in this pass.

| Item | Status | Evidence |
| --- | --- | --- |
| 1.1.1 | done | `src/gobby/sessions/handoff.py:417` writes the marker inside `stage_handoff_attempt`; `52dca8c848` (#22638). |
| 1.1.2 | done | `src/gobby/workflows/engine/evaluation.py:540`: pseudo-rule `pending-handoff-delivery`. Non-block pass-through is proven: `tests/hooks/test_stop_handoff_pending.py:265` asserts that the synthetic `observe-handoff-stop` set_variable fires on the suppressed turn end (`:277`). No test asserts the audit record itself; that is a test-depth note with no broken behavior. |
| 1.1.3 | done | `src/gobby/workflows/reserved_variables.py:5` defines `HANDOFF_TURN_END_PENDING_VARIABLE`, and agent writes are refused; `52dca8c848`. |
| 1.1.4 | done | `rearm-close-gates-on-session-start.yaml:11`, no `when`; the installed row is enabled. |
| 1.1.5 | done, different test | `tests/workflows/test_stop_gates_rules.py:588` `test_staged_handoff_credits_the_stop` (bundled rules, claimed task, STOP and AFTER_AGENT) asserts allow. The planned test name was never used. |
| 1.1.6 | done, different test | `tests/workflows/test_stop_gates_rules.py:605` `test_consumed_handoff_rearms_the_stop_gate` and `tests/hooks/test_stop_handoff_pending.py:280` `test_stop_gates_rearm_after_handoff_consumed`, which shows the marker false after session start and the stop blocking again. The re-arm rule writes only `_handoff_turn_end_pending`, so `handoff_pull_pending` is untouched; no test asserts that explicitly. |
| 1.1.7 | done | The refusal is at `src/gobby/workflows/engine/core.py:606`; the test is `tests/workflows/test_stop_gates_rules.py:2192` `test_force_allow_suppressed_when_task_claimed`. |
| 1.2.1 | done, different mechanism | The template was deleted, not moved to `deprecated/` (`5f328172bc`). Sync soft-deleted the row (installed `get_rule` returns not found). Absence is pinned by `tests/workflows/test_handoff_pull_gate.py:136` `test_retired_templates_do_not_reappear_on_sync` (`:139`). |
| 1.2.2 | done | `rules/AGENTS.md:13` says 8 rules. The count grew from later additions such as the seat-idle and found-work rules; the purpose text has no epic gate. |
| 1.2.3 | done | `tests/workflows/test_stop_gates_rules.py:256` `test_stop_attempts_increments_even_when_force_allowed` exists; no `require-epic-tree-close` reference is left in that file (`69aa85a125`). |
| 1.2.4 | done | `tests/workflows/test_turn_interrupt_stop_gates.py:180` exists. `STOP_GATE_NAMES` at `:29-33` still contains the string `require-epic-tree-close`, but `:81` uses it only to name synthetic rules the test creates in its own temp DB. No bundled rule is involved, so nothing needs editing. |
| 1.2.5 | done | `tests/workflows/test_rule_engine_task_helper_wiring.py:205` was retargeted in `69aa85a125`. The two epic-only wiring tests are gone. |
| 2.1.1 | done | The template and installed row of `surface-memories-on-turn-start` carry the pull guard; `5f328172bc`. |
| 2.1.2 | done | `inject-plan-reviewer-lessons.yaml` carries the guard (`5f328172bc`). The other three lesson injectors were deleted outright in `dea47c35a4` (#23854). |
| 2.1.3 | done | `brevity/reinforce-brevity.yaml` has 2 guarded turn-start reminders, and `restraint/require-restraint-skill.yaml` has 1. The opt-out handlers have no guard (`5f328172bc`). |
| 2.1.4 | done | `memory-lifecycle/increment-parent-turn-seq.yaml` has no pull guard and still advances on resume turns. |
| 2.1.5 | done | `tests/workflows/test_reminder_cadence_rules.py:78` `test_pending_handoff_pull_suppresses_turn_start_reminders`. |
| 2.2.1 | done, different mechanism | No `handoff_conditions.py` and no new helpers. The gate's `when` reads `variables.get('handoff_pull_pending')` and the MCP server and tool names inline (`context-handoff/require-handoff-pull-before-tool.yaml:9-25`). The plan's helper module is unnecessary. |
| 2.2.2 | done | `require-handoff-pull-before-tool.yaml:4`: before_tool, priority 1, enabled; the installed row is enabled. |
| 2.2.3 | done | `tests/workflows/test_handoff_pull_gate.py:88` `test_pull_gate_allows_handoff_prerequisite_tools`. #24421's review reported 17 prerequisite-allow cases passing. |
| 2.2.4 | done | `tests/workflows/test_handoff_pull_gate.py:74` `test_pull_gate_blocks_skill_load_until_pull`. |
| 2.2.5 | done | `rules/AGENTS.md:17` says 16. The plan's figure of 15 was already stale on 2026-10-06; 16 matches the installed inventory. |
| 3.1.1 | done | The `auto-task/` directory is gone and no `auto_task_ref` is left in `src/` or `tests/` (`5f328172bc`); 0 installed rows. |
| 3.1.2 | done | `rules/AGENTS.md` has no `auto-task` row. |
| 3.1.3 | done | `task_tree_complete` and `_is_tree_complete` no longer exist; `tests/workflows/test_handoff_pull_gate.py:133` asserts the helper is absent from `build_condition_helpers()`. |
| 3.1.4 | done | `build_condition_helpers` no longer registers tree completeness (`5f328172bc`, `src/gobby/workflows/safe_evaluator.py`). |
| 3.1.5 | done | No `auto_task_ref` read in `src/gobby/workflows/engine/core.py` (`5f328172bc`). |
| 3.1.6 | done | `preserve-context-on-compact.yaml` lost its autonomous-mode reset (`5f328172bc`). |
| 3.1.7 | done, different test | The planned `test_condition_helpers_without_task_manager_return_false` never existed. `tests/workflows/test_safe_evaluator.py` was reduced in `5f328172bc`, and absence of the helper is pinned at `test_handoff_pull_gate.py:133`. |
| 3.1.8 | done | `tests/workflows/test_reminder_cadence_rules.py:103` `test_ten_turn_session_caps_brevity_and_restraint_reminders`; the autonomous-mode case is removed. |
| 3.1.9 | done | `src/gobby/workflows/condition_helpers.py` is 773 lines, under the 850 heuristic and the 1,000 ceiling. No split is needed and `condition_helpers_shell.py` was never created. |
| 3.1.10 | no remaining obligation | `src/gobby/workflows/engine/core.py` is 953 lines, not under 850, and `core_overrides.py` was never created. The only core.py edit this plan wanted (the `auto_task_ref` read) landed as a one-line deletion. No remaining deliverable targets core.py, it is under the 1,000 ceiling, and the monolith hook still guards any later write that crosses it. |
| 3.1.11 | done | `tests/workflows/test_rule_engine.py:2321` `test_force_allow_trumps_rule_evaluated_block` exists; with no extraction there is nothing new to protect. |
| 4.1.1 | still needed | `tests/install/test_bundled_content_manifest.py:14` has no `pytestmark` and no opt-in. It still runs `uv build --sdist` and `uv build --wheel` with `timeout=300` each. |
| 4.1.2 | still needed | The `.github/workflows/ci.yml:243-257` test job env has the Postgres variables and `GOBBY_TEST_PROTECT`, with no manifest opt-in. |
| 4.1.3 | still needed | `pre-push-test.sh:63-85` has opt-in branches for the sandbox, wheel UI smoke, Droid hook, build canary and e2e lifecycle, and none for the manifest test. |
| 4.1.4 | still needed | `tests/ci/test_postgres_test_stack.py:506` `test_pre_push_excludes_live_opt_in_tests_by_default` names no manifest opt-in. |

## Found in this pass

- **Dead build-canary deselect.** `pre-push-test.sh:76-80` still deselects
  `test_real_small_gobby_build_canary` behind `GOBBY_RUN_BUILD_CANARY`. The module that
  test lived in was deleted in `d8dd689d8d` (#23850, closed 2026-10-21), and
  `tests/ci/test_postgres_test_stack.py:519-522` still asserts the dead branch. #23850 is
  closed and nobody owns this, so it goes into the revised 4.1 because it touches the same
  two files.

## Dispositions for the revised plan

- **Seat injectors on the resume turn.** `roles/inject-seat-common.yaml` (priority 20) and
  `roles/inject-agent-workspace.yaml` (priority 21) fire on turn_start with no pull guard.
  `reset-seat-common-on-context-loss` re-arms both after a clear or compact, so they inject
  on the resume turn. They stay unguarded on purpose. They carry seat identity and policy,
  and the seat guidance itself says to call `gobby-sessions:get_handoff` even during a hold.
  The pull gate at priority 1 refuses every non-exempt tool until the pull runs, so nothing
  injected can act first. The same goes for `plan-mode/handle-plan-mode-entry.yaml` and
  `memory-lifecycle/layered-memory-guidance.yaml::retry-gobby-tools-after-proxy-connects`:
  their directives can only be followed after the pull. Guarding them would just delay
  identity context by one turn and give no enforcement benefit.
- **Clean window.** All remaining work is a test module, the CI workflow, the pre-push
  script and a CI-contract test. It touches no rule template, no daemon code and no
  installed binary, so no restart or cutover is needed. The `clean-window` rationale in
  #22643's description no longer applies; that label is the Orchestrator's to edit.
- **Routing.** The remaining scope is one atomic deliverable. Expansion yields exactly one
  leaf under #22643.

Plan artifact: `.gobby/plans/handoff-stop-gates-and-pull-order.md`
**Plan ID:** handoff-stop-gates-and-pull-order

# Handoff Stop Gates And Pull Order

## Overview
`kind: framing`

The repository owner reported four defects in rule enforcement and test gating. Three
are delivered on 0.5.0. This revision, made under #23669 on 2026-10-28, cuts the plan
down to the one that is left.

Delivered:

1. A session that staged a handoff could not end its turn. `block-tools-after-handoff-compact`
   told it to stop so the queued compact could run, and `require-task-close` refused the
   stop while a task was claimed. #22638 fixed this. The turn-end block gates now stand
   down while a handoff is staged and re-arm at the next session start.
2. Nothing made an agent pull its handoff before loading skills or doing work. #24421
   fixed this with the `require-handoff-pull-before-tool` gate and pull guards on the
   turn-start injectors the plan named. One turn-start skill directive added outside
   that list, the plan-mode reference load, still fires ahead of the pull. That residue
   is deliverable 2.3.
3. The `auto-task` rule group and `require-epic-tree-close` were dead weight on the
   `task_tree_complete` helper. #24421 retired all of them.

Remaining:

- The plan-mode skill directive must wait for the pull (2.3).
- One test, `test_manifest_membership_matches_wheel`, runs two full `uv build`
   invocations on every local pytest run. Bundled-content tamper detection is a
   production concern, served at runtime by `verify_bundled_integrity`; it does not need
   a local test. CI must keep proving that the generated manifest matches the packaged
   tree.

The per-item evidence is in
`.gobby/plans/research/handoff-stop-gates-staleness-2026-10.md`.

## Delivered Scope
`kind: framing`

These sections of the 2026-09-20 plan (`4b8f14d09c`) are delivered, and each is closed
by a task that ended `done`. They are recorded here as evidence and are not expanded.

| Original section | Delivered by | Current evidence |
| --- | --- | --- |
| 1.1 Suppress turn-end block gates while a handoff is staged | #22638: `52dca8c848`, `055288e643` | `stage_handoff_attempt` writes `_handoff_turn_end_pending` (`src/gobby/sessions/handoff.py:417`). `EvaluationMixin._run_rule_loop_pass` suppresses turn-end block gates under the pseudo-rule `pending-handoff-delivery` (`src/gobby/workflows/engine/evaluation.py:540`). `rearm-close-gates-on-session-start` clears the marker with no `when` clause. Tests: `tests/workflows/test_stop_gates_rules.py` at `:588` and `:605`, and `tests/hooks/test_stop_handoff_pending.py` at `:265` and `:280`. |
| 1.2 Retire require-epic-tree-close | #24421: `5f328172bc`, `69aa85a125` | The template is deleted, the installed row is gone, and `test_retired_templates_do_not_reappear_on_sync` pins its absence (`tests/workflows/test_handoff_pull_gate.py:136`). |
| 2.1 Defer turn-start injections while a pull is pending | #24421: `5f328172bc`; #23854: `dea47c35a4` | Memory surfacing, the remaining plan-reviewer lesson injector, and the brevity and restraint reminders all carry `not variables.get('handoff_pull_pending')`. #23854 deleted the other three lesson injectors. Test: `test_pending_handoff_pull_suppresses_turn_start_reminders` (`tests/workflows/test_reminder_cadence_rules.py:78`). Seat Guidance, Agent Workspace and the proxy-retry notice stay unguarded by operator ruling (see Constraints). The plan-mode skill directive is the open residue, deliverable 2.3. |
| 2.2 Block tools until the handoff pull runs | #24421: `5f328172bc` | `context-handoff/require-handoff-pull-before-tool.yaml` is a priority-1 before_tool gate written as an inline `when`. The proposed `handoff_conditions.py` helper module was never needed. Tests: `tests/workflows/test_handoff_pull_gate.py` at `:74` and `:88`. |
| 3.1 Delete the auto-task rules and the tree-completeness helper | #24421: `5f328172bc` | The `auto-task/` directory, `task_tree_complete`, `_is_tree_complete` and the `auto_task_ref` read in `RuleEngine.evaluate` are gone; the installed `auto-task` group has 0 rows. `condition_helpers.py` is now 773 lines and needs no split. `engine/core.py` is 953 lines and was not split. That is not needed: this plan no longer edits it, and it is under the 1,000-line ceiling. |

The installed DB agrees as of 2026-10-28: `stop-gates` has 8 enabled rows and no epic
gate, `context-handoff` has 16 enabled rows including the pull gate, and `auto-task` has
none. The rules reference at `src/gobby/install/shared/workflows/rules/AGENTS.md` lists
the same counts.

## Constraints
`kind: framing`

- Resume-turn context requirement (Orchestrator ruling on PA-01, 2026-10-28, Option 1,
  approved by Josh as answer A on 2026-10-28):
  until the handoff is pulled, the resume turn carries no memory index, no review
  lessons and no skill-load directives. Three one-shot injections are allowed resume-turn
  context and named as exemptions: Seat Guidance (`roles/inject-seat-common.yaml`), Agent
  Workspace (`roles/inject-agent-workspace.yaml`), and the proxy-retry notice
  (`retry-gobby-tools-after-proxy-connects` in
  `memory-lifecycle/layered-memory-guidance.yaml`). They carry identity and recovery
  context, and the seat guidance itself requires `get_handoff` during holds. No
  turn_start follows the pull until the next prompt, so a guard would leave an autonomous
  seat without its policy and workspace path for the whole resumed turn. This replaces
  the original goal wording "no injected payload". Josh approved the narrowing.
- 2.3 edits a bundled rule template, which reaches the installed rule table only at
  daemon start. It is live after a merge and an ordinary daemon restart announced with a
  `global` `send_message`. It needs no cutover, binary install or quiet window. 4.1
  touches only tests, two CI workflows and the pre-push script, and needs no restart.
- CI coverage must not drop. Today two jobs run the parity test, ungated: the CI `test`
  job on both Python matrix entries, and the `release-branch-ci` job through
  `./pre-push-test.sh`. After this change both must still run it.
- Follow the repository's established opt-in shape. Use a module-level `pytestmark`
  combining a category marker with a `skipif` on a `GOBBY_RUN_*` variable, evaluated
  before fixture setup. Record the gate in three places: the CI job environment, a
  pre-push selection branch, and the pre-push opt-in registry test.
- Rejected: representing the delivered sections as `kind: deferred`. A typed deferral
  needs a `deferred-from:<plan-id>:<section-id>` label on its task. #22638 and #24421
  were not created from this plan and carry no such label. The obligations they met are
  finished and need no owner. The #22643 directive asks for delivered parts to be marked
  as delivered with commit evidence, which the framing table above does.
- Rejected under the same ruling: guarding the three exempt injections, either strictly
  (they land at the next prompt) or with re-delivery after a successful `get_handoff`
  (about three more after_tool rules). The first strips seat policy from the resumed
  turn; the second adds mechanism for context the pull gate already makes harmless.
- Rejected: moving the parity test into the CI `build` job to reuse its wheel. The test
  builds an sdist and wheel from a copy of the tracked tree on purpose. Reusing another
  job's wheel would mean rewriting the test, and it would drop the sdist-to-wheel path the
  test exists to check.

## P2: The plan-mode skill directive waits for the pull
`kind: framing`

**Goal**: no skill-load directive reaches the resume turn before the handoff is pulled.

### 2.3 Guard the plan-mode skill directive on a pending pull [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/plan-mode/handle-plan-mode-entry.yaml::*` — scope-reason: adds the pending-pull clause to the single rule's when
- `tests/workflows/test_plan_mode_rules.py::*` — scope-reason: adds one pending-pull case to TestHandlePlanModeEntry and the guard assertion to its when-shape test

Research context. `handle-plan-mode-entry` is a turn_start rule at priority 10, scoped to
`agent_scope: [default]`, with `when: "variables.get('plan_mode') and not
variables.get('plan_skill_directive_delivered')"`. Its effects are a `load_skill`
directive for `gobby:references/plan/overview.md` and `set_variable
plan_skill_directive_delivered: true`, both `delivery: on_receipt`.

How it reaches the resume turn: `context-handoff/preserve-context-on-compact.yaml` and
`plan-mode/reset-plan-mode-on-session-start.yaml` both reset
`plan_skill_directive_delivered` to false at the compact boundary. The session-start rule
also clears `plan_mode`, but turn-start plan-mode resolution (`resolve_plan_mode` in
`src/gobby/workflows/observer_plan_mode.py`) runs before rule evaluation. It sets
`plan_mode` again whenever the provider still reports plan mode on the typed continuation
prompt, and the directive then fires ahead of `get_handoff`. The pull gate refuses the
skill load itself, but the original requirement excludes skill-load directives from the
resume turn by name.

Change: put `not variables.get('handoff_pull_pending') and` before the existing clause.
The guard needs no clean-up. `handoff_pull_pending` is popped inside the `get_handoff`
transaction, so the directive fires once at the next turn_start, and
`plan_skill_directive_delivered` stays false until then. Waiting is harmless: plan mode is
interactive, so the next turn is a user prompt, and plan-artifact writes are already gated
on loading a plan reference by the skill-discovery group.

Tests. `TestHandlePlanModeEntry.test_fires_on_turn_start_with_plan_mode_guard` checks the
`when` text; add an assertion that it contains `handoff_pull_pending`.
`TestHandlePlanModeEntry.test_skill_directive_fires_once_for_default_agent` is the model
for the new case. It syncs bundled rules, builds a `BEFORE_AGENT` `HookEvent` with
`variables = {"_agent_type": "default", "plan_mode": True}`, evaluates through
`RuleEngine`, and checks
`skill_fetch_directive("gobby:references/plan/overview.md") in first.context`. The new
case does the same with `handoff_pull_pending: True` added. It asserts no directive and
`plan_skill_directive_delivered` not set. It then pops `handoff_pull_pending`, evaluates
again, and asserts the directive arrives once.

Planned check, not yet run: `tests/workflows/test_plan_mode_rules.py` passes against the
isolated test hub.

**Acceptance:**

- 2.3.1 - The plan-mode entry rule's when starts with `not variables.get('handoff_pull_pending')` and keeps the plan-mode and directive-delivered clauses. file: `src/gobby/install/shared/workflows/rules/plan-mode/handle-plan-mode-entry.yaml`.
- 2.3.2 - With a pull pending, a plan-mode turn start delivers no plan-reference directive and leaves the delivered flag unset; after the pull marker clears, the next turn start delivers it once. test: `tests/workflows/test_plan_mode_rules.py::TestHandlePlanModeEntry::test_skill_directive_waits_for_pending_handoff_pull`.

## P4: Gate the wheel-manifest parity test to CI
`kind: framing`

**Goal**: an ordinary local pytest run performs no package builds, and CI still proves
the generated manifest matches the packaged tree.

### 4.1 Put the wheel-manifest parity test behind an opt-in flag [category: test]
`kind: deliverable`

Targets:
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: adds the module-level pytestmark opt-in gate beside the module's only test, which stays unchanged
- `.github/workflows/ci.yml`
- `pre-push-test.sh::*` — scope-reason: the top-level pytest selection block gains the manifest opt-in branch and loses the dead build-canary branch; no shell function changes
- `tests/ci/test_postgres_test_stack.py::test_pre_push_excludes_live_opt_in_tests_by_default`
- `tests/ci/test_postgres_test_stack.py::test_ci_test_job_builds_and_runs_local_postgres_test_container`
- `.github/workflows/postgres-pgsearch-smoke.yml`
- `tests/ci/test_release_branch_prerequisites.py::test_release_workflow_provisions_pre_push_tools_and_falkor`

Research context. `test_manifest_membership_matches_wheel`, at line 14 of
`tests/install/test_bundled_content_manifest.py`, is the only test in its module. It
copies every tracked file outside `.gobby/` into a temp tree. It runs
`uv build --sdist` and then `uv build --wheel` from that sdist, each with
`timeout=300`, unzips the wheel, and compares the packaged
`bundled_content_manifest.json` with `build_bundled_content_manifest` over the packaged
`shared/` tree. The module has no `pytestmark`, no marker and no guard. It already
imports `os`, but not `pytest`.

The opt-in convention. The repository has no CI marker, and no test reads the `CI`
environment variable. Only `pre-push-test.sh` does (lines 56-59), to ignore the live AGY
probe under CI. The established gate is a module-level `pytestmark` list, which
pytest applies before fixture setup. `tests/packaging/test_installed_wheel_ui_smoke.py`
lines 22-28 are the canonical example: `pytest.mark.integration` plus a
`pytest.mark.skipif(os.environ.get("GOBBY_RUN_WHEEL_UI_SMOKE") != "1", reason="set
GOBBY_RUN_WHEEL_UI_SMOKE=1 to run installed-wheel UI smoke")`. The marker set in
`pyproject.toml` has `integration`, and `addopts` deselects no marker, so adding the
marker changes no default selection.

Chosen literals:
- variable `GOBBY_RUN_WHEEL_MANIFEST_PARITY`, set to `"1"` to run;
- marker `pytest.mark.integration`;
- skip reason `set GOBBY_RUN_WHEEL_MANIFEST_PARITY=1 to run the sdist-to-wheel manifest parity build`.

The skip reason must not go into `PYTEST_FORBIDDEN_SKIP_REASONS` in `pre-push-test.sh`,
and it matches no entry there. That list is for skips that must never happen.

CI. The `test` job in `.github/workflows/ci.yml` (lines 243-257, approximate) sets job
`env` with the six `GOBBY_POSTGRES_TEST_*` variables and `GOBBY_TEST_PROTECT: "1"`, and
runs the whole suite through `uv run pytest --cov=gobby ...`. Add
`GOBBY_RUN_WHEEL_MANIFEST_PARITY: "1"` to that job `env`. The parity test then keeps
running on both matrix entries, as it does today. In `tests/ci/test_postgres_test_stack.py`,
`test_ci_test_job_builds_and_runs_local_postgres_test_container` loads that job's `env`
and asserts `_job_env(env)` equals the Postgres mapping. `_job_env` filters to the six
Postgres names, so a new variable leaves that assertion intact. Add one assertion to the
test that `env["GOBBY_RUN_WHEEL_MANIFEST_PARITY"] == "1"`. Without it, nothing stops a
later edit from quietly dropping CI parity coverage.

Release branch CI. `.github/workflows/postgres-pgsearch-smoke.yml` (workflow
`Release Branch CI`) runs on pushes to version branches, `release/**` and `v*` tags.
`ci.yml` triggers only on `main`, `0.4.9`, `0.5.0`, its tags and pull requests, so
`release/**` and later version branches get parity coverage from this workflow alone.
Its `release-branch-ci` job runs `./pre-push-test.sh` (line 155), so the parity test
runs there today, ungated. The job `env` (lines 23-31) sets only the Falkor and Postgres
test variables. Add `GOBBY_RUN_WHEEL_MANIFEST_PARITY: "1"` to that job `env`; without
it, the new pre-push `--ignore` drops parity coverage on release branches.
`test_release_workflow_provisions_pre_push_tools_and_falkor` in
`tests/ci/test_release_branch_prerequisites.py` loads that job and already asserts two of
its `env` entries. Add one assertion that
`job["env"]["GOBBY_RUN_WHEEL_MANIFEST_PARITY"] == "1"`. No other workflow job runs the
parity module: the `voice-extra` and `terminal-acceptance` jobs run named paths only.

Pre-push. The `pre-push-test.sh` selection block (lines 54-85, approximate) builds
`PYTEST_SELECTION_ARGS`. A whole-module gate uses `--ignore=<module path>`, following the
`GOBBY_RUN_WHEEL_UI_SMOKE` branch. A single-test gate uses `--deselect=<node id>`. Add a
branch that applies `--ignore=tests/install/test_bundled_content_manifest.py` unless
`GOBBY_RUN_WHEEL_MANIFEST_PARITY` is `1`, placed beside the wheel UI smoke branch.

Found work folded in. The same block has an `if [ "${GOBBY_RUN_BUILD_CANARY:-}" != "1" ]`
branch that deselects `test_real_small_gobby_build_canary`. The module behind that node
id was deleted by `d8dd689d8d` (#23850, closed 2026-10-21), so the branch deselects
nothing. `test_pre_push_excludes_live_opt_in_tests_by_default` (line 506) still asserts
the `GOBBY_RUN_BUILD_CANARY` literal and that node id at lines 519-522. Remove the shell
branch and both assertions. #23850 is closed and no session owns this.

Registry test. `test_pre_push_excludes_live_opt_in_tests_by_default` reads the script
text and asserts each opt-in variable name and its selection target are present. It does
not fail by itself when a gate is added, so update it on purpose. Add assertions for
`GOBBY_RUN_WHEEL_MANIFEST_PARITY` and for
`PYTEST_SELECTION_ARGS+=(--ignore=tests/install/test_bundled_content_manifest.py)`.

Nothing else needs gating. The definition-hash unit tests (`tests/workflows/test_template_hashes.py`,
`tests/sync/test_integrity.py`, `tests/test_build_backend.py`) are unit-marked, run
against temporary directories, and assert on no committed file. Production tamper
detection runs through `verify_bundled_integrity` and the startup refusal
`dirty_bundled_content_refusal`, and depends on no test.

Planned checks, none run yet: the module reports one skip without the variable and one
pass with it set; `tests/ci/test_postgres_test_stack.py` and
`tests/ci/test_release_branch_prerequisites.py` pass; `bash -n pre-push-test.sh` is clean.

**Acceptance:**

- 4.1.1 - The module carries a `pytestmark` list of `pytest.mark.integration` and a skipif on `GOBBY_RUN_WHEEL_MANIFEST_PARITY`, so the test is skipped without the variable and runs with it set to 1. test: `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.
- 4.1.2 - The CI test job env exports `GOBBY_RUN_WHEEL_MANIFEST_PARITY: "1"`, and the CI-contract test asserts it. test: `tests/ci/test_postgres_test_stack.py::test_ci_test_job_builds_and_runs_local_postgres_test_container`.
- 4.1.3 - The pre-push selection block ignores the manifest module unless `GOBBY_RUN_WHEEL_MANIFEST_PARITY` is 1. file: `pre-push-test.sh`.
- 4.1.4 - The opt-in registry test asserts the new variable and its ignore target. test: `tests/ci/test_postgres_test_stack.py::test_pre_push_excludes_live_opt_in_tests_by_default`.
- 4.1.5 - No `GOBBY_RUN_BUILD_CANARY` branch or build-canary deselect remains in the pre-push script, and the registry test no longer asserts either. file: `pre-push-test.sh`.
- 4.1.6 - The `release-branch-ci` job env in `.github/workflows/postgres-pgsearch-smoke.yml` exports `GOBBY_RUN_WHEEL_MANIFEST_PARITY: "1"`, and the release-workflow test asserts it. test: `tests/ci/test_release_branch_prerequisites.py::test_release_workflow_provisions_pre_push_tools_and_falkor`.

## E1 End-to-end verification
`kind: verification`

All commands run against the isolated test hub, from the implementing checkout:

- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_plan_mode_rules.py tests/workflows/test_handoff_pull_gate.py tests/workflows/test_reminder_cadence_rules.py -q`
  passes.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_bundled_content_manifest.py -rs -q`
  reports 1 skipped, with the reason naming `GOBBY_RUN_WHEEL_MANIFEST_PARITY`, and starts
  no `uv build`.
- The same command with `GOBBY_RUN_WHEEL_MANIFEST_PARITY=1` reports 1 passed. It builds
  an sdist and a wheel, so expect several minutes.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ci/test_postgres_test_stack.py tests/ci/test_release_branch_prerequisites.py -q`
  passes.
- `bash -n pre-push-test.sh` is clean, and
  `gcode grep -F "GOBBY_RUN_BUILD_CANARY" pre-push-test.sh tests/ci -m 5` returns no hits.
- `uv run ruff format --check tests/workflows/test_plan_mode_rules.py tests/install/test_bundled_content_manifest.py tests/ci/test_postgres_test_stack.py tests/ci/test_release_branch_prerequisites.py`
  and `uv run ruff check tests/workflows/test_plan_mode_rules.py tests/install/test_bundled_content_manifest.py tests/ci/test_postgres_test_stack.py tests/ci/test_release_branch_prerequisites.py`
  are clean.
- `uv run gobby test-types audit tests/workflows/test_plan_mode_rules.py tests/install/test_bundled_content_manifest.py tests/ci/test_postgres_test_stack.py tests/ci/test_release_branch_prerequisites.py --baseline .gobby/test-types-baseline.json --fail-on-new`
  reports no new findings.

Live check for 2.3, from the main checkout after the merge and an announced daemon
restart: `gobby-workflows:get_rule(name="handle-plan-mode-entry")` shows the
pending-pull clause in the installed `when`. 4.1 needs no live check.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## V1 Plan Changelog
`kind: framing`

- 2026-10-28: Staleness revision and council pass by Plan Writer gobby#18340 under
  #23669. P1-P3 (1.1, 1.2, 2.1, 2.2, 3.1) were dropped to the Delivered Scope table as
  landed through #22638 and #24421. 4.1 is kept and gains the removal of the dead
  `GOBBY_RUN_BUILD_CANARY` branch. Plan Enhancer gobby#18354 converged with no
  suggestions. Plan Adversary gobby#18349 raised PA-01: the original P2 goal of no
  injected payload before the pull was unmet by the seat injectors. The Orchestrator
  ruled Option 1 and Josh approved it as answer A: Seat Guidance, Agent Workspace and the
  proxy-retry notice are named exemptions, and the plan-mode skill directive gets the
  pull guard as new deliverable 2.3. The adversary's two minor corrections (E1 lint scope
  and report routing) were applied at `6bd7de5`. Writer and adversary reached consensus
  on this revision.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Guard the plan-mode skill directive on a pending pull
  category: config
  task_type: bug
  depends_on: []
  validation_criteria: '2.3.1: The plan-mode entry rule''s when starts with `not variables.get(''handoff_pull_pending'')`
    and keeps the plan-mode and directive-delivered clauses. file: `src/gobby/install/shared/workflows/rules/plan-mode/handle-plan-mode-entry.yaml`.

    2.3.2: With a pull pending, a plan-mode turn start delivers no plan-reference
    directive and leaves the delivered flag unset; after the pull marker clears, the
    next turn start delivers it once. test: `tests/workflows/test_plan_mode_rules.py::TestHandlePlanModeEntry::test_skill_directive_waits_for_pending_handoff_pull`.'
  labels:
  - covers:handoff-stop-gates-and-pull-order:2.3:2.3.1
  - covers:handoff-stop-gates-and-pull-order:2.3:2.3.2
  tdd: false
  source_section: '2.3'
  assigned_agent: developer
- title: Put the wheel-manifest parity test behind an opt-in flag
  category: test
  task_type: chore
  depends_on: []
  validation_criteria: '4.1.1: The module carries a `pytestmark` list of `pytest.mark.integration`
    and a skipif on `GOBBY_RUN_WHEEL_MANIFEST_PARITY`, so the test is skipped without
    the variable and runs with it set to 1. test: `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.

    4.1.2: The CI test job env exports `GOBBY_RUN_WHEEL_MANIFEST_PARITY: "1"`, and
    the CI-contract test asserts it. test: `tests/ci/test_postgres_test_stack.py::test_ci_test_job_builds_and_runs_local_postgres_test_container`.

    4.1.3: The pre-push selection block ignores the manifest module unless `GOBBY_RUN_WHEEL_MANIFEST_PARITY`
    is 1. file: `pre-push-test.sh`.

    4.1.4: The opt-in registry test asserts the new variable and its ignore target.
    test: `tests/ci/test_postgres_test_stack.py::test_pre_push_excludes_live_opt_in_tests_by_default`.

    4.1.5: No `GOBBY_RUN_BUILD_CANARY` branch or build-canary deselect remains in
    the pre-push script, and the registry test no longer asserts either. file: `pre-push-test.sh`.'
  labels:
  - covers:handoff-stop-gates-and-pull-order:4.1:4.1.1
  - covers:handoff-stop-gates-and-pull-order:4.1:4.1.2
  - covers:handoff-stop-gates-and-pull-order:4.1:4.1.3
  - covers:handoff-stop-gates-and-pull-order:4.1:4.1.4
  - covers:handoff-stop-gates-and-pull-order:4.1:4.1.5
  tdd: false
  source_section: '4.1'
  assigned_agent: developer
```

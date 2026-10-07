# Staleness report: #22643 Handoff stop gates and pull order

**Verdict: PARTLY STALE.** The staged-handoff stop bypass, reserved marker and unconditional session-start rearm already landed through #22638 (pending-handoff stop fix). Epic enforcement is disabled in the installed DB, but its template/tests remain. Pull ordering, most injection guards, auto-task retirement/decomposition and wheel-test opt-in remain open. The proposed context-handoff count of 15 is contradicted by the present inventory: it already has 15, so the new gate would make 16. Refresh scope, references and validation before council.

Read-only research by gobby#15401 for Lane Manager gobby#15389, 2026-10-06, observations approximately 10:38–10:55 CDT. Canonical plan: `.gobby/plans/handoff-stop-gates-and-pull-order.md`. No council verdict, M1 or implementation approval is supplied here.

## Snapshot and method

- Requested source commit: `93169cf4395cc3730e313efac546a0d8db829a22`; tree: `af73737513ab57afdf17bbd82b3b335b43bddbb0`.
- Plan SHA256: `3cbe6092f3d8db315b429b919ddfad9a4ea4aea9596016982c8079f28e7d724d`. Working plan equals the requested commit and its path is clean. Task #22643 (Handoff stop gates and pull order) remains an unclaimed planning root with `clean-window`; this report does not mutate it.
- HEAD advanced during research through `547e9723b8`, `b5bb2dcd61`, and `366796ff6a`. Each gcode response preserves its actual binding. For all 46 source files in the evidence packet, SHA256 of the requested Git blob equals gcode's content hash and the working file. The negative-search scopes also have no diff against the requested commit. Thus cited source lines apply to the requested snapshot; installed DB state is a separate live observation.
- Used gcode indexed navigation and 49 complete range reads, a complete 15-rule template inventory, and three bounded negative searches. Empty searches carry gcode's warning that they do not prove a repository-wide negative; missing proposed files were separately checked against `git ls-tree` at the pin and the working checkout.
- Installed definitions and inventories were read through `gobby-workflows:get_rule/list_rules`. Enabled state below comes from those DB rows, not template defaults. The API does not expose enabled pins, so this report does not assert current pin metadata.
- “Already landed” means the required code/behavior is present at the requested commit. Existing tests were inspected; their passing status is not newly asserted. “Partly landed” identifies the delivered portion and exact remaining obligation.

Evidence packet: `/tmp/gobby-15401-22643-evidence.json`, SHA256 `81736440c093f83e1f970ba1eb37ab2a9af05701ece863e77f82deff88a6c07e`. It contains raw gcode responses, installed definitions/inventories, pin-byte comparisons, missing-file checks and validation diagnostics.

## Installed rule and hook observations

The logical before-tool hook `block-tools-after-handoff-compact` is enabled in installed row `87f2cfbf-ab46-465c-8735-4992b945ae2b` (DB03). Its condition is terminal `context_compact_handoff_result.delivery_pending` or `attempt_pending`; its reason requires ending the turn. It does not enforce pulling the handoff after resume. Source is `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml:55` (S28).

The assignment's “hook landed today” statement was a guess, not a verified landing. LM7 explicitly corrected it in message `c147bb22-90f6-491b-a3fd-f16942b8a4de`: the correction from gobby#15389 says to treat it as unverified and use row `87f2cfbf` / commit `bb308056a0`. The source path's latest change is `bb308056a0364bbb17d6b73f2a5575557ea78f09`, dated 2026-10-02, #23303 (BEFORE_AGENT cancellation landing). That task is CLOSED/VALID and includes the SessionStart/cancelled-turn gate-clear regression. No independently identified October 6 hook landing was established.

| Evidence | Installed row | State and relevant definition |
| --- | --- | --- |
| DB01 | `76de1039-71d5-4ea7-aa72-cc86526cd6a6` rearm-close-gates-on-session-start | Enabled; session_start; no when; clears only `_handoff_turn_end_pending`. |
| DB02 | `a1575ce9-dcc8-47e9-a045-ae2707b1f4df` require-epic-tree-close | Disabled; definition remains and calls task_tree_complete. |
| DB03 | `87f2cfbf-ab46-465c-8735-4992b945ae2b` block-tools-after-handoff-compact | Enabled; staged terminal delivery gate, not resume-pull gate. |
| DB04 | `9173255a-26a7-4491-a167-e39355ff4286` surface-memories-on-turn-start | Enabled; no handoff_pull_pending guard. |
| DB05 | `6337447d-8e7a-484d-9c9a-69cf96244d62` inject-planner-lessons | Disabled; when=True. |
| DB06 | `bcaa8508-919f-4e29-b9e1-14b0bedda4e8` inject-plan-reviewer-lessons | Disabled; when=True. |
| DB07 | `3f08a9f3-4445-40d2-8533-501b4d208caa` inject-plan-enhancer-lessons | Disabled; when=True. |
| DB08 | `a5fdfeaf-b63a-4963-985a-8a51418904c9` inject-qa-reviewer-lessons | Disabled; when=True. |
| DB09 | `d5e23200-205b-4866-b030-30de90cffb6c` inject-brevity-drift-feedback | Enabled; no pull guard. |
| DB10 | `71367f93-4989-4b5b-907c-a841bda6693c` remind-brevity-on-turn-start | Enabled; no pull guard. |
| DB11 | `489753bc-4d38-41df-afd6-030edacc6dda` remind-restraint-on-turn-start | Enabled; no pull guard. |
| DB12 | `87be0cbf-162c-40e2-ae23-c2dfe9302c9b` opt-out-brevity | Enabled; exact stop-brevity prompt handler. |
| DB13 | `db14cf2d-3840-4b19-82cb-5679ed4bb290` opt-out-restraint | Enabled; exact stop-restraint prompt handler. |
| DB14 | `08af1d81-057f-4db6-af9a-32de29b3b0f8` increment-parent-turn-seq | Enabled; bookkeeping has no pull exclusion. |
| DB15 | `847ec9b4-e627-4e21-9b92-134316d4ec57` preserve-context-on-compact | Enabled; autonomous_mode_reminder_turn reset remains. |
| DB16 | `687a5902-19b0-46f8-af78-88324c2b59b2` inject-autonomous-mode | Enabled; auto_task_ref condition remains. |
| DB17 | `a6f2b0dc-0b8e-42b8-bf4e-83ac6288296c` guide-task-continuation | Enabled; auto_task_ref and task_tree_complete remain. |
| DB18 | `1c250f03-2dab-4b8a-9f91-70c601877100` notify-task-tree-complete | Enabled; auto_task_ref and task_tree_complete remain. |
| DB19 | `ad91d7c1-bdc2-49ae-ac30-46c2c46e9a61` inject-seat-common | Enabled; turn_start injection with no pull guard. |
| DB20 | `f38eacbe-6dab-4c88-8aea-e7e0959ec9e7` require-task-close | Enabled; normal claimed-task stop gate remains. |

`require-handoff-pull-before-tool` returned `Rule 'require-handoff-pull-before-tool' not found`. Final group inventories: context-handoff 15 enabled rows; stop-gates 7 rows, 6 enabled; auto-task 3 enabled rows. The turn_start inventory had 23 rows, including the four disabled lesson rows and the enabled seat injector. Rule counts are not substitutes for checking rule identity and condition.

## Every acceptance item

37 items: 7 already landed, 7 partly landed, 22 still open, 1 contradicted. Item IDs match the canonical narrative.

| Item | Classification | Observation and evidence |
| --- | --- | --- |
| 1.1.1 | already landed | Daemon-owned marker is staged on clear and compact terminal paths; queued web-chat also sets it, idle in-process delivery deliberately does not. `src/gobby/sessions/handoff.py:398` (S01); #22638 pending-handoff stop fix. |
| 1.1.2 | already landed | `_run_rule_loop_pass` suppresses gates as `pending-handoff-delivery`, retains non-block processing, audits allowance, and assembly overrides hardcoded stop blocks. `src/gobby/workflows/engine/evaluation.py:513`, `:581`, `:425`, `:789`, `:810` (S02–S05); #22638 pending-handoff stop fix. |
| 1.1.3 | already landed | Reserved marker and trusted installed-rule writes exist; direct writes are refused. `src/gobby/workflows/reserved_variables.py:5`, `:16`, `:52`; `engine/effects.py:91`; `mcp_proxy/tools/workflows/_variables.py:111` (S06–S08). |
| 1.1.4 | already landed | Unconditional `session_start` rearm template exists and installed row is enabled. `src/gobby/install/shared/workflows/rules/stop-gates/rearm-close-gates-on-session-start.yaml:4` (S09; DB01). |
| 1.1.5 | partly landed | Behavior already has a regression in `tests/hooks/test_stop_handoff_pending.py:258` with claimed-task setup at `:230` (S10). The exact proposed `test_staged_handoff_allows_turn_end_with_claimed_task` in the stop-gate suite is absent; remap the acceptance test. |
| 1.1.6 | partly landed | Delivery, SessionStart rearm, consumption and subsequent blocking are tested at `tests/hooks/test_stop_handoff_pending.py:273` (S10). Exact proposed test is absent; this existing case has no explicit pull-marker-preservation assertion before consuming. Preserve that remaining assertion obligation. |
| 1.1.7 | already landed | Claimed-task refusal remains in `src/gobby/workflows/engine/core.py:609`; named test exists at `tests/workflows/test_stop_gates_rules.py:2183` (S42, S11). It is existing regression coverage, unrun here. |
| 1.2.1 | partly landed | Installed epic gate is disabled (DB02), as #22927 lane-goal retirement required; enabled template remains in synced tree. `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml:7` (S12). Source retirement and orphan sync have not landed. |
| 1.2.2 | already landed | Reference row already says 6 with no epic-tree-close purpose text: `src/gobby/install/shared/workflows/rules/AGENTS.md:13` (S13). Actual inventory still has 7 rows, 6 enabled; the existing number does not establish retirement. |
| 1.2.3 | still open | Retired name is still in rule-name set and both parameter lists. `tests/workflows/test_stop_gates_rules.py:107`, `:594`, `:622` (S14–S15). No scoped suite was executed. |
| 1.2.4 | still open | Interrupt suite still lists the gate at `tests/workflows/test_turn_interrupt_stop_gates.py:32` (S16). |
| 1.2.5 | still open | Dependency test still asserts both task and epic reasons. `tests/workflows/test_rule_engine_task_helper_wiring.py:248` (S17). |
| 2.1.1 | still open | Enabled installed memory-surfacing rule has no pending-pull guard (DB04); template condition matches it. `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-turn-start.yaml:11` (S18). |
| 2.1.2 | partly landed | All four installed lesson injectors are disabled, so currently do not fire (DB05–DB08). Their enabled templates still have `when: True`, with no pull guard. `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml:11` and the three sibling injectors at `:11` (S19–S22). Preserve disabled toggles; do not treat them as active defects or re-enable them. |
| 2.1.3 | partly landed | Opt-out handlers are enabled and remain callable (DB12–DB13). Enabled brevity/drift/restraint reminders still lack the pull guard (DB09–DB11). `src/gobby/install/shared/workflows/rules/brevity/reinforce-brevity.yaml:9`, `:22`, `:47`; `restraint/require-restraint-skill.yaml:9`, `:58` (S23–S24). |
| 2.1.4 | already landed | Existing parent-turn bookkeeping has no pending-pull exclusion and advances on matching parent turns. `src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml:9` (S25; DB14). Keep this behavior while adding injection guards. |
| 2.1.5 | still open | Proposed pending-pull cadence test is absent. Existing cadence and compact-reset cases test the old behavior: `tests/workflows/test_reminder_cadence_rules.py:67`, `:98` (S27). |
| 2.2.1 | still open | Proposed `workflows/handoff_conditions.py` is absent at the pin and working checkout. Scoped gcode search found no pending-pull implementation in workflows; existing helper registry retains task helpers. `src/gobby/workflows/safe_evaluator.py:665` (S36; N01/N02). |
| 2.2.2 | still open | Proposed pull gate YAML is absent from pinned Git tree and working checkout; installed get_rule explicitly returned not found. Current block-tools rule gates staged delivery, not pending pull after resume: `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml:60` (S28; DB03; N01). |
| 2.2.3 | still open | New pull-gate test module is absent. Shared table already exists for the two pressure/retry gates, including newer coordination-wait entry. It has no pending-memory recall case and includes pre-staging calls such as set_handoff. Reconcile allowlist and table semantics for the new gate. `tests/workflows/test_block_tools_after_handoff_compact.py:547` (S29). |
| 2.2.4 | still open | New skill-load pull-gate regression is absent with its module (N01/N02 and pinned-tree inventory). Argumentless pull and unblockable discovery plumbing already exist at `src/gobby/workflows/enforcement/blocking.py:99`, `:150` (S30); they do not implement this gate. |
| 2.2.5 | contradicted | Reference currently says 14 (`src/gobby/install/shared/workflows/rules/AGENTS.md:17`, S13), but source and installed context-handoff inventories already contain 15 without the proposed pull gate. Adding it makes 16 unless council explicitly retires another row. The planned final count 15 is stale (S50; installed inventory). |
| 3.1.1 | still open | All three auto-task templates remain and their installed rows are enabled (DB16–DB18). `src/gobby/install/shared/workflows/rules/auto-task/inject-autonomous-mode.yaml:14`, `guide-task-continuation.yaml:9`, `notify-task-tree-complete.yaml:9` (S32–S34). |
| 3.1.2 | still open | Auto-task reference row remains at `src/gobby/install/shared/workflows/rules/AGENTS.md:18` (S13). |
| 3.1.3 | still open | Both functions still exist. `src/gobby/workflows/condition_helpers.py:837`, `:964` (S37–S38). |
| 3.1.4 | partly landed | Four retained task helpers are already registered with and without a manager, but tree completeness is also still registered. `src/gobby/workflows/safe_evaluator.py:667` (S36). Removal remains open. |
| 3.1.5 | still open | Core still reads `auto_task_ref` for turn-end diagnostics at `src/gobby/workflows/engine/core.py:493` (S41). |
| 3.1.6 | still open | Template and enabled installed compact-preservation rule still reset the autonomous reminder. `src/gobby/install/shared/workflows/rules/context-handoff/preserve-context-on-compact.yaml:54` (S35; DB15). |
| 3.1.7 | still open | Retired helper tests remain, including no-manager false expectation at `tests/workflows/test_safe_evaluator.py:148` (S43). The plan's exact `test_condition_helpers_without_task_manager_return_false` symbol is absent; rebase its test reference. |
| 3.1.8 | still open | Autonomous cadence case and compact reset assertion remain at `tests/workflows/test_reminder_cadence_rules.py:84`, `:119` (S27). |
| 3.1.9 | still open | Sibling module absent; shell wrappers and implementations remain in condition_helpers, now 996 lines rather than the plan's 988. `src/gobby/workflows/condition_helpers.py:547`, `:565`, `:996` (S38–S40; pinned-tree/byte verification). |
| 3.1.10 | still open | Sibling override module absent; override chain remains in core, now 948 lines rather than the plan's 899. `src/gobby/workflows/engine/core.py:609`, `:620`, `:629` (S42; pinned-tree/byte verification). |
| 3.1.11 | partly landed | Named precedence regression already exists at `tests/workflows/test_rule_engine.py:2217` and claimed-task refusal test at `tests/workflows/test_stop_gates_rules.py:2183` (S44, S11). There is no extraction to validate yet; preserve and rerun them after it. |
| 4.1.1 | still open | Whole module has no pytestmark or opt-in guard and still builds sdist and wheel with two 300-second timeouts. `tests/install/test_bundled_content_manifest.py:1`, `:43`, `:53` (S46). Builds were not executed. |
| 4.1.2 | still open | CI test-job environment has protection/database variables and no wheel-manifest opt-in. `.github/workflows/ci.yml:250` (S48). Council must select one literal variable name. |
| 4.1.3 | still open | Existing opt-in selection list has no manifest-test deselect. `pre-push-test.sh:55` (S47). |
| 4.1.4 | still open | Registry test lists existing opt-ins, with no manifest target or new flag. `tests/ci/test_postgres_test_stack.py:506` (S49). |

## Framing, E1 and repairs before council

1. Replace the overview's staged-handoff deadlock premise (plan lines 11–17) with the delivered #22638 (pending-handoff stop fix) baseline. The source landing is `52dca8c848c2532e6ab28af6d3263be2ef300dea`, 2026-09-21; follow-up receipt fix `055288e6439dc938208249c33ef7e77c4a0723cd` is the task's closing commit. Both are ancestors of the requested pin. Preserve the existing terminal/queued-web versus idle-in-process distinction; unconditional bypass for idle web chat would undo that task's reviewed corrections. Remap existing tests and retain the explicit pull-marker-preservation assertion gap; avoid reimplementing completed production work.
2. Distinguish disabled enforcement from source retirement. #22927 (Retire lane /goal and make Lane Manager recover stalled lanes), CLOSED/VALID, explicitly forbids reactivating epic enforcement; linked closing commit is `8df638bbc7d4e86992dc4286e6fbe05d94b2562f`. Memory `9215aba2-b193-5b35-b1f1-24162643ec3d` records the original #22924 lane-goal decision. Physical retirement and its test cleanup remain in this plan; neither disabled installed state nor the already-6 documentation row proves completion.
3. Refresh the injection sweep. The plan's sixteen-file framing predates `inject-seat-common`: enabled template at `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml:11` (S26), latest path commit `3b7df025f5f403fd73d981ceb85901d66be683f3`, #23339 (plan-seat definitions), 2026-10-02. Its condition has no pull guard. Add an explicit council disposition for this injector and resweep active injectors. Preserve disabled lesson rows; memory `19d93123-6486-5660-82eb-fb3d40f29323` records an injection hold pending explicit user review, though it is not proof of the individual rows' current pin metadata.
4. Update the pull-gate reference table to the actual inventory and choose the exact allowlist. Existing pressure/retry prerequisite tables include pre-staging set_handoff, feedback and wait_for_coordination. The new gate's pending-memory recall behavior needs its own proven allowance. `get_handoff` now has optional child-run and failed-attempt recovery selectors (`src/gobby/mcp_proxy/tools/sessions/_handoff.py:161`, S31); preserve argumentless normal consumption and explicitly scope recovery selectors. E1's literal claim that every call other than get_handoff is refused (plan lines 429–430) conflicts with the plan's memory/coordination exceptions and unblockable schema discovery. Correct E1 to the settled allowlist.
5. Rebase decomposition and test references. `condition_helpers.py` is 996 lines; `engine/core.py` 948; `safe_evaluator.py` 887; `sessions/handoff.py` 980; `engine/evaluation.py` 927. New sibling targets are absent. Existing validation/wrapper functions already delegate to config/tasks implementations (`src/gobby/workflows/condition_helpers.py:19`, `:547`, S39–S40); preserve those consumers when moving wrappers. The exact acceptance test in 3.1.7 is absent. Recompute targets, consumer coverage and shared-path ordering after removing completed stop-bypass work.
6. E1 is partly represented by #22638 pending-handoff stop tests, but whole-plan verification is still open: pull-gate tests/files are absent; retired installed rows still exist; opt-in skip/run proof does not exist. Ancestor auto-close remains a separate storage path: `_close_eligible_ancestors` delegates to `close_eligible_parent_chain` at `src/gobby/storage/tasks/_stage_utils.py:171`, with claim/hold/criteria guards at `:211` (S45). Keep it outside helper retirement. No live claim, handoff, task closure, build, daemon activation or restart probe was executed here.

## Validation result

`uv run gobby plans validate .gobby/plans/handoff-stop-gates-and-pull-order.md -p /Users/josh/Projects/gobby` returned **exit 1**, with four `production-size-growth` errors:

- 1.1: handoff.py 980 lines, missing split target.
- 1.1: engine/evaluation.py 927 lines, missing split target.
- 2.2: safe_evaluator.py 887 lines, missing explicit split/move.
- 3.1: safe_evaluator.py 887 lines, missing explicit split/move.

The two 1.1 production targets may disappear when completed work becomes verified framing; the remaining safe-evaluator obligations need a deliberate split and ordered targets. These are plan-validation failures, not failed source tests. Expansion validation was not run: the narrative has no M1 and this assignment authorizes staleness research only. No pytest, package build, lint/type run or runtime mutation probe was run. No main-checkout file or task state was changed.

## gcode citation register

Each S citation resolves to the exact complete gcode range and SHA256 excerpt below. Raw responses in the evidence packet preserve binding, time, full-file content hash, numbered excerpt and bounds. End-of-file clamping warnings are retained; no cited range is incomplete.

| Ref | File and returned lines | Excerpt SHA256 |
| --- | --- | --- |
| S01 | `src/gobby/sessions/handoff.py:377`–409 | `ae3fd6388006d2132cb429cf1065a59ef2581b075315d4ccbb4338752a636515` |
| S02 | `src/gobby/workflows/engine/evaluation.py:485`–550 | `7fd74c5185fa36f5521bdf30a9754387c2fed6ccee458aa0c792f0525f3050e4` |
| S03 | `src/gobby/workflows/engine/evaluation.py:411`–436 | `aa623156d70e9e3cbfecfec2a0ae9b45482632b8205ea84afd2b90b957a06be3` |
| S04 | `src/gobby/workflows/engine/evaluation.py:570`–610 | `e63d875cb108f8a4fd29cb23c14f5204ba55f376a9fea30e5fd84f0ed453d211` |
| S05 | `src/gobby/workflows/engine/evaluation.py:785`–850 | `3fa585161e8ef709c12a53b25460a3f4274973c4c178fc9bded8bf8308e03d94` |
| S06 | `src/gobby/workflows/reserved_variables.py:1`–60 | `5ebc84b3c206be72787eba2f5a0d8c5b4503c6eca81694cfc29c9d16d5eaf79f` |
| S07 | `src/gobby/workflows/engine/effects.py:75`–100 | `aafbad7d35f6dc5234b5d45f3875c439a9036051b493733f334b77121b638379` |
| S08 | `src/gobby/mcp_proxy/tools/workflows/_variables.py:95`–126 | `62e59d5932b5397846fbdb2cbeba9d023bc7b1f7b4a3f073565bae928c6eca3d` |
| S09 | `src/gobby/install/shared/workflows/rules/stop-gates/rearm-close-gates-on-session-start.yaml:1`–12 | `bfe321dcefb483bb9008e15b435ad8176845fdb08899a750a05e6386513ee45e` |
| S10 | `tests/hooks/test_stop_handoff_pending.py:220`–310 | `90037d943276f3773e8c4421d6df85707d0c595b5ef55ad825c062762f62e233` |
| S11 | `tests/workflows/test_stop_gates_rules.py:2179`–2237 | `54bb3993f86f3cdf55ee884e8ae8586e7859288f38d18ca87cf2d386dbb25a35` |
| S12 | `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml:1`–20 | `cf157badb0b1c439231ac7b2c3cafd42456fdbd60abdfcc1a539b0049328d406` |
| S13 | `src/gobby/install/shared/workflows/rules/AGENTS.md:1`–109 | `8114977efcb6fddb411ee489e5f95e76ec7aacd259439d32823177191d2cfe69` |
| S14 | `tests/workflows/test_stop_gates_rules.py:90`–113 | `323ffce97c18460b816d2037cedd7e68d9192b165878d933433b707c241c596e` |
| S15 | `tests/workflows/test_stop_gates_rules.py:590`–650 | `74641b772e8e7a6f0a0adf599551c32261b05ee67e0d357ec94e5f157c0bd250` |
| S16 | `tests/workflows/test_turn_interrupt_stop_gates.py:22`–42 | `d710022eaeff95355cdd1e6950ea59c3e05e53c9a8cea98f14f5a7cb92aa3c5c` |
| S17 | `tests/workflows/test_rule_engine_task_helper_wiring.py:200`–250 | `6a950f8959da05c95cb4f3d29ba29f64e5160f208744cc2be50797a59818c7ef` |
| S18 | `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-turn-start.yaml:1`–34 | `6d9fbe6d45f009632884fc2cf7d78cb9b384c771a0d033b68d33caf118643a40` |
| S19 | `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml:1`–21 | `02592f81269e54ef3538844530b9de845fa8c4a4e6c3445a86ebb1ad854b0cf8` |
| S20 | `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml:1`–21 | `78fa50e94e0a0200fbb3d1dd2dd325c95b2e7b83cc9bee194198177abc4bae1a` |
| S21 | `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml:1`–21 | `2773a8d8778499b89781e027c6565cad630af35ed35d86845e90498f719f4e90` |
| S22 | `src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml:1`–21 | `39303e30b7395467192b759239993dd972feb56513bf28c8fb44ddf10bafdf1d` |
| S23 | `src/gobby/install/shared/workflows/rules/brevity/reinforce-brevity.yaml:1`–59 | `e6eb05e2fd64d2a37ffe30345cb26c3600927fbdf6505b1be39b2615d26974ed` |
| S24 | `src/gobby/install/shared/workflows/rules/restraint/require-restraint-skill.yaml:1`–70 | `4724e04995839d416efa40290a27f7ee8d0e7b02cec27a196e871aab95bf7d51` |
| S25 | `src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml:1`–15 | `4ec100000dc8599fc5cb6e753157c573996469088c627e3f1d02147d69edebda` |
| S26 | `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml:1`–40 | `9c89d30c4465ab3719c3fe2a01330efc5601ef8fb1247256fc0571f6e43f0aec` |
| S27 | `tests/workflows/test_reminder_cadence_rules.py:65`–125 | `0c52e42bb65fbbc7952a7924fe0a5282f7b397dfb41e09726986d911afa3e752` |
| S28 | `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml:1`–100 | `903df61da2655c70319f0a86dc443949d3d6a9cabcee19105bc1f4c61e794952` |
| S29 | `tests/workflows/test_block_tools_after_handoff_compact.py:545`–611 | `0adb11bd43f8c5b2b1272497d35f52d81513ff838c723c642eb7f86741c52a33` |
| S30 | `src/gobby/workflows/enforcement/blocking.py:90`–160 | `3b239325ae849f0e9f0ea2ba7af674dadb94633604d69319f52f256ed3a72df5` |
| S31 | `src/gobby/mcp_proxy/tools/sessions/_handoff.py:161`–189 | `152707f02698e7d631f06d7b77a429c9d3ac53ad089e17abc128f07ab5fc1397` |
| S32 | `src/gobby/install/shared/workflows/rules/auto-task/inject-autonomous-mode.yaml:1`–38 | `5a5e4539d35dce1c3b6b882cb4684f43547d035a3d848da4bad03c9c1d97c31b` |
| S33 | `src/gobby/install/shared/workflows/rules/auto-task/guide-task-continuation.yaml:1`–15 | `a4944ed0361d4f39944bc0d917a052e87aa10993834f80ae98ba9372a6af85bf` |
| S34 | `src/gobby/install/shared/workflows/rules/auto-task/notify-task-tree-complete.yaml:1`–16 | `baf80d53cb1e1388a733e11b100c245c453fc1d92315104d7bf63b72dd98e68f` |
| S35 | `src/gobby/install/shared/workflows/rules/context-handoff/preserve-context-on-compact.yaml:1`–55 | `a83fc0b59f6b890ef928e4a39c39fe2db385d3c6418b978a9543b4ce55285701` |
| S36 | `src/gobby/workflows/safe_evaluator.py:655`–690 | `126c9adbbae6241318f8cf4529d467f0202bf26a5c8f8beddcdd9b47f61fed1d` |
| S37 | `src/gobby/workflows/condition_helpers.py:837`–870 | `c9ef5dc7e754e7d511b6e41a7a596dca1036d0aa63ce8385ec66bb363e219f11` |
| S38 | `src/gobby/workflows/condition_helpers.py:960`–996 | `a40239c374ea3a6b79c55d9050fefee84129a9ddfdbcd5d4cc48224b58523090` |
| S39 | `src/gobby/workflows/condition_helpers.py:1`–34 | `21973514a68d1b8acdc1715b792abe699f36f205c3bbd1a01b934c673ca4a89e` |
| S40 | `src/gobby/workflows/condition_helpers.py:547`–592 | `412e19b7c9b6a75d044e86c12437640c86d3ab19515de74a74b411eb48a47535` |
| S41 | `src/gobby/workflows/engine/core.py:470`–500 | `da6894fabd90d9b6c1c3de93984234bc239bc72c956bc8b6a434cf92634fb77e` |
| S42 | `src/gobby/workflows/engine/core.py:600`–665 | `e52f90330805e4f88e1d8ef2d7bc4eb06222a8c2811c9724320aca46cc01f8bf` |
| S43 | `tests/workflows/test_safe_evaluator.py:1`–180 | `678ab10620b732dfc4a2328782a638ed5bf06857bf35d579ef3246cdcd1d43d3` |
| S44 | `tests/workflows/test_rule_engine.py:2216`–2236 | `39ac76cfbd81dd2c2ff461f82f986931e686e4e93e5d8fd0d81cf2d8c83e01eb` |
| S45 | `src/gobby/storage/tasks/_stage_utils.py:156`–225 | `c63e0e14fa3abd36932367833afc8895d8168e677355c736760bb7f04a7f6670` |
| S46 | `tests/install/test_bundled_content_manifest.py:1`–80 | `88bd2c439c7ff58d27401d4c554d8b06e471cf3afb4c112b7828d6125e929408` |
| S47 | `pre-push-test.sh:1`–180 | `f050dbba54b899ba859bfa6dfbda2c58b54045e5dbfd2212c177bc7958cde7bb` |
| S48 | `.github/workflows/ci.yml:240`–280 | `835f2b2cb51e99abfe58d26cfb13e933420a69bac2828f9058fae311f5953656` |
| S49 | `tests/ci/test_postgres_test_stack.py:480`–530 | `08f5f4ecef56445a5584eb09d6f5e93cbdcb0affeb578e43d7d7aba3507497da` |

S50 is the complete regex inventory of 15 context-handoff rule headers, requested with `^  [a-z0-9-]+:` in that directory. Request fingerprint: `5364b96bed0f02b9d7aee83a9feaa467bb37baa1359a4b4e93a4082828d6be35`. Individual file:line matches and excerpt hashes are preserved in the evidence packet. Its actual binding is `366796ff6a8cc7d5a457a84091eb15164516da76`; every matched file was byte-checked against the requested pin.

Negative evidence requests:

- N01: literal `require-handoff-pull-before-tool`, paths `src/gobby/workflows`, `src/gobby/install/shared/workflows/rules`, `tests/workflows`; fingerprint `d1a7b97408362014f907556d9c18a7cf5a1e9244fb62449c22290939f73cea52`; complete_empty, with the repository-negative warning retained.
- N02: literal `handoff_pull_pending`, paths `src/gobby/workflows`, `crates/ghook`; fingerprint `e19961e0f75623986bca92469c5af02bad1a06711939627a8144e29d0b9f5b38`; complete_empty, with the repository-negative warning retained.
- N03: regex `def (test_staged_handoff_allows_turn_end_with_claimed_task|test_session_start_rearms_close_gate_and_preserves_pull_marker|test_pending_handoff_pull_suppresses_turn_start_reminders|test_condition_helpers_without_task_manager_return_false)\b`, paths `tests/workflows/test_stop_gates_rules.py`, `tests/workflows/test_reminder_cadence_rules.py`, `tests/workflows/test_safe_evaluator.py`; fingerprint `aba1480387230fe44bd06a67894c7884bb30cf43dc7a24a86ca2b3ec09c5fec4`; complete_empty, with the repository-negative warning retained.

Proposed files absent from both pinned Git tree and working checkout:

- `src/gobby/workflows/handoff_conditions.py`.
- `src/gobby/install/shared/workflows/rules/context-handoff/require-handoff-pull-before-tool.yaml`.
- `tests/workflows/test_handoff_pull_gate.py`.
- `src/gobby/workflows/condition_helpers_shell.py`.
- `src/gobby/workflows/engine/core_overrides.py`.

The proposed tests named in 1.1.5, 1.1.6, 2.1.5 and 3.1.7 were also checked by exact scoped gcode search (N03). Source and test paths for those searches have no diff against the requested pin.

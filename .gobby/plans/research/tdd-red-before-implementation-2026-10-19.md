# Why required TDD permits implementation before a named RED

Research for #24740, Named RED-before-implementation enforcement.
Audience: the operator, lane management, and the implementation owner.

## Finding and scope

The production-write guard enforces test-file-first. It opens on an intersection
between written test paths and claimed acceptance paths, without inspecting a
run, failure, test symbol, or chronology. Skill completion opens the developer's
implementation step. Close Gate 12 acts afterward and accepts one task-level
RED-to-GREEN cycle followed by GREEN for every named test. None of these provides
each behavior criterion with an edit-time RED barrier. [S1–S8]

Existing mechanisms therefore do not enforce the requested ordering. This note
changes no source, rule, threshold, waiver, or historical credit. No tests,
replays, reconstructed REDs, or new close previews were run.

The trigger needs a scope correction: #24382, Pane detectors, has a genuine
missing RED for its named Python detector test, but the owner explicitly reports
**no Python detector production-source edit**. Rust production edits occurred;
the reviewer established original RED for all four disputed Rust tests. The
Python finding proves a named-test evidence gap, not un-RED Rust implementation
or an un-RED Python detector production edit. The task description's stronger
claim is unsupported by the records read. [E1–E4]

## Enforcement inventory

Source paths below are repository-relative. `gcode evidence` 1.9.44 read complete,
warning-free excerpts at HEAD `26fdbde183ea69312ea88213f3e397d488bb3a6f`, tree
`401f2a68339d1eaff520645d5c8a96a583584ba4`, in this worktree.

| Point | Source and exact condition | Armed for a TDD-required leaf? | Contract |
| --- | --- | --- | --- |
| Requirement detection | `src/gobby/tasks/tdd_evidence.py:116–133` [S1] | Yes: flag, `tdd:required`, TDD extra skill, or recognized criteria wording. | Applicability only. |
| Claim projection | `src/gobby/workflows/claimed_task_extra_skills.py:37–88` [S2] | Yes: calls the detector, sets `claimed_task_requires_tdd`, adds the skill. | Extracts test paths, without criterion/node RED authorization; aggregates claims. |
| Before-write block | `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-block.yaml:9–19` [S3] | Yes when the requirement flag, canonical write, production path, and closed helper all match. | Blocks until a qualifying test file is written. |
| After-write tracking | Sibling `enforce-tdd-track-tests.yaml:9–17` [S4] | Yes for a successful canonical test write under the requirement flag. | Appends the first test path to `tdd_tests_written`; checks no run. |
| Opening predicate | `src/gobby/workflows/condition_helpers.py:124–141` [S5] | Consulted by the block rule. | `141\| return bool(written & acceptance_paths) if acceptance_paths else bool(written)`. Any qualifying path opens the gate. |
| Developer transition | `src/gobby/install/shared/workflows/agents/developer.yaml:439–469` [S6] | Skill check applies when extras exist. | `442\| - to: implement` requires empty extras or all skills loaded; `469\| allowed_tools: "all"`. No RED prerequisite. |
| Close Gate 12 | `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:745–774` [S7] | Yes: detects TDD from task metadata and calls the evaluator. | Blocks insufficient evidence at close, after edits; deliberate-close branch at 766. |
| Close evaluator | `src/gobby/tasks/tdd_evidence.py:140–174,275–375` [S8] | Called for the named tests by Gate 12. | Breaks on the first cycle at 304–309, then checks every named test for GREEN at 345–361. It does not require every named test's RED. |

Installed DB rows were independently read before handoff: enabled block rule
`70e748ea-0695-4032-92d9-3ed65dbc8952` and enabled track rule
`8938b7df-dcd6-423d-8fc0-2549fe37ff01` matched these conditions. This establishes
observed installed state, not the incident's historical variable values.

Source ledger: SHA-256 `excerpt_hash`, with bounds matching the complete read.

| Ref | Excerpt | Hash |
| --- | --- | --- |
| S1 | `tdd_evidence.py:116–174` | `d55fd65d7465dc722362437a63ba03ce225eb3320c8e2f80797488a70d473219` |
| S2 | `claimed_task_extra_skills.py:37–88` | `d95ea3a5a063d0cbf4dac32049ef1fe98b448dcef25fc51721c96a2c926ae44c` |
| S3 | `enforce-tdd-block.yaml:1–19` | `b9cbec9c21785cce7df4d06018b8b991b77a2a5ca020b07e285bd0e188e0f4eb` |
| S4 | `enforce-tdd-track-tests.yaml:1–17` | `ef7211b440b6b67e2105a2767d606a610f98db90600f93c3d4cf42ee8bb2a63e` |
| S5 | `condition_helpers.py:85–141` | `071077a5f503539579d1e618d33447b64274ac09510e24de0907fa8773a9528c` |
| S6 | `developer.yaml:399–490` | `bfe239ebfca2c55121549183b1a3a9bb0519cfb2bbb4e30f30bb68b8c8e6c9d4` |
| S7 | `_lifecycle_close.py:738–778` | `e09e302fe1d11b961d344c387f6594ce47f1b4e5745968cf773a4e4e7e0dac07` |
| S8 | `tdd_evidence.py:275–375` | `e9a814c449e4bf9fa9851e3ed263fff1c4cad0339c88e06e4e229cfd3b9b3429` |

## Pane detectors chronology

Owner session `gobby#16870`, UUID `ad359f2b-9ab6-4f96-a72c-970d0d769b05`, supplied
E1. Times are 2026-10-18 CDT (UTC−05:00). Line numbers are the owner's original
transcript locators, not this checkout's source lines.

| Time | Retained event | Meaning |
| --- | --- | --- |
| 13:10:36 | Add `test_record_mode_still_asserts`, edit matcher record branch; line 29268. | Test file written. |
| 13:11:05 | Restore the matcher branch's bare return; line 29328. | Defect state before RED. |
| 13:24:04 | `DATABASE_URL=... GOBBY_TEST_PROTECT=1 rtk uv run pytest tests/agents/test_pane_detection_corpus.py -q`; line 30021; recall `d86b5c1715e7`. `test_record_mode_still_asserts` fails with `DID NOT RAISE <class 'AssertionError'>`; 1 failed, 1 passed; raw exit 1. | Matcher-check RED; detector test did not yet exist. |
| 13:25:39 / 13:27:36 | Remove return, then 2 passed, raw exit 0; lines 30059 / 30177. | Matcher-check GREEN. |
| 13:43:04 | First write of `test_detector_cases_match_python`; line 30999. | Different named test in the same acceptance file. |
| 13:55:03 | First execution: `GOBBY_RECORD_PANE_DETECTION_CORPUS=1 pytest tests/agents/test_pane_detection_corpus.py -q`; line 31107; 3 passed in 13.91s, `RAW_EXIT=0`. | Detector expectations written and asserted in record mode; no detector RED. |

S5's path intersection explains the missing barrier: writing this acceptance
file suffices, independently of the earlier matcher RED. It cannot distinguish
the two symbols. S6 permits implementation tooling after skills load. This is a
sufficient structural explanation, not a claim to have recovered the actual
evaluated guard variables during the incident.

E2 preserves the owner's 18:37 UTC handoff, which explicitly planned a Python
detector record run and separately planned Rust stubs, RED, implementation, and
GREEN. The original preview on `9dc437375339` reported the Python test's missing
assertion/panic between test edit and implementation, with `red_runs=[]`,
`green_runs=[]`, `regression_tests=[]`. E3 classifies that finding as genuine
gap (c). Gate 12 caught the retained gap at close, not before edits. The read task
card remained open; approved override authority does not prove a close executed.

E4 establishes Rust RED at 2026-10-19 00:18:38Z before production edits
00:30:58–00:48:57Z. Its later disputed aggregate GREEN is an unread form (b).
Those four findings are not four additional missing-RED cases.
The exact Rust nodes in `crates/gdaemon/tests/pane_detection_corpus.rs` are
`prompt_detector_matches_python`, `idle_detector_matches_python`,
`stall_and_attention_match_python`, and `loop_and_quota_match_python`. The
historical bare RTK `8 passed` text remains insufficient under the accepted
contract requiring the original complete nextest final zero-failure/zero-skip
summary and matching selection/inventory; class (b) supplies no automatic credit.

Evidence read:

- **E1:** Owner packet `f9849f4e-1372-4f1f-8c43-8326c05871d0`, sent 06:16:26Z;
  stored sent-history result `aabeef20-0e72-4352-9c77-cbc080cc8e61`, including
  complete packet at offset 11472. Contains chronology, commands, outputs,
  preview finding, and explicit absence of Python detector source edits.
- **E2:** `get_session_messages(session_id="gobby#16870",offset=727,limit=4,
  full_content=true)` on gobby-sessions; result
  `3a2d1238-cfab-40d7-9ebe-3b57e30b903d`, matching chunk containing the original
  `get_handoff` result. This is planning evidence, not execution credit.
- **E3:** Reviewer `gobby#17118` verdict `ea9195a5-e75c-4414-854f-d056ec9f6fae`
  to the Orchestrator; direct confirmation
  `276d04dd-8fad-4416-8715-acd3332e2933` at 06:36:58Z. Duplicate reports are
  the same finding.
- **E4:** Direct reviewer Rust class (b) verdict delivered after this handoff,
  citing original packets `4583eedc` / `5bb9c2c6` and recall `076a81683485`.
  Follow-up reports accepted classification under specs `45079ea0` / `9d96dc6b`;
  historical aggregate-GREEN eligibility remains unproved.

## Other incidents and bounded count

Reviewer classifications distinguish gate defect (a), unread legitimate form (b),
and genuine evidence gap (c). A defect classification alone grants no historical
run credit.

| Incident | Classification and boundary | Reviewer evidence |
| --- | --- | --- |
| #24270, Node routes | Shell-restore form is (b). Restricted composed restore/zero-line comparison requires upstream diff success, matching final token/path, unique reachable commit. Historical credit remains gate-decided after correction; five detail findings still open. No final whole-task genuine-gap verdict. | E3; latest direct update following research message `287f06ab-d05d-44a5-96dd-e2dc91dc8b97`; spec `070f68c7`, addenda `0d14e6d3` / `d810c06b`. |
| #24595, Caller ownership | Job 15001's six PASSED rows are non-defect and no RED. Separate job 11609 awaits original full sections. Unresolved, not confirmed (c). | E3 and latest update; packet `1cb3a874`, owner `gobby#16717`. |
| #23575, Vector completion | (a): ephemeral original-output authentication has no durable same-job fallback after deletion. Original file/root absent. Later matching-copy hash cannot establish original cutoff/ctime or close credit. Original pre-cutoff full deliveries/first implementation record still needed. | Direct message `aef12d6e-8c0f-4147-9d09-4877d2cc3e21`, 06:44:07Z, superseding pending diagnosis in E3; original packet `496456c7`. |
| #24589, Background-output family | Accepted correction `adbf28c949`, landed `b779ef0447`: gate correction, not confirmed author RED gap. Vector persistence/Rust summary corrections route here; routing grants no historical credit. | E3; approval `741b62d0`, retest `72da2a40`; vector/Rust verdicts above. |

**Confirmed count: one genuine named-RED gap, the Python Pane detectors finding;
zero additional confirmed genuine-gap families among the four other requested
families.** This is a lower bound, not a final zero-incidence rate: Node routes
and Caller ownership have unresolved findings. Duplicate reports, four Rust
findings, and unresolved evidence do not increase the genuine-gap count. This
selected sample supports no prevalence estimate.

## One implementation-task specification

**Proposed title:** Require retained named RED before production writes for every
TDD behavior criterion. This is a proposed task contract, not implementation
approval or an expanded multi-deliverable plan.

**Rule:** Use a conservative leaf-wide barrier: resolve every named test for
every behavior criterion and require each test's authenticated retained RED
before any behavior-bearing production write. An agent-selected active criterion
cannot safely authorize writes without trusted criterion-to-code mapping. The
leaf-wide rule avoids introducing that separate mapping system. It deliberately
requires behavior criteria's REDs before implementation instead of the current
write-one-file-and-retry flow.

All named criteria default to behavior obligations. GREEN-only coverage is
allowed only for stored criteria explicitly marked `regression-only:` and proven
unchanged against the claim baseline; editing the test makes it RED-required.
Missing tests, ambiguous nodes, unauthenticated output, or uncertain regression
status keep production blocked. First-GREEN corpus-generation criteria need
accurate owner-authored criteria, not fabricated RED credit.

Exact targets and changes, within one implementation task:

1. `src/gobby/workflows/claimed_task_extra_skills.py`: retain task/criterion
   identity, full `path::symbol` references, and explicit regression designation.
   Keep claims separate so another task's file write or run cannot open this gate.
2. New `src/gobby/tasks/tdd_write_authorization.py`: derive authorization from
   original transcript evidence, reusing the named-RED and source-window helpers
   in `src/gobby/tasks/tdd_evidence.py`. Extract shared proof primitives as needed
   to keep production files below 1,000 lines. Each required exact node needs its
   own completed assertion/panic-backed failure after its relevant test edit and
   before the proposed implementation event. Compile/collection/infrastructure
   failures, another node's failure, first GREEN, or bare aggregate counts fail.
3. `src/gobby/workflows/condition_helpers.py::tdd_gate_open` and
   `src/gobby/workflows/safe_evaluator.py::build_condition_helpers`: use this
   server-derived authorization; keep `tdd_tests_written` for observability.
   Agent variable writes must not forge run proof. Bind any receipt/cache to
   task, claim continuity, checkout, criteria digest, test/source identity, run
   identity, and event order. Invalidate affected proof on test/criteria changes
   or incompatible reclaim; preserve compatible originals through compaction.
4. Both TDD rule YAML files: make the before-write rule report missing exact-node
   RED and guard all canonical production paths, including shell/mixed patches.
   Describe after-write tracking as observability. Permit test-only writes and
   mechanically verified behavior-free API stubs needed to compile RED; mixed
   real implementation stays blocked. Preserve Rust inline-test/stub proof rules.
5. `src/gobby/install/shared/workflows/agents/developer.yaml` and
   `src/gobby/install/shared/skills/test-driven-development/SKILL.md`: explain the
   leaf-wide barrier. Skill completion still permits preparing tests/stubs; the
   write guard enforces RED. Replace write-one-acceptance-file guidance.
6. `src/gobby/tasks/tdd_evidence.py::evaluate_tdd_evidence`: check the same
   per-obligation chronology at close, replacing the first-cycle short circuit;
   retain later GREEN for every named test and unchanged explicit regressions.
   Preserve waiver authority/delivery gates. Output-authentication corrections
   remain with the Background-output family rather than weakening this check.

Named new tests below are proposed acceptance targets, not existing-test claims:

| File | Required tests |
| --- | --- |
| `tests/workflows/test_tdd_enforcement_rules.py` | `test_acceptance_file_write_without_named_red_stays_blocked`; `test_same_file_other_test_red_does_not_open_gate`; `test_first_green_detector_record_run_stays_blocked`; `test_all_behavior_reds_open_production_gate`; `test_verified_stub_only_write_can_prepare_red`; `test_stub_plus_implementation_patch_is_blocked`; `test_shell_and_structured_write_share_named_red_barrier`. |
| New `tests/tasks/test_tdd_write_authorization.py` | `test_unwritten_named_behavior_test_blocks_other_criterion`; `test_foreign_claim_red_cannot_authorize_write`; `test_test_edit_invalidates_named_red`; `test_compaction_preserves_authenticated_original_red`; `test_compile_failure_cannot_authorize_write`; `test_unchanged_explicit_regression_needs_only_green`. |
| `tests/tasks/test_tdd_cycle_continuity.py` | `test_one_cycle_cannot_cover_second_behavior_criterion`; `test_close_and_write_authorization_use_same_named_red`; `test_changed_regression_loses_green_only_status`. |

Validation: fixture records reproduce the two-Python-test chronology without
replaying historical runs. Prove one honest cycle plus another behavior test's
first GREEN still blocks writes/close despite all final tests passing. Retain
canonical-path, worktree, Rust inline-test, mixed-patch, original-source binding,
and claim-continuity regressions. Run only affected focused files using the
isolated test hub and an RM slot for hub-backed work. Read installed enabled
rules after authorized activation. No full suite, historical replay, new waiver,
or threshold change belongs to the fix. This research commits only this note.

_Last verified: 2026-10-19_

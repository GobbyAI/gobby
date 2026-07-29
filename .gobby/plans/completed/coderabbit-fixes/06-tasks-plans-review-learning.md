# CodeRabbit Fixes: Tasks, Plans, and Review Learning

Task validation/closure, plan evidence, review learning, and associated storage fixes.

Unresolved original findings: **59**

Original finding IDs: 331-332, 361-363, 382-383, 417-418, 420-422, 499, 524-544, 548-551, 559-568, 579-580, 705, 724, 765-766, 768, 771-773, 784

## Finding #331

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py around lines 359 - 368, Update the receipt_packet.error branch in close_task to make the message actionable: state how to remediate the budget overflow, such as assigning or unassigning receipts, splitting the task, or retrying after adjustment, and include offending receipt IDs from disclosure when available. Preserve the existing failure response and evidence_completeness payload.

## Finding #332

In @src/gobby/mcp_proxy/tools/tasks/_verification_receipts.py around lines 95 - 122, Update assign_verification_receipts to catch TaskNotFoundError alongside ValueError around resolve_task_id_for_mcp and the related task lookup, returning the existing structured failure response with the exception message. Preserve the current success response and project-scope validation behavior.

## Finding #361

In @src/gobby/tasks/verification_receipt_packet.py around lines 121 - 128, The _aggregate function labels priority-ordered receipt endpoints as an ID range. Rename receipt_id_range to sample_receipt_ids and preserve the existing first/last values, or explicitly sort receipts by ID before deriving true bounds; ensure consumers no longer interpret the values as an inclusive interval.

## Finding #362

In @src/gobby/tasks/verification_receipt_packet.py around lines 204 - 241, The packet-building loops should avoid repeatedly rendering the entire payload and rebuilding the full tail for each candidate. Update the detail and catalog selection logic around _render to maintain incremental serialized-size accounting for each added entry, using the running total to enforce budget_chars while preserving _DETAIL_LIMIT, mandatory catalog entries, and existing ordering/outcome behavior.

## Finding #363

In @src/gobby/tasks/verification_receipt_packet.py around lines 40 - 59, Update _priority to remove the redundant leading explicit-ID tuple element and return only group, the negated timestamp, and receipt.id; preserve the existing group assignment and remaining tie-break ordering.

## Finding #382

In @tests/tasks/test_verification_outcome_projection.py around lines 53 - 91, Add a focused test alongside test_projection_requires_a_durable_success that supplies receipts ordered with a successful outcome followed by a failure, then assert project_verification_outcomes reports ready as True and preserves the expected outcome counts and latest receipt identity. This should pin the intended durable-success behavior when the newest receipt is failing.

## Finding #383

In @tests/tasks/test_verification_receipt_packet.py around lines 1 - 12, Add the module-level pytest marker `pytestmark = pytest.mark.unit` in tests/tasks/test_verification_receipt_packet.py, alongside the existing imports, so all tests in the module are classified as unit tests.

## Finding #417

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py around lines 161 - 165, Update the ValueError handler in the task commit-linking flow to set error to the stable code invalid_commit_sha, while retaining the exception text in message for diagnostics. Keep the existing success=false response and surrounding commit tracking unchanged.

## Finding #418

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py around lines 106 - 125, Update the exception handler around resolve_task_tagged_commits in the close-task lifecycle flow to log the caught exception with useful context before returning the existing failure response. Add and use a module-level logger for this diagnostic, while preserving the current error payload and control flow.

## Finding #420

In @src/gobby/tasks/commits.py around lines 539 - 575, Extract the shared git-log command construction and sha|message parsing from resolve_task_tagged_commits and auto_link_commits into a reusable helper, then have both flows call it. Preserve each function’s existing branch, since, working directory, and task-ID filtering behavior while ensuring commit extraction is maintained in only one implementation.

## Finding #421

In @src/gobby/tasks/commits.py around lines 603 - 614, Avoid the duplicate and uncaught lookup in the task-linking flow: update _resolve_task_filter to return the already-fetched task object alongside its existing results, then use that object for seq_num and commits instead of calling task_manager.get_task again. Preserve the existing empty AutoLinkResult behavior when resolution fails.

## Finding #422

In @src/gobby/tasks/commits.py around lines 615 - 636, Update the single-task commit-processing flow around resolve_task_tagged_commits so unresolved commit references are detected and recorded in result.skipped_refs before applying the task_id filter. Preserve the existing duplicate, link-error, linked-task, and total-count behavior for resolved commits.

## Finding #499

In @tests/storage/tasks/test_sweep_stale_claims.py around lines 70 - 75, Annotate the pytest fixtures in all affected test functions, including test_sweep_reclaims_task_claimed_by_terminal_session and the functions around the other cited locations. Add the appropriate existing types for temp_db and sample_project while preserving the current test behavior and parametrization.

## Finding #524

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py around lines 409 - 435, Update_recall_validation_lessons to bound recall_review_lessons_by_class with the established async timeout mechanism, and catch all ordinary recall/provider failures so advisory enrichment cannot abort close-task validation. Preserve cancellation propagation where required, and return an empty message plus the existing lesson-recall-failed diagnostic containing the exception detail for handled failures.

## Finding #525

In @src/gobby/mcp_proxy/tools/tasks/_plan_review_approval.py around lines 28 - 31, Move the PlanReviewEvidenceService construction and get_evidence call inside the replay branch in the approval flow. Keep plan_review_mint_result(evidence) unchanged for replay requests, while allowing non-replay approvals with a recorder to skip the unnecessary database fetch.

## Finding #526

In @src/gobby/mcp_proxy/tools/tasks/_plan_review_approval.py around lines 62 - 72, Update _checkpoint_failure to guard the checkpoint_plan_review_lesson_mint call against ReviewEvidenceError and psycopg.Error. If recording the failure checkpoint raises, return a degraded plan_review_mint_result-compatible result instead of propagating the exception, preserving the fail-open behavior of complete_plan_review_mint after approval commits.

## Finding #527

In @src/gobby/mcp_proxy/tools/tasks/_plan_review_backfill.py around lines 19 - 40, Update backfill_plan_review_lessons to catch errors from resolve_task_id_for_mcp before mint_plan_review_lessons runs, converting unknown task IDs and invalid formats into the same structured error dict used by the existing ReviewEvidenceError handling. Preserve the current unavailable-service response and successful lesson backfill flow.

## Finding #528

In @src/gobby/mcp_proxy/tools/tasks/_stage_review.py around lines 372 - 388, Move the planning-stage evidence_id validation before ctx.task_manager.approve_review(...) mutates state, and replace the RuntimeError in the planning branch with the handler’s existing structured error response format. Keep complete_plan_review_mint execution unchanged for valid planning approvals, and update the schema validation around the evidence_id definition if needed to preserve the planning-specific requirement.

## Finding #529

In @src/gobby/plans/manifest_emitter.py around lines 139 - 168, In the manifest synthesis logic around the category routing branch, remove tdd from the earlier tuple assignment so entry["tdd"] is assigned only by the final decision.get expression. Validate non-null assigned_agent and implementation_domain override values as strings before storing them in entry, rejecting invalid types consistently with the existing ManifestSynthesisError handling.

## Finding #530

In @src/gobby/plans/manifest_emitter.py around lines 127 - 129, Validate reviewed routing overrides before copying them in the manifest emission flow around the loop over task_type, depends_on, and tdd: normalize depends_on through the same dependency validation used by _synthesized_dependencies, rejecting unknown, empty, self-referential, and non-list values while preserving valid existing behavior. Also validate task_type against its expected type before assigning it, and only write validated override fields into the entry.

## Finding #531

In @src/gobby/plans/review_coverage.py around lines 516 - 530, Update the exception handling around path resolution in the review evidence validation flow to catch FileNotFoundError separately as retryable source_drift, while reporting other OSError cases such as permission or directory errors as non-retryable evidence failures with appropriate IO-error details. Remove the redundant FileNotFoundError entry from the OSError tuple and preserve the existing invalid_source_path handling for ValueError.

## Finding #532

In @src/gobby/plans/review_coverage.py around lines 569 - 576, Update_required_string to accept an explicit error-code argument in addition to the human-readable owner, and use that argument when constructing ReviewEvidenceError. Update every call site to pass the fixed codes already used by this module, including invalid_lane_results, invalid_candidate, and invalid_dispositions, while retaining owner for the validation message.

## Finding #533

In @src/gobby/plans/review_coverage.py around lines 41 - 55, Define each review-complexity threshold once and reuse those named values in both the complex_review decision and the returned thresholds payload. Update the surrounding review coverage logic without changing the existing threshold values or routing behavior.

## Finding #534

In @src/gobby/plans/review_evidence.py around lines 661 - 708, Keep the per-plan mutation lock from before reading and verifying current_bytes through rendering, writing, and complete_manifest_apply, using the existing transaction_immediate(mutation) flow in the manifest-apply method. Move the verify/render/atomic_write_bytes sequence and completion update into the same locked transaction, while preserving the existing pending, revoked, applied, and payload-conflict checks.

## Finding #535

In @src/gobby/plans/review_evidence.py around lines 551 - 563, Update the interactive evidence validation around parse_checkpoints and render_v1_round_checkpoint to read plan_path.read_bytes() once, store the bytes, and reuse them for both checkpoint parsing and expected-checkpoint validation; preserve the existing missing_v1_checkpoint error behavior.

## Finding #536

In @src/gobby/plans/review_evidence_io.py around lines 319 - 337, The _section_span function currently selects the first matching heading when duplicate manifest keys exist. Track matches for wanted_key while scanning headings, reject the section with the existing ReviewEvidenceError mechanism if more than one match is found, and only return a span when exactly one matching heading exists.

## Finding #537

In @src/gobby/plans/review_evidence_io.py around lines 243 - 268, Refactor render_manifest_plan so the _section_span(text, "M1") lookup and missing-section fallback are handled explicitly before validating the suffix. Keep the invalid_manifest error for non-final M1 sections, but remove the broad try/except around that validation so it cannot be re-raised conditionally by error code; preserve the existing body, suffix, rendering, and parsing behavior.

## Finding #538

In @src/gobby/plans/review_evidence_io.py around lines 291 - 316, Update _parse_rendered_plan to create its temporary plan file inside a TemporaryDirectory, following the existing_snapshot_document pattern, instead of using NamedTemporaryFile in plan_path.parent. Keep the parse_plan calls and exception behavior unchanged, and remove the manual temp_path cleanup since the temporary directory should manage lifecycle cleanup.

## Finding #539

In @src/gobby/plans/review_findings.py around lines 84 - 114, Update_validate_finding to reject adversary-supplied description, fix, and prevention values containing Markdown code-fence markers or leading “#” before they are rendered by the findings formatter. Preserve the existing non-empty validation and use the established_invalid validation error path.

## Finding #540

In @src/gobby/review_learning/class_recall.py around lines 154 - 165, Update recall_review_lessons_by_class to validate limit before converting it, catching None, non-numeric, and otherwise invalid values and raising a clear ValueError consistent with the existing argument validation. Preserve the bounded range of 1 through 5 for valid numeric limits.

## Finding #541

In @src/gobby/review_learning/lessons.py around lines 213 - 236, Update recorders._lesson_finding to canonicalize the plan-review category with slugify(category) when constructing pattern_id, matching _validate_class_scoped_identity’s expected format; preserve the existing lesson_type and check_key components.

## Finding #542

In @src/gobby/review_learning/recorders.py around lines 39 - 60, Update mint_plan_review_lessons to run the synchronous database operations store.list_for_task_stage, get_task, and review_learning_service.checkpoint_plan_review_lesson_mint via asyncio.to_thread, following the existing class_recall.py pattern, while preserving their current arguments and result handling.

## Finding #543

In @src/gobby/review_learning/round_diff.py around lines 79 - 93, Replace the identity-based deduplication in the selection flow with index-based tracking derived from the positions of candidates chosen from ranked. Use those indices when extending selected so candidates are excluded by position rather than id(candidate), while preserving the existing ordering and capped_limit behavior.

## Finding #544

In @src/gobby/review_learning/round_diff.py around lines 121 - 136, The _validated_findings function currently suppresses all malformed round-result failures without any diagnostic signal. Add a debug or warning log for invalid payloads, non-list findings, non-mapping entries, and validation exceptions, including row.evidence_id and the relevant exception; preserve the existing None return behavior after logging.

## Finding #548

In @src/gobby/storage/tasks/_transitions.py around lines 572 - 601, Move the apply_plan_review_manifest call into the db.transaction_immediate(StageReviewApprovalMutation(...)) block so it executes under the task’s serialization lock before authorize_current_attempt and replay checks. Preserve the existing arguments and manifest validation, ensuring concurrent approvals cannot both apply the same plan review manifest.

## Finding #549

In @src/gobby/storage/tasks/_transitions.py around lines 576 - 577, Remove the `or ""` fallback from the `run_id` argument in the manifest apply call, passing `dispatch_run_id` directly. Preserve the existing `authorize_current_attempt` validation and let its non-None contract enforce the required run ID.

## Finding #550

In @src/gobby/storage/tasks/_transitions.py around lines 864 - 877, Reuse the existing_replace_round_section helper in reject_review instead of maintaining a separate inline round-heading replacement, passing the current description, round_number, and replacement section so both paths remain consistent. Leave the escaped integer-derived regex construction in _replace_round_section unchanged.

## Finding #551

In @src/gobby/storage/tasks/_transitions_facade.py around lines 301 - 306, Update the affected transition method signature so round_number, findings, manifest_entries, routing_decisions, coverage_attestation, and evidence_id are declared after the * marker as keyword-only parameters. Preserve their existing defaults and types, and keep the existing keyword-based pass-through call sites unchanged.

## Finding #559

In @tests/plans/test_review_evidence.py around lines 481 - 710, Split test_manifest_compare_and_apply into focused scenario-scoped tests covering canonical/tampered validation, pre-write crash recovery, idempotent re-application and payload changes, post-write checkpoint recovery, and drift revocation. Reuse the existing canonical_approval setup/helper and isolate each scenario with its own fixture state so failures identify the affected behavior without masking later coverage.

## Finding #560

In @tests/plans/test_review_evidence.py around lines 380 - 384, Update the migration execution setup around temp_db.execute and migration so it does not split SQL with raw split(";"). Run the migration as one script or use the project’s SQL-aware statement splitter, preserving execution of all migration statements including quoted semicolons and function bodies before validating catalog().

## Finding #561

In @tests/plans/test_review_evidence.py around lines 26 - 30, Add the pytest integration marker to the test module containing review_setup so tests in tests/plans/test_review_evidence.py are collected by -m integration. Apply the marker at module scope rather than only to the review_setup fixture.

## Finding #562

In @tests/review_coverage_helpers.py around lines 12 - 16, Extract the shared canonical JSON-plus-SHA-256 digest logic into a helper in gobby.plans, then update production functions manifest_digest and attestation_digest in gobby.plans.review_evidence and the test helper manifest_digest to delegate to it. Preserve the existing canonicalization options and digest outputs while eliminating duplicated hashing logic.

## Finding #563

In @tests/review_learning/test_feedback_loop_e2e.py around lines 125 - 127, Update the plan-review assertions around _class_finding to verify that record() does not mutate the caller-provided finding dictionary: deep-copy the finding before the record call, then compare the original input with that snapshot after recording. Keep separate assertions for the recorded payload’s rule_id and absence of path only if those validate the persisted output.

## Finding #564

In @tests/review_learning/test_feedback_loop_e2e.py around lines 169 - 178, Remove the self-asserting checks in the end-to-end test around the candidate serialization and validation-finding literals: delete the equality assertion comparing objects derived from the same candidate bytes and the assertions rechecking locally defined prevention, root_cause, path, and symbol values. Preserve the meaningful recorded-result assertions for pattern_id, finding_fingerprint, and differing occurrence_key.

## Finding #565

In @tests/review_learning/test_lessons.py around lines 82 - 86, Replace the Any annotations on fake_memory_manager and fake_task_manager in test_domain_and_check_key_tags and the additionally affected test with the concrete FakeMemoryManager and FakeTaskManager fixture types imported from tests/review_learning/conftest.py. Preserve the existing fixture behavior and test logic.

## Finding #566

In @tests/review_learning/test_recall_context.py at line 183, Annotate the fake_task_manager parameter in test_code_domain_excludes_plan_lessons with the FakeTaskManager type, preserving the test’s existing behavior.

## Finding #567

In @tests/review_learning/test_retirement.py around lines 24 - 27, Add the module-level pytest marker declaration to tests/review_learning/test_retirement.py, using pytest.mark.unit consistently with sibling tests so the module is included in unit-marker selection. Ensure the required pytest import is present and leave the existing test constants unchanged.

## Finding #568

In @tests/review_learning/test_round_diff.py around lines 25 - 31, Add pytest markers in the test module so pure classification tests are marked unit and DB-backed round/approval tests are marked integration and/or slow, following the repository’s existing marker conventions. Apply the markers to the relevant test functions or classes without changing their behavior.

## Finding #579

In @tests/tasks/test_validation_issues.py around lines 257 - 271, Update test_validation_prompt_structured_issue_contract to construct the validation prompt path from Path(__file__).resolve().parents[2] instead of the current working directory, and pass encoding="utf-8" to read_text().

## Finding #580

In @tests/tasks/test_validator_lesson_injection.py at line 36, Update the module-level_PROMPT_PATH in test_validator_lesson_injection.py to resolve from __file__ rather than the current working directory, matching the path-resolution approach used by test_validation_issues.py while preserving the existing prompt target.

## Finding #705

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py at line 84, Update the response_detail default in the lifecycle-close task transition flow so preview=true retains diagnostic fields including mechanical_gates, selected_evidence, evidence_completeness, and unassigned_receipts. Either default preview requests to diagnostic or update every preview caller to pass response_detail="diagnostic", ensuring no preview path silently uses concise output.

## Finding #724

In @tests/tasks/test_validation.py around lines 2001 - 2006, Update the async test method test_validate_with_validation_criteria_only by adding type annotations for config and mock_llm and an explicit return type, using the appropriate existing project types and the established async test annotation convention.

## Finding #765

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py at line 361, Replace the runtime asserts in the lifecycle-close tool, including the checks for resolved_session_id, receipt_packet, admission, and evidence, with explicit validation that returns blocked(...) or raises RuntimeError before validator calls. Preserve the existing success flow while ensuring missing values produce structured errors even when Python runs with -O.

## Finding #766

In @src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py at line 337, Remove the redundant should_skip alias in the close flow and use skip_leaf_checks directly in the guards around the leaf validation logic. Simplify the final status expression to use validation_status or the skipped/valid result based on skip_leaf_checks, preserving the existing behavior.

## Finding #768

In @src/gobby/storage/tasks/_manager.py around lines 355 - 364, Update the task update flow around require_validation_criteria and_update_task_metadata so the validator’s normalized return value is assigned to the effective validation criteria and persisted. Preserve the existing UNSET handling and task-type validation behavior, ensuring whitespace-padded criteria are stored in normalized form.

## Finding #771

In @src/gobby/tasks/criteria_contract.py around lines 33 - 67, Update split_validation_criteria so that once a list marker is detected, free-text accumulated before the first marker is discarded rather than appended as a criterion. Preserve the existing handling of list items, continuation lines, blank lines, and non-list prose when no marker is present.

## Finding #772

In @src/gobby/tasks/evidence_admission.py around lines 61 - 71, Update_criteria_accept_actor_attestation to avoid accepting criteria solely through naive substring matches, especially when matched phrases are negated. Prefer the existing criteria contract’s explicit structured flag or tag for actor-attestation/manual-review acceptance; if unavailable, add proximity-aware checks that reject phrases preceded by negation terms such as “not,” “cannot,” or “insufficient,” while preserving acceptance for clearly affirmative criteria.

## Finding #773

In @src/gobby/tasks/task_state_evidence.py at line 17, The excerpt, digest, and length logic in task-state evidence duplicates verification_receipts._bounded_output. Extract or reuse a shared helper, preferably through a common storage output-bounds module or a public bounded-output symbol, then update both call sites to use it while preserving the existing truncation, digest, and length behavior.

## Finding #784

In @tests/tasks/contract_validator.py around lines 51 - 56, Update validate_task to detect and handle validation_criteria supplied positionally before injecting kwargs["validation_criteria"], preserving the caller-provided value and preventing duplicate argument errors when delegating to super().validate_task. Keep the existing default criterion behavior for calls that provide neither positional nor keyword criteria, and continue synchronizing self._contract_llm.criteria.

## Forward-Port Disposition Ledger

- Source commit: `7daf01cf5552fc65d2e07ffaca06016ac7db6fa5`
- Original task: `#19004`
- Forward-port base: `bf1b1bd2184adf62e5674fd7eaf6737cef6e87bd`
- Integration task: `#19170`

| Finding | Disposition | Evidence |
| --- | --- | --- |
| #331 | Obsolete | The verification-receipt service was removed by the checklist-close cutover, so the cited empty-capture branch no longer exists. |
| #332 | Obsolete | Receipt-output sanitization was removed with the verification-receipt subsystem. |
| #361 | Obsolete | The cited verification-receipt test module was deleted with that subsystem. |
| #362 | Obsolete | The cited verification-receipt evidence builder test no longer exists. |
| #363 | Obsolete | Validator lesson-injection coverage was removed with the superseded validation path. |
| #382 | Obsolete | Close-time verification-receipt tests were removed by the checklist gate refactor. |
| #383 | Obsolete | Receipt cleanup is no longer part of task close behavior. |
| #417 | Obsolete | `_lifecycle_close_preview.py` was deleted by the checklist-close refactor. |
| #418 | Obsolete | The cited lifecycle close-preview output-digest helper no longer exists. |
| #420 | Carried | One tagged-history parser now owns Git log construction and task-reference extraction for both auto-link flows. |
| #421 | Carried | Task-specific auto-linking resolves the task once and reuses the resolved object. |
| #422 | Carried | Task-specific auto-linking records unresolved tagged references before filtering commits for the requested task. |
| #499 | Carried | Stale-claim sweep fixtures and helpers use concrete `HubDatabase`, `Task`, and mapping annotations. |
| #524 | Obsolete | Lifecycle close-time recall was removed by the checklist-close cutover. |
| #525 | Carried | Non-replay approval recording no longer instantiates or queries the evidence service. |
| #526 | Adapted | The current asynchronous checkpoint path preserves the structured mint-result contract for review-evidence and PostgreSQL persistence failures. |
| #527 | Carried | Backfill task-reference failures return a structured `invalid_task_id` error. |
| #528 | Carried | Planning approval rejects a missing evidence ID before mutating stage state. |
| #529 | Carried | Reviewed routing overrides validate supported task type, dependency, TDD, agent, and implementation-domain values. |
| #530 | Carried | Manifest TDD selection is assigned once after validated override precedence is resolved. |
| #531 | Carried | Coverage source reads distinguish retryable disappearance from non-retryable I/O failures. |
| #532 | Adapted | Explicit stable error codes were merged into the current coverage schema and its newer ledger and requirement validation. |
| #533 | Carried | Review-complexity thresholds are named constants. |
| #534 | Adapted | The current manifest service now performs re-fetch, verification, render, intent, write, and completion within one mutation transaction. |
| #535 | Already satisfied | The current finalization service already reads plan bytes once for checkpoint parsing and comparison. |
| #536 | Carried | Manifest section lookup rejects duplicate M1 keys explicitly. |
| #537 | Carried | Manifest rendering handles a missing M1 section explicitly and preserves unrelated parse errors. |
| #538 | Carried | Render validation parses from an isolated temporary directory instead of a sibling temporary file. |
| #539 | Carried | Finding text rejects fenced blocks and heading-like lines that could alter durable plan structure. |
| #540 | Carried | Class-recall limits reject invalid values with a stable `ValueError`. |
| #541 | Carried | Plan-review lesson pattern IDs slugify category components. |
| #542 | Carried | Blocking evidence, task, and checkpoint operations are offloaded with `asyncio.to_thread`. |
| #543 | Adapted | Positional candidate selection was merged while retaining the current no-fix-policy lesson class. |
| #544 | Adapted | Evidence-scoped invalid-payload diagnostics were added without replacing the current round-diff classifications. |
| #548 | Adapted | The current stage-review transition now holds the approval mutation across manifest application and reuses the pre-held dispatch mutex. |
| #549 | Adapted | The current transition service passes `dispatch_run_id` directly to manifest application without an empty-string fallback. |
| #550 | Already satisfied | The current review rejection path already uses `_replace_round_section`. |
| #551 | Carried | Review approval payload fields are keyword-only in the transition facade. |
| #559 | Adapted | Source regression cases were retained and updated for current telemetry, request-anchor, and atomic rollback contracts. |
| #560 | Adapted | Migration parity coverage uses the repository SQL-script executor for both current migration baselines. |
| #561 | Carried | Plan review-evidence tests are categorized as PostgreSQL integration tests. |
| #562 | Adapted | The shared canonical JSON SHA-256 helper now covers source digests plus the current manifest-service routing and payload digests. |
| #563 | Carried | Feedback-loop coverage proves recording does not mutate caller-owned findings. |
| #564 | Carried | Feedback-loop assertions compare structures directly instead of round-tripping local JSON. |
| #565 | Carried | Lesson test doubles use concrete argument and return annotations. |
| #566 | Carried | Recall-context task-manager doubles use concrete argument annotations. |
| #567 | Carried | Retirement tests are categorized as unit tests. |
| #568 | Carried | Database-backed round-diff cases are integration tests while pure classifiers and caps retain unit markers. |
| #579 | Obsolete | The cited verification-receipt unit tests were deleted with receipt storage. |
| #580 | Obsolete | The cited receipt validation test was removed with the receipt contract. |
| #705 | Obsolete | The verification-receipt buffer path no longer exists. |
| #724 | Obsolete | Lifecycle close no longer fetches or attaches verification receipts. |
| #765 | Obsolete | `verification_receipts.py` was removed by the checklist-close cutover. |
| #766 | Obsolete | Verification-receipt sanitization was removed with that module. |
| #768 | Carried | Task updates persist the normalized validation criterion while preserving `UNSET` when no update was supplied. |
| #771 | Carried | Criteria extraction discards prose before the first list marker. |
| #772 | Obsolete | Actor-attestation admission was removed with the superseded evidence-admission path. |
| #773 | Obsolete | Task-state evidence output bounding was removed with verification receipts. |
| #784 | Obsolete | `TaskValidator.validate_task` is fully keyword-only, so positional validation criteria cannot be supplied. |

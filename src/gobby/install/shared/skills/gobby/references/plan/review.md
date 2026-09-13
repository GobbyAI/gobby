# Adversarial plan review
Load before preparing, performing, or processing adversarial plan review.

## Prepare
Review is optional and requires its own authorization. Base-validate canonical bytes immediately before each round; a manifest-bearing plan also passes expansion mode. Resolve deterministic residue through [repair](repair.md) before preparing evidence.
Call gobby-plans:prepare_plan_review_round immediately before spawning plan-adversary-taskless without task_id and with isolation none. Supply evidence ID, canonical path, clean scope-specific sweep report, round/cap and parent. Bind once with bind_evidence_run; expire failed launches/binds. Immediately save a structured clear_session=false handoff, then use event-driven waits.
Reviewer reads get_plan_review_snapshot for immutable bytes and does not reread the live artifact. Complete any oversized-result retrieval before using it. Never edit the plan. Taskless reviewers never mutate task state; stage-native reviewers use only their authorized verdict transitions.

## Review obligations
Load [coverage](coverage.md) and its linked contract before checking grammar.
Read repository code and Gobby task evidence directly. Resolve each exact
file-qualified Target before usages or blast-radius searches; require successful
symbol validation for indexed targets regardless of category. An accepted repair
is not proof that a finding is resolved: the fresh reviewer re-runs its check.
Load proportionality and recall plan reviewer-miss lessons through review-learning; apply each recalled check. Walk requirements, all inputs/control-flow branches, transitions, races, boundaries, errors, recovery and scope collisions. Trace each obligation to acceptance, targets, consumers and tests; verify self-contained Research context and atomic leaves.
Complete three lanes: requirements_traceability and runtime_invariants completed; repository_blast_radius delegated-verified after spot-checking the deterministic report against exact source symbols and consumers. Use read-only provider-native internal subagents for lanes; capacity/failure moves only that lane to sequential parent work. Lane workers return candidates with all section IDs checked and hashed citations, never findings/verdicts/evidence writes.
The adversary verifies/deduplicates every candidate, records one emitted_finding or dismissed disposition with reason, then performs cross-lane and adjacent-variant sweeps.
Derive shadow manifest through derive_plan_review_manifest even on rejection. Call validate_plan_review_coverage with complete lanes, dispositions and exact derivation status. Preserve its returned attestation verbatim. If it fails, return protocol_failure with exact tool error and draft findings, no verdict.

## Findings and verdict
Use stable finding_id/check_key, evidence section_id, severity blocking or nit, supported category, location, description/fix, prevention and nonempty principle or root_cause. Explain concrete missing behavior; no quotas or praise findings.
Categories: missing-requirement, bad-sequencing, unhandled-edge, weak-testability, traceability, over-engineering, gobby-format. Structural over-engineering requires concrete removable mechanism and a complete simpler solution preserving all requirements; ambition/size alone never qualifies.
Typed repairs: traceability permits add_targets/add_acceptance; bad-sequencing add_dependency; weak-testability add_acceptance; gobby-format all three. Other categories remain prose. Every referenced section must exist in evidence. Only coordinator apply_plan_review_repairs writes accepted repairs after a finalized rejection.
Verify a real nonblank/nonunknown Plan ID outside code fences and reject covers:unknown labels before approval. Do not approve a plan you do not understand; unresolved governing questions become specific blocking missing-requirement findings after direct inspection. Routine blocking findings return needs_review; escalation is only for genuine human intervention or insufficient context.
Return one canonical JSON result: approved or needs_review, findings, exact coverage_attestation; approval includes exact routing_decisions and server-derived manifest_entries. A blocking finding prevents approval. Preserve complete prior rounds.

## Coordinator and recovery
Present every finding with full metadata and collect individual accept/decline votes before editing; judge over-mechanism using restraint. Existing delegated/unattended authority permits coordinator votes with rationale. Rejection uses append then finalize before repair; approval uses [approval](approval.md). Preserve canonical payloads, round IDs and checkpoints. Failed/incomplete evidence never counts as a completed review round.
Spawned taskless reviewers deliver the exact JSON to their parent through send_message, then end_agent_run with the current required structured handoff (current_state and next_steps). Stage-native reviewers first use the authorized approve_review/reject_review/escalate_task stage path, then deliver the exact result and end their run. Fetch those schemas; older no-argument end_agent_run examples are obsolete.
See [Plans and plan mode](../../../../../../../../docs/guides/plans-and-plan-mode.md#optional-adversarial-review).

_Last verified: 2026-09-12_

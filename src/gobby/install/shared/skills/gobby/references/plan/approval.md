# Approval and implementation handoff
Load before accepting review evidence, applying M1, or handing a plan to implementation.

## Mandatory gate
Resolve material decisions, materialize the canonical artifact, and pass project-aware base validation. Explicit user implementation approval is required whether or not optional review ran. Existing authorization persists within its stated scope; neither a menu, drafting continuation, nor reviewer approval supplies human consent.

Before manifest handoff, verify a real nonblank `**Plan ID:** <id>` outside fences and reject any `covers:unknown:` label. Preserve every acceptance item in source order in server-derived validation criteria; never summarize away obligations.

## Reviewed approval
Use the complete canonical approved result, never reconstructed fields:
1. apply_plan_review_manifest compares reviewed sections, re-derives/validates the manifest and atomically records approval intent and manifest checkpoint.
2. append_plan_changelog_round appends prose plus the daemon-rendered canonical V1 fence.
3. finalize_plan_review_evidence persists the result and pending lesson-mint state.
4. Review proven prior-round lessons and checkpoint_plan_review_lesson_mint with minted IDs, none when unproven, or failed with the recorder error. Approval completes only after pending clears.
5. Run `uv run gobby plans validate <plan-file> -p <project-root> --mode expansion`.

Use render_plan_changelog_round only to inspect canonical rendering; never hand-edit fences. Preserve V1 as one kind: verification section with bold round labels, not noncanonical round headings.

## Approval without adversarial review
Coordinator supplies complete routing decisions to derive_plan_handoff_manifest and passes its exact source_plan_hash, rendered_plan_hash and manifest_digest to apply_plan_handoff_manifest. Apply re-derives and rejects drift before atomic write; exact rendered-hash retries are idempotent. Run expansion-mode validation afterward. Never synthesize reviewer verdicts, attestation, or evidence, and never invoke a stub manifest emitter.

## Lessons and boundaries
Reviewer-miss requires every participating section hash unchanged since an earlier finalized round. Fixer-induced-defect requires changed causal section hashes, causal_finding_id and introduced_in_round. A dual-class lesson needs both proof bundles; each independently proven class remains recordable even if the other is unproven. Use review-learning's class-scoped identity, source_kind plan_review, guardrail_target checklist, rule_id plan-review:<category>; a plan-file path is not a promotion anchor. Mint at most five proven lessons per plan, reserving a slot per present class and using its deterministic ranking. Rank reviewer misses by completed rounds missed and fixer-induced defects by
causal occurrences, descending within each class; then severity, `check_key`
ascending, and `finding_id` ascending. Reserve one slot for each present class
before filling the remaining slots in that order. No proof means no lesson.
If implementation handoff is requested while an enhancer or reviewer is active, mark it pending, finish the run, votes, accepted edits and checkpoints before handing off; launch no new optional round.
After approval, choose manual [expansion](expansion.md) or authorized build with planning_seed_state approved and only finalized completed_plan_review_rounds. A file edit that changes reviewed scope requires renewed evidence; interrupted checkpoint recovery follows [repair](repair.md).

For an already-approved autonomous planning-stage review whose lesson mint failed, use gobby-tasks-ops:backfill_plan_review_lessons to retry its durable checkpoint; it does not rerun approval.

See [Manifest-on-approval contract](../../../../../../../../docs/contracts/plan-coverage.md#manifest-on-approval-contract).

_Last verified: 2026-09-12_

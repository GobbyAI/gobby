# Coverage grammar and evidence
Load before authoring targets, acceptance, manifests, deferrals, or checking plan coverage.

## Discover
The [Plan-Coverage Contract](../../../../../../../../docs/contracts/plan-coverage.md) owns the exact grammar and carrier table. Use `gobby-plans:validate_plan` for file-based draft validation; project-aware CLI standard/expansion modes provide the pre-handoff checks. Use `gobby-plans:regenerate_coverage_manifest` for a registered implementation plan.

## Authoring constraints
- Canonical phase headings are `## P<N>: Name` with first-line `kind: framing`. Deliverables carry section IDs, `kind: deliverable`, category, Targets, Research context, and Acceptance. Context may use framing; verification summarizes checks; deferred sections require typed deferrals. M1 is `kind: manifest`.
- Acceptance IDs append `.<n>` to the exact section ID; numeric sections never gain a synthetic A. Each item names a file, symbol, test, or behavior artifact; a test artifact names one symbol as `path::test_symbol`, and a bare test file path fails validation once the plan carries a manifest. Work-enumerating tables need one acceptance item per data row.
- Targets form a contiguous block without a blank line after `Targets:`. Existing indexed symbol-bearing files use exact `path::qualified_name` or justified `path::*` with same-line scope-reason. Bare paths are for new or zero-symbol files. UUIDs, line numbers, module-qualified guesses, and mixed exact/wildcard scopes are invalid.
- Resolve exact symbols in their file with gcode. Sweep usages and literal consumers, including tests, imports and indirect string/re-export sites. Target every owned consumer and required derived carrier; record bounded literal-sweep evidence when the index does not cover the checkout.
- Shared Target paths require dependency ordering even when symbol scopes differ. Heading dependencies can name deliverables or phases; manifest depends_on names sibling source_section IDs only. Reject unknown refs and cycles.
- At 850 lines, targeted production files trigger the contract's decomposition heuristic; the production ceiling stays below 1,000 lines. Whole-file deletion needs its explicit annotation. Do not suppress a missing-index or skipped check and claim success.

## Manifest and deferrals
Draft narrative first. The coordinator applies server-derived M1 only through [approval](approval.md). Preserve 1:1 deliverable/entry/leaf mapping and one `covers:<plan-id>:<section-id>:<item-id>` label per acceptance item.
Automated categories are code, config, docs, refactor, test. Code requires backend/frontend/fullstack domain. TDD true is valid for code/config only and adds one leaf's skill, label and evidence obligations; no duplicate test wrappers. Research/planning/manual belong to direct tasks.
A deferral records task_ref, reason, owner, original_acceptance_items and deferred-from provenance. Open targets or completed/already_implemented targets can satisfy ownership; other closure reasons require a valid replacement. External prerequisites belong in typed deferrals, not sibling manifest edges. Placeholder refs are resolved through expansion's deferral_task_map, then replaced in the canonical plan with validation and hash refresh.

## Coverage and recovery
Operator CLI `gobby plan coverage` requires --plan, --plan-id, --plan-hash and --task-tree. DB mode also needs --root-task and --project-id; matrix-file mode needs --matrix-file and no DB-only scope flags. It writes a managed manifest. Task JSONL is not coverage input.
Check exact plan hash, root/project identity, structured labels and artifact evidence. Free-form plan-ref labels do not count. Strategy plans have no managed coverage manifest. On missing/stale rows inspect the actual task tree and repair its ownership/evidence, regenerate, and rerun QA; do not manufacture covered rows.

See [Coverage CLI](../../../../../../../../docs/contracts/plan-coverage.md#coverage-cli).

_Last verified: 2026-09-12_

# Plan Review Structured Repairs

> **Plan ID:** plan-review-structured-repairs

## Overview
`kind: framing`

Adversarial round 1 of `herdr-terminal-client-qa-fixes` returned 29 prose `fix`
findings that the coordinator hand-applied over two hours. For the mechanical
classes — add these Targets, add this dependency edge, add an acceptance item
for an enumerated clause — the reviewer already knows the exact entries. This
plan lets the adversary return those as typed `repairs` on the finding and adds
one coordinator tool, `apply_plan_review_repairs`, that applies only the
accepted ones after the rejection checkpoint is finalized. The adversary still
never edits the plan; the human vote stays the scope gate; design-class
findings stay prose.

## Constraints
`kind: framing`

Decision Record (confirmed with the user, 2026-08-22):

1. Repairs v1 has exactly three kinds: `add_targets`, `add_dependency`,
   `add_acceptance`. Repairs live on the finding (`repairs: [...]`), gated by
   category: `traceability → {add_targets, add_acceptance}`,
   `bad-sequencing → {add_dependency}`, `gobby-format → all three`,
   `weak-testability → {add_acceptance}`; `unhandled-edge`,
   `over-engineering`, and `missing-requirement` forbid `repairs`. The prose
   `fix` stays required on every finding.
2. Ordering is checkpoint-first: `append_plan_changelog_round` and
   `finalize_plan_review_evidence` run before any repair is applied, so the
   checkpoint records the reviewed artifact and the next round snapshots the
   repaired one.
3. `apply_plan_review_repairs(evidence_id, accepted_finding_ids)` accepts only
   finalized interactive `needs_review` evidence, is idempotent, is
   all-or-nothing (any invalid repair leaves the file untouched), and returns
   a unified diff plus the plan hash before and after.
4. Repairs exist only on `needs_review` results; approval rounds carry none, so
   `derive_plan_review_manifest` and the shadow manifest are unaffected.

Invariants and boundaries:

- `review_repairs` must not import `review_findings`; `review_findings`
  imports `review_repairs`.
- Plan text surgery works on bytes the service already holds under
  `transaction_immediate(PlanReviewEvidenceMutation)` with
  `store.require(for_update=True)`, the same lock `append_plan_changelog_round`
  uses. The V1 fence bytes are never touched.
- Dependency annotations are rebuilt with the public
  `parser.extract_section_dependencies` / `parser.strip_section_dependencies`
  pair and phase membership comes from `manifest_emitter.deliverables_by_phase`;
  no parser change is needed.
- Acceptance insertion anchors on the last `AcceptanceItem.source_line` of
  the section and extends over indented or blank-then-indented continuation
  lines, fenced blocks, and end of file.
- No monolith ceiling is approached: `review_evidence.py` 704 → ~760,
  `_stage_review.py` 831 → ~780, `mcp_proxy/tools/plans/review_evidence.py`
  488 → ~525, `symbol_targets.py` 837 (rename only).
- Consumer sweep (`uv run gobby plans validate` consumer-coverage output,
  recorded 2026-08-22). Every exact Target keeps its signature, so these
  consumers need no edit and stay out of Targets deliberately:
  `render_rejection_section` → `src/gobby/storage/tasks/_review_transitions.py`
  (exercised by `tests/storage/test_stage_review_findings.py`, targeted in
  1.1); `register_review_stage_tools` →
  `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py`;
  `_validate_round_entry_plan` → `tests/plans/test_review_evidence.py` (the
  function remains and delegates to the lifted helper); `parse_symbol_targets`
  → `tests/plans/test_symbol_targets.py` (its public behavior is unchanged by
  the private rename); `register_review_evidence_tools` →
  `src/gobby/mcp_proxy/tools/plans/__init__.py`. `_parse_target_line` has one
  consumer, `parse_symbol_targets`, renamed in the same deliverable.
- Out of scope: auto-applying without a vote, design-class repairs, changing
  the approval commit order, and the task-bound `plan-adversary` vote flow
  (that agent gets the blocklist entry and advisory wording only).

## P1: Typed repairs on findings
`kind: framing`

**Goal**: A finding can carry validated, canonicalized `repairs`, and every
surface that accepts findings shares one schema.

### 1.1 Validate typed repairs on plan-review findings [category: code]
`kind: deliverable`

Targets:
- `src/gobby/plans/review_repairs.py`
- `src/gobby/plans/review_findings.py::_validate_finding`
- `src/gobby/plans/review_findings.py::render_rejection_section`
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py::register_review_stage_tools`
- `tests/plans/test_review_repairs_validation.py`
- `tests/mcp_proxy/test_stage_review_schema.py::*` — scope-reason: assert the shared finding schema identity and the new `repairs` property
- `tests/storage/test_stage_review_findings.py::*` — scope-reason: staged rejection path persists and projects repairs

Create `src/gobby/plans/review_repairs.py` with:

- `REPAIR_KINDS = ("add_targets", "add_dependency", "add_acceptance")`,
  `REPAIR_KINDS_BY_CATEGORY` (the Decision Record matrix; categories absent
  from the mapping forbid `repairs`), and `REPAIR_SCHEMA`, a JSON-schema
  fragment for the `repairs` array with `oneOf` per kind.
- `validate_finding_repairs(raw, *, prefix, category, section_ids) -> list[dict]`
  returning the canonical list. Rules: `repairs` is optional; when present it
  is a non-empty list of objects with no unknown keys; `kind` is in
  `REPAIR_KINDS` and allowed for `category`; `section_id` is in
  `section_ids` (it may differ from the finding's own `section_id`); exactly
  one payload key matches the kind and is non-empty:
  - `add_targets.entries`: each entry parses through
    `symbol_targets.parse_target_line` to exactly one `SymbolTarget` with zero
    issues; entries are unique by `reference`.
  - `add_dependency.on`: unique refs, each in `section_ids`, none equal to
    `section_id`.
  - `add_acceptance.items`: each is `{prose, artifact}`; both single-line and
    non-empty; `artifact` matches `^(file|symbol|test|behavior):\s*\S`.
  Errors are `ReviewEvidenceError("invalid_round_result", ...)` with the
  `prefix` (for example `findings[2].repairs[0].entries[1]`).

In `src/gobby/plans/review_findings.py`:

- `_ALLOWED_FIELDS` gains `"repairs"`; `_validate_finding` calls
  `validate_finding_repairs` and writes the canonical list back onto the
  finding (same pattern as `_SECTION_SET_FIELDS`).
- Export `FINDING_ITEM_SCHEMA`, built from `FINDING_SEVERITIES`,
  `FINDING_CATEGORIES`, and `REPAIR_SCHEMA`, with the same `required` list and
  `additionalProperties: False` the inline schema has today.
- `render_rejection_section` emits a `**Repairs:**` projection per finding
  that carries repairs: one bullet per repair, `kind section_id: entries`.

In `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`, the inline finding item
schema inside `register_review_stage_tools` (the `reject_review` registration)
becomes `FINDING_ITEM_SCHEMA`; the enum lists are no longer duplicated.

Tests in `tests/plans/test_review_repairs_validation.py` (pure): the category
matrix accepts and rejects per kind; every shape error names its prefix;
canonicalization round-trips (order preserved, no extra keys); the
`FINDING_ITEM_SCHEMA` validates a finding with repairs through `jsonschema`;
the rejection projection renders `**Repairs:**`.

**Acceptance:**

- 1.1.1 - `validate_finding_repairs` enforces the category matrix, the per-kind payload shape, entry parsing, uniqueness, and manifest membership, and returns the canonical list. test: `tests/plans/test_review_repairs_validation.py::test_category_matrix`.
- 1.1.2 - `_validate_finding` accepts `repairs`, rejects it for forbidden categories, and writes the canonical list back onto the finding. test: `tests/plans/test_review_repairs_validation.py::test_finding_canonicalizes_repairs`.
- 1.1.3 - `FINDING_ITEM_SCHEMA` is the single finding schema: `reject_review`'s registered input schema uses it and it lists `repairs`. test: `tests/mcp_proxy/test_stage_review_schema.py::test_reject_review_uses_shared_finding_schema`.
- 1.1.4 - The staged rejection path persists findings with repairs and the rendered rejection section shows a `**Repairs:**` projection while the plan bytes stay unchanged. test: `tests/storage/test_stage_review_findings.py::test_rejection_persists_repairs_without_editing_plan`.

## P2: Coordinator apply path
`kind: framing`

**Goal**: Accepted repairs are applied mechanically, atomically, and
idempotently by a coordinator-only gobby-plans tool after the rejection
checkpoint is finalized.

### 2.1 Apply accepted repairs under the evidence lock [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/plans/review_repairs.py`
- `src/gobby/plans/review_evidence.py::*` — scope-reason: adds the `apply_plan_review_repairs` method; no existing method signature changes
- `src/gobby/plans/review_evidence_io.py::_validate_round_entry_plan`
- `src/gobby/plans/symbol_targets.py::_parse_target_line`
- `src/gobby/plans/symbol_targets.py::parse_symbol_targets`
- `tests/plans/test_review_repairs_apply.py`
- `tests/plans/test_review_repairs_service.py`

Renames only: `symbol_targets._parse_target_line` becomes the public
`parse_target_line` (its one caller, `parse_symbol_targets`, follows). In
`src/gobby/plans/review_evidence_io.py`, lift the parse step of
`_validate_round_entry_plan` into
`parse_plan_bytes(plan_name, content, *, parse_mode="draft") -> PlanDocument`
(writes a temp file under the plan's name, parses, raises
`ReviewEvidenceError("invalid_plan", ...)`); `_validate_round_entry_plan`
keeps its signature and calls it.

Add to `src/gobby/plans/review_repairs.py`:

- `select_accepted_repairs(evidence, accepted_ids) -> tuple[list[Repair], list[Skipped]]`
  reading `evidence.round_result["findings"]`; unknown ids raise
  `unknown_finding_id`; accepted findings without `repairs` become
  `{finding_id, reason: "prose_only"}`.
- `apply_repairs(current: bytes, *, plan_name, repairs) -> RepairOutcome`
  (`updated: bytes`, `applied: list[dict]`, `skipped: list[dict]`,
  `diff: str`). Algorithm:
  1. Decode UTF-8; `lines = text.splitlines()`; require that
     `"\n".join(lines) + ("\n" if text.endswith("\n") else "")` equals `text`,
     else `unsupported_plan_text` (rejects CRLF and exotic separators).
  2. `before = parse_plan_bytes(plan_name, current)`;
     `manifest_before = build_section_manifest(current)`;
     `targets_before, _ = parse_symbol_targets(before)`; phase membership from
     `manifest_emitter.deliverables_by_phase`.
  3. Group repairs by `section_id`; the section must exist
     (`repair_section_missing`) and be a deliverable
     (`repair_section_not_deliverable`). Process sections bottom-up; within a
     section compute every insertion point from the original lines, then
     splice bottom-up so earlier indexes stay valid.
     - `add_targets`: references already present in `targets_before` for the
       section are `already_present` and skipped; the rest are appended as
       `` - `{reference}` `` (plus `` — scope-reason: … `` for `::*`) after
       the tail of the section's last `Target(s):` block, found with the same
       stop rule as `semantic_lint.iter_target_block_lines` (blank line, next
       `Target:`, `**Acceptance:**`, heading, `kind:` marker, or a line that is
       neither a bullet nor contains a backtick or `/`). This also works for
       an inline singular `Target:` header. With no block, insert
       `["", "Targets:", *bullets, ""]` after the section's `kind:` line.
     - `add_dependency`: `existing = extract_section_dependencies(title)`;
       a ref is skipped when already present or when its phase is already
       present (`1.1` when the heading depends on `P1`); the heading line is
       rebuilt as `strip_section_dependencies(title).rstrip() + " (depends: a, b)"`
       with the merged, order-preserving list.
     - `add_acceptance`: insertion point is after the last acceptance item's
       continuation tail (indented lines, blank-then-indented lines, fenced
       blocks), or directly after `**Acceptance:**` when the block is empty;
       next id is `max(numeric suffix) + 1`; the separator (` - ` or ` — `) is
       copied from the last bullet, default ` - `; items whose normalized body
       already exists are `already_present`.
  4. Rebuild text; `parse_plan_bytes` again (`invalid_repair`, file untouched);
     `build_section_manifest(updated)` must have the same keys as
     `manifest_before` and the set of changed hashes must equal the repaired
     section ids; `parse_symbol_targets(after)` issues must not grow.
  5. Return `RepairOutcome` with `diff = difflib.unified_diff(...)`.

Add `PlanReviewEvidenceService.apply_plan_review_repairs(evidence_id, accepted_finding_ids)`
to `src/gobby/plans/review_evidence.py`, under
`transaction_immediate(PlanReviewEvidenceMutation)` with
`store.require(for_update=True)`. Gates in order: `evidence_not_found`,
`wrong_plan`, `not_interactive_evidence`, `evidence_not_finalized`,
`evidence_replay`, `not_rejection_round`, `unknown_finding_id`. Empty
`accepted_finding_ids` returns `{ok: True, changed: False}`. It calls
`atomic_write_bytes` only when bytes changed. Result:
`{ok, evidence_id, changed, applied: [{finding_id, kind, section_id, added}],
skipped: [{finding_id, reason}], diff, plan_hash_before, plan_hash_after}`.

Tests: `tests/plans/test_review_repairs_apply.py` (pure bytes in, bytes out)
covers each kind incl. inline `Target:`, no-block insertion, `already_present`,
a `::*` entry conflicting with an exact target → `invalid_repair`,
heading-in-phase dedupe, multi-line last acceptance item, EOF without newline,
separator style, two sections bottom-up with only their hashes changing, CRLF →
`unsupported_plan_text`, post-surgery parse failure leaving bytes untouched,
and an empty diff when nothing changes. `tests/plans/test_review_repairs_service.py`
(real Postgres, its own `repair_setup` fixture modeled on `review_setup` in
`tests/plans/conftest.py`) covers every gate, unknown id, the end-to-end
prepare → bind → append → finalize → apply → re-apply no-op flow, V1 fence
bytes unchanged, and an `atomic_write_bytes` failure leaving the file untouched.

**Acceptance:**

- 2.1.1 - `apply_repairs` performs all three surgeries with the tail rules above, rejects unsupported line endings and post-surgery parse failures without changing bytes, and only repaired sections change hash. test: `tests/plans/test_review_repairs_apply.py::test_two_sections_change_only_their_hashes`.
- 2.1.2 - `PlanReviewEvidenceService.apply_plan_review_repairs` enforces the gate order, is idempotent, writes atomically only on change, and returns the diff and both hashes. test: `tests/plans/test_review_repairs_service.py::test_apply_then_reapply_is_noop`.
- 2.1.3 - `parse_target_line` is public and `parse_plan_bytes` is lifted from `_validate_round_entry_plan` with unchanged round-entry behavior. test: `tests/plans/test_review_repairs_apply.py::test_add_targets_entries_parse_with_public_helper`.

### 2.2 Register the apply_plan_review_repairs gobby-plans tool [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/plans/review_evidence.py::register_review_evidence_tools`
- `tests/mcp_proxy/tools/test_plan_review_evidence_errors.py::*` — scope-reason: error-envelope cases for the new tool
- `tests/mcp_proxy/test_plans_tools.py::*` — scope-reason: registration and input-schema assertions for the new tool

In `register_review_evidence_tools`, register a sync
`apply_plan_review_repairs(evidence_id: str, accepted_finding_ids: list[str])`
that delegates to the service and wraps failures with
`_error_payload(exc, "apply_plan_review_repairs_failed")`, next to
`finalize_plan_review_evidence`. Input schema requires exactly
`{evidence_id, accepted_finding_ids}`.

**Acceptance:**

- 2.2.1 - The gobby-plans registry exposes `apply_plan_review_repairs` with `required == {evidence_id, accepted_finding_ids}` and returns the service result. test: `tests/mcp_proxy/test_plans_tools.py::test_apply_plan_review_repairs_registered`.
- 2.2.2 - Service errors surface as `{ok: False, error: <code>}` envelopes with the `apply_plan_review_repairs_failed` fallback. test: `tests/mcp_proxy/tools/test_plan_review_evidence_errors.py::test_apply_plan_review_repairs_error_envelope`.

## P3: Contracts and skills
`kind: framing`

**Goal**: The adversary contract, the review methodology, and the coordinator
workflow describe repair-class findings and the apply step.

### 3.1 Repair-class findings in adversary contracts and plan skills [category: docs] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml::*` — scope-reason: rejection template, class-split paragraph, and blocklist entry
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: blocklist entry and advisory-repairs wording
- `src/gobby/install/shared/skills/plan-review/SKILL.md`
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `docs/contracts/plan-coverage.md`
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — scope-reason: contract template and blocklist assertions
- `tests/agents/test_plan_adversary_manifest.py::*` — scope-reason: task-bound contract blocklist and advisory wording
- `tests/skills/test_plan_review_skill.py::*` — scope-reason: repair-class section and version pin
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: step-4 apply ordering and Recovery entry

- `plan-adversary-taskless.yaml`: the rejection template gains `location`
  (required by the validator, missing today) and an optional `repairs` block
  with one example per kind; a paragraph states the repair class vs design
  class split; `gobby-plans:apply_plan_review_repairs` joins
  `blocked_mcp_tools`.
- `plan-adversary.yaml` (task-bound): same blocklist entry plus "repairs are
  advisory to the planner" — no vote exists there, the planner owns
  between-round edits.
- `plan-review/SKILL.md` → 1.4.0: a "Repair class vs design class" section
  with the category matrix, the closed-loop note (a repair satisfies the
  reviewer's own check; a fresh reviewer runs each round; design repairs stay
  prose), the payload schema, and one example.
- `plan/SKILL.md` → 4.0.0: step 4 becomes append → finalize →
  `apply_plan_review_repairs(evidence_id, accepted_finding_ids)` → hand-apply
  accepted prose-only fixes → base-validate; Recovery gains `invalid_repair`
  ⇒ plan untouched, hand-apply that finding's `fix`, re-running is always
  safe.
- `docs/contracts/plan-coverage.md`: one paragraph after the "adversary MUST
  NOT edit the plan file" rule describing typed repairs as payload that only
  the coordinator tool writes after the vote and the finalized checkpoint.
- Run `uv run gobby sync` and confirm with `get_skill(name="plan-review")` and
  `get_skill(name="plan")` that the DB rows carry the new text.

**Acceptance:**

- 3.1.1 - Both adversary contracts block `gobby-plans:apply_plan_review_repairs`; the taskless rejection template contains `location:` and `repairs:`. test: `tests/agents/test_plan_adversary_taskless_definition.py::test_rejection_template_carries_location_and_repairs`.
- 3.1.2 - `plan-review` 1.4.0 documents the repair-class matrix and the closed-loop note. test: `tests/skills/test_plan_review_skill.py::test_repair_class_section`.
- 3.1.3 - `plan` 4.0.0 orders step 4 as append → finalize → apply repairs → prose fixes → validate and lists `invalid_repair` in Recovery. test: `tests/skills/test_plan_skill_delegated_mode.py::test_adversary_presentation_contract`.
- 3.1.4 - The contract paragraph on typed repairs follows the adversary write-scope rule. file: `docs/contracts/plan-coverage.md`.

## Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| Plan artifact | #20685 | in progress |

## V1 Plan Changelog
`kind: verification`

No review rounds yet.

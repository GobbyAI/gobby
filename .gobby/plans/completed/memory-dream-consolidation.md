# Memory Dream Consolidation, Abstraction, and Reorganization

**Plan ID:** memory-dream-consolidation

## Overview
`kind: framing`

Extend the nightly Memory Dream sweep from per-memory garbage collection into a
bounded corpus-maintenance pass with all three requested behaviors: existing
prune/refresh/rescope decisions, abstraction of related episodic records into a
durable semantic pattern, and reversible reorganization through duplicate merge
and compound-memory split actions.

The design keeps the existing admitted-run, 25-candidate work-unit, truth-digest,
durable-checkpoint, and secondary-reconciliation architecture. Compound actions
reuse the PostgreSQL `create_memory(..., supersedes=[...])` primitive inside one
ambient transaction so source fences, output creation, supersession, snapshots,
and the post-commit notification succeed or roll back together.

## Constraints
`kind: framing`

- This plan implements the user direction recorded on 2026-08-26 in task
  `#21007`: retain Dream and cover pruning, abstraction, and reorganization.
- Nightly mutation remains the current product behavior. `dry_run` and
  `skip_consolidation` remain explicit preview/inventory controls; this plan adds
  no second approval workflow or feature flag.
- Every planner call remains bounded to at most 25 candidates. The existing
  `MAX_SUPERSEDES_IDS = 20` limit also bounds one merge/abstraction source set.
- `review-lesson` memories remain excluded before candidate paging. Preference
  memories are never automatic abstraction or split sources.
- No database migration is required. Existing JSONB run plans/summaries and
  snapshot `before_data`/`after_data` rows can represent the new action payloads
  and role-qualified journal entries.
- Project/global visibility remains authoritative. One compound action may touch
  only one exact storage scope; project-visible runs may inspect globals, while a
  merge or split never crosses the project/global boundary.
- Explicit selectors are normalized and stored in admission options. An
  unfiltered aggregate run never covers a filtered request, and incompatible
  selector requests conflict rather than coalesce.
- Existing `min_action_confidence` and `min_delete_confidence` are sufficient.
  Merge, abstraction, and split require the greater of those two values because
  every compound action hides at least one source. No single-purpose confidence
  setting is added.
- Generated bundled content is updated through the repository manifest builder;
  hand-edited manifest hashes are out of scope.
- Current implementation anchor inventory (2026-08-27):
  - Selector/run state: `src/gobby/memory/dream/options.py:14-93`,
    `src/gobby/storage/memories_dreams.py:197-282`,
    `src/gobby/memory/dream/candidates.py:24-68`, and
    `src/gobby/memory/dream/orchestrator.py:217-466`.
  - Generation schema and action validation:
    `src/gobby/memory/generation_schemas.py:15-44`,
    `src/gobby/memory/dream/models.py:92-171`, and
    `src/gobby/memory/dream/plan.py:30-229`.
  - Bundled planner prompt and paging:
    `src/gobby/install/shared/prompts/memory/dream.md:12-82` and
    `src/gobby/memory/dream/planner.py:38-244`.
  - Apply path: `src/gobby/memory/dream/apply.py:31-96`,
    `src/gobby/memory/dream/apply.py:257-346`, and
    `src/gobby/memory/dream/storage_actions.py:47-189`.
  - Revert path: `src/gobby/memory/dream/apply.py:99-254`,
    `src/gobby/memory/dream/storage_actions.py:191-274`, and
    `src/gobby/memory/dream/storage_journal.py:252-309`.
  - Planner/schema tests: `tests/memory/test_dream.py:309-664` and
    `tests/memory/test_dream.py:2885-2903`.
  - Apply/revert tests: `tests/memory/test_dream.py:668-1011` and
    `tests/memory/test_create_supersedes.py:181-272`.
  - Selector/admission tests: `tests/memory/test_dream_options.py:12-66` and
    `tests/memory/test_dream.py:1210-1360`.
  - Public-surface tests: `tests/mcp_proxy/tools/test_memory.py:713-900`,
    `tests/servers/routes/test_memory_routes.py:98-306`, and
    `tests/cli/test_memory_cli.py:583-908`.

## D1: Confirmed Decision Record
`kind: framing`

1. Selector shape: use the repository's established `tags_all`, `tags_any`, and
   `tags_none` vocabulary plus ordered `candidate_ids`. Exact IDs and tag filters
   compose as an intersection.
2. Selector scope: candidate IDs require `project_id` or `global_only`; CLI
   selector use therefore requires an explicit project/global scope. Tag-only
   selectors may fan out through aggregate mode because each scope evaluates the
   same normalized filters against its own truth digest.
3. Abstraction trigger: an automatic abstraction group contains 3–20 active,
   same-scope `fact`/`context` memories sharing a non-null `source_task_id`; when
   every member lacks a task, a shared non-null `source_session_id` is the
   fallback key. The full filtered group must fit one planner page. Partial,
   oversized, mixed-scope, preference, or `review-lesson` groups remain ordinary
   per-memory candidates.
4. Action vocabulary: retain `keep|delete|refresh|review|promote`; add `merge`
   with `merge_mode=deduplicate|abstract` and add `split`. Abstraction is a
   constrained merge that emits a new `pattern` memory with a non-empty rationale.
5. Mutation primitive: execute compound actions through the existing atomic
   superseding create primitive inside Dream's ambient transaction. Split creates
   every output in that transaction and applies supersession on the final output,
   making any failure roll back the entire set.
6. Revert contract: role-qualified snapshots distinguish hidden sources, newly
   created outputs, and an existing deduplicated survivor. Revert deletes a
   Dream-created output only when its complete current row still equals the
   recorded after-state; a later edit becomes a visible conflict.
7. Grounding and confidence: current-truth digest rules stay authoritative.
   Compound actions must cite the source relationship and preservation/generalization
   rationale, meet `max(min_action_confidence, min_delete_confidence)`, and degrade
   to visible `keep` actions when validation cannot prove the complete group.
8. Least-mechanism checkpoint: existing run tables, snapshots, admission,
   `create_memory` supersession, reconciliation, and confidence settings solve the
   required boundaries. New work is limited to selector plumbing, planner action
   contracts, deterministic origin grouping, and one compound apply/revert path.

## P1: Scope and Planner Contract
`kind: framing`

**Goal:** Make bounded candidate selection and the new planner vocabulary explicit,
validated, truth-grounded, and stable across every trigger surface.

### 1.1 Expose bounded candidate and tag selectors [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/dream/options.py::*` — scope-reason: normalize, validate, serialize, and compare every run selector
- `src/gobby/storage/memories_dreams.py::*` — scope-reason: apply selector predicates consistently to candidate rows, IDs, and aggregate scope discovery
- `src/gobby/memory/dream/candidates.py::*` — scope-reason: distinguish explicit ordered snapshots from streaming filtered pages and report selector misses
- `src/gobby/memory/dream/protocols.py::*` — scope-reason: carry selector-aware candidate and storage contracts through the Dream facade
- `src/gobby/memory/dream/orchestrator.py::*` — scope-reason: execute exact-ID snapshots and filtered streaming units without losing order or bounds
- `src/gobby/memory/dream/storage_runs.py::*` — scope-reason: make admission equivalence and aggregate coverage selector-aware
- `src/gobby/memory/dream/service.py::*` — scope-reason: validate selector scope and propagate filtered aggregate/project execution
- `src/gobby/memory/dream/coordinator.py::*` — scope-reason: preserve selectors from public trigger through async execution
- `src/gobby/mcp_proxy/tools/memory_dream.py::*` — scope-reason: expose the complete selector contract on the internal MCP tool
- `src/gobby/servers/routes/memory_dream.py::*` — scope-reason: validate and route selector-bearing HTTP requests
- `src/gobby/cli/memory/dream.py::*` — scope-reason: expose repeated selector options and actionable scope errors on the operator CLI
- `tests/memory/test_dream_options.py::*` — scope-reason: pin normalization, validation, serialization, and scope-key behavior for all selector combinations
- `tests/memory/test_dream.py::*` — scope-reason: pin candidate ordering, filtering, selector misses, and admission coverage in the existing Dream integration suite
- `tests/memory/test_dream_coordinator.py::*` — scope-reason: prove the coordinator preserves selector options and does not double-launch conflicting runs
- `tests/mcp_proxy/tools/test_memory.py::*` — scope-reason: cover MCP selector routing and invalid unscoped requests
- `tests/servers/routes/test_memory_routes.py::*` — scope-reason: cover HTTP selector routing, status codes, and aggregate tag filters
- `tests/cli/test_memory_cli.py::*` — scope-reason: cover repeated candidate/tag flags and required selector scope diagnostics

Extend `DreamRunOptions` with normalized tuple fields `candidate_ids`, `tags_all`,
`tags_any`, and `tags_none`; serialize them as ordered JSON lists. Strip and
deduplicate values in first-seen order, reject empty values, reject overlaps
between required and excluded tags, and validate candidate IDs as UUIDs. Bound an
explicit ID snapshot to `dry_run_max_candidates`; a larger request fails before
admission with its actual and permitted counts.

Candidate IDs bypass cooldown while retaining active-row, visibility, memory-type,
and tag predicates. Hydration preserves request order and fails the run before any
planner call when an ID is missing, hidden, outside scope, or removed by a supplied
filter; the error lists every rejected ID and its selector class. Streaming and
dry-run candidate enumeration apply the same tag predicates in PostgreSQL. Exact IDs
and tag filters compose as an intersection.

Admission compares complete normalized options. Equivalent selectors coalesce;
different IDs, order, or tag filters conflict. An aggregate tag-filtered run may
cover an equivalent project tag-filtered request. No aggregate run covers a request
with `candidate_ids`, and no unfiltered run covers a filtered request.

HTTP and MCP accept all four selectors. CLI adds repeatable `--candidate-id`,
`--tag-all`, `--tag-any`, and `--tag-none` options plus an explicit project/global
scope for candidate IDs. Invalid combinations produce typed/actionable diagnostics
before a run row or background task is created.

**Acceptance:**

- 1.1.1 - Exact IDs retain request order, bypass cooldown, compose with tag filters, and report every missing/hidden/foreign/filtered ID before planning. test: `tests/memory/test_dream.py::test_explicit_candidate_selector_is_ordered_and_fail_closed`.
- 1.1.2 - Normal, preview, and aggregate enumeration share `tags_all`/`tags_any`/`tags_none` semantics and exclude protected memories. test: `tests/memory/test_dream.py::test_tag_scoped_sweep_filters_every_candidate_path`.
- 1.1.3 - Admission coalesces only identical normalized selectors and rejects false coverage by unfiltered or aggregate runs. test: `tests/memory/test_dream.py::test_postgres_admission_does_not_cover_narrow_selectors`.
- 1.1.4 - MCP, HTTP, and CLI surfaces preserve selector lists and return actionable scope/limit errors without launching work. test: `tests/servers/routes/test_memory_routes.py::test_memory_dream_selector_contract`.

### 1.2 Add merge, abstraction, and split planner actions [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/generation_schemas.py`
- `src/gobby/install/shared/prompts/memory/dream.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate deterministic checksums for the changed bundled Dream prompt
- `src/gobby/memory/dream/models.py::*` — scope-reason: represent, serialize, and compute affected IDs for both simple and compound actions
- `src/gobby/memory/dream/plan.py::*` — scope-reason: validate discriminated action shapes, group overlap, grounding metadata, and confidence floors
- `src/gobby/memory/dream/planner.py::*` — scope-reason: keep complete origin groups on one bounded planner page and render eligible-cluster context
- `src/gobby/memory/dream/candidates.py::*` — scope-reason: select complete filtered origin groups ahead of ordinary per-memory pages
- `src/gobby/memory/dream/protocols.py::*` — scope-reason: expose origin-cluster selection through the internal manager protocol
- `src/gobby/storage/memories_dreams.py::*` — scope-reason: discover complete same-origin groups under the existing visibility and scope predicates
- `src/gobby/memory/dream/orchestrator.py::*` — scope-reason: route grouped work units and pass complete candidate state to planner validation
- `tests/memory/test_dream.py::*` — scope-reason: cover schemas, prompt rules, origin grouping, validation degradation, pagination, and summaries for compound actions

Replace the single permissive action item schema with a discriminated `oneOf`:

- Existing actions retain their current fields and semantics.
- `merge` requires `merge_mode`, non-empty `survivor_content`, unique
  `superseded_ids`, output `memory_type`, optional tags, non-empty rationale,
  reason, and confidence. `deduplicate` requires at least one superseded ID and
  may retain a presented candidate whose content exactly equals
  `survivor_content`. `abstract` requires 3–20 source IDs, `memory_type=pattern`,
  new content distinct from every source, and one complete eligible origin group.
- `split` requires one source `memory_id` and 2–8 ordered output objects. Every
  output has non-empty distinct content, canonical memory type, optional tags,
  and non-empty rationale. Output content may equal neither the source nor any
  active memory in the same scope.

Origin grouping prefers a shared non-null `source_task_id`; only rows with no task
ID may fall back to `source_session_id`. A group is eligible for abstraction when
all filtered active members are `fact`/`context`, same-scope, unprotected, count
3–20, and present together on one planner page. The oldest due member admits the
whole group into that work unit, including members still inside cooldown. If the
group exceeds the candidate or rendered-character limit, the planner receives
ordinary candidates and no abstraction eligibility marker.

Planner page construction treats one eligible origin group as an indivisible
item. Each page receives an `eligible_abstraction_clusters` block listing exact
candidate IDs and their shared origin key. The prompt permits `merge_mode=abstract`
only for a listed cluster; the resulting pattern must state only durable knowledge
supported across the sources and consistent with the supplied current-truth digest.
Partial truth digest absence never proves staleness or supports a new claim.

Duplicate merge requires semantically equivalent claims and preserves the strongest
current wording. Split requires independently useful outputs whose union preserves
the entire source without adding a claim. Every compound reason cites the source
relationship and the relevant truth evidence. The validator independently enforces
shape, candidate membership, same scope, group completeness, non-overlap, output
bounds, canonical memory types, and confidence
`>= max(min_action_confidence, min_delete_confidence)`.

Replace the one-action-per-candidate guard with group ownership: a candidate may be
owned by exactly one simple or compound action. A merge's presented existing
survivor is owned implicitly and receives no separate `keep`; all other omitted
candidates receive visible `keep`. Any malformed, overlapping, incomplete,
cross-scope, below-threshold, or ungrounded compound action degrades every referenced
candidate to `keep` with one stable diagnostic reason.

**Acceptance:**

- 1.2.1 - JSON schema and `DreamAction` round-trip every simple/merge/split shape while rejecting extra or action-incompatible fields. test: `tests/memory/test_dream.py::test_dream_actions_schema_discriminates_compound_actions`.
- 1.2.2 - Full 3–20 member origin clusters stay together on one planner page; partial, oversized, protected, preference, cross-scope, and explicitly filtered groups cannot abstract. test: `tests/memory/test_dream.py::test_abstraction_origin_cluster_must_be_complete_and_bounded`.
- 1.2.3 - Validation accepts grounded duplicate/abstract/split plans, applies the shared destructive confidence floor, assigns every candidate once, and visibly keeps every invalid group. test: `tests/memory/test_dream.py::test_compound_plan_validation_is_group_safe`.
- 1.2.4 - Prompt instructions require truth-grounded lossless abstraction/split rationales and retain every current delete/review/promote safety rule. test: `tests/memory/test_dream.py::test_dream_prompt_declares_compound_actions_and_truth_digest`.

## P2: Atomic Apply and Revert
`kind: framing`

**Goal:** Apply each validated compound action as one fenced transaction and make
every Dream-owned change safely reversible without deleting later user work.

### 2.1 Apply and revert journaled compound actions [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/storage/memories_base.py::*` — scope-reason: make listener registration and notification delivery obey one transaction-aware base contract
- `src/gobby/memory/dream/protocols.py::*` — scope-reason: expose the existing synchronous superseding-create primitive through a narrow Dream storage protocol
- `src/gobby/memory/dream/storage_actions.py::*` — scope-reason: add atomic multi-row source fences, compound creation/supersession, role-qualified snapshots, and conflict-aware reversal
- `src/gobby/memory/dream/storage_journal.py::*` — scope-reason: capture complete created/survivor/source rows and provide fenced created-row deletion
- `src/gobby/memory/dream/apply.py::*` — scope-reason: dispatch compound actions, count row mutations, preserve dry-run previews, reconcile, and reverse role-qualified snapshots
- `tests/memory/test_create_supersedes.py::*` — scope-reason: prove ambient transaction reuse, rollback, deterministic output identity, and post-commit listener timing
- `tests/memory/test_dream.py::*` — scope-reason: cover compound apply, crash-safe snapshots, stale fences, summaries, reconciliation, and conflict-aware revert

Add a synchronous compound method beside `apply_candidate_action`. It receives the
validated action, complete selected `DreamCandidate` states, the bound existing
`LocalMemoryManager.create_memory_with_outcome` callable, and an after-commit
notification. Inside one ambient database transaction it:

1. Locks every selected source ID in sorted order and verifies
   `dream_due_version`, `updated_at`, project, global flag, active visibility, and
   the action's exact scope. A mismatch returns no mutation and leaves the changed
   rows due.
2. Captures role-qualified source/survivor snapshots before mutation.
3. Calls the existing superseding create primitive. Merge performs one create;
   split creates its ordered outputs with no supersession, then creates the final
   output with `supersedes=[source_id]`. Ambient transaction reuse makes every
   nested call part of the same commit.
4. Rejects a split unless every output outcome is `created`. Merge may resolve to
   a new output or to the one presented active survivor; any other dedup target
   rolls back.
5. Captures a `before_data=null` snapshot for each created row and a before/after
   snapshot for an existing survivor. It then stamps all visible outputs with the
   current `last_dreamed_at`, preventing same-run reselection, and completes every
   snapshot before commit.
6. Reports row-level mutations: hidden sources plus created or materially changed
   outputs. Action counts remain one per planner action.

Make `MemoryStoreBase.notify_changed` delegate through `db.after_commit`, which
runs immediately outside a transaction and queues exactly once inside the ambient
compound transaction. A rollback therefore emits no listener event. Durable
embedding-change rows from the create primitive remain in the same transaction;
the existing sweep reconciliation converges Qdrant, cross-reference, and graph
projections after commit or on later repair.

Use snapshot action roles `merge-source`, `merge-survivor`, `merge-created`,
`split-source`, and `split-created`. Revert continues in descending snapshot order,
so outputs are handled before sources. Created-output reversal compares the complete
current row with `after_data` under `FOR UPDATE`; an unchanged row is hard-deleted,
while any later content, tag, rationale, type, provenance, scope, deletion, or
timestamp change produces a conflict and preserves the row. Existing survivors
restore only action-owned tags/cooldown state behind an after-state fence. Source
rows restore visibility and crossrefs, increment `dream_due_version`, and become due
for reevaluation. Failures retain `revert_failed`; conflicts remain explicit in the
successful partial-revert result, matching the existing revert contract.

Dry-run output includes full merge/split payloads in request order, with no rows,
snapshots, listeners, or secondary work changed. An expected compound failure records
one error detail for the action and advances cooldown only for still-matching sources;
an unexpected exception rolls back and keeps the run failure path unchanged.

**Acceptance:**

- 2.1.1 - Merge and split create/supersede all rows and complete every role-qualified snapshot in one transaction; injected failures leave no outputs, hidden sources, snapshots, embedding changes, or notifications. test: `tests/memory/test_dream.py::test_compound_apply_is_atomic_and_journaled`.
- 2.1.2 - Full selected-state fences prevent a planner result from hiding a source edited, moved, promoted, deleted, or re-due-marked after selection. test: `tests/memory/test_dream.py::test_compound_apply_rejects_any_stale_source`.
- 2.1.3 - Revert deletes unchanged Dream-created outputs, restores sources/existing survivors, preserves later-edited outputs as conflicts, and is idempotent on retry. test: `tests/memory/test_dream.py::test_compound_revert_is_created_row_conflict_aware`.
- 2.1.4 - Dry-run plans, action/mutation/snapshot summaries, post-commit listener timing, and secondary reconciliation remain accurate for mixed simple and compound pages. test: `tests/memory/test_dream.py::test_mixed_dream_page_reports_compound_outcomes`.

## V1: Plan Verification
`kind: verification`

- The plan has no filler test tasks; executable regressions live in the behavior
  deliverable that requires them.
- Dependencies are acyclic and order every shared target: `1.1 -> 1.2 -> 2.1`.
- All implementation sections are `category: code`, self-contained, and suitable
  for backend routing with TDD enabled when a manifest is later approved.
- Exact behavior covers selector empties/malformed IDs, missing/foreign rows,
  filtered aggregates, planner page bounds, invalid/overlapping groups, stale-row
  races, transaction rollback, created-row edit conflicts, dry-run, and mixed pages.
- No targeted production source is currently at or above 850 lines.
- Required implementation validation:
  `DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_dream.py tests/memory/test_dream_options.py tests/memory/test_dream_coordinator.py tests/memory/test_create_supersedes.py tests/mcp_proxy/tools/test_memory.py tests/servers/routes/test_memory_routes.py tests/cli/test_memory_cli.py -q`;
  focused Ruff and mypy on touched production paths; test-quality and test-types
  audits on touched Python tests; suppression ratchet; and committed bundled-manifest
  parity after regenerating the prompt checksum.

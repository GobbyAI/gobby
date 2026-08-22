# Review Protocol Diet

**Plan ID:** review-protocol-diet

## Overview
`kind: framing`

Reduce adversarial plan-review ceremony by removing persisted records whose
historical cost exceeds their demonstrated audit value. Keep the evidence
integrity core unchanged: immutable plan snapshots, exactly-once binding to the
reviewer run, and resumable approval/finalization checkpoints.

The implementation replaces per-candidate, per-sweep, and per-edge proof
objects with compact receipts derived and validated by the daemon. Canonical
findings remain the durable reviewer yield.

## Measurement Baseline
`kind: framing`

The baseline is a read-only aggregate of authoritative PostgreSQL rows for the
current project. It includes all 32 evidence rows finalized from
2026-07-26T17:12:59Z through 2026-07-27T15:41:41Z:

- `adversary-convergence-improvements`: 22 rounds, 125 findings.
- `herdr-terminal-client`: 5 rounds, 89 findings.
- `wiki-codewiki-restructure`: 5 rounds, 59 findings.

Two byte metrics are reported because they answer different questions:

- **Stored bytes** use PostgreSQL `pg_column_size`.
- **Wire bytes** use UTF-8 JSON text; canonical compact JSON removes
  serialization whitespace for shape-to-shape comparison.

The finalized sample contains:

| Measure | Total | Median per round | p95 per round | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Immutable snapshot, stored bytes | 7,165,402 | 210,225 | — | — |
| `round_result`, stored bytes | 222,593 | 6,467.5 | 14,569 | 23,994 |
| `round_result`, JSON text bytes | 456,687 | — | — | — |
| `round_result`, canonical compact bytes | 446,910 | — | — | — |
| Coverage attestation, stored bytes | 29,824 | 932 | 932 | 932 |
| Coverage attestation, canonical compact bytes | 24,592 | 768 | 771 | 772 |

Immutable snapshots are the dominant stored payload and are intentionally
retained. Existing coverage attestations already fit under 1 KiB when they lack
the newer record bundle.

The 32 finalized rows contain 476 reviewer candidates, aggregate disposition
counts of 267 emitted and 209 dismissed, and 273 persisted findings. The six
finding discrepancy proves that aggregate candidate disposition fields do not
currently reconcile to the canonical yield. All 32 finalized rows predate the
full persisted record-bundle schema and contain zero candidate-disposition,
cross-lane, adjacent-variant, or causal-repair records.

Rich repair ceremony is visible in expired attempts:

| Measure across expired attempts | Count | Stored bytes | Canonical compact bytes |
| --- | ---: | ---: | ---: |
| Repair attestations | 64 | 514,347 for attestation arrays | 752,538 |
| Per-edge `repair_bundle_interactions` | 746 | included above | included above |
| Non-null deviation proof objects | 8 | included above | 9,228 |
| Rows with `prior_round_context` | 6 | 1,870,187 | 4,580,550 JSON text bytes |

Each rich repair attestation carries 13 fields and each graph edge carries its
own disposition and validation prose. This payload scales with graph edges
rather than repaired findings.

Reviewer-agent effort is measurable by joining finalized evidence
`dispatch_run_id` values to `agent_runs`:

- 54,813.293297 wall-clock seconds, or 15.2259 reviewer-agent hours.
- Median 1,582.7896 seconds per round; p95 2,424.2823 seconds.
- 3,570 tool calls; median 107.5 per round.
- 261 turns; median 7.5 per round.

The 16 expired attempts bound to runs account for another 41,440.420553
seconds, or 11.5112 agent hours: 11 successful runs that were never finalized,
2 cancellations, 2 errors, and 1 timeout. Six additional expired evidence rows
were never bound. Preparation refusals before evidence insertion, coordinator
time, and fixer time are absent from current durable telemetry and must remain
reported as unavailable until instrumented.

## Decision Record
`kind: framing`

| Protocol element | Decision | Replacement | Budget |
| --- | --- | --- | --- |
| Candidate, cross-lane, adjacent-variant, and causal sweep records | Cut persisted records and their MCP schema | Daemon validates lane completion, source hashes, canonical findings, and aggregate yield while the result is in memory | Zero persisted sweep-record objects |
| `repair_bundle_interactions` | Cut per-edge submissions and persistence | Daemon derives one typed `SweepScope` during preparation and carries its digest plus scope delta to the reviewer | Zero persisted per-edge records; receipt size is independent of graph-edge count |
| `deviation_from_minimal_repair` | Cut the five-field prose object | Explicit repair/carry resolution plus actual section-hash changes and validation evidence | Zero deviation proof objects |
| Coverage attestation | Slim to a version-2 coverage receipt | Three completed lane summaries, source digest, aggregate yield counts, completion flags, shadow-manifest status, and receipt digest | Canonical compact receipt and PostgreSQL stored size each remain at or below 1,024 bytes per round |

Reviewer-dismissed candidates stop entering the durable quality ledger.
Emitted non-blocking findings, user-visible quality decisions, and explicit
carry resolutions remain durable. Internal candidate brainstorming is no
longer treated as an audit artifact.

The version-2 contract is the sole accepted submission contract. Historical
finalized and expired round results remain immutable audit payloads and are
decoded only by the metrics report. Runtime preparation, validation, and
finalization have no version-1 fallback.

## Constraints
`kind: framing`

- Preserve `snapshot`, `plan_hash`, and `section_manifest` immutability.
- Preserve one-time `dispatch_run_id` binding and fresh evidence after
  spawn/bind failure, timeout, or source drift.
- Preserve manifest intent, V1 checkpoint, finalization, and lesson-mint
  recovery semantics and idempotence.
- Preparation performs one repository index pass and one daemon-owned
  `SweepScope` derivation. Coordinators do not submit a duplicate graph.
- A repair receipt scales as `O(repaired findings + changed sections)`.
- A repair resolution must identify at least one genuinely hash-changed
  section and at least one validation-evidence reference.
- Metrics distinguish stored, JSON-text, and canonical compact bytes.
- Historical analytics may recognize legacy keys for measurement; public tools
  and runtime validators accept only the new contract.
- Structured metrics contain identifiers, counts, sizes, durations, and error
  codes. They exclude plan text, finding prose, snapshots, connection strings,
  and repository content.
- Coordinator and fixer effort remain explicitly unavailable until those
  lifecycles gain authoritative trace spans.
- Review lane count, reviewer model selection, finding semantics, enhancement
  workflow, and build handoff are outside this implementation.

## P1: Replace Persisted Ceremony with Receipts
`kind: framing`

**Goal**: Make coverage and repair evidence proportional to durable review
outcomes while retaining server-verifiable integrity.

### 1.1 Replace sweep record bundles with a compact coverage receipt [category: code]
`kind: deliverable`

Targets:
- `src/gobby/plans/review_coverage.py`
- `src/gobby/plans/review_sweeps.py`
- `src/gobby/plans/review_ledger.py`
- `src/gobby/plans/review_evidence.py`
- `src/gobby/plans/review_evidence_models.py`
- `src/gobby/mcp_proxy/tools/plans/review_evidence.py`
- `src/gobby/mcp_proxy/tools/plans/review_evidence_schemas.py`
- `tests/plans/test_review_coverage.py`
- `tests/plans/test_review_ledger.py`
- `tests/plans/test_review_evidence_models.py`
- `tests/mcp_proxy/test_review_evidence_schemas.py`
- `tests/review_coverage_helpers.py`

Replace the version-1 attestation and `record_bundle` with this exact
version-2 receipt shape:

```json
{
  "version": 2,
  "evidence_id": "<uuid>",
  "lanes": [
    {
      "lane_id": "requirements_traceability",
      "status": "completed",
      "candidate_count": 0
    },
    {
      "lane_id": "repository_blast_radius",
      "status": "completed",
      "candidate_count": 0
    },
    {
      "lane_id": "runtime_invariants",
      "status": "completed",
      "candidate_count": 0
    }
  ],
  "source_digest": "<sha256>",
  "yield_counts": {
    "candidate_count": 0,
    "finding_count": 0
  },
  "checks_complete": {
    "cross_lane_interaction": true,
    "adjacent_variant": true
  },
  "shadow_manifest_status": {},
  "receipt_digest": "<sha256>"
}
```

`validate_plan_review_coverage` receives canonical `findings` with the lane
results and derives both yield counts. It continues to validate the exact
three-lane order, completed status, source citations against current file
hashes, expected shadow-manifest result, approval manifest validity, and the
receipt digest. Candidate and finding counts are independent measures; the
validator does not invent a one-candidate-to-one-finding mapping.

Remove candidate-disposition, cross-lane, adjacent-variant, and causal-sweep
schemas from the MCP input. Remove `review_sweeps.py` after moving any remaining
small source-hash or prior-context helper to its owning module and proving it
has no consumers.

Change quality-ledger merging to consume canonical findings and carry
resolutions directly. Keep emitted non-blocking findings and user-visible
quality decisions. Remove dismissed-candidate aliases, rationales, reinjection,
and reopen checks that depend on a record bundle.

**Acceptance:**

- 1.1.1 - Version-2 validation returns the exact compact receipt and rejects missing lanes, stale source hashes, false completion flags, invalid shadow manifests, count mismatches, and digest mismatches. symbol: `gobby.plans.review_coverage.validate_review_coverage`.
- 1.1.2 - Public schemas and round-result validation reject `record_bundle`, candidate dispositions, sweep records, and version-1 submissions. file: `src/gobby/mcp_proxy/tools/plans/review_evidence_schemas.py`.
- 1.1.3 - Quality-ledger state is derived from canonical findings and carry resolutions without persisted dismissed-candidate entries. test: `tests/plans/test_review_ledger.py`.
- 1.1.4 - Focused receipt tests assert canonical compact bytes and PostgreSQL stored bytes are each at most 1,024 per round. test: `tests/plans/test_review_coverage.py`.

### 1.2 Replace repair attestations with daemon-derived repair receipts [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/plans/review_repair.py`
- `src/gobby/plans/review_sweep_scope.py`
- `src/gobby/plans/review_evidence_preparation.py`
- `src/gobby/plans/review_evidence.py`
- `src/gobby/plans/review_evidence_models.py`
- `src/gobby/plans/review_evidence_store.py`
- `src/gobby/mcp_proxy/tools/plans/review_evidence.py`
- `src/gobby/mcp_proxy/tools/plans/review_evidence_schemas.py`
- `src/gobby/storage/migrations/349_review_protocol_diet.sql`
- `src/gobby/storage/postgres_baseline_schema.sql`
- `tests/plans/test_review_repair.py`
- `tests/plans/test_review_evidence_store.py`
- `tests/storage/test_plan_review_protocol_diet_migration.py`

Replace `repair_attestations` with `repair_receipts`. The coordinator submits
one minimal input per `repair` resolution:

```json
{
  "prior_finding_id": "<finding-id>",
  "changed_section_ids": ["1.1"],
  "validation_evidence": ["<command or durable evidence reference>"]
}
```

Preparation resolves `check_key` from the finalized prior finding, proves each
claimed section is in the real snapshot hash diff, and persists this canonical
receipt:

```json
{
  "prior_finding_id": "<finding-id>",
  "check_key": "<prior check key>",
  "section_hash_changes": [
    {
      "section_id": "1.1",
      "before_hash": "<sha256-or-null>",
      "after_hash": "<sha256-or-null>"
    }
  ],
  "validation_evidence": ["<command or durable evidence reference>"],
  "sweep_scope_digest": "<daemon-derived sha256>"
}
```

Nullable before/after hashes represent added or removed sections; the pair
must differ. Reject duplicate receipts, missing repair receipts, receipts for
carry decisions, unknown findings, stale check keys, empty validation evidence,
unchanged sections, and sections outside the actual diff.

Derive `SweepScope` once inside preparation from the accepted repair IDs and
actual changed sections. Keep its typed nodes, edges, digest,
`required_scope_delta`, and `inventory_churn` as daemon-owned reviewer context.
Remove the coordinator-facing `derive_plan_review_sweep_scope` step and remove
submitted sweep graphs. No receipt stores changed-symbol lists, consumer lists,
adjacent-variant lists, deferrals, query prose, per-edge interactions,
accepted-resolution prose, or deviation proof objects.

Migration 349 renames the database column to `repair_receipts`, updates the
baseline schema, and normalizes existing arrays plus nested
`prior_round_context` repair data in one transaction. It joins
`prior_evidence_id` to the prior section manifest to construct before/after
hash pairs. It fails explicitly when a live row cannot be normalized; it never
silently discards a recoverable checkpoint. Finalized and expired
`round_result` JSON remains byte-for-byte historical audit data.

**Acceptance:**

- 1.2.1 - Preparation accepts only one minimal receipt per repair decision and emits the exact server-enriched canonical shape. symbol: `gobby.plans.review_repair.validate_repair_preparation`.
- 1.2.2 - Sweep scope and digest are derived once by the daemon and malformed or hand-shrunk coordinator graphs are absent from the public preparation API. symbol: `gobby.plans.review_evidence_preparation.prepare_review_round_context`.
- 1.2.3 - Persisted repair evidence contains zero `repair_bundle_interactions` and zero `deviation_from_minimal_repair` objects and grows only with repaired findings and changed sections. test: `tests/plans/test_review_repair.py`.
- 1.2.4 - Migration 349 preserves valid live checkpoints, normalizes repair data idempotently, and aborts on an unprovable live receipt. test: `tests/storage/test_plan_review_protocol_diet_migration.py`.

## P2: Update Orchestration and Observability
`kind: framing`

**Goal**: Remove the old ceremony from every caller and make the remaining
cost visible from authoritative data.

### 2.1 Update review coordinators, agent contracts, and bundled content [category: config] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `src/gobby/install/shared/skills/plan-review/SKILL.md`
- `src/gobby/install/shared/workflows/agents/planner.yaml`
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`
- `src/gobby/plans/review_terminal.py`
- `src/gobby/storage/tasks/_stage_states.py`
- `src/gobby/storage/tasks/_review_transitions.py`
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`
- `src/gobby/install/bundled_content_manifest.json`
- `tests/skills/test_plan_review_skill.py`
- `tests/agents/test_plan_adversary_internal_research_definition.py`
- `tests/storage/test_stage_review_findings.py`

Rewrite interactive, autonomous, staged, and terminal review instructions to
submit canonical findings, three lane results, completion flags, repair/carry
resolutions, changed section IDs, and validation evidence. Remove every
instruction and schema reference to record bundles, per-edge interactions,
deviation proofs, changed-symbol inventories, consumer/variant lists, and the
separate sweep-scope tool call.

Keep the current launch/bind/compact/wait sequence, timeout/source-drift expiry,
immutable requirements bundle, user voting, manifest apply, V1 checkpoint,
finalization, and lesson-mint recovery order. Regenerate bundled content with
the repository command used by existing bundled-content tests.

Update staged-review repair submission encoding and terminal review surfaces
to the minimal receipt input. One canonical payload shape must cross
interactive and staged review; no adapter accepts the removed fields.

**Acceptance:**

- 2.1.1 - Installed plan and plan-review skills describe the version-2 coverage receipt and minimal repair receipt without removed ceremony fields. file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 2.1.2 - Taskless, taskful, staged, and terminal review paths use the same receipt contract and preserve launch, binding, timeout, and checkpoint recovery order. test: `tests/storage/test_stage_review_findings.py`.
- 2.1.3 - Bundled-content hashes are regenerated and content-installation tests detect drift. file: `src/gobby/install/bundled_content_manifest.json`.

### 2.2 Record and report review-protocol cost [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/plans/review_telemetry.py`
- `src/gobby/mcp_proxy/metrics_events.py`
- `src/gobby/cli/plans.py`
- `tests/plans/test_review_telemetry.py`
- `tests/mcp_proxy/test_metrics_events.py`
- `tests/cli/test_plans.py`

Use the existing `MetricsEventStore` for preparation events. Record
`event_type=plan_review_protocol` with names `prepare_succeeded` and
`prepare_refused`. Store project/session identifiers in their existing
columns. Metadata contains only round number, refusal stage, error code,
repair count, changed-section count, and submitted/canonical byte counts.

Add a JSON-capable review-protocol metrics command under `gobby plans`. For a
bounded time range and project, report:

- finalized, expired-bound, and expired-unbound round counts;
- coverage-receipt stored and canonical compact total/median/p95/maximum bytes;
- repair-receipt counts and bytes;
- persisted sweep-record, edge-record, and deviation-proof counts;
- preparation refusals grouped by deterministic error code;
- reviewer-agent wall time, tool calls, and turns by joining
  `dispatch_run_id` to `agent_runs`;
- explicit unavailable markers for coordinator and fixer effort.

The report may decode named legacy keys to measure historical rows. It does not
route legacy payloads back into validators. A default finalized-only view must
reproduce the 32-round baseline within serialization-metric definitions.

**Acceptance:**

- 2.2.1 - Preparation success and every deterministic refusal path produce a queryable redacted metrics event, including refusals before evidence insertion. test: `tests/mcp_proxy/test_metrics_events.py`.
- 2.2.2 - The metrics report joins bound agent runs, separates stored from wire bytes, groups refusal codes, and marks untraced coordinator/fixer effort unavailable. symbol: `gobby.plans.review_telemetry.derive_review_protocol_metrics`.
- 2.2.3 - The finalized-history fixture reproduces 32 rounds, 15.2259 reviewer-agent hours, 3,570 tool calls, 261 turns, zero finalized sweep/edge/deviation records, and the documented coverage byte distribution. test: `tests/plans/test_review_telemetry.py`.

## P3: Prove Cutover and Recovery
`kind: framing`

**Goal**: Lock the diet budgets and evidence-integrity invariants into one
end-to-end regression surface.

### 3.1 Add protocol-diet cutover and recovery regression coverage [category: test] (depends: P2)
`kind: deliverable`

Targets:
- `tests/plans/test_review_protocol_diet_e2e.py`
- `tests/plans/test_repair_gate_e2e.py`
- `tests/plans/test_review_evidence.py`
- `tests/plans/test_review_evidence_store.py`

Exercise a rejection round, repair preparation, bound reviewer completion,
approval manifest intent, V1 checkpoint persistence, finalization, and lesson
mint checkpoint using only version-2 receipts. Inject crashes after bind, after
manifest intent, after V1 checkpoint, and after finalization. Resume each state
through the existing recovery drain.

Assert snapshots, hashes, section manifests, run binding, and durable approval
payloads are unchanged across recovery. Assert repeated operations remain
idempotent. Assert new persisted evidence contains no removed keys and meets
the 1 KiB coverage-receipt budget. Generate a large `SweepScope` fixture and
prove repair receipt size is unchanged when edge count increases while
repaired-finding and changed-section counts stay fixed.

Run the focused review-evidence, repair, telemetry, MCP-schema, staged-review,
skill, migration, and new end-to-end test files. Run Ruff on touched Python
files and validate this plan artifact.

**Acceptance:**

- 3.1.1 - Crash recovery preserves immutable snapshots, exactly-once run binding, manifest intent, V1 checkpoint, finalization, and lesson-mint state. test: `tests/plans/test_review_protocol_diet_e2e.py`.
- 3.1.2 - Version-2 end-to-end rows contain zero sweep records, zero per-edge records, zero deviation proofs, and coverage receipts no larger than 1,024 stored or canonical compact bytes. test: `tests/plans/test_review_protocol_diet_e2e.py`.
- 3.1.3 - Repair receipt size is independent of `SweepScope` edge count and grows only with repaired findings and changed sections. test: `tests/plans/test_repair_gate_e2e.py`.
- 3.1.4 - Focused tests, Ruff checks, migration tests, and plan validation pass from a clean task-owned diff. behavior: "review protocol diet verification gate" in `tests/plans/test_review_protocol_diet_e2e.py`.

## V1 Plan Changelog
`kind: verification`

Initial validated draft. No enhancement or adversarial-review round is part of
task #19244.

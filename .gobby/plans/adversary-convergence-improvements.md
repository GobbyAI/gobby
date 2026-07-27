# Plan-Adversary Convergence & Orchestration Improvements

**Plan ID:** adversary-convergence-improvements

## Overview
`kind: framing`

`docs/research/plan-adversary-improvements.md` (task #18957) and
`docs/plans/adversary-convergence.md` (task #18960) diagnose why interactive
plan review takes too many rounds and too much wall time. Two independent
evidence bases agree on the cause: in the 25-round corpus (6 plans, 109
findings) every finding is `severity: blocking`, and one plan ran 22 rounds
without approving on a 1–3 finding floor; in the post-restart cohort
(`#9640`–`#9644`), 15 of 25 round-2 blockers were fixer-induced or incomplete
repairs, 10 were round-1 reviewer misses, and zero came from new user scope.
Live corroboration from session `#9602` shows the fixer-induced share climbing
as totals fall — 0/17 → 7/12 → 5/7 (71%) — and three consecutive rounds ending
with no canonical `round_result`.

The intended outcome is fewer avoidable rounds (measured by repeated
`check_key`s and fixer-induced findings, not raw round count) and lower
per-round wall time, without weakening review rigor. The 71% figure sets the
priority: the convergence tail is now almost entirely self-inflicted, so the
repair-proof gate (P2) carries most of the expected value.

## Constraints
`kind: framing`

Decisions already taken: scope is convergence **and** orchestration; the full
P0 atomic finalizer is deferred (file as its own task at build start — only the
narrow terminal-completion guard is promoted, section 3.1); severity model is
trace gate **with carry-forward ledger** — nothing is discarded.

Constraints discovered during exploration (all verified against source):

1. **There is no lessons table.** Review lessons are project memories whose
   entire schema lives in tags (`src/gobby/review_learning/lessons.py:244-282`);
   recall pages every matching memory (`class_recall.py:234-257`); minting
   requires `severity == "blocking"` (`round_diff.py:53`). The quality ledger
   must not reuse it — ledger entries go on `plan_review_evidence` as JSONB.
2. **The code index cannot answer "who implements protocol P".** Exactly three
   edge labels exist — `DEFINES`, `IMPORTS`, `CALLS`
   (`crates/gcode/src/graph/code_graph/read/payload_queries.rs`). Protocol
   implementors stay a prompt-level obligation.
3. **A global lane admission budget is not implementable.** Native lane
   subagents create no `agent_runs` row; `count_active_agents`
   (`src/gobby/dispatch/agent_counts.py:8-31`) and `MAX_ACTIVE_AGENTS`
   (`dispatch/constants.py:5`) are structurally blind to them; Codex emits no
   subagent events (`adapters/acp_hook_adapter.py:19`).
4. **No code-index generation counter exists.** Reads check freshness via a
   lock-free pre-gate (`crates/gcode/src/freshness.rs:95-123`) and reindex
   under an advisory try-lock when tripped. Pin by protocol, not by schema.
5. **Files at or near the 1000-line limit take no new code:**
   `src/gobby/plans/review_evidence.py` (966), `src/gobby/plans/coverage.py`
   (984), `src/gobby/storage/tasks/_transitions.py` (977), and
   `src/gobby/agents/agent_cleanup.py` (980, on the terminal path 7.2 touches).
   Refactor tasks for all four must be filed at build start (Guiding
   Principle 2). New code goes in new modules or files with headroom;
   `agent_cleanup.py` keeps delegation only.
6. **Latent adjacent bug:** `spawn_executor.py:44` strips `Task` from
   Gobby-managed Claude agents, so a provider override to `claude` would
   silently kill all three native lanes. Fix while adjacent (section 6.3).

Out of scope, with reasons: full P0 atomic finalizer (deferred by decision);
global lane admission budget (constraint 3); `index_generation` counter in
gcode (6.1 pins by protocol); protocol-implementor index queries
(constraint 2); bounding native lane duration (Gobby cannot observe or kill
provider-internal subagents); step-state loss across compaction and
daemon-stop resume (tracked as open tasks #18973 / #18974, deliberately
standalone — if a review round again ends without a canonical result, check
the reviewer session for a compaction event before blaming the agent
definition, and escalate 3.1 to the full P0 finalizer if it reproduces
without one).

Success metric: fixer-induced and repeated-`check_key` findings per round, not
raw round count — raw round count alone would reward weaker review. Calls per
finding is a dryness signal, never an efficiency target.

## P1: Finding severity and failure traces
`kind: framing`

**Goal**: Severity carries information, and `blocking` costs a structural
proof.

### 1.1 Extend finding severities and require failure traces on blocking [category: code]
`kind: deliverable`

Target: `src/gobby/plans/review_findings.py`, `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `tests/review_learning/test_round_diff.py`, `tests/storage/test_stage_review_findings.py`

Today `FINDING_SEVERITIES = frozenset({"blocking", "nit"})`
(`review_findings.py:14`). `nit` reads as cosmetic, so every substantive
observation becomes `blocking`, and approval requires an unbounded generator to
emit exactly zero critiques — an exit condition that stayed unreachable for 22
rounds on one plan.

Changes (the file is 193 lines; ample headroom):

- Extend `FINDING_SEVERITIES` to `{"blocking", "major", "minor", "nit"}`.
- Add an optional `failure_trace` object field to `_ALLOWED_FIELDS` (line
  39-46) with required sub-fields `preconditions`, `action`, `wrong_outcome`,
  `violated_obligation` (all non-empty strings) and `citation` reusing the
  `_citation_list` shape from `review_coverage.py:457-498` (path, lowercase
  sha256, optional line_start/line_end).
- In `_validate_finding` (117-158), reject `severity: blocking` when
  `failure_trace` is missing or malformed. Follow the existing all-or-nothing
  pattern used for the causal triple at lines 149-153.
- Render the invalid-severity diagnostic from `FINDING_SEVERITIES` instead of
  its current hard-coded two-value text, so the message can never lie about
  the vocabulary.

Every producer of a finding must speak one vocabulary, or wiring the canonical
validator into the interactive path (4.3) rejects the payloads it is meant to
gate. Two parity defects exist today and are fixed here:

- The validator's `_REQUIRED_STRING_FIELDS` names `fix`
  (`review_findings.py:26-36`) while the taskless reviewer emits
  `suggested_fix`, so a compliant reviewer payload fails on both an unknown
  field and a missing one. Collapse the two into a single canonical remedy
  field, `minimal_repair` (7.1 defines its semantics), and update the
  validator, its renderer, and the taskless agent's result contract together.
  No aliasing — one name.
- `_stage_review.py:609` hard-codes `"enum": ["blocking", "nit"]` for the
  stage-native producer, which would reject `major`/`minor` the moment the
  shared skill teaches four tiers. Extend that enum in the same change.

The rename has no alias, so every literal finding producer must migrate in
this deliverable or its suite breaks. Sweep them all: the in-repo fixtures
`tests/review_learning/test_round_diff.py:51` and
`tests/storage/test_stage_review_findings.py:148,161` construct findings with
`"fix"` today, and blocking fixtures additionally need well-formed
`failure_trace` data once 1.1's gate lands. `suggested_fix` survives only on
lane candidates.

The normative decision boundary between the four labels (what makes an
observation `blocking` versus `major` versus `minor` versus `nit`) is defined
once, in the shared severity matrix that 4.3 adds to the plan-review skill and
contract — reviewers never improvise the boundary per round.

**Acceptance:**

- 1.1.1 - `FINDING_SEVERITIES` contains exactly blocking, major, minor, nit. symbol: `gobby.plans.review_findings.FINDING_SEVERITIES`.
- 1.1.2 - A `blocking` finding without a well-formed `failure_trace` is rejected with a diagnostic naming the missing sub-field. test: `tests/plans/test_review_findings.py::test_blocking_requires_failure_trace`.
- 1.1.3 - `major` and `minor` findings validate without a `failure_trace`, and a malformed trace on any severity is rejected all-or-nothing. test: `tests/plans/test_review_findings.py::test_failure_trace_all_or_nothing`.
- 1.1.4 - The invalid-severity diagnostic enumerates the four-value vocabulary derived from `FINDING_SEVERITIES`. test: `tests/plans/test_review_findings.py::test_invalid_severity_diagnostic_derives_from_constant`.
- 1.1.5 - `minimal_repair` is the single canonical remedy field on **findings** across the validator, its renderer, and the taskless result contract; no `fix` or `suggested_fix` key remains on a finding. Lane candidates keep their own `suggested_fix` field, which this rename does not touch. test: `tests/plans/test_review_findings.py::test_single_canonical_remedy_field`.
- 1.1.6 - The stage-native review schema accepts all four severities, and a payload emitted verbatim by the taskless contract validates. test: `tests/mcp_proxy/test_stage_review_schema.py::test_severity_enum_parity_with_findings`.
- 1.1.7 - Every in-repo literal finding producer builds `minimal_repair` with valid `failure_trace` on blocking fixtures, and no test constructs a finding with `fix`. test: `tests/review_learning/test_round_diff.py::test_findings_use_canonical_remedy_field`.

## P2: Repair-proof gate
`kind: framing`

**Goal**: Round N+1 cannot start until every accepted round-N finding carries a
proof of repair; the next round routes through causal surfaces first.

### 2.1 Migration 343: quality ledger and repair attestation columns [category: code] (depends: P1)
`kind: deliverable`

Targets: `src/gobby/storage/migrations/343_plan_review_quality_ledger.sql`, `src/gobby/storage/postgres_baseline_schema.sql`, `src/gobby/plans/review_evidence_models.py`, `src/gobby/plans/review_evidence_store.py`

One migration carries all three columns (they land in different steps but
share the schema change): add `quality_ledger JSONB`, `repair_attestations
JSONB` (both with a `jsonb_typeof(...) = 'array'` CHECK mirroring
`plan_review_evidence_manifest_array`, `postgres_baseline_schema.sql:544-545`)
and `prior_round_context JSONB` (object CHECK). 342 is the highest existing
migration; 343 is free. Update the baseline schema alongside the migration.

`prior_round_context` is the durable home for everything the next round's
snapshot must reconstruct without re-reading mutable live state: the causal
context (2.4), the typed consumer-site inventory and index token (5.2), and
the requirements bundle (6.5). Preparation derives and persists all of it
inside the same transaction that writes the evidence row, so a restart cannot
leave a half-populated context and a caller cannot resubmit it. The context
exists only after a successful preparation — an unavailable index aborts the
whole transaction (5.2.4) rather than recording its own failure, so no
partial or "unavailable" context state is ever readable.

A column is complete only with its full lifecycle, so this deliverable also
owns the typed model and store IO for all three columns: `PlanReviewEvidence`
(`review_evidence_models.py:73`) gains the fields, and
`PlanReviewEvidenceStore` (`review_evidence_store.py:13`) gains idempotent
write paths and deserializing read paths. Write transitions are pinned here
and consumed later: preparation writes attestations and context (2.2, 5.2,
6.5), finalization writes the merged ledger (4.1), and the snapshot reader
deserializes them (2.4, 4.2).

**Acceptance:**

- 2.1.1 - Migration adds all three JSONB columns with CHECK constraints. file: `src/gobby/storage/migrations/343_plan_review_quality_ledger.sql`.
- 2.1.2 - Baseline schema carries the same columns and constraints. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 2.1.3 - Typed model and store round-trip all three columns: written values deserialize identically through the store's read paths, and rewriting is idempotent. test: `tests/plans/test_review_evidence_store.py::test_evidence_jsonb_columns_round_trip`.

### 2.2 Repair attestation gate in round preparation [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/plans/review_repair.py` (new module), `src/gobby/install/shared/skills/plan/SKILL.md`, `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`, `src/gobby/storage/tasks/_transitions_facade.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/storage/tasks/_stage_states.py`, `src/gobby/dispatch/spawn.py`, `src/gobby/install/shared/workflows/agents/planner.yaml`

A mandatory input needs a production producer, or the gate refuses every real
round 2 while unit fixtures pass. Neither coordinator builds these records
today: the taskless plan skill presents votes, revises, and loops straight back
to preparation, and `prepare_plan_review_round` accepts no attestation
parameter at all. So this deliverable owns both the validator **and** the two
producers. The taskless coordinator constructs one resolution record per prior
finding from the recorded vote, and an attestation per `repair` decision from
the actual edit — changed sections and symbols, consumer and adjacent sweeps,
validation evidence, deferred sites, and any deviation.

The staged path needs a durable carrier and a real producer, not just an
instruction: `submit_for_review` conveys only `review_notes`
(`_stage_review.py:213-281`), the public stage-state submit seam accepts only
those notes while the private transition can already persist artifact updates
(`_stage_states.py:172-190,339-372`), and `planner.yaml:90-106,180-204` — the
agent that actually revises after a rejection — is told to resubmit notes and
nothing else. So a resubmission's repair records have neither an author nor
anywhere to live, and a restart before dispatch loses them. `planner.yaml` is
therefore an owned surface here: it constructs the typed payload at the real
submit call, which threads through the public stage-state method into the
existing artifact-update seam as one atomic write.
The staged producer therefore persists **one typed round-bound submission
payload** into the existing stage state at resubmission, and preparation loads
and consumes it idempotently. This crosses the same fixed-signature chain 4.3's
ledger does — wrapper → `_transitions_facade.py` → `_transitions.py` — plus the
dispatcher that calls preparation. No new store and no new transport: the
payload rides stage state that already survives restarts. Acceptance enters
through the real coordinator path, never injected fixtures.

The evidence snapshot carries `changed_section_ids` and nothing about why they
changed (`review_evidence.py:185-199`). Extend `prepare_plan_review_round`
(service `src/gobby/plans/review_evidence.py:49-177`, MCP wrapper
`src/gobby/mcp_proxy/tools/plans/review_evidence.py:29-64`) to accept
`repair_attestations: [...]`, one per accepted prior finding, with fields:
`prior_finding_id`, `check_key`, `changed_section_ids`, `accepted_resolution`,
`deviation_from_minimal_repair` (nullable), `changed_symbols`,
`consumer_sites_swept`, `adjacent_variants_swept`, `validation_evidence`,
`deferred_sites`.

Preparation also accepts a prior-finding resolution record per finding with
`decision: repair | carry`. The required resolution universe is derived
server-side from the canonical prior `round_result` persisted on the
finalized evidence row — never from the caller's submitted list: exactly one
resolution record per prior finding, and a missing record for any prior
finding refuses preparation naming it (an omitted record cannot erase a
finding from the required set). `carry` is permitted only for non-blocking
findings and routes the finding directly into the quality ledger (4.1); every
`repair` decision defines the required attestation set. The required
repair-ID set is computed server-side from those resolution records — exactly
one attestation per repair ID; duplicate or unknown IDs and check-key
mismatches are rejected.

Three remedy vocabularies stay distinct and must not be collapsed into each
other: a lane **candidate** carries `suggested_fix` (its own schema, untouched
by 1.1), a **finding** carries `minimal_repair` (the canonical remedy after
1.1), and an **attestation** carries `deviation_from_minimal_repair` naming
how the applied repair departed from the finding's `minimal_repair`. The
deviation gate reads the finding's `minimal_repair` as its comparison source.

Validation lives in the new `review_repair.py` module (constraint 5: no new
code in `review_evidence.py`). Refuse round N+1 preparation when any
repair-decided finding from round N lacks an attestation, or when an
attestation's claimed `changed_section_ids` is not a non-empty subset of the
actual hash-diff computed by `_changed_sections_since_prior_round`
(`review_evidence.py:742-765`). Persist attestations to the
`repair_attestations` column from 2.1.

**Acceptance:**

- 2.2.1 - Attestation validation module exists with the ten-field record shape. file: `src/gobby/plans/review_repair.py`.
- 2.2.2 - Preparation refuses when a repair-decided prior finding lacks an attestation. test: `tests/plans/test_review_repair.py::test_unattested_finding_refuses_preparation`.
- 2.2.3 - Preparation refuses when a claimed `changed_section_ids` is not a non-empty subset of the real hash-diff, and rejects duplicate, unknown, or check-key-mismatched attestations. test: `tests/plans/test_review_repair.py::test_attestation_must_match_hash_diff`.
- 2.2.4 - Resolution records with `decision: carry` are accepted only for non-blocking findings and route into the ledger; a mixed repair/carry preparation succeeds with exactly the repair set attested. test: `tests/plans/test_review_repair.py::test_mixed_repair_carry_preparation`.
- 2.2.5 - The resolution universe is loaded from the finalized prior round server-side; omitting a resolution record for any prior finding refuses preparation naming that finding. test: `tests/plans/test_review_repair.py::test_omitted_resolution_record_refuses`.
- 2.2.6 - One candidate's `suggested_fix` flows into a finding's `minimal_repair` and then into an attestation's `deviation_from_minimal_repair` without field collision, and each schema rejects the other two's remedy key. test: `tests/plans/test_review_repair.py::test_remedy_vocabulary_round_trip`.
- 2.2.7 - The taskless producer builds resolution records and attestations from recorded votes and a supplied edit diff, and the wrapper schema transports them unchanged; an incomplete record set is refused naming what is missing. test: `tests/plans/test_review_repair.py::test_taskless_producer_builds_records`.
- 2.2.8 - The staged producer serializes one typed round-bound submission payload and consumes it idempotently from injected stage state, so a replayed consumption yields the same records exactly once. test: `tests/plans/test_review_repair.py::test_staged_submission_payload_round_trip`.

### 2.3 Deviation proof [category: code] (depends: 2.2)
`kind: deliverable`

Target: `src/gobby/plans/review_repair.py`

When `deviation_from_minimal_repair` is non-null, additionally require: the
violated invariant restated, the original counterexample, how the
alternative closes it, plus validation evidence that the original
counterexample no longer reproduces under the alternative, and an
`accepted_risk` field naming any new risk the deviation introduces (explicit
`none` when there is none — silence is not acceptance). The gate demands
proof and never forbids deviation — `#9640` proved all three of its
deviations wrong, but `#9602` shipped two deviations that were better than
the reviewer's proposal; the coordinator being right is the common case.

Those five fields are one **closed, named object**, not prose: taskless
findings, staged findings, the renderer, and coverage validation would
otherwise each satisfy this paragraph with a different shape, leaving the
mandatory escalation evidence non-interoperable end to end. Pin
`deviation_from_minimal_repair` to exactly `violated_invariant`,
`original_counterexample`, `how_alternative_closes_it`, `validation_evidence`,
and `accepted_risk` — all non-empty strings, `accepted_risk` accepting the
literal `none` — and reuse that one schema unchanged across every producer,
validator, and renderer, with a parity test asserting they agree.

**Acceptance:**

- 2.3.1 - A deviating attestation without the three proof fields is rejected; one with them passes. test: `tests/plans/test_review_repair.py::test_deviation_requires_proof`.
- 2.3.2 - A deviation without counterexample validation evidence or without an explicit `accepted_risk` value is rejected. test: `tests/plans/test_review_repair.py::test_deviation_counterexample_and_risk`.
- 2.3.3 - One named five-key schema defines the object, and every producer, validator, and renderer resolves to that same definition: a payload valid under one surface validates under all of them, and an extra or missing key is rejected everywhere. test: `tests/plans/test_review_repair.py::test_deviation_schema_parity_across_surfaces`.

### 2.4 Causal routing in the next-round snapshot [category: code] (depends: 2.3)
`kind: deliverable`

Target: `src/gobby/plans/review_evidence_io.py`, `src/gobby/plans/review_repair.py`

Extend `snapshot_payload` output with `prior_round_context`: prior finding IDs,
check keys, repair attestations, changed acceptance-item IDs and changed
section targets from the inter-round diff, and the quality ledger (once 4.1
lands the ledger; until then the key carries findings and attestations only). Lanes
start at causal surfaces, then complete their normal exhaustive pass —
full-plan coverage is unchanged, and `_validate_lanes`
(`review_coverage.py:283-342`) still requires every lane to enumerate every
deliverable section. Assembly of the context happens in `review_repair.py` or
`review_evidence_io.py`, not `review_evidence.py` (constraint 5).

**Acceptance:**

- 2.4.1 - The context builder produces a `prior_round_context` structure carrying prior finding IDs, check keys, attestations, changed acceptance-item IDs, and changed section targets from injected round-N inputs. test: `tests/plans/test_review_repair.py::test_prior_round_context_structure`.
- 2.4.2 - Lane validation still requires every lane to cover every deliverable section. test: `tests/plans/test_review_coverage.py::test_lanes_still_cover_all_sections`.

### 2.5 Two-round refusal end-to-end coverage [category: test] (depends: 2.6, 8.1)
`kind: deliverable`

Target: `tests/plans/test_repair_gate_e2e.py`

Extend the `feedback-lesson-loop` end-to-end pattern (commit `8aba2321a`) with
a two-round review harness where round 1's repair deliberately omits a known
consumer: round 2 preparation must refuse before any spawn, naming the
unattested finding. A second case omits the resolution record for one prior
finding entirely — preparation must refuse naming that finding, proving the
server-loaded universe (2.2) holds at the service boundary. This pins the
gate's behavior at the service boundary rather than unit level.

**Acceptance:**

- 2.5.1 - End-to-end test drives two rounds and asserts refusal-before-spawn with the specific missing attestation named. test: `tests/plans/test_repair_gate_e2e.py::test_omitted_consumer_refuses_round_two`.
- 2.5.2 - Omitting one prior finding's resolution record end-to-end refuses round-2 preparation naming that finding. test: `tests/plans/test_repair_gate_e2e.py::test_omitted_resolution_refuses_round_two`.
- 2.5.3 - An attestation that looks complete but sweeps a strict subset of the server-derived universe refuses round-2 preparation before spawn, naming the omitted site. test: `tests/plans/test_repair_gate_e2e.py::test_subset_attestation_refuses_before_spawn`.

### 2.6 Server-derived repair universe [category: code] (depends: 2.4, 5.2)
`kind: deliverable`

Target: `src/gobby/plans/review_repair.py`, `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/install/shared/skills/plan/SKILL.md`, `src/gobby/install/shared/workflows/agents/planner.yaml`

The producer must be able to **see** the universe before it attests to
sweeping it. Deriving the site graph inside round-N+1 preparation and
validating already-submitted attestations against it in the same call cannot
succeed on the first attempt that contains any newly derived site: the
coordinator cannot attest to a site it has never observed, and a failed
preparation persists nothing a retry could learn from. The same deterministic
consumer-sweep/repair-universe helper therefore runs **before** submission, so
taskless and staged producers construct attestations against the exact site
graph; preparation then rederives it from current repository state and compares.
Drift between the two is rejected and construction reruns. No preview state
machine and no second persistent transition — the helper is the one 5.2 already
owns, called earlier by the producer.

"Called earlier" needs a seam the producers can actually reach: both are
model-driven, `run_consumer_sweep`'s production callers are the CLI and the
expansion gate, and neither the plan skill nor `planner.yaml` has an operation
to invoke. Expose the existing helper through one **stateless read-only**
`gobby-plans` operation returning the typed graph and its canonical digest —
no persistence, no lease, no state transition.

The call order is **edit → derive → attest → submit, with no edit after the
derivation.** The graph is a function of changed sections, symbols, targets,
and contracts, so deriving it before the repair would produce a digest of the
wrong tree and guarantee the drift rejection it exists to prevent. Preparation
rederives from the submitted revision and compares. On drift the producer is
still live on both paths — the taskless coordinator has not yet left its turn,
and a staged mismatch reuses the existing failed-dispatch rollback to restore
the revision/resubmission step — so a rejection routes back to a producer that
can rebuild the attestation rather than stranding the round.

Attestation sweep fields are proof only if the obligation set they discharge
is derived by the server, never by the payload being validated. From the
finalized prior round and the 5.2 consumer-site inventory, derive per repair:
required consumer sites, adjacent variants (check-key expansion over the
inventory), changed contracts, and repair-bundle interaction edges. Adjacency
is the **union** of every shared identity the evidence model already carries —
section, `check_key`, changed contract, and changed target or resource — not
shared section alone. Two findings on different sections routinely mutate the
same contract or file, which is exactly the cross-repair conflict this graph
exists to expose; joining on section only would let a fixer make two locally
valid, mutually incompatible changes. Every edge in that union carries a
cross-repair sweep record, and 2.2 exposes the same graph before edits rather
than deriving a second one. Validate each attestation's `consumer_sites_swept`,
`adjacent_variants_swept`, and `deferred_sites` against that universe: every
derived site is swept, deferred with a reason, or explicitly dispositioned;
zero-result sweep claims carry query evidence (the search that returned
nothing); anything else refuses preparation. This closes the pre-spawn hole
that post-spawn coverage (5.1) cannot reach.

**Acceptance:**

- 2.6.1 - The sweep universe is derived server-side from the prior round plus the site inventory; an attestation covering a strict subset with no deferrals is refused naming the missing sites. test: `tests/plans/test_review_repair.py::test_sweep_universe_subset_refused`.
- 2.6.4 - The registered read-only operation returns the typed site graph and its canonical digest for a given worktree state, and the helper rejects attestations built against a different digest. test: `tests/plans/test_review_repair.py::test_universe_visible_before_attestation`.
- 2.6.2 - A zero-result sweep claim without query evidence is refused. test: `tests/plans/test_review_repair.py::test_zero_result_requires_query_evidence`.
- 2.6.3 - Adjacency is computed over the union of shared section, `check_key`, changed contract, and changed target/resource: two accepted findings on *different* sections that touch the same contract or target are adjacent and require a cross-repair interaction record, whose absence refuses preparation. test: `tests/plans/test_review_repair.py::test_repair_bundle_interaction_edges`.

## P3: Terminal-completion guard
`kind: framing`

**Goal**: A plan reviewer cannot terminate cleanly having delivered nothing.

### 3.1 end_agent_run completion guard and session-resolution fix [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py`, `src/gobby/plans/review_evidence_store.py`, `src/gobby/mcp_proxy/tools/agents_context.py`, `src/gobby/mcp_proxy/stdio_proxy.py`, `src/gobby/servers/routes/mcp/endpoints/execution.py`, `src/gobby/mcp_proxy/tools/agents_registry.py`, `src/gobby/utils/session_context.py`, `src/gobby/plans/review_evidence_models.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`

`end_agent_run` takes zero arguments and accepts no result
(`agents_lifecycle_tools.py:100-146`), so a reviewer can terminate cleanly
having emitted nothing — three consecutive `#9602` rounds did exactly that,
the third after an explicit prompt instruction not to. The guard has to be
structural. Two changes in the same function:

1. **Completion guard.** Add a run-keyed accessor to `PlanReviewEvidenceStore`
   (`WHERE dispatch_run_id = %s` — the unique partial index at
   `338_plan_review_evidence.sql:67-69` makes this one indexed lookup; the
   store has no run-keyed method today), wire the store into
   `AgentsRegistryContext` through the production constructor
   (`create_agents_registry`) so the guard exists on the real path, not only
   in test doubles. In `end_agent_run` — when live unexpired, unfinalized
   evidence is bound to this run — require `agent_runs.result` to parse as
   the canonical round result via `validate_round_result` (not marker
   sniffing: a message merely containing the words `verdict` and
   `coverage_attestation` must not pass) and require
   `coverage_attestation.evidence_id` to equal the bound evidence row's ID.
   The guard accepts the **full verdict universe** the contract defines, via
   a verdict-discriminated union in the round-result validator rather than
   one shape for all outcomes. `approved` and `needs_review` are *reviewed*
   verdicts and require the canonical coverage attestation.
   `needs_requirements` and `inconclusive` are *non-attested terminal
   branches* — a reviewer whose snapshot drifted under it never completed
   lanes and structurally cannot produce a coverage attestation — so they
   require the bound `evidence_id` plus typed reason data instead. Demanding
   coverage from every verdict would trap exactly the runs that most need to
   exit cleanly, which is the failure this section exists to prevent.

   "Typed reason data" is specified here as an exact JSON discriminated union
   keyed on `verdict`, because several independent producers write it —
   both adversary prompts, the timeout path (6.3), the index-mismatch path
   (6.1) — and `send_message` persists whatever they emit verbatim. Without
   one published shape they can each satisfy their local criteria while
   emitting mutually incompatible envelopes that the validator then rejects.
   The union defines, per branch: the discriminator, the required fields, and
   a closed `reason_code` enum covering at minimum `source_drift`,
   `missing_requirements`, `index_mismatch`, and `timeout`. It is published
   verbatim in the shared skill and both adversary definitions, and 6.3 and
   6.1 are pinned to it rather than describing their own envelopes.
   `send_message` to the parent auto-writes `agent_runs.result`
   (`agent_messaging.py:190-209`), so a compliant reviewer satisfies the
   guard by doing exactly what the contract already orders: send the
   structured result, then terminate. Do **not** gate on finalization —
   `finalize_plan_review_evidence` is the coordinator's post-run step and
   absent from the reviewer's `allowed_mcp_tools`; a finalization gate would
   deadlock every round. Precedent for reading state on the self-termination
   path: `_complete_self_terminated_run` already loads session variables to
   attach `adversary_verdict` (`agents_termination.py:119-129`).
2. **Trusted run identity and session-resolution fix.** `"No active session
   context available"` originates at `agents_lifecycle_tools.py:108-110` when
   `ctx.get_current_session_id()` reads a ContextVar the proxy failed to
   resolve or seed (`utils/session_context.py:277-292`,
   `hooks/mcp_dispatch.py:157-164`) — and then the agent cannot terminate at
   all. Observed once in `#9602` round 3. A run-keyed fallback that itself
   needs the session to discover the run is circular, so carry the trusted
   agent-run identity independently and at request level, along the whole
   real chain rather than at one convenient layer: `stdio_proxy.py` attaches
   the managed run ID as transport metadata on every proxied call from a
   spawned agent (the daemon knows it at spawn),
   `servers/routes/mcp/endpoints/execution.py` — which owns header
   extraction, validation, context seeding, dispatch, and reset — validates
   it against the run's own server-side binding and rejects a mismatch,
   `session_context.py` seeds and resets a per-request run
   ContextVar around each invocation, and `end_agent_run` resolves
   run → child session from that identity when the session ContextVar is
   unset. The value never travels in tool arguments, so a caller can neither
   omit nor forge it. Wire the getter through `create_agents_registry`
   alongside the evidence store.

**Acceptance:**

- 3.1.1 - `PlanReviewEvidenceStore` gains a dispatch-run-keyed accessor. symbol: `gobby.plans.review_evidence_store.PlanReviewEvidenceStore.get_by_dispatch_run`.
- 3.1.2 - `end_agent_run` with bound live evidence refuses when the result is missing, fails `validate_round_result`, or attests a different `evidence_id`; with a valid delivered result it completes. test: `tests/mcp_proxy/test_agents_lifecycle.py::test_end_agent_run_refuses_without_round_result`.
- 3.1.3 - A run whose session context fails ContextVar resolution still terminates via the request-level run identity; a caller-supplied or mismatched run ID is ignored and cannot bind another run. test: `tests/mcp_proxy/test_agents_lifecycle.py::test_end_agent_run_run_identity_fallback_and_spoofing`.
- 3.1.4 - `create_agents_registry` (`mcp_proxy/tools/agents_registry.py:33`) injects both the evidence store and the run-identity getter, exercised through the production constructor rather than a test double. test: `tests/mcp_proxy/test_agents_lifecycle.py::test_registry_constructor_injects_dependencies`.
- 3.1.5 - The validator is verdict-discriminated: reviewed verdicts require canonical coverage, while `needs_requirements` and `inconclusive` require `evidence_id` plus typed reason data and terminate cleanly. A real source-drift run that never completed a lane terminates without fabricating an attestation. test: `tests/mcp_proxy/test_agents_lifecycle.py::test_verdict_discriminated_terminal_branches`.
- 3.1.7 - The published union covers every verdict and every `reason_code` (`source_drift`, `missing_requirements`, `index_mismatch`, `timeout`), and a payload emitted verbatim by each producer surface validates against it. test: `tests/plans/test_review_evidence_models.py::test_terminal_branch_union_producer_parity`.
- 3.1.6 - The run ID survives the stdio-proxy → execution-endpoint → ContextVar chain end to end: headers forward, the endpoint binds server-side and seeds/resets the ContextVar, and forged, absent, and mismatched IDs are each rejected at the endpoint rather than trusted downstream. test: `tests/servers/test_mcp_execution_context.py::test_run_identity_transport_chain`.

## P4: Quality ledger and approval condition
`kind: framing`

**Goal**: Non-blocking findings and dismissals persist across rounds instead of
being re-derived, and approval is a bounded condition.

### 4.1 Carry-forward quality ledger module [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Target: `src/gobby/plans/review_ledger.py` (new module)

Merge the prior finalized round's `quality_ledger` (column from 2.1) with the
current round's non-`blocking` findings, `carry`-decided resolution records
(from 2.2), and `dismissed` candidate dispositions. Cross-round identity is
**not** round-local IDs: assign an immutable `ledger_entry_id` on first
insertion, coalesce later observations by the canonical key
`(kind, check_key, ordered_section_pairs)` — where the section component is the
unique source section IDs sorted into one stable order and each paired with
its hash at entry, never two parallel lists in producer order. Two producers
naming the same sections in different orders must land on one entry, or
coalescing silently mints duplicates and the repeated-check convergence count
is wrong. Retain
round-local finding/candidate IDs as aliases — a reworded finding with a
regenerated ID must coalesce into its existing entry, not duplicate it.
Track `first_seen_round`, `rounds_carried`, `source_section_ids`, and
`section_hashes_at_entry`. Mark an entry `stale` when its sections' hashes
changed — reuse `reviewed_section_hashes` (`review_evidence_io.py:151`) and
`_changed_sections_since_prior_round` (`review_evidence.py:742-765`) — and
create a fresh entry for the changed surface. Validate entries with a sibling
of `_validate_finding` reusing `FINDING_CATEGORIES` and the `check_key` regex
so vocabulary stays shared with the lessons pipeline. Dismissed entries carry
candidate identity, `check_key`, source hash, and rationale
(`kind: dismissed`). Only the 2.1 columns and existing section-hash helpers
are required, so this runs beside 2.2–2.5; 4.2 is the integration point with
the snapshot context. The merged ledger has a concrete write transition: it
is persisted to the evidence row's `quality_ledger` column at
`finalize_plan_review_evidence` through the 2.1 store API (call-site wiring
only in `review_evidence.py`; merge logic stays here), and the next round's
preparation reads it back.

The dismissed inputs need a durable source, because the boundary that sees them
discards them. Coverage validation canonicalizes candidate dispositions and
returns counts, booleans, and digests (`review_coverage.py:382-454`); the
canonical `round_result` carries findings, not dispositions; and finalization
persists only that result. Nothing durable therefore survives the coverage call
— least of all across a restart, or on a `needs_review` round where dismissed
candidates matter most.

The fix is to stop discarding them, not to add a second store. The dismissed
disposition records travel **inside the canonical `round_result`** alongside
5.1's sweep records, which is where reviewer-derived evidence already lives.
That result is already digest-bound by the coverage attestation, already
validated, and already persisted whole at finalization, so the ledger merges
from it with no sidecar row, no separate digest, and no compare-and-set
protocol — a retry cannot swap a disposition set behind the result because the
set *is* part of the result. This keeps 4.1 a pure helper: it merges records
handed to it and owns no production call site.

**Acceptance:**

- 4.1.1 - Ledger module merges, carries, and stales entries across three synthetic rounds. test: `tests/plans/test_review_ledger.py::test_merge_and_staleness_across_rounds`.
- 4.1.2 - Ledger entry validation shares category and check-key vocabulary with findings. symbol: `gobby.plans.review_ledger`.
- 4.1.3 - A reworded finding with a new round-local ID coalesces into its existing `ledger_entry_id`; a source-hash change stales the old entry and creates a fresh one; and the same section set supplied in a different order coalesces to the same entry. test: `tests/plans/test_review_ledger.py::test_canonical_coalescing_order_and_hash_split`.
- 4.1.4 - The merge builds dismissed entries only from disposition records carried in the canonical round result, and rejects a result whose `disposition_counts` disagree with the records it carries. test: `tests/plans/test_review_ledger.py::test_dismissed_entries_from_canonical_result`.

### 4.2 Dismissal do-not-reopen injection [category: code] (depends: 2.4, 4.1)
`kind: deliverable`

Target: `src/gobby/plans/review_ledger.py`, `src/gobby/plans/review_evidence_io.py`, `src/gobby/plans/review_coverage.py`

Inject ledger `dismissed` entries into the next round's `prior_round_context`
(from 2.4) so a later reviewer does not re-derive and re-dismiss the same
candidate. Reopen is permitted only when a named source hash or plan section
changed — the ledger carries the hashes to prove it.

Injection alone makes the rule advisory, and an advisory no-reopen rule is not
one: prompt context persuades, it does not enforce, so a reviewer can reopen an
unchanged dismissed candidate and still produce a valid attestation — defeating
the convergence guarantee this section exists to provide. The **server-owned
coverage validator** enforces it, comparing emitted candidate identities
against ledger identities under the same bound plan and source hashes and
rejecting an unchanged reopen. Reopening stays available through the typed
changed-hash path. This reuses the validator 5.1 already extends; no new gate.

**Acceptance:**

- 4.2.1 - A dismissed candidate appears in the next snapshot's context and is marked reopenable only on hash change. test: `tests/plans/test_review_ledger.py::test_dismissal_injection_and_reopen_rule`.
- 4.2.2 - Coverage validation rejects a candidate that reopens a ledger dismissal under unchanged plan and source hashes, and accepts the same identity once its named source or section hash changes. test: `tests/plans/test_review_coverage.py::test_unchanged_dismissal_reopen_rejected`.

### 4.3 Approval condition and surfacing [category: code] (depends: 2.6, 3.1, 4.1, 5.1)
`kind: deliverable`

Target: `src/gobby/plans/review_coverage.py`, `src/gobby/review_learning/round_diff.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `docs/contracts/plan-coverage.md`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/install/shared/skills/plan/SKILL.md`, `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`

Approval becomes **zero remaining `blocking` findings** (equivalently: no
finding carrying a required failure trace). The ledger is surfaced in full at
approval alongside the manifest, so declining to fix a `major` is an explicit
recorded choice rather than an omission. Surfacing is a delivery requirement,
not a storage one: persisting entries for a future round does not show them to
anyone. The server-derived merged ledger therefore travels in the existing
approved round-result envelope beside the canonical manifest, is validated
with it, and renders on the coordinator's existing approval display path (and
the staged transport where that surface applies). No new API, endpoint, or
dashboard.

Delivery means every approval consumer, not just the envelope. Both coordinator
displays enumerate approval content without the ledger today — the taskless plan
skill lists `routing_decisions` and `manifest_entries` (`plan/SKILL.md:101`),
and staged `approve_review` accepts `routing_decisions` in its signature and
JSON schema with no ledger parameter (`_stage_review.py:309,353-354,465`).
Ordering also matters: `approve_review` persists durable approval intent when
it links `review_approved` (`:410-414`), so the server-derived ledger is
materialized **before** that intent is written, not derived afterwards from a
frozen result — otherwise the approval a user sees and the approval the system
stored disagree.

The ledger is **server-derived, and therefore never a caller input.** Exposing
it as a parameter on staged approve/reject and threading it through the facade
would give one canonical value two authorities: `review_ledger` merges it from
the persisted prior ledger and the delivered `round_result`, while a caller
could substitute, omit, or race a different value — and a caller copy that the
server rederives anyway is a redundant echo. It is derived exactly once, inside
the same terminal transition that 7.2 owns, from state the server already
holds: after the delivered result is validated and before any stage mutation.
That single derived value is what gets persisted, carried in the approved
envelope, and rendered on both displays. A derivation failure leaves stage
state unchanged rather than persisting a ledger-less approval. A ledger entry carried unresolved
across three or more rounds feeds `record_review_lesson` with
`decision="no-fix-policy"`, `guardrail_target="checklist"` — the existing
promotion-ladder rung, reused rather than rebuilt. Lesson-minting eligibility
(`round_diff.py:53` requires `severity == "blocking"` today) is extended
**narrowly** for this path: `decision="no-fix-policy"` plus
`rounds_carried >= 3` plus a quality-ledger source entry; ordinary
finding-derived lessons keep the blocking requirement. The 5.1 record bundle
rides the same canonical result through both verdicts — taskless delivery and
staged approve/reject alike — and is revalidated at the terminal boundary
before the merge reads it, so neither verdict path can discard the dismissed
identities and rationales the ledger depends on.

The skill and contract updates carry one shared **severity matrix** — the
normative decision boundary for all four labels: `blocking` requires a
demonstrated violation of a required obligation plus the failure trace;
`major` is material non-gating quality or operability risk requiring an
explicit ledger decision; `minor` is localized hardening with bounded effect;
`nit` is cosmetic. Include table-driven contract examples with boundary cases
drawn from the cited corpus. Update all surfaces: the skill's severity table
(SKILL.md:397-411) and finding schema (435-469), the contract doc, and the
agent definition's result contract (yaml:99-117), which additionally gains
required `failure_trace` (on blocking) and `minimal_repair` fields,
deduplicated against the skill's schema rather than restated.

Enforcement binds the interactive path, not just the reviewer side: the
evidence-bound finalize/apply path invokes the finding validator
(`validate_plan_review_findings`) on the canonical round result using
immutable section IDs (call-site wiring only in `review_evidence.py`; logic
stays in `review_findings.py`/`review_coverage.py`), and approval at finalize
requires zero `blocking` findings server-side — `validate_round_result` alone
accepts arbitrary finding objects, so without this wiring every rule in this
phase is bypassable by interactive review.

**Acceptance:**

- 4.3.1 - Approval validation passes with open major/minor ledger entries and fails with any blocking finding. test: `tests/plans/test_review_coverage.py::test_approval_condition_blocking_only`.
- 4.3.2 - Skill, contract, and agent definition document the four-tier severity model with the shared decision matrix, table-driven boundary examples, and ledger surfacing. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 4.3.3 - Three-round unresolved carry mints a no-fix-policy lesson through the existing recorder. test: `tests/review_learning/test_no_fix_policy_lesson.py::test_carry_three_rounds_mints_lesson`.
- 4.3.4 - Ordinary finding-derived lesson minting still requires `severity == "blocking"`; the no-fix-policy path requires all three eligibility conditions. test: `tests/review_learning/test_no_fix_policy_lesson.py::test_minting_eligibility_paths`.
- 4.3.5 - Both coordinator approval displays render the ledger from the server-derived approved envelope: the taskless plan skill enumerates it beside `routing_decisions` and `manifest_entries`, and the staged display reads it from the same envelope. Neither accepts it as an input. file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 4.3.6 - Neither staged approve/reject nor the facade exposes a ledger parameter, and the persisted and displayed ledger equals the server derivation from the persisted prior ledger and the delivered result: a caller that supplies its own value cannot change what is stored or shown. Derivation precedes stage mutation, and a derivation failure leaves stage state unchanged. test: `tests/storage/test_stage_review_findings.py::test_approval_ledger_is_server_derived`.

## P5: Derived sweep evidence
`kind: framing`

**Goal**: Coverage attestations are derived from records, not asserted by the
agent.

### 5.1 Structured sweep records replace attested booleans [category: code] (depends: 2.4, 5.2)
`kind: deliverable`

Target: `src/gobby/plans/review_coverage.py`

`cross_lane_interaction_complete` and `adjacent_variant_complete` are
agent-asserted booleans checked only for literal `True`
(`review_coverage.py:387-396`, re-asserted at `:193` and `:198`), then
hard-coded `True` into the emitted attestation (`:109-110`; key allowlist at
`:137-138`). Replace both with record lists:

- `cross_lane_interactions[]` — participating candidate IDs, affected
  sections, interaction checked, disposition.
- `adjacent_variant_sweeps[]` — check key, seed candidate, query/index
  evidence, sites checked, resulting candidate IDs.
- `causal_repair_sweeps[]` — prior finding ID, changed sections/contracts,
  sites checked, disposition.
- `candidate_dispositions[]` — every candidate the lanes raised, with identity,
  `check_key`, source hash, disposition, and rationale. Today the attestation
  reduces these to `disposition_counts` and the result keeps only findings, so
  dismissed candidates are unrecoverable after validation and 4.1's ledger has
  no input. Carrying the records in the result makes them durable for free:
  the result is already digest-bound, validated, and persisted whole.

Records alone are not enough — the server defines the **required sweep
universe**, or omission still passes: the universe arrives as the typed
`prior_round_context` structure (2.4) carrying prior repairs, changed
contracts, and the consumer-site inventory (5.2), rather than being
re-derived here from loose inputs, and each completion boolean is computed as
an empty set difference between required keys and validated records. Zero-result sweeps require query evidence (the
search that returned nothing), so an unexecuted sweep cannot masquerade as an
empty one. Reject unreferenced candidates and unswept changed repair
surfaces; keep emitting the derived booleans in the attestation so existing
consumers are unaffected. The file is 595 lines with 405 free (constraint 5
headroom).

**Acceptance:**

- 5.1.1 - Attestation booleans are computed as empty set differences between server-derived required sweep keys and validated records; hand-computed fixtures match. test: `tests/plans/test_review_coverage.py::test_derived_sweep_booleans`.
- 5.1.2 - A candidate referenced by no sweep record fails validation with a diagnostic naming it. test: `tests/plans/test_review_coverage.py::test_unreferenced_candidate_rejected`.
- 5.1.3 - Fixtures cover empty-valid, partial, zero-result (with and without query evidence), and extra-record cases. test: `tests/plans/test_review_coverage.py::test_sweep_universe_fixtures`.
- 5.1.4 - Validation rejects a result whose `candidate_dispositions` do not reconcile with its `disposition_counts`, so the counts cannot disagree with the records that back them. test: `tests/plans/test_review_coverage.py::test_dispositions_reconcile_with_counts`.
- 5.1.5 - Coverage validation returns one canonical validated record bundle — sweep records plus dispositions with identities and rationales — rather than reducing them to the compact attestation, and that same bundle is what the round result carries. A bundle dropped between validation and the result is detected. test: `tests/plans/test_review_coverage.py::test_validator_returns_canonical_record_bundle`.

### 5.2 Machine-generated consumer inventory [category: code] (depends: 4.2)
`kind: deliverable`

Target: `src/gobby/plans/consumer_sweep.py`, `src/gobby/plans/review_evidence_io.py`, `src/gobby/agents/code_index.py`, `src/gobby/mcp_proxy/tools/plans/review_evidence.py`

`run_consumer_sweep` (`consumer_sweep.py:88-122`) queries the code index only
against declared section targets, from two call sites (`cli/plans.py:230` and
the spawn-time gate `tasks/expansion/_plan_gate.py:113`). Extend it to derive
changed acceptance items, targets, symbols, and contracts from the
inter-round revision and emit a typed candidate site inventory that travels
into the next-round snapshot's `prior_round_context` (2.4) through evidence
preparation and `review_evidence_io.py` — the inventory has a named producer,
transport, and consumer, not just a generator.

Derived sites are **not** gated on coordinator pre-disposition. Each derived
site joins the **required sweep universe** that 2.6 already validates:
attestations must sweep it, defer it with a reason, or disposition it. The
producer sees that universe beforehand through 2.6's stateless read-only
operation over this same helper — the graph is derived deterministically from
committed repository state, so computing it early costs nothing and commits to
nothing. Preparation rederives it inside its own transaction (2.1) and compares
digests, which is what makes the pre-attestation view safe rather than a
preview protocol: nothing is persisted between the two calls, and drift is
rejected rather than trusted.

Turning a value result into a typed failure changes every caller's control
flow, and one of them is not ready for it: the spawn plan gate
(`tasks/expansion/_plan_gate.py:30,93`, reached from
`spawn_agent/_implementation.py:181`) assumes a result and catches only
database errors, so the new error escapes as an unstructured failure instead
of the deterministic no-spawn outcome it already knows how to produce. The gate
catches the typed error and translates it to that existing structured outcome,
and both callers — CLI validation and the spawn gate — are pinned with parity
tests.

When the code index is unavailable, preparation **aborts and rolls back** with
a typed `inventory_unavailable` error, and no spawn may proceed; the
coordinator retries a fresh preparation once the index recovers. There is no
persisted unavailable record: a record written inside the failing transaction
rolls back with it, and one committed outside it would let the repair-universe
checks read a missing inventory as an empty universe and pass. Refusing to
produce evidence at all is both simpler and the only honest state.

This deliverable also owns the **index token** (6.1's protocol consumes it):
via a helper in `code_index.py`, compute the repository-source digest D0, run
the index operation, read `last_indexed_at`, compute D1, and accept the token
only when D0 equals D1 — rerunning the whole sequence otherwise. Binding both
digests around the index operation is what ties the timestamp to the tree it
actually describes; a mutation landing after the index but before a single
digest read would otherwise yield a stable token over a stale index. Recheck
the token after inventory derivation, and store it as a typed `index_token`
inside `prior_round_context`.

That retry is **bounded**, or a continuously mutating repository spins inside
preparation forever: nothing outside can stop it, because the per-index process
timeout does not bound the enclosing loop and 6.3's reviewer deadline only
starts once a child run exists, which it never does. Cap it at three attempts
under an overall monotonic deadline derived from the existing per-index
timeout, with bounded backoff between attempts. Exhaustion is a typed
`index_unstable` failure that rolls the whole preparation transaction back —
no evidence row, no spawn — leaving a later preparation free to succeed once
the tree settles. Ownership sits here rather than in 6.1 because
the inventory must run under the settled token and 5.2 precedes 6.1; 6.1 only
teaches the prompts to verify it. No new column.

Two concrete wins inside the existing edge set:

- `_direct_file_consumers` (`consumer_sweep.py:294-305`) computes
  `_module_candidates` (line 297) and discards it on the real path — wire it
  to `code_imports(source_file, target_module)` for actual import edges. The
  tuple is already computed and threaded to the test seam.
- Constructor sites: call resolution binds a bare `Service()` to the class
  symbol (`crates/gcode/src/index/parser/calls/resolution.rs:64-66`), so
  `code_calls WHERE callee_symbol_id = <class>` yields constructor sites —
  with the honest caveat that static-method calls are indistinguishable.
  Both are hub Postgres tables (baseline `:1624`, `:1634`; DAL
  `src/gobby/code_index/_storage/relations.py:25-128`; matching index at
  baseline `:1661`).

Protocol implementors stay out of scope (constraint 2) and remain a prompt
obligation in the `repository_blast_radius` lane — 6.1 owns stating that in
the plan-review skill (single owner for lane-instruction edits).

`_module_candidates` (`consumer_sweep.py:315-323`) returns an empty set for
any path not ending in `.py`, so import-edge discovery is Python-only. This
plan's targets are Python, Markdown, YAML, and SQL — no TypeScript — so
cross-language import resolution is **out of scope** here. What is in scope is
never claiming completeness the sweep cannot deliver: the inventory records
the set of languages it resolved and marks every changed target in an
unsupported language as `language_unsupported` rather than silently omitting
it, so a future web-surface plan sees an honest gap instead of a false clean
sweep.

**Acceptance:**

- 5.2.1 - File consumers include import-edge results, not only symbol-call-derived ones. test: `tests/plans/test_consumer_sweep.py::test_import_edge_file_consumers`.
- 5.2.2 - Inter-round changed symbols produce a typed candidate site inventory in the next-round snapshot's `prior_round_context`. test: `tests/plans/test_consumer_sweep.py::test_inter_round_site_inventory`.
- 5.2.4 - An unavailable index raises the typed `inventory_unavailable` error from the sweep rather than returning a partial or empty inventory. test: `tests/plans/test_consumer_sweep.py::test_inventory_unavailable_raises_typed_error`.
- 5.2.5 - Token capture accepts only when the digests bracketing the index operation agree; a mutation landing between the index and the digest read forces a rerun, the token is rechecked after inventory derivation, and retry exhaustion is reported rather than silently accepted. test: `tests/plans/test_consumer_sweep.py::test_index_token_brackets_index_operation`.
- 5.2.8 - A read-only verifier sits beside the token producer, consumes exactly the producer's digest inputs, and returns a typed match/mismatch result. symbol: `gobby.agents.code_index.verify_index_token`.
- 5.2.11 - A thin stateless read-only wrapper exposes the verifier and is reachable through the review-evidence tool surface with a typed result, so no caller needs an unplanned access path. test: `tests/plans/test_consumer_sweep.py::test_index_verifier_wrapper_registered`.
- 5.2.7 - A changed target in a language the sweep cannot resolve is recorded as `language_unsupported` instead of being omitted from the inventory. test: `tests/plans/test_consumer_sweep.py::test_unsupported_language_marked_not_omitted`.
- 5.2.9 - Under continuous repository mutation the settle helper stops at three attempts or its monotonic deadline, whichever comes first, and raises typed `index_unstable` rather than looping. test: `tests/plans/test_consumer_sweep.py::test_index_settle_retry_is_bounded`.
- 5.2.10 - The spawn plan gate converts a typed unavailable-sweep error into its existing structured no-spawn outcome rather than letting it escape, and the CLI caller keeps its own reporting: both callers are asserted against the same raised error. test: `tests/plans/test_consumer_sweep.py::test_typed_sweep_error_handled_by_both_callers`.

### 5.3 Strict MCP schemas for review-evidence tools [category: code] (depends: 6.2)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/plans/review_evidence.py`

Every structured payload is declared as bare `{"type": "object"}` —
`routing_decisions` (:189), `lane_results` (:220-223),
`candidate_dispositions` (:224), `shadow_manifest_status` (:225),
`round_result` (:263, :291, :320) — so `#9640` had to search implementation
source to discover the disposition record shape after all lanes returned,
then failed its first composed call. Publish full nested schemas with
`properties`/`required` and put a compact example in each tool description.

`shadow_manifest_status` is an **output** here, not an input: 6.2 removes the
echoed 42 KB manifest and derives the canonical shadow server-side from
`evidence_id` plus the reviewed `routing_decisions`. Publishing a strict input
schema for it would preserve the echo this plan deletes and leave two
representations of one manifest free to diverge. The validator accepts
`routing_decisions`; only the compact derived status appears in the emitted
attestation.
The file is 369 lines; schema constants may need a sibling module if the
result approaches the 1000-line limit.

**Acceptance:**

- 5.3.1 - Every structured payload parameter carries a nested schema with properties and required lists, and `shadow_manifest_status` is absent from the input surface — the validator takes `routing_decisions` and emits only the derived compact status. file: `src/gobby/mcp_proxy/tools/plans/review_evidence.py`.
- 5.3.2 - A malformed disposition record is rejected at the schema layer with a field-level diagnostic. test: `tests/mcp_proxy/test_review_evidence_schemas.py::test_disposition_schema_rejects_malformed`.
- 5.3.3 - `prepare_plan_review_round` publishes strict schemas for its `repair_attestations` and resolution-record parameters, and a wrapper round trip carries both through to the service boundary unchanged. test: `tests/mcp_proxy/test_review_evidence_schemas.py::test_preparation_payload_schema_round_trip`.

## P6: Orchestration cost
`kind: framing`

**Goal**: Cut per-round wall time through the levers Gobby actually controls
(constraint 3).

### 6.1 Pin one code-index generation by protocol [category: config] (depends: 3.1, 5.2, 7.1)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/plan/SKILL.md`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`

Lanes run read-only searches with `--no-freshness`
(`crates/gcode/src/cli.rs:42`). The isolation preflight already exercises this
pairing (`src/gobby/agents/code_index.py:85-108`). This removes the observed
60-second recovery loop and repeated 9–19 second index-contention waits.

The pin is verified, not assumed: `--no-freshness` disables checks without
holding a lock or epoch, so separate lane processes can otherwise observe
different index states with no detectable citation drift. There is exactly
**one** indexing site — evidence preparation invokes 5.2's
D0 → index → timestamp → D1 capture, which both runs the index and settles the
`index_token` in `prior_round_context`. No separate coordinator-side `gcode
index` before spawn: a second index run outside the capture is precisely the
mutation window the bracketing exists to close. This deliverable owns the
protocol that consumes the token: the spawn prompt carries it and lanes verify
it before and after their searches.

Verification needs one **canonical verifier**, or each lane compares whatever
state it chooses and a lane that silently skips the check still passes its
tests. 5.2 adds a small read-only verifier beside the token producer, reusing
exactly the producer's digest inputs and returning a typed match/mismatch
result rather than a bare boolean. Every lane invokes it immediately before
analysis, and the review skill names that call as the required step.

Mismatch ends the **run**, not just the lane. The reviewer is immutably bound
to its evidence through a unique `dispatch_run_id`, so it cannot rebind to a
replacement, and rerunning only the affected lanes would mix results derived
from two different index generations into one attestation. The protocol is
therefore: the bound child emits a typed `inconclusive` result naming the
index mismatch against the old evidence and terminates (3.1's guard accepts
that branch); the parent expires that evidence, prepares a fresh snapshot,
inventory, and token, then spawns and binds a **new** reviewer run that reruns
all three lanes under the new evidence ID. Still no `index_generation` counter
(constraint 4): the token uses existing state, zero schema change, zero Rust
change.

This deliverable owns lane-instruction edits in the review skill, including
the constraint-2 statement that protocol implementors remain a prompt
obligation in the `repository_blast_radius` lane (from 5.2).

**Acceptance:**

- 6.1.1 - The plan skill's spawn step relies on preparation's single indexing site and instructs no separate pre-spawn `gcode index`. file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 6.1.2 - The adversary definition and review skill instruct `--no-freshness` on all lane searches. file: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`.
- 6.1.3 - The review skill and adversary definition instruct lanes to verify the snapshot's `index_token` before and after searching and to report a mismatch rather than proceeding. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 6.1.4 - The verifier classifies a repository mutation and a reindex as mismatches against a captured token, returning the typed mismatch result rather than a bare boolean. test: `tests/plans/test_consumer_sweep.py::test_verifier_classifies_mutation_and_reindex`.
- 6.1.5 - The review skill and adversary definitions require every lane to invoke the canonical verifier immediately before analysis and, on mismatch, to emit the typed `inconclusive` index-mismatch result and terminate rather than rerunning in place. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 6.1.6 - The verifier wrapper is present in the reviewer's `allowed_mcp_tools` on both adversary definitions, and a lane invokes it before and after its searches on the real path with no unplanned access route. test: `tests/plans/test_review_coverage.py::test_lane_verifier_invocation_and_allowlist`.

### 6.2 Fix snapshot transport [category: code] (depends: 5.2, 6.3)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/plans/review_evidence_io.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/dispatch/spawn.py`, `src/gobby/dispatch/prompts.py`, `tests/storage/test_stage_review_findings.py`

Changing the transport changes the consumer contract, so this deliverable
migrates the readers with it: the shared plan-review skill and **both**
adversary definitions (staged and taskless) describe a single whole-snapshot
call today, and existing wrapper tests assert that shape. Leaving them
unmigrated strands a live reviewer on page one — a worse failure than the
truncated envelope this fixes.

Migrating the definitions is not sufficient for the staged path, because the
stage-native reviewer never calls the snapshot tool: `dispatch/spawn.py:121`
passes `service.snapshot_bytes(prepared.evidence_id)` into
`dispatch/prompts.py:176`, which decodes the whole plan and embeds it inline
between `<plan-review-snapshot>` markers (`:203-205`), and
`tests/storage/test_stage_review_findings.py:424-425` pins that exact inline
shape. A definition telling the staged reviewer to page evidence while its
prompt still carries raw snapshot bytes leaves two contradictory transports
live at once and strands the staged reviewer without `prior_round_context` —
which is also how the requirements bundle (6.5) reaches it. The staged prompt
therefore carries the evidence handle and the reviewer pages through the same
protocol as the taskless one; the pinning test migrates with it.

`get_plan_review_snapshot` accepts an `evidence_id` handle, then decodes and
returns the whole 140 KB plan inline
(`mcp_proxy/tools/plans/review_evidence.py:66-81`). Because that exceeds the
15,000-char offload threshold (`ToolResultOffloadConfig.threshold_chars`,
`config/features.py:130`; envelope 8,000, page 2,000 at `:131-133`), the
adversary receives an 8 KB envelope plus a `result_id` and pages the plan
back in 2 KB chunks — strictly worse than either full inlining or a clean
handle. Changes:

- Add `offset` / `limit` paging so the plan arrives in whole pages instead of
  a truncated envelope, with a completeness contract on every response:
  `snapshot_hash`, `total_sections`, `next_offset`, and deterministic section
  ordering. There is no `section_ids` scope parameter: `_validate_lanes`
  requires every lane to enumerate every deliverable section
  (`review_coverage.py:283-342`), so per-lane subsets have no valid consumer
  and would let a lane fetch a fraction while still listing every ID.
  There is also **no fetch receipt**, at either lane or parent level. Under a
  stateless offset/limit API a receipt the caller echoes back proves nothing
  the snapshot hash does not already prove, and making it causal would mean a
  new stateful fetch-tracking protocol with no correctness consumer. The
  reader verifies locally instead: concatenate the pages, check the result
  against `snapshot_hash`, then review. Evidence that a model actually read
  the content stays where it always was — exhaustive lane section IDs and
  hashed section-specific citations.
- Page the **whole envelope**, not only plan sections. `prior_round_context`
  carries sidecar records whose size the plan does not control: 6.5's
  requirements bundle holds the full immutable content of arbitrary designated
  repository documents, and the ledger and consumer inventory grow with round
  count. A single requirement source above the 15,000-char offload threshold
  reproduces the exact truncation failure this deliverable exists to fix while
  every plan-section criterion above still passes. The cursor therefore
  enumerates one deterministic record stream over all classes — plan sections,
  requirement sources, ledger, and inventory — with per-record hashes, a total
  record count, and a bundle digest, so the same union-and-reconstruct proof
  covers every byte a lane must read. No second transport.
  Records are **not** the paging unit, because no size bound on a record is
  survivable. Sections have no byte limit in the grammar (`parse_plan` and the
  section-manifest builder enforce structure and hashes only), and this very
  plan already violates any such bound: §7.2 is 15,634 characters and the V1
  changelog is 207,823 bytes, both past the 15,000-character offload
  threshold. A rule that rejects oversized records would make preparation
  refuse the plan that defines it, and it would keep breaking as any section
  grew. Rejecting records is therefore deleted.
  Page the **canonical serialized envelope byte stream** instead, with the
  offset/limit/`next_offset`/`snapshot_hash` contract already specified above:
  chunks align to UTF-8 code-point boundaries, the reader concatenates and
  verifies the full hash before parsing, and record identity is recovered by
  parsing the reassembled envelope rather than by a fragment protocol. This is
  strictly less mechanism than either a per-record bound or fragment identity
  with offsets and reassembly proofs, and it is the only form that transports
  every valid plan.
- Cache the derived manifest keyed on `(evidence_id, routing_digest)` —
  `validate_plan_review_coverage` re-derives it on every call, spinning up a
  `TemporaryDirectory` and re-rendering (`review_evidence.py:212-215, 278`).
- Accept `routing_decisions` in place of the echoed 42 KB shadow manifest and
  derive the canonical shadow once, server-side. There is **no** derivation
  token: `evidence_id` plus the reviewed routing decisions already identify the
  derivation and key its cache, so a token would be a second identity mechanism
  with its own issuer, lifecycle, and validation surface buying no capability
  the existing validated inputs lack. A cache miss rederives.

New logic goes in `review_evidence_io.py` or a new module, not
`review_evidence.py` (constraint 5).

**Acceptance:**

- 6.2.1 - Every page of a 140 KB snapshot stays under the offload threshold and carries `snapshot_hash`, `total_sections`, and `next_offset`. test: `tests/plans/test_snapshot_transport.py::test_paged_fetch_under_threshold`.
- 6.2.2 - Repeated coverage validation with unchanged routing reuses the cached manifest instead of re-rendering. test: `tests/plans/test_snapshot_transport.py::test_manifest_cache_hit`.
- 6.2.4 - The page union equals the section manifest exactly and the concatenated pages hash to `snapshot_hash`, so a reader detects a missed or duplicated page locally. test: `tests/plans/test_snapshot_transport.py::test_page_union_and_local_hash_verification`.
- 6.2.5 - The shared plan-review skill and both adversary definitions drive a deterministic `next_offset` loop to exhaustion with local hash verification. The direct integration seam at `tests/mcp_proxy/test_plans_tools.py:140,204`, which today asserts a whole snapshot returned inline, is migrated to page through `next_offset` and verify per-page size, reconstructed hash, and section union. test: `tests/mcp_proxy/test_plans_tools.py::test_snapshot_pages_to_exhaustion`.
- 6.2.6 - The staged dispatch prompt carries the evidence handle instead of inline snapshot bytes: no `<plan-review-snapshot>` body is embedded, the stage-native reviewer reconstructs the plan by paging to exhaustion and verifying `snapshot_hash`, and `prior_round_context` is visible on that path. The inline-shape assertion at `tests/storage/test_stage_review_findings.py:424-425` is migrated to the handle contract. test: `tests/storage/test_stage_review_findings.py::test_staged_prompt_uses_evidence_handle`.
- 6.2.7 - A round whose requirements bundle, ledger, and consumer inventory each exceed the offload threshold pages every record class under it, and the reader reconstructs all four classes against their per-record hashes and the bundle digest before review begins. Every response stays under the threshold after serialization overhead. The regression pins this plan itself as a fixture — whose §7.2 exceeds 15,000 characters and whose V1 changelog exceeds 200,000 bytes — and round-trips it: pages align to UTF-8 code-point boundaries, concatenation matches `snapshot_hash`, and every record class parses out of the reassembled envelope. No record size causes a refusal. test: `tests/plans/test_snapshot_transport.py::test_sidecar_records_paged_and_bounded`.

### 6.3 Bound the adversary run [category: code] (depends: 3.1, 6.1)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/agents/spawn_executor.py`, `src/gobby/install/shared/skills/plan/SKILL.md`

**Neither** adversary definition has a `timeout:` field — both mention timeouts
only in lane prose — so bounding one leaves the stage-native reviewer able to
hang indefinitely while this deliverable claims the run is bounded. Both get
the same explicit timeout and the same recovery transition; the staged one
additionally restores stage state consistently and wakes only the direct
coordinator.

`plan-adversary-taskless.yaml` has no `timeout:` field, so
`agent_health.py:187-201` never bounds the run. The schema supports it:
`AgentDefinitionBody.timeout` (`workflows/definitions.py:463`, `0` = none)
flows into `SpawnRequest.timeout_seconds`
(`spawn_agent/_implementation.py:330-334`) and is enforced as child-run
wall-clock — `nightly-linter.yaml` (1200s) and `wiki-researcher.yaml`
(2700s) already use it. A lane deadline has never been enforced anywhere;
native lane duration remains unbounded — state that limitation in the skill
rather than implying a guarantee that does not exist.

- Set an explicit `timeout:` on the adversary definition (2700s, matching the
  longest observed healthy round with margin).
- Complete the timeout transition — no partial-result reuse: a timed-out run
  is classified `inconclusive: timeout`, the coordinator expires the bound
  evidence (`expire_plan_review_evidence`) and retries the same display round
  from fresh evidence. The plan skill documents this transition as the sole
  recovery path. (A partial-result checkpoint was considered and dropped: the
  V1/E9 decision declined its only resume consumer, leaving a writer with no
  reader — durability machinery without a complete state transition.)
- Regression-cover the timeout wake path: the parent's terminal result for
  native-lane runs is retained, and the timeout wakes only the direct parent
  session, never other ancestors.
- Fix the adjacent latent bug (constraint 6): `spawn_executor.py:44` strips
  `Task` from Gobby-managed Claude agents; a provider override to `claude`
  silently kills all three native lanes with no diagnostic. Emit a warning
  at spawn when the definition requests native-subagent research and the
  provider's runtime has the facility stripped.

**Acceptance:**

- 6.3.1 - Both adversary definitions carry the same nonzero `timeout:` enforced by the health monitor, so neither the taskless nor the stage-native reviewer can run unbounded. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.
- 6.3.2 - The plan skill documents the timeout transition as expire-then-fresh-retry with no checkpoint consumer, pinned to 3.1's `timeout` reason code. file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 6.3.3 - Spawning a claude-provider agent whose definition relies on native subagents logs an explicit diagnostic. test: `tests/agents/test_spawn_executor.py::test_native_subagent_strip_warns`.
- 6.3.4 - The adversary definition declares the native-lane duration limitation explicitly rather than implying a guarantee the runtime does not provide. file: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`.

### 6.4 Reviewer compaction isolation [category: code]
`kind: deliverable`

Target: `src/gobby/sessions/summary_context.py`, `src/gobby/sessions/workspace_context.py`

`_build_summary_prompt_context` (`summary_context.py:161-176`) injects four
repo-wide inputs into every session summary: `handoff_ctx.git_status` (:164),
`get_file_changes(...)` (:165, includes `git ls-files --others` — other
sessions' untracked artifacts), `get_git_diff_summary(...)` (:166, full
`git diff HEAD` capped at 8000 chars), and `_format_structured_context(...)`
(:167, formats `git_commits[:10]`). Root cause: summary inputs are computed
from the shared repo root for any non-isolated session
(`workspace_context.py:33-46` resolves the terminal cwd; reviewer sessions
run `isolation: none`). The `git log` runs in `enrich_git_context`
(`workspace_context.py:66-87`) unguarded, unlike `git_status` behind the
`:51` guard.

Correlate git output against `handoff_ctx.files_modified` (the
transcript-derived set), or drop lines 164-167 entirely for sessions that
made no edits. Make the `git log` block conditional, matching the guarded
`git_status`. Second-order win: `summary_context` feeds `_source_hash_payload`
(`:179-194`) → `source_context_hash` → `choose_summary_refresh`, so repo-wide
git output in the hash forces a full LLM regeneration on any unrelated
commit — fixing the leak removes that cache thrash. Session-scoped inputs
(`transcript_summary`, `unresolved_errors`, `claimed_tasks`,
`session_memories`) are correct and stay.

**Acceptance:**

- 6.4.1 - A no-edit session's summary context contains no path outside its transcript-derived `files_modified`. test: `tests/sessions/test_summary_context.py::test_no_edit_session_excludes_repo_noise`.
- 6.4.2 - An unrelated commit no longer changes a no-edit session's `source_context_hash`. test: `tests/sessions/test_summary_context.py::test_hash_stable_across_unrelated_commits`.

### 6.5 Immutable requirements bundle [category: code] (depends: 6.2, 6.3, 7.2)
`kind: deliverable`

Target: `src/gobby/plans/review_requirements.py` (new module), `src/gobby/plans/review_evidence_io.py`, `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/install/shared/skills/plan/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/workflows/observer_plan_mode.py`, `src/gobby/install/shared/skills/plan-draft/SKILL.md`, `src/gobby/plans/review_coverage.py`, `src/gobby/plans/review_findings.py`

Canonical requirements live outside the plan artifact and today none of them
are part of the hashed evidence, so the `requirements_traceability` lane can
attest plan-to-prompt parity while never seeing user-owned requirements, or
attest against sources that changed mid-round.

The **authoritative source universe** is defined here, or two conforming
implementations snapshot different requirements and both pass their tests. It
is exactly: (a) for a task-bound review the parent task's `title`,
`description`, **and** `validation_criteria` — three distinct authoritative
fields on the Task model (`storage/tasks/_creation.py:36,111`), and acceptance
that exists only in `validation_criteria` is mandatory acceptance, so hashing
description alone yields a hash-valid bundle missing the requirements the
traceability lane is supposed to check; or for a taskless interactive review
the **anchored** initiating request; plus (b) documents the plan explicitly
designates as requirement sources — a plan cites many research
documents, sibling plans, contracts, and test files that are references, not
requirements, and nothing may promote a reference into a requirement
implicitly.

"The initiating request" is not selectable from a session ID alone: a taskless
planning session holds many user turns, its transcript keeps growing during
review, and a restart can reorder what a heuristic would pick — so implementers
would each invent a different live-transcript rule and round 2 could anchor to
a different request than round 1. When taskless `/gobby plan` starts, the
server captures a provider-neutral **request anchor** — an identity plus the
request content at that moment — before any plan work begins.

The capture site must be the event that already possesses the authoritative
identity and exact bytes, which rules out both surfaces this section named
first. `handle-plan-mode-entry.yaml` fires on `turn_start` but its only effect
is `load_skill: plan`, and the rule-evaluation namespace exposes no request
content; instructing the plan skill to copy its own prompt text yields a
model-supplied value, which is precisely what "server-owned" excludes. The
smallest existing owner that already holds all three inputs is the plan-mode
entry observer: `resolve_plan_mode` reads `event.data` at
`observer_plan_mode.py:67` and is the code that sets `plan_mode` — so it
necessarily runs before the entry rule that gates on that variable.

The capture point inside it is the **`plan_mode` false→true transition at the
shared mutation site**, not the prompt-fallback tail. `resolve_plan_mode` has
five authoritative exits and four of them return before the tail: web-chat
metadata (`:70-78`), structured hook mode (`:80-91`), Codex collaboration mode
(`:93-103`), and provider-native hook state (`:105-114`), leaving only the
workflow-variable and prompt-fallback paths (`:116-131`) reaching
`data.get("prompt")` at `:128`. Anchoring at that tail would satisfy a
prompt-fallback fixture while the common entry branches load the plan skill
with no anchor and then fail preparation closed. `data` is already in scope at
`:67` before any branch. Some exits resolve mode without *reading* those bytes — the managed web-chat
branch (`:70-78`) resolves from `metadata.get("chat_mode")` or the persisted
session, and the Codex collaboration branch resolves from the transcript — but
not reading the payload is not the same as the payload being absent, and both
of those transitions commonly carry the prompt in `event.data`. The rule is
therefore keyed on the **payload, not the branch**: on the false→true
transition, capture the request identity and exact bytes whenever the event
actually carries them, which includes web-chat and Codex entries. Only when
that content is genuinely absent does the transition reuse the anchor already
persisted for the session, and fail closed when none exists. Nothing
manufactures bytes — an empty or guessed anchor is worse than none, because it
validates — and nothing rejects a first entry that arrived with a perfectly
good request attached.
`_apply_resolved_mode` (`:175-187`) is the shared transition for four of them,
but it is **not** the only writer: the marker-based fallback
`detect_plan_mode_from_context` sets `variables["plan_mode"] = True` directly
(`:277`, `:349`), which is the sixth path and the one a plain `/gobby plan`
turn actually takes. Anchoring only in `_apply_resolved_mode` would leave that
path anchorless. Every `plan_mode` activation therefore routes through one
anchor-aware transition helper that takes the event-owned request bytes —
`_apply_resolved_mode` delegates to it, and the marker branches call it instead
of assigning the variable — so the anchor is written once, on the false→true
transition, wherever that transition happens. Later turns and restarts never
overwrite the initial bytes. The
`prepare_plan_review_round` wrapper copies both into `prior_round_context`
atomically on first preparation, and later rounds reuse the anchored content
rather than re-reading the transcript. This reuses existing persisted
session/workflow state; it adds no anchor subsystem. A missing anchor fails
preparation closed.

The marker also needs an author. `requirement-source:` appears nowhere in the
skills tree today, and the interactive plan workflow delegates document
construction to `plan-draft`, whose `## Constraints` contract never emits it —
so the parser and bundle could pass every test while no real plan ever
designates a requirement document. `plan-draft` is therefore a target here:
when the user identifies a repository document as canonical, it emits the
marker, and it preserves existing markers across revisions. It reuses the
grammar, parser, and bundle defined below; no discovery protocol and no second
transport.

"Explicitly designates" needs one exact grammar, or two implementations
recognize different sets while both passing their tests. The marker is a
line-leading `requirement-source:` label followed by exactly one
repository-relative path, recognized only inside the plan's `## Constraints`
section and only outside fenced code blocks. The parser lives with the bundle
assembler in a new `review_requirements.py` module (constraint 5) and is the
sole owner of that grammar. A malformed marker fails preparation closed rather
than being skipped — silent skipping is how a requirement quietly leaves the
bundle. Paths resolve under the project root and may not escape it; a
designated source that is missing, unreadable, or outside the root fails
closed; duplicate designations collapse to one requirement ID.

At preparation, snapshot those sources into a **requirements bundle** inside
`prior_round_context` (2.1): hashed content plus compact requirement IDs,
persisted in the same transaction as the rest of the context. The bundle rides
the 6.2 paging transport. Both adversary definitions and the review skill
direct the traceability lane to cite requirement IDs from the bundle instead
of reading live task state or unhashed documents — the staged definition still
declares live parent-task context canonical today, so migrating only the
taskless one would leave the stage-native reviewer on mutable requirements.

Those citations need a wire form, and the existing one cannot express them:
the canonical citation validator accepts repository path + SHA-256 + span and
rehashes the path from the live worktree (`review_coverage.py:345-379,457-559`).
Task title, description, `validation_criteria`, and the taskless request anchor
have no repository path, so a compliant requirement citation is rejected by the
closed shape, and citing a path instead re-reads mutable state — the exact
thing the immutable bundle exists to prevent. Widen the **existing** citation
type into one discriminated union rather than adding a parallel channel:
repository citations keep path, SHA-256, and span unchanged; requirement
citations carry `requirement_id`, `content_sha256`, and an optional span into
the immutable content, and validate against the bound bundle rather than the
worktree. Lane candidates, failure traces, the strict schemas (5.3), and the
digest consumers all take the union. The two consumers that must implement it
are owned here so the manifest leaf carries them: `review_coverage.py` holds
`_citation_list` and the rehash path (`:345-379,457-559`), and
`review_findings.py` holds the blocking failure-trace validator 1.1 defines
(`:49-158`). One union validator serves both; there is no parallel citation
channel. Both files sit ahead of 6.5 in the shared-file order already.

**Acceptance:**

- 6.5.3 - Both adversary definitions and the review skill direct the traceability lane to the bundle's requirement IDs, and the staged definition no longer declares live parent-task context canonical. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.
- 6.5.4 - The marker parser is exercised against canonical, duplicate, fenced-code, outside-project, missing, unreadable, malformed, and ordinary-reference fixtures; only declared sources enter the bundle and malformed markers fail preparation closed. test: `tests/plans/test_review_requirements.py::test_requirement_marker_grammar`.
- 6.5.5 - A stage-native review with no live task access still traces requirements from the immutable bundle. test: `tests/plans/test_review_requirements.py::test_stage_native_no_live_task_access`.
- 6.5.6 - The assembled bundle is observed directly for both task-bound and taskless modes: exact source set, stable compact IDs across reassembly, lowercase SHA-256 content hashes, duplicate collapse, ordinary-reference exclusion, and a changed source producing a changed hash under the same ID. test: `tests/plans/test_review_requirements.py::test_bundle_representation_properties`.
- 6.5.7 - Given an anchored session state, the bundle assembler reuses the round-1 anchor for later rounds rather than re-reading the transcript, and a missing anchor raises the fail-closed error. test: `tests/plans/test_review_requirements.py::test_anchor_reuse_and_missing_anchor_fails_closed`.
- 6.5.8 - The anchor is written by the plan-mode entry observer from the event's own request bytes before the plan skill loads, and no model-supplied or fixture-supplied value can satisfy it: a session whose skill-authored value disagrees with the observed request keeps the observed bytes. test: `tests/workflows/test_observer_plan_mode.py::test_entry_observer_owns_request_anchor`.
- 6.5.9 - The anchor is established on every server-recognized plan-mode entry, parameterized across all six activation paths as separate fixtures — web-chat metadata, structured hook mode, Codex collaboration mode, provider-native hook state, workflow variables, and the marker-only prompt fallback, which is asserted independently of the workflow-mode case because it writes `plan_mode` without `_apply_resolved_mode`. A branch whose event carries request content authors the anchor from those bytes — including web-chat and Codex first entries, which are asserted with content present and must not fail closed; a transition whose payload genuinely lacks content reuses the persisted anchor, and fails closed only when none exists. Neither fabricates content. No path reaches `load_skill: plan` with a manufactured anchor; later turns and a restart reuse the initial bytes without overwrite. test: `tests/workflows/test_observer_plan_mode.py::test_anchor_written_on_every_entry_branch`.
- 6.5.10 - No production writer sets `plan_mode` true outside the anchor-aware transition helper; a direct assignment added anywhere in the observer fails the check. test: `tests/workflows/test_observer_plan_mode.py::test_single_plan_mode_activation_path`.
- 6.5.12 - A requirement citation naming a `requirement_id` and `content_sha256` from the bound bundle validates without touching the worktree, a repository citation still validates by path and SHA, and a requirement citation whose hash disagrees with the bundle is rejected. The union round-trips through lane candidates, failure traces, and the strict schemas. test: `tests/plans/test_review_coverage.py::test_citation_union_repository_and_requirement`.
- 6.5.11 - `plan-draft` emits a `requirement-source:` marker when the user designates a repository document as canonical, preserves existing markers across a revision, and emits none when no document is designated — so a plan authored through the real drafting path yields a non-empty bundle. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`.

## P7: Minimal repair and telemetry
`kind: framing`

**Goal**: Remedies are sized to the defect, and convergence becomes measurable.

### 7.1 Split defect from minimal repair [category: code] (depends: 1.1, 4.3)
`kind: deliverable`

Target: `src/gobby/plans/review_findings.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`

8 of 17 remedies in one wiki-plan round were larger than the minimal correct
repair (artifact grew 28 → 38 sections), while `#9602` fixed a fence with
less mechanism than proposed. In the finding schema, require
`minimal_repair`: the smallest edit to **existing** sections that closes the
failure trace. A remedy proposing a new deliverable must justify why no
existing host section can own the obligation.

That conditional rule needs a machine-readable discriminant, or the validator
cannot enforce it: equivalent prose can describe either branch, so a free-form
`minimal_repair` string leaves the validator with no way to know when the
justification is mandatory — a required input with no producer. Findings
therefore carry `repair_scope: existing_sections | new_deliverable`. The
existing branch reuses `section_id` and `participating_section_ids` for its
host; `new_deliverable_justification` is required only on the new branch and
rejected on the other. These ride the taskless, staged, and contract schemas
already being updated, in their existing owner order — no new subsystem.
Enforcing a new required field in a closed schema needs the **producers** to
emit it, or the first real round rejects a compliant reviewer: both adversary
result contracts are targets here so the taskless and staged producers gain
`repair_scope` and its conditional justification together, validated by the one
shared validator rather than two divergent shapes.

**Acceptance:**

- 7.1.1 - Findings carry a validated `minimal_repair` and a `repair_scope` discriminant; `new_deliverable` without `new_deliverable_justification` is rejected, `existing_sections` carrying that justification is rejected, and the discriminant round-trips through the taskless, staged, and contract schemas. Both adversary result contracts declare the field, so a reviewer following either contract produces a result the shared validator accepts. test: `tests/plans/test_review_findings.py::test_minimal_repair_required`.

### 7.2 Convergence telemetry [category: code] (depends: 4.3, 5.3, 6.2, 6.3, 7.1)
`kind: deliverable`

Target: `src/gobby/plans/review_telemetry.py` (new module), `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/mcp_proxy/tools/agents_termination.py`, `src/gobby/agents/agent_health.py`, `src/gobby/agents/agent_cleanup.py`, `src/gobby/hooks/session_coordinator.py`, `src/gobby/hooks/event_handlers/_session_end.py`, `src/gobby/workflows/engine/enforcement_completion.py`, `src/gobby/agents/run_completion.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`, `src/gobby/storage/tasks/_transitions_facade.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/mcp_proxy/tools/agent_cancellation.py`, `src/gobby/mcp_proxy/tools/agent_messaging.py`, `src/gobby/storage/inter_session_messages.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_health.py`

Persist per plan and round: repeated `check_key`s, reviewer-miss count,
fixer-induced count, artifact growth (section/target/acceptance deltas),
remedy scope, calls per finding, per-lane duration, wall time, and ledger
entries carried. Telemetry measures the finished system, so it depends on
every producer it measures — the repair gate (via 4.3's chain), sweep
records (via 5.3 → 6.2 → 5.2), orchestration (6.3), and minimal repair
(7.1) — and cannot freeze its schema before they exist.

Metrics are **split by authority**, because the reviewer cannot know its own
totals at the moment it sends its result — wall time, final turn count, and
calls per finding are all still accumulating, so requiring the reviewer to
report them invites fabricated or partial values that strict validation would
happily accept:

- **Reviewer-emitted** (it alone knows these): the reviewer-miss and
  fixer-induced classifications, remedy scope, repeated check keys, ledger
  entries carried, artifact deltas — each with the contributing
  finding/ledger IDs and the inputs behind the classification. Each
  classification record also carries a `check_key_class` beside those IDs: the
  reviewer already assigns `check_key`, so grouping related keys is its
  judgment to record, and E1.1.3's consecutive-round comparison needs it as a
  comparable field rather than the human reading `adversary-convergence.md`
  describes. That record is its **only** carrier — `check_key_class` is not a
  top-level finding field, which the closed finding validator
  (`review_findings.py:24-46,117-127`) would reject, and the two placements
  would otherwise produce incompatible round results from one section. No
  server-side taxonomy and no second schema field. These ride a
  `convergence_telemetry` object inside the canonical `round_result` under a
  strict published schema (extending 5.3's wrapper), validated by
  `review_telemetry.py` at the evidence-bound finalize. A reviewer emits this
  only if its prompt tells it to, and `send_message` persists the payload
  verbatim, so nothing downstream can reconstruct a field that was never
  written: the shared skill and both adversary result contracts are targets
  here, carrying the exact field list, provenance requirements, zero-value
  semantics (an absent count and a genuine zero are distinguishable), and
  which fields apply on which verdict branch. Those prompt edits sit in the
  same owner chain as the rest (1.1 → 3.1 → 4.3 → 7.1 → 6.1 → 6.3 → 6.2 →
  7.2 → 6.5 → 9.3, a total order over every section editing bundled content).

  The **staged** reviewer needs its own transport: it never calls
  `send_message`, terminating instead through `approve_review` /
  `reject_review` (`mcp_proxy/tools/tasks/_stage_review.py:302`), whose
  schemas expose no telemetry object, after which the task transition
  reconstructs `round_result` without it. Reviewer-owned telemetry emitted on
  that path is therefore dropped before anything can persist it. Carry the
  strict object through approve/reject into the canonical round result;
  `_transitions.py` is at 977 lines (constraint 5), so the construction helper
  goes in a sibling module.
- **Daemon-derived** (authoritative only after the run ends): wall time, tool
  calls, and turns, read from the bound `AgentRun`. Per-lane duration and
  per-lane call counts have no daemon-side source today — native lane
  subagents emit no events (constraint 3) — so either instrument them at the
  orchestrator or persist an explicit `unavailable` reason. Never leave the
  field silently absent.

The two halves need **one owned ordering**, or the merge races the parent: the
parent is woken on the run's terminal transition and can finalize the evidence
while self-termination cleanup is still computing final calls and turns, after
which a late aggregate merge collides with an already-finalized
`round_result`. Two validation states make the ordering checkable rather than
implied: a result is **delivered** when the reviewer-owned fields validate at
`end_agent_run` (3.1's guard checks this state, so it never rejects a result
merely for lacking daemon aggregates), and **enriched** once the daemon
aggregates are merged. Finalization requires enriched, so it can never accept
a result with missing daemon fields.

The ordering rule: terminal cleanup settles the counters and idempotently
merges the aggregates into the bound result **before** the parent is woken, on
every terminal path. Those paths run through different code today —
self-termination via `mcp_proxy/tools/agents_termination.py`, timeout and
failure via `agent_health.py` into `agent_cleanup.py` — so extract one terminal
helper they all delegate to rather than duplicating the sequence.
`agent_cleanup.py` is at 980 lines (constraint 5) and receives delegation only.
Timeout and failure expire the evidence (6.3), so their aggregates merge into
the run record rather than a finalized round result, and only the direct parent
is woken. 6.3 states the staged timeout policy but owns definitions and
configuration, so the **runtime** side of that policy lives here with the other
terminal transitions: on the staged variant the helper expires the old
evidence and restores the stage to its pre-review state before waking the
coordinator, leaving the retry to prepare and dispatch fresh evidence. Without
that, an implementer holding only this section satisfies every runtime
criterion while a timed-out stage stays stuck mid-review.

Several routes reach terminal state **without** `end_agent_run` and would
otherwise bypass both this enrichment and 3.1's canonical-result guard. The
rule is stated as an invariant rather than a list, because enumerating routes
one at a time is how this hole has stayed open: **every** call site that
transitions a run with bound plan-review evidence to a terminal state routes
through the extracted helper. A delivered result enriches; anything else fails
the run and expires its evidence. Runs with no bound evidence keep their
current behavior. The known routes today, all of which must be converted and
none of which may be assumed exhaustive:

| Route | Entry point |
| --- | --- |
| Self-termination | `mcp_proxy/tools/agents_termination.py` |
| Timeout / health failure | `agents/agent_health.py` → `agents/agent_cleanup.py` |
| Provider exit at SESSION_END | `SessionCoordinator.complete_agent_run` (`hooks/session_coordinator.py:498`) |
| Workflow termination | `EnforcementCompletionMixin._complete_agent_workflow_run` (`workflows/engine/enforcement_completion.py:95`) |
| Workflow fallback | `complete_and_notify_agent_run` (`agents/run_completion.py:24`) |
| Explicit kill | `terminalize_killed_agent_run` (`mcp_proxy/tools/agent_cancellation.py:101`), whose error branch calls `run_storage.fail` with direct delivery |
| Cancellation | `terminalize_cancelled_agent_run` (`mcp_proxy/tools/agent_cancellation.py:56`), including its lifecycle-less direct transition fallback |
| Deferred tmux health check | `_deferred_tmux_health_check` (`mcp_proxy/tools/spawn_agent/_health.py:92`, scheduled at `:152`), which marks the run failed and delivers directly — and races evidence binding, since dispatch binds immediately after `spawn_agent_impl` returns (`dispatch/spawn.py:328-367`) |
| Unregister cancellation | `agents_query_tools.py:616` → `AgentRunner.cancel_run` (`agents/runner_queries.py:59`) → storage cancellation (`storage/agents/_lifecycle.py:339`), which reaches terminal state ahead of the generic delivery path |

Each row has a fallback or error branch that bypasses the primary path, which
is exactly where previous rounds' repairs leaked; the acceptance item below
therefore searches for terminalizing call sites rather than trusting this
table to stay complete.

The delivered → enriched progression must also be **monotonic**, or the two
halves race. `send_message` delivers to the parent mailbox and separately
writes `agent_runs.result`, so a mailbox success with a failed write leaves
`end_agent_run` unable to observe delivered state, and a duplicate or late
send arriving after terminal cleanup can overwrite an enriched result with the
raw reviewer payload. For a run with live bound evidence: persist and validate
delivered state before acknowledging parent delivery, and guard every result
write with a state predicate (compare-and-set) that refuses to regress
enriched or terminal state. This reuses the same helper and state model — no
second delivery path. A reviewer whose provider simply exits
would therefore be recorded as a clean success having delivered nothing —
exactly the hole P3 exists to close, reached by a different door. Both routes
delegate to the same terminal helper for evidence-bound runs: if the result is
already in the delivered state it is enriched normally, otherwise the run fails
and its evidence expires. Runs with no bound plan-review evidence keep their
current behavior unchanged.

The staged verdict path needs the same ordering, and today it inverts it.
`approve_review` / `reject_review` finalize inside the verdict tool call —
before `end_agent_run` — while finalization requires the enriched state that
only post-termination cleanup can produce. Adding a delivered-state writer to
the staged wrappers alone leaves that deadlock intact, and 3.1's guard still
has nothing to check, because the staged reviewer never calls `send_message`
and so never writes `AgentRun.result` at all. The resolution keeps one owned
ordering rather than a second staged one: staged approve/reject record a
**durable delivered intent** and mutate no stage state, and the same extracted
terminal helper then settles counters, enriches, finalizes evidence, commits
the stage transition, and wakes the coordinator as one replayable ordered
transition. Delivered intent is written through the same run- and
evidence-bound compare-and-set writer `send_message` uses, so both producers
reach 3.1's guard through one state model.

Moving the verdict commit behind that boundary moves **every effect that
depends on the verdict** with it. The staged wrappers today also mint and
backfill review lessons, link persisted artifacts, release claims, notify or
relay, and tick workflow state; `plan-adversary.yaml:317,333,341,397,406` and
`plan/SKILL.md:155,181` make the workflow wait on `lesson_mint_status` before
the run ends. Left unordered, those effects either run before durable intent —
where a crash exposes uncommitted verdict state — or after terminal commit
with no replay owner, and the lesson-mint wait deadlocks against a terminal
owner that has not run. The whole verdict-dependent bundle therefore runs
inside the same terminal helper after enrichment and stage commit, reusing the
existing mint recovery/checkpoint for replay, and the workflow path becomes
durable intent followed directly by `end_agent_run`. No second state machine.

Replay only converges if each effect is **idempotent across the window between
its durable write and the checkpoint that records it**, and one of them is not:
`_relay_signoff_to_build_coordinator_sync` (`_stage_review.py:107-174`) calls
`create_message`, which mints a fresh UUID and does an unconditional INSERT
(`storage/inter_session_messages.py:141-194`), so a crash after that INSERT
duplicates the relay on replay. Naming lesson-mint recovery alone is not
enough. Each effect in the bundle takes a stable idempotency key built from
identity the transition already has — evidence ID, run ID, effect kind, and
target — through its existing API, so a replay finds its own prior write
instead of creating a second one.

The key has to reach a **durable uniqueness boundary**, not just the caller:
`create_message` mints `uuid.uuid4()` at `inter_session_messages.py:163`, so a
stable key held above that call changes nothing. Reuse the message primary key
itself as the carrier — derive the message ID deterministically from evidence
ID, run ID, effect kind, and target, pass it into `create_message`, and on
conflict verify and reuse the existing row. `inter_session_messages.py` is a
target here for that reason. The same duplication exists one layer up in
delivery: `send_message` commits the parent mailbox message before it writes
`AgentRun.result` (`agent_messaging.py:175-209`), so a mailbox success followed
by a failed result write produces a second canonical round-result message on
retry, and the result CAS cannot detect it because it never had the message's
identity. Either give the mailbox insert the same deterministic identity or
perform the insert and the delivered-state CAS in one transaction. Introduce an
outbox only if some existing effect API cannot accept the key.

Exactly-once applies to the **durable** effects, not the live wake. The
completion registry and debounce state are process-local
(`events/completion_registry.py:32`, cleared during `agent_cleanup.py:171`),
so proving one wake across a crash or restart would require a durable wake
protocol that nothing else needs. Correctness rests where durable identity
already exists: one deterministic result message that the recipient consumes
idempotently. The wake is at-least-once and best-effort, and a duplicate wake
costs a redundant read of an already-correct result.

The record persists on the evidence row's existing `round_result` and the E1
baseline-comparison artifact is its reader: one direct consumer, no new
dashboard or telemetry subsystem. Calls per finding is a dryness signal, not
an efficiency target — it rose 4.6 → 62 along a converging sequence, and
`adversary-convergence.md` explicitly retracts the earlier "high calls =
waste" claim. Record it; do not optimize it.

**Acceptance:**

- 7.2.2 - Every reviewer-miss and fixer-induced classification carries its contributing finding/ledger IDs and classification inputs, and the E1 comparison artifact is derivable from the stored records alone. test: `tests/plans/test_review_telemetry.py::test_classification_provenance`.
- 7.2.3 - The wrapper publishes the strict `convergence_telemetry` schema and rejects a malformed object at the schema layer. test: `tests/mcp_proxy/test_review_evidence_schemas.py::test_convergence_telemetry_schema`.
- 7.2.4 - Run aggregates are derived from the bound `AgentRun` for successful, timed-out, and failed runs, and per-lane figures that have no source carry an explicit `unavailable` reason rather than being omitted. test: `tests/plans/test_review_telemetry.py::test_daemon_derived_aggregates_across_terminal_states`.
- 7.2.5 - Aggregates merge before the parent wakes: a parent that finalizes immediately on wake still observes the merged values, and the merge is idempotent under retry. test: `tests/plans/test_review_telemetry.py::test_merge_precedes_parent_wake`.
- 7.2.11 - A timed-out adversary run is classified `inconclusive`/`timeout` per 3.1's union, retains its terminal result, and wakes only the direct parent session. On the staged variant the same helper additionally expires the old evidence and restores the stage to its pre-review state, after which fresh evidence prepares and dispatches; a run left mid-review restores no differently than one that never started. test: `tests/agents/test_adversary_timeout.py::test_timeout_classification_retention_and_wake_isolation`.
- 7.2.6 - The delivered state validates reviewer-owned fields only, so the completion guard accepts a result before enrichment; finalization requires the enriched state and rejects missing daemon fields. Every terminal path routes through the one extracted helper. test: `tests/plans/test_review_telemetry.py::test_delivered_and_enriched_states`.
- 7.2.7 - Every route in the terminal-path table is exercised with bound evidence — SESSION_END provider exit, workflow termination via the lifecycle terminalizer, the `complete_and_notify_agent_run` fallback, explicit kill including its `run_storage.fail` error branch, and cancellation including its lifecycle-less direct transition — and each fails the run and expires its evidence unless a delivered result is present, in which case it enriches and notifies in that order. A run with no bound evidence is unaffected. test: `tests/agents/test_terminal_paths.py::test_no_terminal_route_bypasses_guard`.
- 7.2.12 - Delivered state is persisted and validated before parent delivery is acknowledged, and result writes are compare-and-set: a mailbox success with a failed result write is detected, a duplicate retry is idempotent, and a late send racing enrichment cannot regress an enriched or terminal result. test: `tests/agents/test_terminal_paths.py::test_result_state_is_monotonic`.
- 7.2.13 - The terminal helper orders staged verdict handling as settle → enrich → finalize → commit stage → verdict-dependent effects → wake, driven against injected run and evidence state; a crash injected before and after each boundary replays to the same durable state, and no verdict-dependent effect runs before durable intent. test: `tests/agents/test_terminal_paths.py::test_staged_verdict_terminal_ordering`.
- 7.2.14 - Every verdict-dependent effect — signoff relay, artifact linking, claim release, lesson minting, workflow tick, and parent wake — survives a crash injected after its durable write and before its checkpoint, replaying to exactly one durable effect. The deterministic identity reaches the message primary key, so a replayed signoff relay conflicts with and reuses its own row instead of inserting a second. The wake is at-least-once: a duplicate wake is asserted harmless because the recipient consumes the deterministic result idempotently. test: `tests/agents/test_terminal_paths.py::test_verdict_effects_idempotent_across_replay`.
- 7.2.15 - Reviewer-result delivery is retry-safe across its two durable writes: a crash after the parent mailbox insert and before the `AgentRun.result` write replays to exactly one canonical round-result message, and a crash after the result write and before acknowledgement does not produce a second. test: `tests/agents/test_terminal_paths.py::test_delivery_mailbox_and_result_are_one_identity`.
- 7.2.16 - The deferred tmux health check routes its terminal decision through the extracted helper once evidence is bound, on both sides of the bind race: firing immediately before `bind_evidence_run` keeps the existing pre-bind spawn-failure behavior, and firing immediately after settles, expires the evidence, and wakes only the direct parent rather than calling `run_storage.fail` and delivering directly. Unregister cancellation routes through the same helper. test: `tests/agents/test_terminal_paths.py::test_deferred_health_check_respects_evidence_bind`.
- 7.2.10 - A search for run-terminalizing call sites (`run_storage.fail` / `run_storage.complete` and the lifecycle terminalizers) finds none that reaches a terminal state for an evidence-bound run without the extracted helper, so the table cannot silently fall out of date. test: `tests/agents/test_terminal_paths.py::test_all_terminalizing_call_sites_route_through_helper`.
- 7.2.8 - The shared skill and both adversary result contracts specify the exact `convergence_telemetry` fields, provenance, zero-value semantics, and per-verdict availability; a payload emitted verbatim by each producer survives `send_message`, delivery enrichment, and finalization. test: `tests/plans/test_review_telemetry.py::test_producer_contract_survives_delivery`.
- 7.2.9 - Entering through `LocalTaskManager`, telemetry emitted by the staged reviewer survives `approve_review` and `reject_review` — across the facade's signature forwarding and the transition's result construction — into the persisted canonical round result at finalization; the concrete result-builder lives in a sibling module, leaving `_transitions.py` under 1,000 lines. test: `tests/plans/test_review_telemetry.py::test_staged_path_carries_telemetry`.

## P8: Service integration ownership
`kind: framing`

**Goal**: One owner for the shared review-evidence service, so parallel leaves
never race on it and it stays under the line limit.

### 8.1 review_evidence.py integration owner [category: refactor] (depends: 2.6, 4.3, 5.1, 6.2, 6.5, 7.2)
`kind: deliverable`

Target: `src/gobby/plans/review_evidence.py`

Almost every phase needs a call site in `review_evidence.py`: preparation
(2.2, 5.2, 6.5), snapshot assembly (2.4), coverage validation (5.1),
finalization (4.1, 4.3, 7.2), and inter-round diffing. The file is at
966/1000 lines (constraint 5), so unordered concurrent leaves would both
conflict with each other and push it over the limit — the very failure this
plan exists to prevent.

This deliverable is the single owner of that file. It lands after every
upstream schema and helper module, performs **call-site wiring only** (no new
logic, which lives in `review_repair.py`, `review_ledger.py`,
`review_telemetry.py`, `review_evidence_io.py`, and `review_coverage.py`),
and extracts existing helpers into a sibling module wherever needed to finish
under 1,000 lines. No other deliverable edits `review_evidence.py`; their
prose says "call-site wiring only" precisely because that wiring is collected
here.

Single ownership only works if upstream leaves can still **close** before it
lands, and prose alone does not achieve that: the manifest compiler derives
each leaf's obligations from the acceptance items that literally sit under it,
so a service-level assertion left upstream is an assertion that leaf cannot
satisfy at close. Every service-bound item is therefore **physically located
here**, with 8.1.x IDs. Upstream deliverables keep only what their own module
and the MCP wrapper can prove: validation, refusal, merge, and derivation
against injected inputs. 2.5's end-to-end harness depends on this section as
well as 2.6.

**Acceptance:**

- 8.1.1 - `review_evidence.py` is under 1,000 lines after all wiring lands. file: `src/gobby/plans/review_evidence.py`.
- 8.1.2 - Preparation, snapshot assembly, coverage validation, and finalization each invoke their upstream module through this file, with no duplicated logic. test: `tests/plans/test_review_evidence.py::test_service_integration_call_sites`.
- 8.1.3 - The full plan-review service suite passes end to end with every phase wired: prepare → snapshot → coverage → finalize → next-round context. test: `tests/plans/test_review_evidence.py::test_full_round_lifecycle_integration`.
- 8.1.4 - Every upstream deliverable's acceptance passes against its module and the MCP wrapper alone, with this file's wiring absent. The check enumerates the helper-local criteria each upstream leaf retains — validation, refusal, merge, derivation, classification, and parsing against injected inputs — and asserts that no upstream item names production preparation, snapshot assembly, finalization, or evidence expiry. test: `tests/plans/test_review_evidence.py::test_upstream_leaves_close_independently`.
- 8.1.5 - Preparation persists the whole `prior_round_context` atomically; a failure mid-preparation leaves no partially populated context and no evidence row, and a later read reconstructs it from the row alone after the live sources have changed. test: `tests/plans/test_review_evidence.py::test_prior_round_context_atomic_and_source_independent`.
- 8.1.6 - A non-empty consumer inventory succeeds on its first preparation call with no coordinator pre-disposition, and its sites appear in the required sweep universe. test: `tests/plans/test_review_evidence.py::test_inventory_first_call_succeeds`.
- 8.1.7 - The settled `index_token` is stored in `prior_round_context` and survives a restart round-trip; a mismatch between inventory time and verification time is detected. test: `tests/plans/test_review_evidence.py::test_index_token_persistence`.
- 8.1.8 - Preparation snapshots the requirements bundle into the evidence row, and traceability succeeds from that bundle alone when live task access is unavailable. test: `tests/plans/test_review_evidence.py::test_requirements_bundle_persisted_and_sufficient`.
- 8.1.9 - Finalization persists the merged quality ledger and the next round's preparation reads it back. test: `tests/plans/test_review_evidence.py::test_ledger_round_trip_through_finalize`.
- 8.1.10 - The evidence-bound finalize path rejects a round result whose findings fail the finding validator and refuses approval while any blocking finding remains. test: `tests/plans/test_review_evidence.py::test_finalize_validates_findings_and_blocks_approval`.
- 8.1.11 - Each finalized round validates and persists the `convergence_telemetry` object in `round_result` and reads it back durably, with repeated check keys counted across rounds. test: `tests/plans/test_review_evidence.py::test_telemetry_persisted_at_finalize`.
- 8.1.12 - The production round-N+1 `snapshot_payload` carries `prior_round_context` end to end, assembled from the persisted evidence row rather than recomputed from live state. test: `tests/plans/test_review_evidence.py::test_snapshot_carries_prior_round_context`.
- 8.1.13 - An unavailable index rolls the whole preparation back: no evidence row is written, spawn is refused, and a fresh preparation succeeds after recovery. test: `tests/plans/test_review_evidence.py::test_inventory_unavailable_aborts_preparation`.
- 8.1.15 - A finalized approval carries the server-derived merged ledger beside the manifest in the same envelope, and non-empty carried `major`/`minor` and dismissed entries survive derivation, V1 checkpoint rendering, and finalization with the same entries at every stage. The assertion fails if the ledger is empty, so a no-op derivation cannot pass. test: `tests/plans/test_review_evidence.py::test_approval_surfaces_carried_ledger`.
- 8.1.17 - The production edit → derive → attest → submit sequence closes on both routes: a round containing newly derived sites succeeds on its first preparation call, preparation rederives and accepts the matching digest, and a drifted digest is rejected back to a live producer — the taskless coordinator still in its turn, or the staged path through the existing failed-dispatch rollback — which rebuilds the attestation and succeeds. test: `tests/plans/test_review_evidence.py::test_repair_universe_production_sequence`.
- 8.1.16 - The real production paths that upstream leaves prove only against injected inputs are exercised here end to end: a taskless round-1 rejection, revision, and round-2 preparation with no fixture; a staged resubmission whose payload survives a restart before dispatch and is consumed exactly once; a settle exhaustion that rolls the whole preparation back leaving no evidence row and no spawn, with a later preparation succeeding; a real taskless plan-entry session anchoring at plan start and reusing that anchor across turns and a restart; and a real staged approve and reject reaching `end_agent_run` with a delivered result and no `send_message`, having mutated no stage state at verdict time. test: `tests/plans/test_review_evidence.py::test_production_paths_end_to_end`.
- 8.1.14 - An index-token mismatch drives the whole replacement transition: the bound child's typed `inconclusive` result expires the old evidence, the parent prepares a new evidence ID with fresh snapshot, inventory, and token, binds a distinct reviewer run, reruns all three lanes under it, and reuses no lane result from the old generation. test: `tests/plans/test_review_evidence.py::test_index_token_mismatch_replaces_run`.

## P9: Incidental fixes
`kind: framing`

**Goal**: Land the three small defects found during this planning cycle.

### 9.1 Collapsed block reasons keep their recovery directive [category: code]
`kind: deliverable`

Target: `src/gobby/workflows/engine/evaluation.py`, `src/gobby/workflows/engine/blocked_tool_recovery.py`

`evaluation.py:182-197` collapses repeat blocks within a turn to a stub;
`block_reason_signature` (`blocked_tool_recovery.py:41-43`) hashes the full
reason text, so a reason with a stable dynamic part (a pending
`recall_request_id`, a task id) collapses on every block after the first.
Observed twice this planning cycle in parallel batches; acting on a stub once
cost six round-trips. Keep the prose collapse, always re-append the recovery
directive:

```python
if block_signature in shown:
    response.reason = (
        f"Rule enforced by Gobby: [{resolved_rule_name}] "
        "(full reason shown earlier this turn — scroll up)."
        + recovery_directive_suffix(response.reason)   # new
    )
```

`blocked_tool_recovery.py:21-31` parses only the rule-name prefix; recovery
state is tracked structurally (`remember_blocked_tool_recovery_state`,
`:109-119`), never parsed from reason text — so `recovery_directive_suffix`
is a new helper colocated there that lifts the recovery directive out of the
original full reason before the collapse discards it.

Extraction is defined around a **complete actionable directive**, not the
literal `call_tool` token on one physical line. Enabled rules use other action
verbs and other layouts: `require-memory-review-before-status` directs a
`set_variable` with no `call_tool` at all, and `block-gobby-tasks-cli.yaml:17`
splits one `call_tool(...)` invocation across physical lines. A line-oriented
lift would emit an unusable fragment or nothing while a one-line fixture still
passed. The helper therefore preserves the whole directive — balanced through
its closing parenthesis, spanning lines where needed — and the acceptance
fixtures enumerate the enabled action syntaxes rather than pinning the one
cited rule. Rejected alternatives: exempting whole rules from collapse (loses
the savings on genuinely repetitive gates); an `always_verbose:` block-effect
field (more surface than the problem warrants).

**Acceptance:**

- 9.1.1 - A collapsed repeat block still carries its one-line recovery directive. test: `tests/workflows/test_block_rendering.py::test_collapsed_reason_keeps_directive`.
- 9.1.2 - Reasons without an actionable directive collapse to the bare stub unchanged. test: `tests/workflows/test_block_rendering.py::test_no_directive_collapses_clean`.
- 9.1.3 - Fixtures are derived from an inventory of the actionable reasons in the enabled bundled rules rather than a hand-listed set, covering at minimum single-line `call_tool`, a `call_tool` split across physical lines, direct MCP-tool calls, a `set_variable` directive with no `call_tool`, backticked shell or `gcode` commands, and alternative-command lists. Each collapsed reason carries a complete executable command rather than a fragment, and the inventory check fails when an enabled rule introduces a form the extractor cannot render. test: `tests/workflows/test_block_rendering.py::test_directive_forms_survive_collapse`.

### 9.2 Remove the orphaned search_memories injection renderer [category: refactor]
`kind: deliverable`

Targets: `src/gobby/hooks/dispatchers/mcp.py`, `src/gobby/workflows/engine/delivery_formatting.py`, `src/gobby/workflows/engine/effects.py`, `src/gobby/workflows/engine/injection_tracking.py`, `src/gobby/workflows/engine/core.py`, `src/gobby/hooks/factory.py`, `docs/contracts/memory-usefulness-label.md`, `tests/hooks/test_hook_manager.py`, `tests/workflows/test_delivery_pipeline.py`

Task #18897 moved daemon-owned recall off turn-start injection; the renderer
was left behind. Verified against the installed DB: zero enabled rules
produce a `search_memories` result for injection. Dead surface to delete, per
file:

| File | Dead symbols |
| --- | --- |
| `hooks/dispatchers/mcp.py` | `PROJECT_MEMORY_CONTEXT_BUDGET` (`:30`), the `search_memories` formatter branch (`:194-197`), `_is_review_lesson_memory` (`:251-262`, only caller is the `:196` branch), `_project_memory_omitted_line` / `_render_project_memory` / `_project_memory_render_len` / `_project_memory_next_line_budget` / `_fit_memory_line` (`:265-306`), `ProjectMemoryRenderOutcome` / `format_project_memories_with_outcome` / `_format_project_memories` (`:309-395`) |
| `workflows/engine/delivery_formatting.py` | duplicate `_is_review_lesson_memory` (`:30-36`), `_format_search_memories_result` (`:42-138`) |
| `workflows/engine/effects.py` | the `search_memories` inject-dispatch branch (`:155-164`); the review-lessons branch at `:165-185` stays |
| `workflows/engine/injection_tracking.py` | `_filter_new_memories`, `_track_injected_ids`, `_append_outcome_row`, `_record_injection_outcomes`, and `_record_payload_drop` (`:123-145`, zero callers already) |
| `workflows/engine/core.py` + `hooks/factory.py` | engine-level `injection_outcome_recorder` wiring (`core.py:97,109`, `factory.py:535-551`) |

Verified-safe seams: the `recall_injection_outcomes` table keeps its second
writer (`memory/recall.py:168-175`) and live readers
(`recall_shadow_signals.py:305-401` consumers); the live
`_format_review_lessons_result` depends only on `_is_empty_inject_payload`,
`_filter_and_track_new_review_lessons`, and `format_review_lesson_guidance`.
Update or delete pinned tests: `tests/hooks/test_hook_manager.py:13,795,814`
and `:642-728`; `tests/workflows/test_delivery_pipeline.py:127-350` (and its
`:323` `injection_outcome_recorder` wiring). Update project memory
`12798b8b` (documents the two rendering paths) once this lands.

`docs/contracts/memory-usefulness-label.md` is normative and names the deleted
surface directly — `EffectsMixin._format_search_memories_result` at lines 63
and 236 — so deleting the code without rewriting the contract leaves a false
contract that every acceptance item below would still pass. Rewrite both
references around the surviving review-lesson delivery and memory-context
paths.

**Acceptance:**

- 9.2.1 - All listed `mcp.py` symbols are removed and no `src/` reference to them remains. file: `src/gobby/hooks/dispatchers/mcp.py`.
- 9.2.2 - `_format_search_memories_result` and the duplicate `_is_review_lesson_memory` are removed. file: `src/gobby/workflows/engine/delivery_formatting.py`.
- 9.2.3 - The `search_memories` inject-dispatch branch is removed and the review-lessons branch is untouched. file: `src/gobby/workflows/engine/effects.py`.
- 9.2.4 - The four generic tracking helpers and `_record_payload_drop` are removed; `_filter_and_track_new_review_lessons` is untouched. file: `src/gobby/workflows/engine/injection_tracking.py`.
- 9.2.5 - The `injection_outcome_recorder` wiring is removed from engine core and hook factory. file: `src/gobby/workflows/engine/core.py`.
- 9.2.6 - Tests pinning the dead path are updated or deleted. file: `tests/workflows/test_delivery_pipeline.py`.
- 9.2.7 - The live review-lessons injection path still renders end to end via `inject-review-lessons-for-touched-files`. test: `tests/workflows/test_delivery_pipeline.py::test_review_lessons_path_survives_orphan_removal`.
- 9.2.8 - The memory-usefulness contract describes the surviving delivery paths and retains no reference to any deleted symbol. file: `docs/contracts/memory-usefulness-label.md`.

### 9.3 Harden the recall-gate block reasons and refresh the bundled manifest [category: config] (depends: 6.1, 6.3, 6.5)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/rules/memory-lifecycle/require-memory-recall-retrieval.yaml`, `src/gobby/install/bundled_content_manifest.json`

The recall pipeline is deliberately asynchronous (`MemoryRecallDispatcher`
background thread; `MemoryRecallDeliveryQueue.queue`,
`hooks/memory_recall_delivery.py:37-64`, upserts after the prompt hook
returns), so the request id does not exist at prompt-context time and the
`before_tool` block **is** the announcement channel — its wording is
load-bearing. Append to the `before_tool` reason: "This is the only permitted
call — every other tool stays blocked until it completes. Issue it alone, not
inside a parallel batch." (Sibling calls in the same batch are evaluated
before the retrieval lands.) Append the single-permitted-call sentence to the
`turn_end` reason. Bundled-rule drift refresh applies the new text to the
installed row while preserving the enabled toggle; no migration, no engine
change.

Every edit to a bundled skill, agent definition, or rule template changes a
hash in `src/gobby/install/bundled_content_manifest.json`, and
`tests/test_build_backend.py:508` asserts the committed manifest matches the
shared tree. Leaving it stale fails that test; letting each leaf regenerate it
creates the shared-target race this plan is otherwise eliminating. This
deliverable lands last among the bundled-content editors (1.1, 4.3, 6.1, 6.3,
6.5 and its own rule change) and regenerates the manifest exactly once with
the canonical writer.

**Acceptance:**

- 9.3.1 - Both rule reasons carry the only-permitted-call directive and the solo-call instruction. file: `src/gobby/install/shared/workflows/rules/memory-lifecycle/require-memory-recall-retrieval.yaml`.
- 9.3.2 - The committed bundled-content manifest matches the shared tree after every bundled edit in this plan. test: `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`.

## E1: End-to-End Verification
`kind: verification`

### E1.1 Live convergence regression [category: test] (depends: 6.4, 7.2, 8.1, 9.1, 9.2, 9.3)
`kind: deliverable`

Target: `tests/plans/test_convergence_regression.py` (new)

Manifest derivation emits tasks for deliverables, so a bare `verification`
section produces no task and nothing schedules the live comparison below —
every unit and integration leaf could close while the measurement this plan
exists to produce is never run. This deliverable is that owner. It runs the
live regression and writes the comparison artifact E1's commands describe,
recording fixer-induced count, repeated check keys, rounds-to-approval, wall
time, and carried ledger entries. The commands stay as its implementation
detail.

**Acceptance:**

- E1.1.1 - The live regression runs `/gobby plan` against `.gobby/plans/completed/context-mode-borrowings.md` and writes a comparison artifact carrying rounds-to-approval, fixer-induced count, repeated check keys, per-round wall time, and ledger entries carried, each read from persisted telemetry rather than recomputed. test: `tests/plans/test_convergence_regression.py::test_live_regression_writes_comparison_artifact`.
- E1.1.3 - The regression **fails** when convergence regresses, asserting the stated targets over persisted telemetry rather than only recording them: rounds-to-approval in single digits, zero exact `check_key` repeats, no two consecutive rounds sharing a `check_key_class`, and a strictly decaying finding tail. Exhaustive lane coverage is unchanged, and wall-time variance is bounded separately so a slow run alone does not fail it. test: `tests/plans/test_convergence_regression.py::test_convergence_targets_asserted`.
- E1.1.2 - A reviewer summary produced with concurrent editors and overlapping plan artifacts in the worktree contains no path outside its own transcript-derived `files_modified`. test: `tests/plans/test_convergence_regression.py::test_compaction_isolation_under_concurrent_editors`.

Unit and integration suites named per deliverable, plus:

- **Live regression target:** run `/gobby plan` against
  `.gobby/plans/completed/context-mode-borrowings.md` — the plan that ran 22 rounds
  without approving — and compare rounds-to-approval, fixer-induced count,
  and repeated check keys against its recorded history.
- **Compaction isolation:** with concurrent editors and overlapping plan
  artifacts in the worktree, assert a reviewer summary contains no path
  outside its own transcript-derived `files_modified`.
- **Commands:**

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/plans/ -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_summary_context.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/review_learning/ -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/ -k "block or framing or delivery" -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/ -k "lifecycle or review_evidence" -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/test_build_backend.py -k bundled_content_manifest -v
uv run gobby plans validate .gobby/plans/adversary-convergence-improvements.md
uv run ruff check src/ && uv run mypy src/
```

## V1 Plan Changelog
`kind: verification`

**Enhancement round 1 (2026-07-26)** — plan-enhancer-taskless (codex / gpt-5.6-sol, run
`ce4d5db4`), `converged: false`, 9 suggestions, all `severity: opportunity`.
Votes: E1–E8 accepted, E9 declined.

- **E1 (accepted)** → 1.1 / 4.3: shared normative severity matrix in the
  skill and contract with table-driven boundary examples.
- **E2 (accepted)** → 4.3: `round_diff.py` added to targets; narrow
  lesson-minting eligibility for `no-fix-policy` + `rounds_carried >= 3` +
  ledger source; ordinary minting keeps the blocking requirement (4.3.4).
- **E3 (accepted)** → 2.2 / 4.1: prior-finding resolution records
  (`decision: repair | carry`), server-derived required repair-ID set,
  exactly-one attestation per repair ID, subset (not intersect) hash-diff
  check, mixed repair/carry test (2.2.4).
- **E4 (accepted)** → 6.2: paging completeness contract (`snapshot_hash`,
  `total_sections`, `next_offset`, deterministic ordering) with
  reconstruction-equality integration test (6.2.4).
- **E5 (accepted)** → 7.2: `convergence_telemetry` object in the existing
  `round_result` with classification provenance; aggregate through the
  coordinator round result; E1 comparison artifact derived from records; no
  new subsystem (7.2.2).
- **E6 (accepted)** → 5.1: server-derived required sweep universe; completion
  booleans as empty set differences; query evidence for zero-result sweeps;
  four-case fixtures (5.1.3).
- **E7 (accepted)** → 4.1: immutable `ledger_entry_id` with canonical
  coalescing key; round-local IDs become aliases; hash change stales and
  splits (4.1.3).
- **E8 (accepted)** → 4.1 / 4.2 dependencies narrowed: 4.1 depends on 1.1 +
  2.1 (parallel with 2.2–2.5); 4.2 depends on 2.4 + 4.1 as the integration
  point.
- **E9 (declined)** — timeout-resume path for the 6.3 adversary timeout:
  resume protocol for a bound that has never fired (the timeout itself is new
  in this plan); the 6.3 checkpoint already preserves dispositioned
  candidates; revisit with evidence if timeouts recur in practice.

**Adversarial review round 1 (2026-07-26)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `74d77770`), verdict `needs_review`; three lanes completed,
27 candidates → 11 findings (10 blocking, 1 nit), 16 dismissed. Votes: all 11
accepted.

- **F-immutable-requirement-snapshot (accepted)** → new 6.5: hashed
  parent-task and named-document requirements bundle on the evidence row,
  scoped consumption via 6.2 transport, no-live-task-access test.
- **F-repair-gate-universe (accepted)** → 2.2 / 2.3 / 2.5 + new 2.6: the
  resolution universe is loaded server-side from the finalized prior round
  (an omitted record cannot erase a finding); deviations validate the
  original counterexample with explicit `accepted_risk`; server-derived
  consumer/variant/contract universe and repair-bundle interaction edges
  gate attestation sweeps pre-spawn.
- **F-evidence-column-lifecycle (accepted)** → 2.1 / 4.1: typed model and
  store IO for both JSONB columns with pinned write transitions
  (preparation writes attestations, finalize writes the merged ledger) and
  round-trip tests (2.1.3, 4.1.4).
- **F-interactive-finding-contract (accepted)** → 4.3: finding validator
  wired into the evidence-bound finalize/apply path; zero-blocking approval
  enforced server-side (4.3.5); taskless contract gains `failure_trace` and
  `minimal_repair`.
- **F-consumer-inventory-integration (accepted)** → 5.2 / 2.4: typed
  inventory transported into `prior_round_context` via evidence IO;
  coordinator disposition gate before spawn; fail-closed
  `inventory_unavailable` path (5.2.3); changed acceptance items and targets
  in causal context.
- **F-timeout-recovery-transition (accepted)** → 6.3: partial-result
  checkpoint dropped (E9's decline left it a writer with no reader); timeout
  classifies inconclusive, expires evidence, retries the round fresh;
  native-lane wake-isolation and result-retention regression tests (6.3.4).
- **F-telemetry-integration (accepted)** → 7.2: depends on 4.3 / 5.3 / 6.3 /
  7.1; producer, strict wrapper schema (7.2.3), finalize-time validation,
  and E1 reader pinned with a durable round-trip.
- **F-shared-target-sequencing (accepted)** → dependency chains added across
  every shared write surface: coverage 5.1→4.3; evidence IO
  2.4→4.2→5.2→6.2→6.5; MCP wrapper 6.2→5.3→7.2; prompt surfaces
  4.3→7.1→6.1→6.3→6.5; evidence store 2.1→3.1; repair module 2.2→2.3→2.4→2.6.
- **F-terminal-guard-contract (accepted)** → 3.1: strict
  `validate_round_result` parse bound to the live `evidence_id` replaces
  marker sniffing; trusted run identity seeded into invocation context at
  dispatch (breaks the circular session→run lookup);
  `create_agents_registry` production wiring with negative and
  real-constructor tests (3.1.4).
- **F-index-generation-proof (accepted)** → 6.1: index token
  (`last_indexed_at` + repository-source digest) captured after the
  coordinator index; lanes verify before/after with rerun-on-mismatch
  (6.1.3); still no schema counter.
- **N-stale-severity-diagnostic (accepted)** → 1.1: the invalid-severity
  diagnostic derives from `FINDING_SEVERITIES` (1.1.4).

**Adversarial review round 2 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `fec44e33`), verdict `needs_review`; three lanes completed,
20 candidates → 9 findings (all blocking), 11 dismissed. Votes: all 9
accepted; three taken in the reviewer's simpler form, which removed mechanism
rather than adding it.

- **F-preparation-context-lifecycle (accepted)** → 2.1: migration 343 gains a
  third column, `prior_round_context JSONB`, as the durable home for causal
  context, the consumer-site inventory (or its `inventory_unavailable`
  record), and the requirements bundle; preparation derives and persists it
  atomically in one transaction (2.1.4), and 6.5 stores the bundle there.
  Declined the proposed two-phase preview-then-prepare API — derive-and-persist
  inside the existing transaction is the least mechanism that closes it.
- **F-review-evidence-integration-owner (accepted)** → new P8/8.1: one
  integration-owner deliverable for `review_evidence.py`, depending on every
  upstream module, performing call-site wiring only and extracting helpers to
  finish under the 1,000-line limit. No other leaf edits that file.
- **F-repair-universe-integration (accepted)** → 5.1 depends on 2.4 + 5.2 and
  consumes the typed `prior_round_context` universe instead of re-deriving
  it; 2.5 depends on 2.6 and gains a strict-subset attestation refusal test
  (2.5.3).
- **F-finding-contract-surface-parity (accepted)** → 1.1: verified in source —
  the validator requires `fix` (`review_findings.py:26-36`) while the taskless
  reviewer emits `suggested_fix`, and `_stage_review.py:609` hard-codes
  `enum: ["blocking", "nit"]`. `minimal_repair` becomes the single canonical
  remedy field across validator, renderer, and taskless contract, and the
  stage-native enum gains the new severities (1.1.5, 1.1.6).
- **F-index-token-atomic-capture (accepted)** → 6.1: token capture retries
  until the pre- and post-index source digests agree, so a stale index is
  never paired with a fresh digest; preparation's inventory runs under the
  settled token (6.1.4).
- **F-terminal-guard-totality (accepted)** → 3.1: the guard accepts the full
  verdict universe including `needs_requirements` and `inconclusive` (3.1.5),
  and the run ID travels as authenticated transport metadata bound
  server-side to the child session with a per-request ContextVar, so a caller
  can neither omit nor spoof it (3.1.3).
- **F-telemetry-authoritative-production (accepted)** → 7.2: metrics split by
  authority — the reviewer emits classifications and provenance, the daemon
  derives wall time, calls, and turns from the bound `AgentRun` after terminal
  cleanup, and per-lane figures with no source carry an explicit
  `unavailable` reason (7.2.4).
- **F-scoped-fetch-lane-completeness (accepted)** → 6.2: `section_ids` lane
  splitting **removed** — it contradicted the unchanged all-sections-per-lane
  contract. Paging plus a server-issued page-coverage receipt proves lane
  completeness by transport instead (6.2.1, 6.2.4).
- **F-orphan-memory-contract (accepted)** → 9.2: verified — 
  `docs/contracts/memory-usefulness-label.md` names
  `_format_search_memories_result` at lines 63 and 236. Added to targets with
  a no-stale-reference acceptance item (9.2.8).

**Adversarial review round 3 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `4b91b7aa`), verdict `needs_review`; three lanes completed,
18 candidates → 10 findings (all blocking), 8 dismissed. Votes: 9 accepted,
1 accepted in part. Two accepted findings removed mechanism introduced in
round 2 — the coordinator's own repairs are now the dominant defect source,
exactly the fixer-induced pattern this plan targets.

- **F-remedy-contract-boundaries (accepted)** → 1.1 / 2.2: three remedy
  vocabularies stay distinct — candidates keep `suggested_fix`, findings use
  `minimal_repair`, attestations use `deviation_from_minimal_repair`
  (renamed). 1.1.5 is scoped to findings so the rename cannot strip the live
  candidate field; round-trip test 2.2.6.
- **F-integration-leaf-closure (accepted)** → 8.1 / 5.3 / 2.5: acceptance is
  split by layer so upstream leaves can close before the integration owner
  lands (8.1.4). Preparation MCP schemas and the wrapper round trip go to 5.3
  (5.3.3); service-level round trips belong to 8.1; 2.5 depends on 8.1 as
  well as 2.6.
- **F-terminal-guard-production-totality (accepted)** → 3.1: the round-result
  validator becomes verdict-discriminated — reviewed verdicts require
  canonical coverage, non-attested terminal branches require `evidence_id`
  plus typed reason data (3.1.5). A drifted reviewer never completed a lane
  and cannot produce an attestation, so round 2's "accepts all verdicts" was
  self-contradictory. The run-ID chain now names its real surfaces: stdio
  proxy → execution endpoint → session ContextVar (3.1.6).
- **F-requirements-source-universe (accepted)** → 6.5: the authoritative
  source universe is defined (parent task or initiating request plus
  explicitly designated requirement references, resolved repo-relative,
  failing closed on unreadable references), so two implementations cannot
  snapshot different requirements and both pass.
- **F-inventory-disposition-transition (accepted)** → 5.2: the coordinator
  pre-disposition gate is **removed**. Deriving the inventory inside
  preparation while refusing until its sites are dispositioned made every
  non-empty inventory unsatisfiable on the first call. Sites now join the
  required sweep universe 2.6 already validates (5.2.3).
- **F-page-receipt-consumer-binding (accepted)** → 6.2: per-lane page
  receipts **removed**. Native lanes have no server-authenticated identity,
  so one parent receipt replayed three times proves nothing; a single parent
  reconstruction receipt plus exhaustive lane section IDs and hashed
  citations is the honest evidence (6.2.4, 6.2.5).
- **F-terminal-telemetry-ordering (accepted)** → 7.2: one owned ordering —
  terminal cleanup merges daemon aggregates idempotently into the bound
  result *before* the parent wakes, on success, timeout, and failure (7.2.5).
- **F-index-token-lifecycle-order (accepted)** → 5.2 / 6.1: token capture and
  storage move to 5.2 as a typed `index_token` inside `prior_round_context`,
  since the inventory must run under it and 5.2 precedes 6.1; 6.1 keeps only
  the verification protocol (5.2.4, 6.1.3).
- **F-bundled-content-manifest-owner (accepted)** → 9.3: verified —
  `tests/test_build_backend.py:508` asserts manifest/tree parity. 9.3 depends
  on the bundled-content editors, owns
  `src/gobby/install/bundled_content_manifest.json`, and regenerates it once
  (9.3.2).
- **F-cross-language-import-inventory (accepted in part)** → 5.2: verified —
  `_module_candidates` (`consumer_sweep.py:315-323`) returns empty for any
  non-`.py` path. Accepted the correctness half: unresolvable targets are
  recorded as `language_unsupported` rather than silently omitted, so the
  inventory never claims false completeness (5.2.5). **Declined** building
  TypeScript/TSX/JS/JSX import resolution — this plan's targets contain no
  TypeScript, so that is a general-facility feature rather than a defect in
  this plan, and adding it here is precisely the scope creep the plan warns
  against. The explicit gap marker leaves a future web-surface plan an honest
  signal instead of a silent hole.

**Adversarial review round 4 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `de412927`), verdict `needs_review`; three lanes completed,
15 candidates → 9 findings (8 blocking, 1 nit), 6 dismissed. Votes: all 9
accepted. Two check keys repeated from round 3 —
`integration-owner-leaf-closure` and `immutable-requirement-source-discovery` —
the first repeats in this review, and both because a round-3 repair described
an intent in prose without making it structural. Both are now structural.

- **F-integration-manifest-closure (accepted, repeat)** → 8.1: prose about
  splitting acceptance by layer did not move anything; the manifest compiler
  reads the items that literally sit under each leaf. Eight service-bound
  acceptance items were physically relocated from 2.1, 4.1, 4.3, 5.2, 6.5,
  and 7.2 into 8.1 with 8.1.x IDs (8.1.5–8.1.11), and 2.6 joins 8.1's
  dependencies.
- **F-terminal-endpoint-target (accepted)** → 3.1: verified —
  `src/gobby/servers/routes/mcp/endpoints/execution.py` exists and owns header
  extraction, validation, context seeding, dispatch, and reset, yet was
  missing from targets, leaving the authenticated chain unowned (3.1.6 now
  tests it directly).
- **F-requirement-marker-grammar (accepted, repeat)** → 6.5: "declared
  marker" was never defined, so implementations could still diverge. The
  grammar is now exact — a line-leading `requirement-source:` label with one
  repo-relative path, recognized only in `## Constraints` and outside fenced
  code, parsed by `review_requirements.py` as sole owner, failing closed on
  malformed input (6.5.4 fixtures).
- **F-snapshot-consumer-migration (accepted)** → 6.2 / 6.5: paging changes
  the consumer contract, so the shared skill, **both** adversary definitions,
  and the existing wrapper tests migrate with it (6.2.5); the staged
  definition also stops declaring live parent-task context canonical (6.5.5).
- **F-finding-remedy-fixtures (accepted)** → 1.1: verified —
  `tests/review_learning/test_round_diff.py:51` and
  `tests/storage/test_stage_review_findings.py:148,161` construct findings
  with `fix`. Added to targets; the aliasless rename must migrate every
  literal producer (1.1.7).
- **F-inventory-unavailable-transition (accepted)** → 5.2: the persisted
  `inventory_unavailable` record was incoherent — written inside the failing
  transaction it rolls back, written outside it lets the repair-universe
  checks read a missing inventory as an empty one. Preparation now aborts and
  rolls back with a typed error and refuses spawn (5.2.4). Less mechanism,
  and the only honest state.
- **F-index-token-index-race (accepted)** → 5.2: both digests must bracket
  the index operation (D0 → index → timestamp → D1, accept only on D0 = D1),
  with a recheck after inventory derivation (5.2.5).
- **F-terminal-transition-staging (accepted)** → 7.2: verified —
  `src/gobby/agents/agents_termination.py` does not exist; the real paths are
  `mcp_proxy/tools/agents_termination.py` for self-termination and
  `agent_health.py` → `agent_cleanup.py` for timeout and failure. Targets
  corrected, delivered/enriched validation states defined so the guard never
  rejects a pre-enrichment result and finalization never accepts a
  post-enrichment gap, and one extracted terminal helper serves all three
  paths (7.2.6). `agent_cleanup.py` (980 lines) joins constraint 5's refactor
  list.
- **F-parent-receipt-ceremony (accepted, nit)** → 6.2: the parent
  reconstruction receipt retained in round 3 is **also** dropped. Under a
  stateless offset/limit API an echoed receipt proves nothing the snapshot
  hash does not; the reader concatenates pages and verifies the hash locally.
  The receipt idea has now been removed in two successive rounds — round 3
  killed the per-lane form, round 4 the parent form.

**Adversarial review round 5 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `9ff26a7b`), verdict `needs_review`; three lanes completed,
6 candidates → 4 findings (all blocking), 2 dismissed. Votes: all 4 accepted.
Findings dropped 9 → 4 and every one is a regression introduced by a round-4
repair, so the remaining tail is entirely fixer-induced.

- **F-inventory-unavailable-context-regression (accepted)** → 2.1: round 4
  removed the persisted `inventory_unavailable` record from 5.2 and left 2.1
  still instructing its implementer to write one. 2.1 now states that
  `prior_round_context` exists only after a successful preparation.
- **F-terminal-registry-constructor-target (accepted)** → 3.1: verified —
  `create_agents_registry` lives at `mcp_proxy/tools/agents_registry.py:33`
  and was absent from targets, so 3.1.4 could pass without touching the real
  constructor. Added to targets; 3.1.4 exercises injection of both
  dependencies.
- **F-terminal-path-totality (accepted)** → 7.2: verified —
  `SessionCoordinator.complete_agent_run`
  (`hooks/session_coordinator.py:498`) marks a run successful on SESSION_END
  after nonzero activity and notifies the parent, with no `end_agent_run`
  call. A reviewer whose provider simply exits would be recorded as a clean
  success having delivered nothing, reaching P3's exact hole through a
  different door. Both that route and workflow termination now delegate to
  the same terminal helper (7.2.7); non-review runs unchanged.
- **F-index-token-recovery-transition (accepted)** → 6.1: two contradictions
  from round 4's split of token ownership. 5.2 defines the capture as
  D0 → index → timestamp → D1 while 6.1 still called for a separate
  coordinator-side `gcode index` — a second index run outside the bracket is
  the exact mutation window the bracketing closes. And "rerun affected lanes
  on mismatch" was incoherent against an immutable token: recovery now
  expires the evidence and re-prepares snapshot, inventory, and token
  together (6.1.4).

**Adversarial review round 6 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `47d2eea7`), verdict `needs_review`; three lanes completed,
15 candidates → 8 findings (all blocking), 7 dismissed. Votes: all 8 accepted.
Four check keys repeated, so this round's repairs were applied as complete
sweeps rather than spot fixes: every upstream acceptance item was re-classified
helper-local or service-level in one pass, and the whole prompt-owner chain was
ordered at once.

- **F-shared-prompt-write-order (accepted, repeat)** → 6.2 / 6.3 / 4.3: round 4
  gave 6.2 skill and agent-YAML targets without placing it in the prompt chain.
  One ordered chain now exists — 4.3 → 7.1 → 6.1 → 6.3 → 6.2 → 6.5 → 9.3 — with
  9.3 still the final bundled-manifest owner.
- **F-terminal-branch-schema-parity (accepted)** → 3.1: "typed reason data" is
  now an exact JSON discriminated union keyed on `verdict` with a closed
  `reason_code` enum (`source_drift`, `missing_requirements`,
  `index_mismatch`, `timeout`), published verbatim to both adversary producers
  and the skill, with 6.1 and 6.3 pinned to it instead of describing their own
  envelopes (3.1.7). 4.3 now depends on 3.1.
- **F-requirements-bundle-representation-coverage (accepted)** → 6.5: 6.5.4
  observed source *selection* but never the bundle's representation, so an
  unstable bundle could pass. Added one helper-local item asserting exact
  source set, stable compact IDs, lowercase SHA-256 hashes, duplicate
  collapse, and changed-content identity (6.5.6).
- **F-integration-owner-acceptance-closure (accepted, repeat×3)** → 8.1: the
  round-5 move was again partial — 2.4.1 still asserted the production
  snapshot payload and 6.1.4 service-level evidence expiry. Rather than move
  two more items, every upstream acceptance item was swept and re-classified:
  2.4.1, 5.2.4, and 6.1.4 became helper-local assertions and their
  service-level halves became 8.1.12–8.1.14; 8.1.4 now enumerates the
  helper-local criteria and asserts no upstream item names production
  preparation, snapshot assembly, finalization, or expiry.
- **F-index-mismatch-run-replacement (accepted, repeat)** → 6.1: round 5's
  recovery was unimplementable — the reviewer is immutably bound to its
  evidence by a unique `dispatch_run_id` and cannot rebind to a replacement,
  and rerunning only affected lanes would mix two index generations in one
  attestation. The bound child now emits typed `inconclusive` and terminates;
  the parent expires, re-prepares, and spawns a **new** run that reruns all
  three lanes.
- **F-index-token-verifier-surface (accepted)** → 5.2 / 6.1: added the
  smallest read-only verifier beside the token producer, consuming exactly the
  producer's digest inputs and returning a typed result, with every lane
  required to invoke it immediately before analysis (5.2.8, 6.1.5).
- **F-telemetry-producer-contract (accepted)** → 7.2: the reviewer emits
  telemetry only if its prompt says to, and `send_message` stores the payload
  verbatim, so nothing downstream can reconstruct an unemitted field. The
  shared skill and both adversary contracts are now targets, carrying exact
  fields, provenance, zero-value semantics, and per-verdict availability
  (7.2.8).
- **F-terminal-workflow-owner-target (accepted)** → 7.2: verified —
  `EnforcementCompletionMixin._complete_agent_workflow_run`
  (`workflows/engine/enforcement_completion.py:95`) prefers a lifecycle
  terminalizer and falls back to `complete_and_notify_agent_run`
  (`agents/run_completion.py:24`). Round 5 promised delegation without naming
  either file, so the fallback would have silently reopened the bypass. Both
  are targets and both exits are exercised (7.2.7).

**Adversarial review round 7 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `066a9291`), verdict `needs_review`; three lanes completed,
12 candidates → 6 findings (all blocking), 6 dismissed. Votes: all 6 accepted.

- **F-shared-prompt-order-round7 (accepted, repeat)** → 6.5 / 7.2 / 3.1: this
  check key had now recurred three times because each repair added the one
  edge the reviewer named. Instead the whole set was audited: ten sections
  edit `install/shared`, and the graph left 7.2 unordered against both 6.2 and
  6.5, plus 1.1 against 3.1. Three edges make the order **total** —
  1.1 → 3.1 → 4.3 → 7.1 → 6.1 → 6.3 → 6.2 → 7.2 → 6.5 → 9.3 — verified by
  checking every pair for reachability rather than by inspection.
- **F-terminal-union-producer-targets (accepted)** → 3.1: 3.1.7 requires the
  terminal union published verbatim in the shared skill and both adversary
  definitions while 3.1 targeted none of them. Added, which also places 3.1 at
  the head of the prompt chain via the existing 3.1 → 4.3 edge.
- **F-index-replacement-run-acceptance (accepted)** → 8.1.14: round 6 defined
  the run-replacement protocol without an acceptance item observing it, so a
  partial implementation could pass. 8.1.14 now asserts the full transition —
  typed `inconclusive`, evidence expiry, new evidence ID, distinct bound run,
  all three lanes rerun, no old lane result reused.
- **F-staged-telemetry-transport (accepted)** → 7.2: verified — the staged
  reviewer never calls `send_message`, terminating through `approve_review` /
  `reject_review` (`_stage_review.py:302`), whose schemas expose no telemetry
  object, so reviewer-owned telemetry on that path is dropped before anything
  can persist it. Added `_stage_review.py` as a target with the construction
  helper kept out of the 977-line `_transitions.py` (7.2.9).
- **F-snapshot-wrapper-test-migration (accepted)** → 6.2.5: verified —
  `tests/mcp_proxy/test_plans_tools.py:140,204` calls
  `get_plan_review_snapshot` with only `evidence_id` and asserts the whole
  snapshot inline. That direct seam is now in the migration scope.
- **F-ledger-section-order-identity (accepted)** → 4.1: the canonical
  coalescing key used `source_section_ids` without pinning order, so two
  producers naming the same sections differently would mint two entries and
  corrupt the repeated-check count. The key now uses unique IDs in one stable
  order, each paired with its hash (4.1.3 permutation test).

**Adversarial review round 8 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `f0b40032`), verdict `needs_review`; three lanes completed,
12 candidates → 4 findings (all blocking), 8 dismissed. Votes: all 4 accepted.
Every finding repeated an earlier check key, all from the same root cause: a
repair that named the file the reviewer cited instead of closing the class.
Each was therefore fixed as a class this round.

- **F-staged-telemetry-transition-ownership (accepted, repeat)** → 7.2:
  verified — `approve_review` exists at `_transitions_facade.py:296` (signature
  forwarding) and `_transitions.py:433` (result construction), so targeting
  only `_stage_review.py` could not carry telemetry into the persisted result.
  Both files are targets; 7.2.9 now enters through `LocalTaskManager` and
  verifies approve and reject through finalization, with the concrete builder
  in a sibling module so `_transitions.py` stays under 1,000 lines.
- **F-timeout-terminal-leaf-ownership (accepted, repeat)** → 6.3 / 7.2: 6.3.2
  and 6.3.4 asserted runtime behavior in `agent_health.py` and
  `agent_cleanup.py`, which 6.3 does not target and 7.2 owns. The runtime
  assertion moved to 7.2.11; 6.3 keeps timeout configuration, the documented
  expire-and-retry policy, and the provider-capability diagnostic.
- **F-parent-kill-terminal-bypass (accepted, repeat)** → 7.2: verified —
  `terminalize_killed_agent_run` (`agent_cancellation.py:101`) and
  `terminalize_cancelled_agent_run` (`:56`) both carry bypass branches
  (`run_storage.fail` with direct delivery; a lifecycle-less direct
  transition). Rather than append two more files, 7.2 now states the rule as
  an **invariant** — every call site transitioning an evidence-bound run to a
  terminal state routes through the extracted helper — with a table of the
  seven known routes explicitly marked non-exhaustive, and 7.2.10 searching
  for terminalizing call sites so the table cannot silently go stale.
- **F-taskless-requirement-source-anchor (accepted, repeat)** → 6.5: "the
  initiating session request" is not selectable from a session ID when a
  planning session has many turns, a growing transcript, and restarts. The
  server now captures a provider-neutral request anchor at taskless
  `/gobby plan` start, first-round preparation persists identity and content,
  later rounds reuse it, and a missing anchor fails closed (6.5.7).

**Adversarial review round 9 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `c0e93a72`), verdict `needs_review`; three lanes completed,
12 candidates → 3 findings (all blocking), 9 dismissed. Votes: all 3 accepted.
Each finding proposed the minimal repair and explicitly ruled out a new
subsystem, and each identified an acceptance item satisfiable by helper
fixtures while production stayed broken.

- **F-taskless-request-anchor-production-path (accepted, repeat)** → 6.5:
  round 8 defined the anchor but every 6.5 target began at bundle assembly or
  later, so 6.5.7 could pass against fixtures while production never created
  one. The plan skill and the `prepare_plan_review_round` wrapper are now
  targets: the anchor is written into existing session state before plan work
  begins and copied atomically into `prior_round_context` on first
  preparation, and 6.5.7 enters through that real path.
- **F-round-result-delivery-monotonicity (accepted)** → 7.2: `send_message`
  delivers to the parent mailbox and separately writes `agent_runs.result`, so
  a mailbox success with a failed write leaves `end_agent_run` unable to see
  delivered state, and a duplicate or late send after terminal cleanup can
  overwrite an enriched result with the raw reviewer payload. Delivered state
  is now persisted and validated before delivery is acknowledged, and result
  writes are compare-and-set against a state predicate that cannot regress
  enriched or terminal state (7.2.12). `agent_messaging.py` added as a target.
- **F-approval-ledger-delivery-path (accepted)** → 4.3 / 8.1: the plan has
  required since P4 that the ledger be surfaced in full at approval, yet every
  acceptance item only proved persistence — entries stored for a future round
  are shown to nobody. The merged ledger now travels in the existing approved
  round-result envelope beside the manifest and renders on the coordinator's
  existing approval path (8.1.15), with no new API or dashboard.

**Adversarial review round 10 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `6822ae18`), verdict `needs_review`; three lanes completed,
9 candidates → 3 findings (all blocking), 6 dismissed. Votes: all 3 accepted.
Two carry `introduced_in_round: 10` against round 9's own repairs: each round-9
fix named the surface the reviewer cited and stopped at the seam where the
value is *produced*, leaving the production consumer or producer untouched.

- **F-stage-native-snapshot-transport (accepted, repeat)** → 6.2: verified —
  the stage-native reviewer never calls the snapshot tool at all.
  `dispatch/spawn.py:121` feeds `snapshot_bytes` to `dispatch/prompts.py:176`,
  which decodes the plan and embeds it inline at `:203-205`, and
  `tests/storage/test_stage_review_findings.py:424-425` pins that shape.
  Migrating only the two definitions would leave two contradictory transports
  live and strand the staged reviewer without `prior_round_context` — the same
  channel 6.5's requirements bundle rides. Both production files and the
  pinning test are now 6.2 targets (6.2.6).
- **F-taskless-anchor-authoritative-producer (accepted, repeat)** → 6.5: round
  9 named the plan skill and the preparation wrapper, but a skill instruction
  produces a *model-supplied* value, which is what "server-owned" excludes, and
  the wrapper runs after plan work has begun. Checked the reviewer's proposed
  target and rejected it: `handle-plan-mode-entry.yaml` fires on `turn_start`
  with only a `load_skill` effect and the rule namespace exposes no request
  content. The smallest owner already holding identity, bytes, and session
  state is the entry observer — `observer_plan_mode.py:128` reads
  `data.get("prompt")` and is what sets `plan_mode`, so it necessarily runs
  before the rule gating on it. It now writes the anchor (6.5.8).
- **F-approval-ledger-production-consumer (accepted, repeat)** → 4.3 / 8.1:
  round 9 put the ledger in the envelope but left both consumer surfaces out —
  `plan/SKILL.md:101` enumerates `routing_decisions` and `manifest_entries`
  only, and staged `approve_review` takes `routing_decisions` with no ledger in
  signature or schema (`_stage_review.py:309,353-354,465`). Ordering was also
  wrong: `:410-414` persists approval intent when it links `review_approved`,
  so the ledger is materialized before that write rather than derived from a
  frozen result (4.3.5, 4.3.6). 8.1.15 now fails on an empty ledger, closing
  the no-op derivation that let it pass at the service seam.

**Adversarial review round 11 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `e4b1523a`), verdict `needs_review`; three lanes completed,
8 candidates → 6 findings (all blocking), 2 dismissed. Votes: all 6 accepted.
Findings rebounded from 3 and the dismissal rate fell from 67% to 25%. Three
findings carry `introduced_in_round: 11` against round-10 repairs; three are
first observations in sections earlier rounds had already touched.

One class now accounts for most of the tail and is named explicitly here:
**a required input with no production producer or durable transport.** It has
recurred against the request anchor (rounds 8–11), the approval ledger
(9–11), repair attestations, and dismissed dispositions. Each earlier repair
supplied a validator, a schema, or a wrapper and left the producer implicit.

- **F-approval-ledger-canonical-path (accepted, 3rd repeat)** → 4.3: the
  round-10 repair stopped at the outer wrapper. `_transitions_facade.py:296`
  forwards and `_transitions.py:433` constructs and persists the canonical
  result, both fixed-signature and both ledger-less, so 4.3.6 and 8.1.15 could
  pass at wrapper and service seams while staged approval stayed empty. Round 8
  established this exact chain for telemetry; the ledger now takes it too,
  stated as an invariant over canonical result fields rather than a per-field
  file list. 4.3.6 enters through `LocalTaskManager`.
- **F-requirements-bundle-paging (accepted)** → 6.2: 6.2.1–6.2.4 bound plan
  section pages only, while 6.5 attaches arbitrary repository documents to
  `prior_round_context`. One requirement source over the 15,000-char threshold
  reproduces the truncation this deliverable exists to fix, with every existing
  criterion green. The cursor now enumerates one record stream over sections,
  requirement sources, ledger, and inventory (6.2.7) — no second transport.
- **F-repair-attestation-production (accepted)** → 2.2: verified — 2.2 targeted
  only the new validator module, and `prepare_plan_review_round` accepts no
  attestation parameter at all, so every real round 2 would be refused while
  fixture tests passed. 2.2 now owns both producers, taskless and staged, with
  acceptance entering through the real coordinator path (2.2.7, 2.2.8).
- **F-ledger-disposition-durable-handoff (accepted)** → 4.1: the merge consumes
  dismissed dispositions, but coverage validation reduces them to counts and
  digests (`review_coverage.py:382-454`) and the canonical result carries
  findings, not dispositions — so on `needs_review`, where dismissed candidates
  matter most, nothing durable survives. Coverage validation now persists
  ledger-ready records into the 2.1 columns at validation time (4.1.4).
- **F-request-anchor-branch-coverage (accepted, repeat)** → 6.5: my round-10
  repair picked the right file and the wrong line. `resolve_plan_mode` has five
  authoritative exits; four return before `data.get("prompt")` at `:128`
  (web-chat `:70-78`, structured `:80-91`, Codex collaboration `:93-103`,
  provider-native `:105-114`). Anchoring at that tail passes a fallback fixture
  while common entries load the plan skill with no anchor. `data` is in scope
  at `:67` and every branch mutates through `_apply_resolved_mode`, so the
  anchor is written there on the false→true transition (6.5.9).
- **F-recovery-directive-extraction (accepted)** → 9.1: lifting the
  `call_tool`-bearing *line* misses both enabled forms — a `set_variable`
  directive with no `call_tool`, and `block-gobby-tasks-cli.yaml:17`, which
  splits one invocation across physical lines. Extraction is now defined around
  a complete balanced directive with fixtures per action syntax (9.1.3).

**Adversarial review round 12 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `e98cba8c`), verdict `needs_review`; three lanes completed,
17 candidates → 10 findings (all blocking), 7 dismissed. Votes: all 10
accepted. Findings rose 3 → 3 → 6 → 10; six carry `introduced_in_round: 12`
against round-11 repairs. The trend is divergent and the cause is visible in
the repairs themselves: round 11 answered four "no production producer"
findings by **adding** mechanism — a sidecar disposition store, a record-stream
cursor, a staged producer, a shared mutation site — and each addition created
the surface round 12 found. Round 12's repairs therefore prefer deletion, and
three of the ten were closed by removing what round 11 added rather than
extending it.

- **F-R12-DISPOSITION-PRODUCTION-OWNER + F-R12-DISPOSITION-DIGEST-BINDING
  (accepted, both closed by deletion)** → 4.1 / 5.1: round 11 invented a
  sidecar store for dismissed dispositions, which put a production-persistence
  acceptance inside a helper-only leaf (violating 8.1's ownership invariant)
  and left the sidecar unbound to the attestation, so a retry could swap the
  set behind an identical digest. Both vanish once the records travel **in**
  the canonical `round_result` beside 5.1's sweep records: it is already
  digest-bound, validated, and persisted whole. No sidecar row, no second
  digest, no compare-and-set. 4.1 returns to a pure helper (4.1.4, 5.1.4).
- **F-R12-SNAPSHOT-RECORD-CHUNKING (accepted, bounded not engineered)** → 6.2:
  round 11's record cursor still breaks on one requirement source larger than a
  page, and the proposed fix was a fragment protocol with identity, offsets,
  lengths, and reassembly proofs — a second transport in all but name. A
  designated requirement source is a human-authored contract, so an oversized
  one fails preparation closed naming the source, exactly as a malformed marker
  does. Section, ledger, and inventory records are bounded by construction.
- **F-R12-REQUEST-ANCHOR-FALLBACK (accepted, 4th repeat)** → 6.5: verified —
  `_apply_resolved_mode` is not the only writer. `detect_plan_mode_from_context`
  assigns `variables["plan_mode"] = True` directly (`:277`, `:349`), and that
  marker path is the one an ordinary `/gobby plan` turn takes. Every activation
  now routes through one anchor-aware transition, and 6.5.10 forbids any direct
  assignment so a fifth repeat cannot be reintroduced.
- **F-R12-STAGED-ENRICHMENT-ORDER + F-R12-STAGED-DELIVERED-STATE (accepted)** →
  7.2: the staged path is circular — approve/reject finalize inside the verdict
  call, before `end_agent_run`, while finalization requires the enriched state
  only post-termination cleanup produces; and the staged reviewer never writes
  `AgentRun.result` at all, so 3.1's guard has no input. Staged verdicts now
  record durable delivered intent and mutate no stage state; the existing
  terminal helper settles, enriches, finalizes, commits, and wakes as one
  replayable transition (7.2.13).
- **F-R12-STAGED-TIMEOUT-PARITY (accepted)** → 6.3: verified — **neither**
  definition carries a `timeout:` key; both mention timeouts only in lane
  prose. Bounding the taskless one alone let the stage-native reviewer hang
  while the deliverable claimed the run was bounded (6.3.1).
- **F-R12-STAGED-REPAIR-TRANSPORT (accepted, repeat)** → 2.2: `submit_for_review`
  carries only `review_notes` and dispatch prepares from task and stage
  identity, so staged repair records had nowhere durable to live. One typed
  round-bound payload now rides existing stage state across the same
  fixed-signature chain 4.3 uses (2.2.8).
- **F-R12-INDEX-SETTLE-BOUND (accepted)** → 5.2: the D0→index→D1 settle loop had
  no attempt or time bound, and nothing outside can stop it — 6.3's deadline
  starts only once a child run exists. Three attempts under a monotonic
  deadline, then typed `index_unstable` with full rollback (5.2.9).
- **F-R12-RECOVERY-DIRECTIVE-INVENTORY (accepted, narrowed)** → 9.1: the fixture
  *list* was the wrong unit — bundled reasons also use direct MCP-tool calls,
  backticked shell commands, and alternative-command lists. 9.1.3 now derives
  cases from the bundled inventory and fails on an unrenderable new form,
  instead of growing a hand-listed set every round.

**Adversarial review round 13 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `6f3c4829`), verdict `needs_review`; three lanes completed,
4 candidates → 2 findings (both blocking), 2 dismissed. Votes: both accepted.
Findings fell 10 → 2 and candidate generation collapsed 17 → 4 (lanes raised
1, 0, and 3). The round-12 diagnosis held: once repairs stopped adding
mechanism, the defect surface stopped growing. This round's prompt also asked
the reviewer to apply guiding principle 12 to its own remedies and to separate
plan-level defects from implementation detail; both `suggested_fix` values came
back explicitly bounded ("add no fragments, digest, or state machine";
"add no discovery protocol or transport").

- **F-R13-SNAPSHOT-RECORD-SIZE-BOUND (accepted, repeat)** → 6.2: my round-12
  repair bounded requirement sources and then asserted plan sections were
  "bounded by the section grammar" and ledger/inventory records "bounded by
  construction". Verified against source: `parse_plan` and the section-manifest
  builder enforce structure and hashes but no byte limit, computing spans from
  heading indices — so the assertion was simply false and a single oversized
  plan section is exactly as unpageable. Deleted the claim and applied one
  preparation-time size check across every record class (6.2.7).
- **F-R13-REQUIREMENT-SOURCE-AUTHORING-PRODUCER (accepted)** → 6.5: the same
  no-producer class, now on the authoring side. Verified — `requirement-source`
  appears nowhere in the skills tree, and the plan workflow delegates document
  construction to `plan-draft`, whose Constraints contract never emits it, so
  parser and bundle tests could pass while no real plan designates a
  requirement document. `plan-draft` now owns emission and preservation,
  reusing the existing grammar, parser, and bundle (6.5.11).

**Adversarial review round 14 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `889507d9`), verdict `needs_review`; three lanes completed,
8 candidates → 3 findings (all blocking), 5 dismissed. Votes: all 3 accepted.
Two of the three **shrink** the plan, and one carries
`category: over-engineering` — the reviewer now applies principle 12 to the
artifact rather than only to its coverage.

- **F-R14-DERIVATION-TOKEN-NO-CONSUMER (accepted, deletion)** → 6.2: the
  derivation token has been in this plan since the research doc, and it never
  had a consumer. `evidence_id` plus the reviewed routing decisions already
  identify the derivation and key its cache, so the token was a second identity
  mechanism carrying its own issuer, lifecycle, and validation surface for no
  capability. Deleted, along with acceptance 6.2.3. A cache miss rederives.
- **F-R14-SERVER-DERIVED-LEDGER-ECHO (accepted, deletion)** → 4.3: rounds 10–12
  progressively threaded the ledger through staged approve/reject and the
  facade as a caller input, while 4.3 called it server-derived — two
  authorities for one value, permitting substitution or omission, and a
  redundant echo whenever the server rederives. Removed the parameter and both
  forwarding targets; it is derived once in 7.2's terminal transition from the
  persisted prior ledger and the delivered result, before stage mutation
  (4.3.6). This also reverses part of the round-11 repair, which had added the
  facade and transition files here.
- **F-R14-STAGED-TIMEOUT-RESTORATION-OWNER (accepted)** → 7.2: 6.3 promises a
  staged timeout restores stage state but owns definitions and configuration,
  while 7.2 owns the terminal runtime paths and omitted restoration from both
  its body and 7.2.11 — so an implementer holding only 7.2 could satisfy every
  runtime criterion and leave a timed-out stage stuck mid-review. The runtime
  half now sits with the other terminal transitions, reusing the same helper.

**Adversarial review round 15 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `6ba35aa9`), verdict `needs_review`; three lanes completed,
18 candidates → 8 findings (all blocking), 10 dismissed. Votes: 7 accepted,
1 accepted in part.

**Reviewer reliability note.** This round's findings cite file paths that do
not exist — `src/gobby/tools/plan_approval.py`, `src/gobby/models/review_findings.py`,
`src/gobby/tools/manifest_emitter.py`, `src/gobby/tools/plan_review.py`. There
is no `src/gobby/tools/` package. Every substantive claim was therefore
re-verified against real source before acceptance, and all of them held:
`approve_review` exists at four genuine sites, `gobby.plans.manifest_emitter`
is real, `validation_criteria` is a distinct Task field
(`storage/tasks/_creation.py:36,111`), and `lesson_mint_status` is genuinely
waited on (`plan-adversary.yaml:317,333,341,397,406`). Treat this round's
locations as approximate; the findings themselves are sound.

- **F-R15-LEDGER-ACCEPTANCE-CONTRADICTION (accepted)** → 4.3: round 14 removed
  the ledger from the caller contract but left 4.3.5 requiring staged
  `approve_review` to "accept and forward it in signature and JSON schema" —
  directly contradicting the 4.3.6 it was written alongside. Both displays now
  render from the server-derived envelope.
- **F-R15-SHADOW-ECHO-SCHEMA-STALE (accepted, over-engineering)** → 5.3: 6.2
  deleted the 42 KB shadow echo while 5.3 still listed `shadow_manifest_status`
  as a strict validator *input*, so an implementer could preserve the echo.
  It is output-only now.
- **F-R15-INTEGRATION-LEAF-ACCEPTANCE-LEAK (accepted, 5th repeat)** → 8.1: the
  `integration-owner-leaf-closure` invariant broke again, and again by my own
  hand — rounds 12 and 13 pushed real prepare/rollback/finalize/terminal
  assertions into 2.2.7, 2.2.8, 5.2.9, 6.5.7, and 7.2.13, whose helper-only
  targets cannot implement them. Each is narrowed to what its leaf proves
  against injected inputs; the production paths collect in new 8.1.16.
- **F-R15-IMMUTABLE-REQUIREMENTS-CUTOFF (accepted in part)** → 6.5: verified —
  `title`, `description`, and `validation_criteria` are three distinct
  authoritative Task fields, and acceptance living only in
  `validation_criteria` is mandatory acceptance, so hashing description alone
  yields a hash-valid bundle missing exactly what the traceability lane checks.
  All three are now hashed. **Declined** the taskless half — freezing "ordered
  server-owned requirement turns at a cutoff" reintroduces the live-transcript
  heuristic the anchor was chosen in rounds 8–12 to eliminate, trading a
  deterministic source for a cutoff rule two implementers would read
  differently.
- **F-R15-REPAIR-OVERLAP-UNIVERSE (accepted)** → 2.6: the repair graph joined on
  shared `section_id` alone, so two findings on different sections mutating one
  contract were never adjacent — the exact conflict the graph exists to expose.
  Adjacency is now the union of section, `check_key`, changed contract, and
  changed target/resource.
- **F-R15-MINIMAL-REPAIR-SCOPE-SCHEMA (accepted)** → 7.1: the validator was told
  to distinguish existing-section repairs from justified new deliverables using
  a free-form prose field. Added a `repair_scope` discriminant; the
  justification is required on one branch and rejected on the other.
- **F-R15-E1-VERIFICATION-UNOWNED (accepted)** → E1: `manifest_emitter.py:94`
  emits only `kind: deliverable` sections, so E1 as a bare verification section
  produced no task and the live regression this plan exists to measure had no
  owner. Promoted to deliverable E1.1 with executable acceptance; confirmed the
  emitter selects over the flat section list, so nesting under a verification
  parent does not exclude it.
- **F-R15-STAGED-INTENT-SIDE-EFFECT-ORDER (accepted, repeat)** → 7.2: round 12
  moved the verdict commit behind the terminal boundary without moving the
  effects that depend on it. Lesson minting, artifact linking, claim release,
  and workflow ticks were left unordered, and the workflow's
  `lesson_mint_status` wait deadlocks against a terminal owner that has not run.
  The whole verdict-dependent bundle now runs inside the terminal helper after
  stage commit, reusing the existing mint recovery for replay.

**Adversarial review round 16 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `5279d941`), verdict `needs_review`; three lanes completed,
10 candidates → 6 findings (all blocking), 4 dismissed. Votes: all 6 accepted.
Citations were real paths again after round 15's fabricated ones. Manifest
entry count moved 25 → 26, confirming E1.1 is emitted.

- **F-R16-OVERSIZED-SNAPSHOT-SELF-HOST (accepted, deletion)** → 6.2: the
  sharpest finding of the review. My round-12 repair chose a fail-closed size
  bound over a fragment protocol, and round 13 extended that bound to every
  record class. Both were wrong, and measurably so: §7.2 of this plan is
  15,634 characters and the V1 changelog is 207,823 bytes, so the rule would
  make preparation **reject the plan that defines it**, and would break again
  whenever any section grew. Records are not the paging unit. Deleted the
  rejection; the canonical serialized envelope byte stream pages through the
  offset/`next_offset`/`snapshot_hash` contract already specified, aligned to
  UTF-8 boundaries, with record identity recovered by parsing the reassembled
  envelope. Less mechanism than either prior answer, and the only one that
  transports every valid plan. The regression now pins this plan as its
  fixture.
- **F-R16-CONVERGENCE-ASSERTION (accepted, repeat)** → E1.1: round 15 gave the
  new verification owner artifact-*writing* acceptance and no assertion, so it
  would pass after recording unchanged or worse convergence. E1.1.3 now fails
  when rounds-to-approval leaves single digits, an exact check key repeats, two
  consecutive rounds share a check-key class, or the tail stops decaying, with
  wall-time variance bounded separately.
- **F-R16-REQUIREMENT-CITATION-CONTRACT (accepted)** → 6.5: adding task fields
  to the bundle in round 15 created citations with no wire form — the validator
  accepts path + SHA and rehashes from the live worktree
  (`review_coverage.py:345-379,457-559`), while task title, description,
  `validation_criteria`, and the anchor have no path. Widened the existing
  citation type into a discriminated union rather than adding a channel.
- **F-R16-REPAIR-UNIVERSE-VISIBILITY (accepted)** → 2.6: deriving the site graph
  inside preparation while validating attestations against it in the same call
  is unsatisfiable on the first round containing a new site — the coordinator
  cannot attest to what it has not seen, and failed preparation persists
  nothing to learn from. The 5.2 helper now runs before submission; preparation
  rederives and compares, rejecting drift.
- **F-R16-COVERAGE-RECORD-HANDOFF (accepted)** → 5.1: round 12 put the records
  in `round_result` without assigning the validator a return shape that
  preserves them, so validation could still reduce them to the attestation.
  5.1.5 pins the canonical bundle and 4.3 carries it through both verdicts with
  terminal revalidation.
- **F-R16-STAGED-REPAIR-PRODUCER-SEAM (accepted, repeat)** → 2.2: verified —
  `planner.yaml:90-106,180-204` tells the revising agent to resubmit notes only,
  and the public stage-state submit seam takes notes while the private
  transition can already persist artifact updates
  (`_stage_states.py:172-190,339-372`). Added `planner.yaml` and
  `_stage_states.py` as owned surfaces with one atomic write.

**Adversarial review round 17 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `4122cb5c`), verdict `needs_review`; three lanes completed,
9 candidates → 5 findings (all blocking), 4 dismissed. Votes: all 5 accepted.
Four carry `introduced_in_round: 17` against round-16 repairs — each round-16
fix was directionally right and left one seam unowned.

- **F-R17-REGRESSION-FIXTURE-PATH (accepted)** → E1.1: verified by direct
  filesystem check — `.gobby/plans/context-mode-borrowings.md` does not exist;
  the tracked fixture is `.gobby/plans/completed/context-mode-borrowings.md`.
  The regression this whole plan is measured by would have failed before
  measuring anything. Both references corrected.
- **F-R17-CITATION-UNION-OWNER (accepted)** → 6.5: round 16 widened citation
  semantics without owning the two files that must implement them —
  `review_coverage.py` (`_citation_list`, rehash) and `review_findings.py`
  (the blocking failure-trace validator). Both added; both already precede 6.5
  in the shared-file order.
- **F-R17-REPAIR-UNIVERSE-PRODUCER-SEAM (accepted)** → 2.6 / 5.2: round 16 said
  producers call the helper before submission but gave them nothing to call —
  `run_consumer_sweep`'s production callers are the CLI and the expansion gate,
  and 5.2 still carried the contradictory "no point at which the coordinator
  could have seen the sites" paragraph. One stateless read-only `gobby-plans`
  operation exposes the graph and its digest; the stale paragraph is rewritten
  rather than left to contradict 2.6.
- **F-R17-TERMINAL-EFFECT-IDEMPOTENCY (accepted)** → 7.2: verified —
  `_relay_signoff_to_build_coordinator_sync` (`_stage_review.py:107-174`) calls
  `create_message`, which mints a fresh UUID and does an unconditional INSERT
  (`inter_session_messages.py:141-194`), so a crash between that write and the
  checkpoint duplicates the relay. 7.2.13 promised replay convergence while
  naming recovery only for lesson minting. Every effect now takes a stable
  idempotency key from identity the transition already holds (7.2.14).
- **F-R17-CONVERGENCE-CLASS-PRODUCER (accepted)** → E1.1 / 7.2: round 16's
  E1.1.3 asserted "no two consecutive rounds share a check-key class" while
  nothing defined class identity — `adversary-convergence.md` is explicit that
  its grouping was a human reading. I had written an unimplementable predicate.
  `check_key_class` is now a reviewer-emitted field beside the `check_key` the
  reviewer already assigns, with classification inputs recorded; no server-side
  taxonomy.

**Adversarial review round 18 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `2561f665`), verdict `needs_review`; three lanes completed,
13 candidates → 6 findings (all blocking), 7 dismissed. Votes: all 6 accepted.
Four carry `introduced_in_round: 18`, and the pattern in them is consistent:
each round-17 repair was correct in intent and landed one layer above where it
had to bite. Four of the six sit in §7.2's runtime plumbing.

- **F-R18-SIGNOFF-IDEMPOTENCY-CARRIER (accepted, repeat)** → 7.2: round 17 put
  the stable idempotency key in prose above the storage boundary, but
  `create_message` mints `uuid.uuid4()` at `inter_session_messages.py:163`, so
  the key never reached a durable uniqueness boundary and replay still
  duplicated. The message primary key is now the carrier — derived
  deterministically, conflict-verified — and the file is an owned target.
- **F-R18-ROUND-RESULT-MAILBOX-IDEMPOTENCY (accepted, repeat)** → 7.2: the same
  defect one layer up. `send_message` commits the parent mailbox message before
  writing `AgentRun.result` (`agent_messaging.py:175-209`), so a failed result
  write makes the retry insert a second canonical round-result message that the
  monotonic CAS cannot see. One identity across both writes, or one transaction
  (7.2.15).
- **F-R18-IMMEDIATE-HEALTH-TERMINAL-BYPASS (accepted)** → 7.2: a terminal route
  the invariant's table never listed — `_deferred_tmux_health_check`
  (`spawn_agent/_health.py:92`, scheduled `:152`) marks the run failed and
  delivers directly, and it races evidence binding because dispatch binds right
  after `spawn_agent_impl` returns. Added to the table, targets, and 7.2.16
  with a test on both sides of the race.
- **F-R18-CHECK-KEY-CLASS-CARRIER (accepted, repeat)** → 7.2: my round-17 text
  said "each finding also carries a `check_key_class`" while the same section
  defines it as reviewer-emitted telemetry. The closed finding validator
  (`review_findings.py:24-46,117-127`) rejects a top-level field, so the two
  readings produce incompatible round results. Pinned to the telemetry
  classification record as its only carrier.
- **F-R18-REPAIR-UNIVERSE-PRODUCTION-SEQUENCE (accepted, repeat)** → 2.6: my
  round-17 repair said producers call the operation "before editing and
  submission" — wrong order. The graph is a function of the changed sections
  and symbols, so a pre-edit derivation digests the wrong tree and guarantees
  the drift rejection it exists to prevent. Now edit → derive → attest →
  submit, with the wrapper, plan skill, and `planner.yaml` as targets and a
  named recovery for drift on both paths.
- **F-R18-REQUEST-ANCHOR-BYTES-PRODUCER (accepted)** → 6.5: my round-12 claim
  that event bytes reach every exit is false. The managed web-chat branch
  (`:70-78`) resolves mode from metadata or the persisted session and returns
  without reading request content; the Codex branch reads the transcript.
  Mode-only branches now reuse a persisted anchor or fail closed — never
  fabricate, since an empty anchor validates.

**Adversarial review round 19 (2026-07-27)** — plan-adversary-taskless (codex /
gpt-5.6-sol, run `78e416af`), verdict `needs_review`; three lanes completed,
19 candidates → 10 findings (all blocking), 9 dismissed. Votes: all 10
accepted. Asked to judge the plan as a whole and approve if implementable, the
reviewer declined and emitted its largest count since round 12.

- **F-R19-ANCHOR-CONTENT-AVAILABILITY (accepted, over-correction reversed)** →
  6.5: round 18 keyed anchor policy on the *branch* rather than the payload,
  forcing web-chat and Codex entries to reuse-or-fail-closed. Both commonly
  carry the prompt in `event.data`, so a valid first entry would be rejected
  outright. Policy is now keyed on whether content is actually present.
- **F-R19-WAKE-EXACTLY-ONCE (accepted, deletion)** → 7.2: my 7.2.14 demanded
  exactly one wake, but the completion registry and debounce state are
  process-local (`events/completion_registry.py:32`, cleared at
  `agent_cleanup.py:171`), so proving it across a crash would need a durable
  wake protocol nothing else wants. Exactly-once now binds the durable result
  message; the wake is at-least-once and a duplicate is harmless.
- **F-R19-UNREGISTER-TERMINAL-BYPASS (accepted, 3rd terminal-route repeat)** →
  7.2: another route the table missed — `agents_query_tools.py:616` →
  `cancel_run` → `_lifecycle.py:339` reaches terminal state ahead of generic
  delivery. Added.
- **F-R19-SHARED-TARGET-ORDERING (accepted, 6th repeat)** → 4.3: my round-18
  addition of the skill and wrapper to 2.6 left it unordered against 4.3, which
  owns the same files. One edge, 4.3 → 2.6.
- **F-R19-INTEGRATION-OWNER-CLOSURE (accepted, 6th repeat)** → 8.1: my
  round-18 2.6.4 asserted production preparation and recovery while 8.1.4
  excludes upstream leaves from exactly that. Contradictory ownership; the
  production sequence moved to 8.1.17 and 2.6.4 is helper-local.
- **F-R19-DISMISSAL-REOPEN-GATE (accepted)** → 4.2: the no-reopen rule was
  injected as prompt context and enforced nowhere, so a reviewer could reopen
  an unchanged dismissal and still pass coverage. The server-owned validator
  enforces it now (4.2.2).
- **F-R19-CONSUMER-SWEEP-CALLER-CONTRACT (accepted)** → 5.2: turning the sweep
  result into a typed failure left the spawn plan gate — which catches only
  database errors — to leak it as an unstructured failure (5.2.10).
- **F-R19-INDEX-VERIFIER-CALLABLE (accepted)** → 5.2: `verify_index_token` had
  no MCP seam, so lanes required to call it before and after research had no
  way to (5.2.11). Same class as round 17's helper-seam finding.
- **F-R19-DEVIATION-WIRE-SCHEMA (accepted)** → 2.3: the five deviation fields
  were prose, so four surfaces could implement four shapes. Pinned as one
  closed named object with a parity test (2.3.3).
- **F-R19-REPAIR-SCOPE-PRODUCER-PARITY (accepted)** → 7.1: `repair_scope` was
  enforced on consumers with no producer owner, so the first real round would
  reject a compliant reviewer. Both adversary contracts are targets now.

**Adversarial review round 20 (2026-07-27, final round)** — plan-adversary-taskless
(codex / gpt-5.6-sol, run `d882d4f6`), verdict `needs_review`; three lanes
completed, 2 candidates → 1 finding (blocking), 1 dismissed. Lanes raised 1, 0,
and 1. Accepted.

- **F-R20-VERIFIER-LEAF-OWNERSHIP (accepted)** → 5.2 / 6.1: my round-19
  callable-verifier repair loaded wrapper registration, allowlist parity, and
  real lane invocation onto 5.2, whose targets are helper files, while 6.1
  already owns lane instructions and depends on 5.2 — so 5.2 could not close
  before 6.1 ran. Split at the ownership line: 5.2.11 keeps wrapper
  registration and typed reachability with the wrapper added to its targets;
  allowlist parity and real before/after lane invocation move to 6.1.6.

```json plan-review-round
{"evidence_id":"47308c34-0974-46e7-92e9-d44ac52f0689","plan_hash":"3172f60c523c9c44e2ab717b64cdfa0039fcac1b02f59915e14424d8c1cf9610","round_number":20,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"93914ad81edc4951bdc851fb6ec5952fb507ebc15f7f5d010887a8d072ee2632","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":1,"total":2},"evidence_id":"47308c34-0974-46e7-92e9-d44ac52f0689","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":26,"manifest_digest":"57ec5a80967284646e9316c64086bde53e5f8d3bd057537fecefc25f66e3229d","status":"valid"},"source_digest":"80524763129b7c92b4c746137e09928e26eac06c272bfdb1976be0e25276c071","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"F-R19-INDEX-VERIFIER-CALLABLE","causal_section_ids":["5.2"],"check_key":"index-token-verifier-invocation","description":"Section 5.2.11 requires the verifier's MCP wrapper, review allowlist, and real before/after lane invocation, but section 5.2 targets only consumer_sweep.py, review_evidence_io.py, and code_index.py. Section 6.1 explicitly owns lane-instruction edits and depends on 5.2. Expansion therefore produces a 5.2 leaf that cannot satisfy its acceptance before 6.1 runs, or forces the implementer to violate the target/ownership split and duplicate downstream edits. If shipped unfixed, implementation blocks at 5.2 or lands without a callable verifier, leaving the index-token invariant unenforceable.","finding_id":"F-R20-VERIFIER-LEAF-OWNERSHIP","introduced_in_round":20,"location":"Sections 5.2 acceptance 5.2.11, 6.1 lane-instruction ownership, and 8.1.4 upstream-leaf closure","prevention":"Map every acceptance clause to its owning target files and reject a leaf whose acceptance requires a dependent leaf's surfaces.","principle":"Every manifest leaf must be independently closeable using surfaces it owns before any dependent leaf runs.","root_cause":"The round-19 callable-verifier repair added downstream wrapper, allowlist, and lane-invocation obligations to the helper leaf instead of splitting them at the declared consumer owner.","section_id":"5.2","severity":"blocking","suggested_fix":"Keep section 5.2 helper-local: add the review-evidence MCP wrapper to its Targets and narrow 5.2.11 to verifier registration plus typed wrapper reachability. Move allowlist parity and real before/after lane invocation into section 6.1 acceptance, where prompt-consumer ownership already lives and which already depends on 5.2."}],"reviewer_session":"#9725","round":20,"round_number":20,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```
**Review outcome after 20 rounds.**

Findings per round: 11, 9, 10, 9, 4, 8, 6, 4, 3, 3, 6, 10, 2, 3, 8, 6, 5, 6,
10, 1. Candidates in the final round: 2. The adversary never returned
`approved`; this plan is **not formally converged**, and the last round's
single finding is recorded above as fixed rather than as an approval.

What the sequence actually shows:

- **The tail is self-inflicted.** Across rounds 9–20, most findings carried
  `introduced_in_round` equal to the current round — they were defects in the
  *previous round's repair*, not defects the reviewer had missed earlier. The
  two divergent spikes (round 12 at 10, round 19 at 10) both followed rounds
  where repairs added mechanism.
- **Deletion converges; addition does not.** Rounds 13, 14, 16, and 20 — the
  four lowest counts — all followed repairs that removed mechanism or reused an
  existing carrier. Round 16's strongest finding deleted a rule of mine that
  would have made preparation reject this very plan.
- **Six check keys recurred four or more times**: `shared-target-write-order`,
  `integration-owner-leaf-closure`, `terminal-run-transition-totality`,
  `request-anchor-*`, `repair-attestation-production-path`, and the
  producer-parity family. Every recurrence traced to a repair that named the
  cited instance instead of closing the class.

Known-open risk at hand-off: no round has re-reviewed the round-20 repair, and
the recurring classes above are the places to look first if implementation
stalls. The plan validates (9 phases, 26 manifest entries, consumer sweep
passing) and every acceptance item names an executable test, file, or symbol.

```json plan-review-round
{"evidence_id":"9d9ae002-6801-4d8a-bb57-07c2ce7d4cde","plan_hash":"6dc7ae420b848c6ff82833b54b4bf2eab29d49d5eacd31cde51e77e1a0d4f275","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ca891be549ffaaaeef4456870dcd7dac31210a2838a5c749742e4c23ab6aa85c","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":16,"emitted_findings":11,"total":27},"evidence_id":"9d9ae002-6801-4d8a-bb57-07c2ce7d4cde","lanes":[{"candidate_count":11,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":10,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":22,"manifest_digest":"9a9716839c01cd722e749f7c04bc1e32c086adca1aafdd70104f87e708ec324a","status":"valid"},"source_digest":"2315cf0a1271065db9fcd96cad57606881b170a2cb137b4c89c99ce201ba7c0c","version":1},"findings":[{"category":"missing-requirement","check_key":"immutable-requirements-bundle","description":"Research P11 identifies canonical requirements outside the immutable snapshot. Sections 2.4 and 6.2 add causal context and paging without snapshotting/hashing those requirements or updating the prompt consumers, so a lane can attest plan-to-prompt parity while missing user-owned requirements.","finding_id":"F-immutable-requirement-snapshot","location":"§§ 2.4 and 6.2","prevention":"Map every evidence-base recommendation to a deliverable or explicit deferral, then trace each snapshot field through producer, lane projection, consumer, and drift test.","principle":"Requirements traceability must bind canonical requirements to the same immutable evidence reviewed by every lane.","root_cause":"The plan scopes transport to plan sections and leaves the parent-task description and named requirement documents outside evidence preparation; it also leaves the taskless workflow and review skill on whole-snapshot/echo behavior.","section_id":"6.2","severity":"blocking","suggested_fix":"Extend evidence preparation and snapshot IO with hashed parent-task and named-document requirement bundles plus compact IDs; target the taskless agent and plan-review skill to consume scoped pages/tokens and verify reconstruction; add a test where live task access is unavailable yet traceability succeeds from immutable evidence."},{"category":"missing-requirement","check_key":"repair-proof-authoritative-universe","description":"Omitting a resolution record erases its finding from the required set. Even present attestations can name incomplete consumer and adjacent sweeps, and deviating fixes need not demonstrate the original counterexample or accepted new risk. Post-spawn coverage in 5.1 cannot repair this pre-spawn hole.","finding_id":"F-repair-gate-universe","location":"§§ 2.2, 2.3, 2.5, and 5.1","prevention":"For every repair gate, enumerate the authoritative source set, require exactly one resolution per source item, and compare proof records against the complete derived universe before spawn.","principle":"A repair gate must derive obligations from canonical prior results and repository evidence, never from the caller payload being validated.","root_cause":"Caller-supplied resolution records define the required repair set, while attestation fields are checked for identity and section hashes without comparison to a server-derived consumer/variant/contract universe or cross-finding overlap graph.","section_id":"2.2","severity":"blocking","suggested_fix":"Load the canonical prior round result, require exactly one repair/carry resolution for every finding, derive required consumers/variants/changed contracts and repair-bundle interaction edges server-side, validate swept/deferred dispositions and zero-result evidence, and require deviation validation under the original counterexample plus explicit acceptance of added risk."},{"category":"traceability","check_key":"evidence-jsonb-lifecycle-wiring","description":"Following the listed targets can leave both JSONB columns unread and unwritten. In particular, a merge module and next-round injection do not persist the current round's ledger into the evidence row.","finding_id":"F-evidence-column-lifecycle","location":"§§ 2.1, 2.2, 4.1, and 4.2","prevention":"Trace each new database field through migration, baseline, typed row, insert/update/finalize paths, snapshot reader, and integration test.","principle":"A persisted field is complete only when schema, typed model, write transition, read transition, and round-trip test are all named.","root_cause":"Sections add quality_ledger and repair_attestations columns plus new helper modules, while PlanReviewEvidence and PlanReviewEvidenceStore are absent from the persistence work and no current-round ledger write transition is specified.","section_id":"2.1","severity":"blocking","suggested_fix":"Add review_evidence_models.py, review_evidence_store.py, and the service integration points to the affected targets; define idempotent writes and deserialization for both columns; test prepare/finalize/snapshot round trips from a current disposition through the next round."},{"category":"weak-testability","check_key":"interactive-finding-validation-path","description":"The proposed failure-trace, minimal-repair, severity, and blocking-approval rules can all be bypassed by interactive plan review even after their local validators and documentation are implemented.","finding_id":"F-interactive-finding-contract","location":"§§ 1.1, 4.3, and 7.1","prevention":"For each required result field, identify every producer and the single canonical validator invoked before durable apply/finalize.","principle":"Schema requirements must gate the canonical production persistence path and every canonical producer.","root_cause":"The evidence-bound interactive path calls validate_round_result, which accepts arbitrary finding objects, without invoking the finding validator; the taskless producer contract also omits failure_trace and minimal_repair.","section_id":"4.3","severity":"blocking","suggested_fix":"Wire validate_plan_review_findings into the evidence-bound apply/finalize path using immutable section IDs, enforce zero blocking findings for approval, update or deduplicate the taskless result contract to require failure_trace and minimal_repair, and add service-level rejection/approval tests."},{"category":"traceability","check_key":"consumer-inventory-snapshot-gate","description":"The planned inventory cannot reach prior_round_context as written. Causal routing also omits changed acceptance-item IDs and changed target inventory, and the existing sweep can skip when index storage is unavailable.","finding_id":"F-consumer-inventory-integration","location":"§§ 2.4, 2.5, and 5.2","prevention":"Trace generated inventories from source diff through storage/serialization, gate consumption, unavailable-index behavior, and end-to-end refusal tests.","principle":"Machine-generated inventory must have a named producer, transport, mandatory disposition gate, and failure policy.","root_cause":"run_consumer_sweep currently serves CLI validation and the expansion spawn gate, while section 5.2 targets only consumer_sweep.py and does not wire typed inventory into evidence preparation/snapshot IO or require coordinator disposition before adversary spawn.","section_id":"5.2","severity":"blocking","suggested_fix":"Target evidence preparation and review_evidence_io, derive changed acceptance items/targets/symbols/contracts, persist or deterministically reconstruct typed candidate sites, require every site to be dispositioned before spawn, fail closed or explicitly fall back when inventory is unavailable, and add service-level snapshot/refusal tests."},{"category":"over-engineering","check_key":"timeout-transition-completeness","description":"The checkpoint is a structural mechanism with no complete consumer, so a timed-out adversary still cannot converge to a canonical result. Regression coverage for native lane terminal-result retention and ancestor-wake isolation is also absent.","finding_id":"F-timeout-recovery-transition","location":"§ 6.3 and V1/E9","prevention":"Walk timeout-before-work, timeout-after-lane, timeout-after-disposition, and timeout-before-delivery paths from trigger through durable state to the next actor.","principle":"Every timeout needs one complete state transition; partial durability machinery needs a concrete writer and reader.","root_cause":"The plan introduces a partial-result checkpoint with no schema, writer, validation point, reader, rebind rule, or retry transition, while the changelog explicitly declines the only stated resume consumer.","section_id":"6.3","severity":"blocking","suggested_fix":"Use the simpler form: drop partial-result reuse, classify timeout as inconclusive/timeout, expire evidence, and retry the same display round from fresh evidence without a changelog checkpoint. Add native-lane wake/result-retention regression tests. If reuse remains required, specify the full monotonic checkpoint schema, writer, validator, replacement-run reader, source-hash guard, and rebind tests."},{"category":"bad-sequencing","check_key":"telemetry-production-sequencing","description":"The task can pass its named tests without production rounds ever emitting usable convergence telemetry, and concurrent implementation can freeze the schema before the measured subsystems exist.","finding_id":"F-telemetry-integration","location":"§ 7.2","prevention":"For each metric, identify producer, provenance IDs, validator, storage field, consumer, and dependency edge before scheduling telemetry.","principle":"Telemetry that measures a finished system must land after its producers and be wired through validation, persistence, and a named reader.","root_cause":"Section 7.2 depends only on P4 and targets a standalone module, while it measures repair, sweep, orchestration, and minimal-repair outputs and has no round-result producer/validator, strict MCP schema, finalizer, or complete acceptance schema.","section_id":"7.2","severity":"blocking","suggested_fix":"Depend on the repair, sweep, orchestration, and 7.1 outputs; target the round-result model, MCP schema/wrapper, coordinator/finalizer, and E1 comparison consumer; assert every metric listed in the evidence base with provenance and a durable round-trip test."},{"category":"bad-sequencing","check_key":"shared-target-write-order","description":"Expansion may schedule conflicting edits concurrently: 4.3/5.1 share coverage; 2.4/4.2/6.2 share evidence IO; 5.3/6.2 share the MCP wrapper; and 4.3/6.1/6.3/7.1 overlap prompt surfaces.","finding_id":"F-shared-target-sequencing","location":"§§ 4.3, 5.1, 5.3, 6.1, 6.2, 6.3, and 7.1","prevention":"Build a target-to-deliverable map and either consolidate each shared surface under one owner or add semantic dependency edges before manifest handoff.","principle":"Independently schedulable plan leaves must not own overlapping write surfaces without an explicit merge order.","root_cause":"The manifest leaves that edit review_coverage.py, review_evidence_io.py, the review-evidence MCP wrapper, plan-review skill, and taskless agent YAML have no dependency chain despite shared ownership.","section_id":"6.2","severity":"blocking","suggested_fix":"Consolidate shared-file changes into owner deliverables or add dependency chains reflecting schema first, service/transport second, and prompt/contract consumers last; re-derive the manifest and verify no unordered shared write surface remains."},{"category":"unhandled-edge","check_key":"terminal-result-identity","description":"A message containing verdict and coverage_attestation can satisfy the guard without a valid round result. The proposed dispatch-run lookup is circular because end_agent_run currently discovers the run only through session context.","finding_id":"F-terminal-guard-contract","location":"§ 3.1","prevention":"Test malformed content, wrong evidence, missing session context, missing trusted run identity, and the real registry-construction path.","principle":"Self-termination must validate a canonical result bound to a trusted current-run and evidence identity.","root_cause":"Substring checks accept arbitrary parent-directed text, the fallback needs run_id after the missing session ContextVar has already failed, and the production AgentsRegistryContext constructor wiring is omitted.","section_id":"3.1","severity":"blocking","suggested_fix":"Parse one unambiguous canonical payload with validate_round_result, verify coverage_attestation.evidence_id against live evidence, carry trusted agent-run identity independently through invocation context, resolve the child session from that run, target create_agents_registry for new dependencies, and add negative plus real-constructor tests."},{"category":"unhandled-edge","check_key":"code-index-generation-consistency","description":"The plan calls this protocol a pin, yet separate lane processes can observe different index states with no detectable citation drift.","finding_id":"F-index-generation-proof","location":"§§ 5.2 and 6.1","prevention":"Verify an index/repository token before and after every lane and exercise concurrent edit/reindex cases.","principle":"All parallel lanes must either observe one code-index generation or detect drift and rerun affected work.","root_cause":"A coordinator index followed by --no-freshness disables checks without holding a lock or epoch; path citation rehash cannot detect an omitted consumer introduced during a concurrent edit/reindex.","section_id":"6.1","severity":"blocking","suggested_fix":"Without adding the declined schema counter, capture the existing code_indexed_projects.last_indexed_at plus a repository-source digest immediately after indexing; require each lane to verify both before and after search, rerun affected lanes on mismatch, and test concurrent reindex and newly introduced omitted-consumer cases."},{"category":"weak-testability","check_key":"severity-diagnostic-parity","description":"An invalid new severity would still report the old two-value vocabulary.","finding_id":"N-stale-severity-diagnostic","location":"§ 1.1","prevention":"Search exhaustive diagnostics whenever an enum-like constant changes.","principle":"Operator diagnostics that enumerate a closed vocabulary should derive from its canonical constant.","root_cause":"The current invalid-severity diagnostic hard-codes blocking and nit while section 1.1 expands the constant.","section_id":"1.1","severity":"nit","suggested_fix":"Render the diagnostic from FINDING_SEVERITIES and add one invalid-severity assertion."}],"reviewer_session":"#9675","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"b8a7e010-897e-432b-ba4f-1c8fb712e3f3","plan_hash":"591e2738ba5d66adfa29a02aa4b8a0a29d7f7503621decc77413ec5cd7372e57","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1c8ecfe9d74b82c9048d0468addab4347cb951ff33b5a5a4e6c288e77517916e","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":9,"total":20},"evidence_id":"b8a7e010-897e-432b-ba4f-1c8fb712e3f3","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":8,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":24,"manifest_digest":"924e2d180c4077fa92c60806cc5e7551be93e994049978568e27934c0d345902","status":"valid"},"source_digest":"7011220aed7370e413486325790111294e72eb2fa9c1df0cdebc5e8fcef5adbe","version":1},"findings":[{"category":"traceability","causal_finding_id":"F-immutable-requirement-snapshot","causal_section_ids":["6.5"],"check_key":"immutable-preparation-context-lifecycle","description":"The consumer inventory/inventory_unavailable state and requirements bundle are promised as immutable evidence-row data but have no column or named encoding, model/store path, or complete producer-to-coordinator transition. They can be lost, resubmitted by the caller, or reconstructed from live mutable state.","finding_id":"F-preparation-context-lifecycle","introduced_in_round":2,"location":"§§ 2.1, 5.2, and 6.5","prevention":"Trace each prepared field through authoritative source derivation, schema, atomic write, typed read, snapshot transport, restart, and live-source-mutation tests.","principle":"Every preparation-owned value consumed later must have one immutable typed storage, write, read, and restart lifecycle.","root_cause":"Round-1 repairs added inventory and requirements data conceptually, while migration 343, PlanReviewEvidence, and its store add lifecycle only for quality_ledger and repair_attestations; the coordinator also has no preview transition for dispositioning a server-derived inventory before preparation can succeed.","section_id":"6.5","severity":"blocking","suggested_fix":"Extend migration 343 and the baseline/model/store with checked typed prior-round-context and requirements-bundle storage. Derive inventory through a two-phase preview-then-prepare API, or persist it at prior finalization, then atomically persist all context and add restart plus live-source-mutation round-trip tests."},{"category":"bad-sequencing","causal_finding_id":"F-shared-target-sequencing","causal_section_ids":["1.1","2.1","2.2","2.3","2.4","2.5","2.6","3.1","4.1","4.3","5.2","5.3","6.1","6.2","6.3","6.5","7.1","7.2"],"check_key":"review-evidence-service-integration-owner","description":"Preparation, snapshot assembly, coverage validation, finalization, and inter-round diffing all require edits in review_evidence.py. The plan schedules those changes across unordered leaves and provides no integration leaf, so concurrent expansion can conflict or push the file over the repository limit.","finding_id":"F-review-evidence-integration-owner","introduced_in_round":2,"location":"§§ 2.2, 2.4, 2.6, 4.1, 4.3, 5.1, 5.2, 6.2, 6.5, and 7.2","prevention":"Build a target-to-deliverable map including implicit call-site wiring, assign one integration owner per shared production file, and enforce the 1,000-line source limit before expansion.","principle":"Independently schedulable leaves must not share a production write surface without one explicit owner and dependency order.","root_cause":"Multiple repaired sections require signature or call-site changes in the 966-line PlanReviewEvidenceService but omit it from targets and remain unordered; the added dependency chains cover helper modules and prompt surfaces rather than this common service.","section_id":"4.3","severity":"blocking","suggested_fix":"Create one review_evidence.py integration-owner deliverable that depends on every upstream schema/helper, performs only call-site wiring, and extracts logic to new modules so the service stays below 1,000 lines. Remove implicit service ownership from parallel leaves."},{"category":"weak-testability","causal_finding_id":"F-repair-gate-universe","causal_section_ids":["2.2","2.3","2.5","2.6"],"check_key":"repair-universe-service-integration","description":"The E2E leaf can finish before the server-derived consumer universe exists and never tests an apparently complete attestation that omits one required site. Post-spawn coverage likewise has no ordered typed input carrying prior repairs, changed contracts, or inventory.","finding_id":"F-repair-universe-integration","introduced_in_round":2,"location":"§§ 2.5, 2.6, and 5.1","prevention":"For each authoritative universe, trace producer, immutable transport, validator input, dependency edge, and a service-boundary strict-subset rejection test.","principle":"A server-derived obligation universe must be wired into every validator and exercised end to end with a strict-subset failure.","root_cause":"Section 2.5 precedes 2.6 and tests missing whole records, while 5.1 precedes the prior-context and inventory producers it needs to derive required sweep keys.","section_id":"2.6","severity":"blocking","suggested_fix":"Make 5.1 depend on 2.4 and 5.2 and wire a typed required-sweep universe into coverage validation. Make 2.5 depend on 2.6 and add a two-round service test that submits a strict-subset attestation and verifies refusal before spawn naming the omitted site."},{"category":"traceability","causal_finding_id":"F-interactive-finding-contract","causal_section_ids":["4.3"],"check_key":"finding-contract-surface-parity","description":"Wiring the canonical validator as planned will reject the taskless payload because its remedy fields diverge. Updating the shared plan-review skill to four severities also makes the legacy stage-native producer emit values its strict MCP schema rejects.","finding_id":"F-finding-contract-surface-parity","introduced_in_round":2,"location":"§§ 1.1, 4.3, and 7.1","prevention":"Enumerate every closed-schema producer and consumer, choose one field vocabulary, and run one exact production-payload round trip through each public entry point.","principle":"One canonical finding schema must reach every producer, wrapper, validator, renderer, and legacy consumer atomically.","root_cause":"The existing validator/renderer require fix, the taskless contract emits suggested_fix and will add minimal_repair, and the stage-review MCP schema still permits only blocking/nit with no new fields.","section_id":"4.3","severity":"blocking","suggested_fix":"Use the simpler form: make minimal_repair the single canonical remedy field and remove fix/suggested_fix ambiguity. Update review_findings, its renderer, the taskless schema, the shared skill/contract, and stage-review MCP schema together; add schema-layer and finalize-path tests using the exact emitted payload."},{"category":"unhandled-edge","causal_finding_id":"F-index-generation-proof","causal_section_ids":["6.1"],"check_key":"index-token-atomic-capture","description":"An old index can be paired with a new repository digest and then remain unchanged throughout all lane checks, so the proposed before/after verification accepts an internally inconsistent token and can omit a newly introduced consumer.","finding_id":"F-index-token-atomic-capture","introduced_in_round":2,"location":"§§ 5.2 and 6.1","prevention":"Walk source-before-index, edit-during-index, edit-after-index-before-token, inventory, and lane before/after verification as one state machine.","principle":"An index token must prove that indexed content and repository digest describe the same stable source state.","root_cause":"Capturing the source digest only after gcode index leaves a race where a source edit follows indexing but precedes digest capture; preparation-time inventory is not explicitly bound to the same token.","section_id":"6.1","severity":"blocking","suggested_fix":"Capture digest A, run gcode index, capture last_indexed_at and digest B, and retry until A equals B. Persist that token on evidence, run preparation inventory with freshness disabled under the same token, and verify it before and after inventory and every lane."},{"category":"unhandled-edge","causal_finding_id":"F-terminal-guard-contract","causal_section_ids":["3.1"],"check_key":"terminal-guard-totality","description":"When session resolution fails, the fallback still has no trusted request-level run identity. Even with identity present, a valid needs_requirements or inconclusive/source_drift result is rejected by the proposed guard, leaving reachable contract outcomes unable to terminate cleanly.","finding_id":"F-terminal-guard-totality","introduced_in_round":2,"location":"§ 3.1","prevention":"Test every identity source and canonical verdict through the real MCP wrapper, request context, registry constructor, result parser, and end_agent_run path.","principle":"Self-termination must resolve trusted run identity on every request and accept every canonical terminal verdict.","root_cause":"The child environment contains the run ID, but stdio forwarding, daemon request context, and the production registry expose only project/session identity. The selected validate_round_result helper also accepts only approved and needs_review while the reviewer contract permits needs_requirements and inconclusive.","section_id":"3.1","severity":"blocking","suggested_fix":"Forward the managed run ID as authenticated transport metadata, bind it server-side to the child session, seed/reset a per-request run ContextVar, and wire its getter through create_agents_registry. Extend the canonical round-result validator to the full verdict universe and add omission, spoofing, wrong-evidence, needs_requirements, and inconclusive tests."},{"category":"bad-sequencing","causal_finding_id":"F-telemetry-integration","causal_section_ids":["7.2"],"check_key":"telemetry-authoritative-production","description":"Calls per finding, wall time, and final turn counts are incomplete when the reviewer sends its result, and per-lane duration/call counts have no daemon-side source. Strict validation can accept fabricated or partial values while production still cannot emit authoritative telemetry.","finding_id":"F-telemetry-authoritative-production","introduced_in_round":2,"location":"§§ 3.1, 6.3, and 7.2","prevention":"For every metric, name its authority, availability point, unavailable state, persistence transition, and reader before fixing the schema.","principle":"Telemetry must be produced after its authoritative inputs exist and must represent unavailable measurements explicitly.","root_cause":"The reviewer writes round_result before terminal cleanup computes final turns/tool calls, and provider-native lanes create no AgentRun records; 7.2 assigns all metrics to reviewer self-reporting without adding an observable producer.","section_id":"7.2","severity":"blocking","suggested_fix":"Split telemetry by authority: let the reviewer emit classification and repair provenance, then derive aggregate run statistics after terminal cleanup from the bound AgentRun. Instrument native-lane timing/calls at the orchestrator or persist an explicit unavailable reason; cover success, timeout, and failed runs."},{"category":"over-engineering","check_key":"scoped-fetch-lane-completeness","description":"section_ids-based lane splitting has no valid consumer under the unchanged all-sections-per-lane contract. A lane can fetch a subset, list all IDs, and pass; the page-union test proves server reconstruction rather than per-lane review completeness.","finding_id":"F-scoped-fetch-lane-completeness","location":"§§ 2.4 and 6.2","prevention":"Match transport partitions to the actual coverage invariant and bind fetch receipts to attestations before adding section-scoping mechanisms.","principle":"Transport optimization must preserve the evidence each lane is required to inspect, and added scoping needs a concrete consumer.","root_cause":"The plan treats the three concern lanes as disjoint section owners even though every lane must enumerate and inspect every deliverable; validation checks claimed IDs rather than fetched bodies.","section_id":"6.2","severity":"blocking","suggested_fix":"Use the simpler form: drop section-based lane splitting, page the full immutable snapshot to every lane, and require a server-issued page/section digest receipt covering the full manifest in each lane result. Retain offset/limit paging and manifest caching only where they have concrete consumers."},{"category":"traceability","check_key":"orphan-removal-contract-sweep","description":"Section 8.2 deletes _format_search_memories_result and related injection-outcome wiring while the memory usefulness contract still identifies those surfaces as live. The implementation can satisfy every listed acceptance item and leave the normative contract false.","finding_id":"F-orphan-memory-contract","location":"§ 8.2","participating_section_ids":["8.2"],"prevention":"For symbol deletion, sweep source, tests, registries, docs/contracts, memories, and generated manifests before declaring the removal complete.","principle":"Deleting a named runtime surface requires updating every normative contract that declares it live.","root_cause":"The blast-radius inventory covered implementation and tests but omitted docs/contracts/memory-usefulness-label.md, which names the renderer and injection-outcome writer that 8.2 removes.","section_id":"8.2","severity":"blocking","suggested_fix":"Add docs/contracts/memory-usefulness-label.md to 8.2 targets and acceptance, rewrite both references around the surviving helper-delivery and memory-context paths, and verify no contract reference to the deleted symbols remains."}],"reviewer_session":"#9680","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"0a6c7fd3-aabb-4f21-beee-c71204e19250","plan_hash":"528278841e9b744604d54cff8aa9feef8633eac58f15fa4096aab839fba4c90b","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"95ca1a23efff253b83a13fd37f39112f70a61606ce7d78a0fad9d49f5bccb450","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":10,"total":18},"evidence_id":"0a6c7fd3-aabb-4f21-beee-c71204e19250","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"e99189ae6fe5a3c13d2acd6ca35300fecdba88d34861c3491cc118566c07ae51","status":"valid"},"source_digest":"918e803e7548d21d4d0843fa1d681ceb7c6ede849b73a8623aa290886d081dbf","version":1},"findings":[{"category":"traceability","causal_finding_id":"F-finding-contract-surface-parity","causal_section_ids":["1.1"],"check_key":"remedy-record-vocabulary-boundaries","description":"`minimal_repair` becomes the canonical finding remedy, yet `deviation_from_suggested_fix` remains the deviation gate. Meanwhile lane candidates legitimately still use `suggested_fix`; acceptance 1.1.5 can be read as removing that live candidate field too. The repair gate therefore has no canonical source for deciding deviation, and a global rename would break coverage candidates.","finding_id":"F-remedy-contract-boundaries","introduced_in_round":3,"location":"§§ 1.1, 2.2, 2.3, and 7.1","prevention":"Enumerate every record type that carries remedy text, choose its field intentionally, and round-trip one lane candidate through finding emission and repair attestation.","principle":"Canonical finding remedies and adjacent repair records need explicit record-type boundaries and one traceable semantic link.","root_cause":"The round-2 rename covered final findings while leaving repair attestations dependent on the removed suggested_fix field and leaving the distinct lane-candidate suggested_fix contract unscoped.","section_id":"2.2","severity":"blocking","suggested_fix":"Keep `suggested_fix` explicitly scoped to lane candidates, rename the attestation field to `deviation_from_minimal_repair`, and add one exact candidate → finding → attestation round-trip test."},{"category":"bad-sequencing","causal_finding_id":"F-review-evidence-integration-owner","causal_section_ids":["P8","8.1","P9","9.1","9.2","9.3"],"check_key":"integration-owner-leaf-closure","description":"Several upstream leaves must pass preparation/finalization tests that require `review_evidence.py` wiring, although 8.1 is the sole later owner of that file. Section 2.5 can also run before 8.1, and no section owns the strict repair-attestation/resolution schema and exact MCP wrapper round trip. Expansion can therefore produce leaves whose acceptance is impossible when they close.","finding_id":"F-integration-leaf-closure","introduced_in_round":3,"location":"§§ 2.2, 2.5, 4.1, 4.3, 5.3, 6.5, 7.2, and 8.1","prevention":"After assigning an integration owner, classify every acceptance item as helper-local, public-wrapper, service-integration, or end-to-end and order each under the corresponding owner.","principle":"Every manifest leaf must be independently closable; integration acceptance belongs to the leaf that owns the integration surface.","root_cause":"The new service owner was added after upstream leaves without moving their service-level acceptance or the public wrapper contract to dependency-ordered owners.","section_id":"8.1","severity":"blocking","suggested_fix":"Keep helper-local acceptance upstream, assign the preparation MCP schemas and wrapper round trip to 5.3, move service-bound round trips to 8.1, and make 2.5 depend on 8.1 while retaining its dependency on 2.6."},{"category":"unhandled-edge","causal_finding_id":"F-terminal-guard-totality","causal_section_ids":["3.1"],"check_key":"terminal-verdict-transport-totality","description":"`needs_requirements` and `inconclusive` are declared valid, yet repeated source drift cannot produce the required coverage attestation, and timeout may end before lanes complete. The listed targets also stop short of the actual stdio → HTTP → request-ContextVar chain that must carry and authenticate the run ID. Acceptance can pass with fakes while production remains unable to validate or identify these terminal branches.","finding_id":"F-terminal-guard-production-totality","introduced_in_round":3,"location":"§§ 3.1, 5.1, and 6.3","prevention":"Trace each terminal verdict and identity source through producer, serialization, authenticated transport, request context, validator, registry construction, and end-agent execution.","principle":"A terminal guard is total only when every verdict has a producible envelope and trusted identity reaches the guard through the real production transport.","root_cause":"The repair names the consumer-side guard but omits the stdio header path, HTTP verification/context seeding, registry constructor, canonical validator owner, and a verdict-dependent non-attested result shape.","section_id":"3.1","severity":"blocking","suggested_fix":"Define a verdict-discriminated round-result union: reviewed verdicts require canonical coverage; non-attested terminal branches require a bound `evidence_id` plus typed reason data. Add the stdio proxy, MCP execution endpoint, session ContextVar, registry constructor, and round-result validator to 3.1 ownership, then test real source-drift and spoofed/missing run-ID paths end to end."},{"category":"missing-requirement","check_key":"immutable-requirement-source-discovery","description":"The plan names research documents, another plan, target contracts, and test files, but 6.5 supplies no declaration or parsing rule that distinguishes requirement sources from ordinary references. Two conforming implementations can snapshot different requirements while both satisfy the current acceptance items.","finding_id":"F-requirements-source-universe","location":"§§ Overview, 2.1, and 6.5","participating_section_ids":["2.1","6.5"],"prevention":"For every snapshot bundle, specify source discovery, identity, path scope, duplicate handling, read failure, hashing, and live-source mutation behavior.","principle":"An immutable requirements bundle needs one deterministic, fail-closed source universe before hashing can make it authoritative.","root_cause":"The bundle promises parent requirements and documents the plan names without defining which references qualify, how paths resolve, or what missing and unreadable sources do.","section_id":"6.5","severity":"blocking","suggested_fix":"Define the authoritative universe as the parent task or initiating session request plus explicitly designated requirement-document references, resolve repository-relative paths under the project root, fail closed on missing/unreadable references, and test duplicates, non-requirement references, and no-task interactive review."},{"category":"unhandled-edge","causal_finding_id":"F-preparation-context-lifecycle","causal_section_ids":["2.1","6.5"],"check_key":"preparation-inventory-disposition-transition","description":"Preparation derives the inventory inside one transaction, callers cannot resubmit context, and preparation refuses while any site is undispositioned. A non-empty inventory therefore has no successful first call: the coordinator cannot disposition sites it has not seen, and retrying from an error would be an unstated, non-durable preview protocol.","finding_id":"F-inventory-disposition-transition","introduced_in_round":3,"location":"§§ 2.1, 2.4, 5.2, and 8.1","prevention":"For each pre-spawn gate, order authoritative derivation, actor visibility, disposition, validation, durable write, retry, and abandonment paths.","principle":"A gate cannot require an actor to disposition server-derived data before the same single-shot transition first reveals that data.","root_cause":"Round-2 atomic persistence removed the proposed preview transition while retaining a coordinator-disposition prerequisite with no input surface or earlier durable producer.","section_id":"5.2","severity":"blocking","suggested_fix":"Use the simpler transition: remove coordinator pre-disposition and treat every server-derived site as part of the required repair/sweep universe that attestations must cover or defer inside the same preparation transaction. Add a non-empty-inventory success/refusal test. Introduce a preview API only if a genuine human decision remains required."},{"category":"over-engineering","causal_finding_id":"F-scoped-fetch-lane-completeness","causal_section_ids":["6.2"],"check_key":"lane-page-receipt-consumer-binding","description":"The server-issued page receipt can prove that the parent reconstructed the snapshot. It cannot prove that each native lane fetched the full snapshot because those lanes lack a server-authenticated identity; the same receipt can be replayed in all three results. Adding lane authentication solely for this proof would expand mechanism without improving evidence that a model actually reviewed the content.","finding_id":"F-page-receipt-consumer-binding","introduced_in_round":3,"location":"§§ 5.1 and 6.2","prevention":"Before adding a receipt, name its issuer, authenticated subject, replay boundary, verifier, and the simpler invariant already available.","principle":"A proof mechanism needs an authentic subject; a replayable receipt adds ceremony without proving the claimed per-consumer fact.","root_cause":"The receipt repair assumes lane identity, while provider-native lanes have no AgentRun identity and can all echo the parent reviewer's receipt.","section_id":"6.2","severity":"blocking","suggested_fix":"Replace per-lane receipts with one parent snapshot-reconstruction receipt, then retain exhaustive lane section IDs and hashed section-specific citations as the lane evidence. Add a replay test showing one parent receipt cannot be presented as three independent fetch proofs."},{"category":"bad-sequencing","causal_finding_id":"F-telemetry-authoritative-production","causal_section_ids":["7.2"],"check_key":"terminal-telemetry-persistence-order","description":"The parent can be woken and finalize the evidence after the AgentRun terminal transition while self-termination cleanup is still computing final calls/turns. A later aggregate merge conflicts with the already-finalized `round_result`; timeout and failed paths expire evidence without the named finalization reader. The proposed record is therefore neither authoritative nor durable across the accepted terminal states.","finding_id":"F-terminal-telemetry-ordering","introduced_in_round":3,"location":"§§ 3.1, 6.3, 7.2, and 8.1","prevention":"For every post-terminal metric, draw the exact success, timeout, and failure event order through persistence and parent wake, then race the earliest consumer.","principle":"Daemon-derived terminal metrics must be persisted after their authoritative inputs exist and before any consumer is released to finalize the immutable record.","root_cause":"The authority split was added without ordering terminal cleanup, aggregate merge, parent wake, evidence finalization, timeout, and failed-run expiry.","section_id":"7.2","severity":"blocking","suggested_fix":"Choose one ordering and own it explicitly: complete terminal cleanup and idempotently merge daemon aggregates into the bound result before waking the parent, for success, timeout, and failure. Target the run-completion and cleanup paths, then test an immediate parent-finalization race plus timeout/failure persistence."},{"category":"bad-sequencing","causal_finding_id":"F-index-token-atomic-capture","causal_section_ids":["6.1"],"check_key":"index-token-storage-consumer-order","description":"Section 6.1 says the settled token is persisted and gates preparation inventory, but the typed location is unnamed and 5.2 is an upstream dependency of 6.1. The inventory producer can close and run before the token helper exists, and the current acceptance only tests helper drift rather than durable binding.","finding_id":"F-index-token-lifecycle-order","introduced_in_round":3,"location":"§§ 2.1, 5.2, 6.1, and 8.1","prevention":"Trace each token through capture, typed storage, restart, transport, first consumer, last consumer, mismatch, and retry-exhaustion, then verify dependency direction.","principle":"A consistency token must have typed durable storage and be produced before every operation it is claimed to bind.","root_cause":"The atomic-capture repair stayed in 6.1, which is ordered after 5.2 through 5.1 → 4.3 → 7.1, while no prior-context field or integration acceptance owns the token lifecycle.","section_id":"6.1","severity":"blocking","suggested_fix":"Move token capture/storage ownership to 5.2 or an earlier existing host, store a typed `index_token` inside `prior_round_context`, and let the later 6.1 prompt edits consume it. Add restart round-trip, missing-token, before/after-inventory mismatch, and retry-exhaustion tests without adding a new column."},{"category":"bad-sequencing","check_key":"bundled-content-manifest-final-owner","description":"Each shared skill, agent definition, or rule edit changes a hash in `src/gobby/install/bundled_content_manifest.json`. Leaving it untouched fails `test_committed_bundled_content_manifest_matches_shared_tree`; letting every leaf refresh it creates a shared-target race. E1 does not run that parity test.","finding_id":"F-bundled-content-manifest-owner","location":"§§ 1.1, 4.3, 6.1, 6.3, 6.5, 7.1, and 9.3","participating_section_ids":["1.1","4.3","6.1","6.3","6.5","7.1","9.3"],"prevention":"For every generated registry, inventory all source editors, assign one final regeneration owner, and run the repository's exact source-to-generated parity test.","principle":"Generated registries shared by multiple leaves need one dependency-ordered final owner and their exact parity validation.","root_cause":"Seven sections edit hashed `install/shared` assets, while none targets the committed bundled-content manifest or its parity test.","section_id":"9.3","severity":"blocking","suggested_fix":"Make 9.3 depend on 6.5, add `src/gobby/install/bundled_content_manifest.json` to its targets, regenerate it once with the canonical writer after the rule edit, and run `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`."},{"category":"missing-requirement","check_key":"cross-language-import-consumer-inventory","description":"Implementing the stated `_module_candidates` wiring finds Python import consumers only. TypeScript, TSX, JavaScript, and JSX targets remain absent from the server-derived universe even though the code index stores those imports, so repairs in Gobby's web surface can receive a falsely complete inventory.","finding_id":"F-cross-language-import-inventory","location":"§ 5.2","participating_section_ids":["5.2"],"prevention":"For each consumer edge, enumerate supported file extensions, absolute and relative specifiers, index resolution semantics, unsupported languages, and one real fixture per variant.","principle":"A repository blast-radius inventory must cover every indexed language variant it claims to support or explicitly disposition exclusions.","root_cause":"The planned import-edge wiring reuses `_module_candidates`, whose current path-to-module mapping rejects non-Python files although gcode indexes JavaScript and TypeScript import specifiers.","section_id":"5.2","severity":"blocking","suggested_fix":"Add language-aware `.ts`, `.tsx`, `.js`, and `.jsx` target matching using graph-resolved imports or importer-relative resolution, explicitly mark unsupported languages, and add real Python plus TypeScript/TSX import-edge tests."}],"reviewer_session":"#9690","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"bd3f4134-152f-4596-9e8b-03c79c6f81e7","plan_hash":"b4021b73e6c0af40f5ae7d680eaec84db3903595d72f8c1f7f1930d6075ba0a8","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c70d8d734d1de21b9c4277525018e1e46d099587d1b9baa44ff699844ff5e852","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":9,"total":15},"evidence_id":"bd3f4134-152f-4596-9e8b-03c79c6f81e7","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"24463c9a9b8f2cf5c2ac0fcfc8d29de33e7f13e48b5bb9b9519bc9b6e85de9c4","status":"valid"},"source_digest":"ba2d63b8a7b35419ea3bad29144d09af3fbb81ab9087ed01861a013c2d3e421b","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"F-integration-leaf-closure","causal_section_ids":["8.1","5.3","2.5"],"check_key":"integration-owner-leaf-closure","description":"The compiler still assigns service-level criteria and covers labels to upstream leaves, so 8.1.4 cannot make those leaves independently closable. The integration owner can also run before 2.6 even though it claims every phase is wired.","finding_id":"F-integration-manifest-closure","introduced_in_round":4,"location":"§§ 2.1–7.2 and 8.1","prevention":"After assigning an integration owner, move each service-bound acceptance item to that section, regenerate the manifest, and verify the owner depends on every wired helper.","principle":"Acceptance ownership must be represented by the manifest leaf that carries the criterion, and an integration owner must depend on every helper it wires.","root_cause":"The round-3 repair described a prose-level transfer of service acceptance without moving the acceptance items, while 8.1 still has no direct or transitive dependency on 2.6.","section_id":"8.1","severity":"blocking","suggested_fix":"Physically move service-bound acceptance items to 8.1 with 8.1.x IDs, leave helper/wrapper-local checks upstream, add 2.6 to 8.1's dependencies, and make 8.1.4 enumerate the remaining independently closable checks."},{"category":"traceability","causal_finding_id":"F-terminal-guard-production-totality","causal_section_ids":["3.1"],"check_key":"terminal-run-identity-endpoint-ownership","description":"The HTTP MCP execution endpoint owns header extraction, validation, context seeding, dispatch, and reset, yet `src/gobby/servers/routes/mcp/endpoints/execution.py` is absent from 3.1 targets. The real authenticated transport chain therefore remains unowned.","finding_id":"F-terminal-endpoint-target","introduced_in_round":4,"location":"§ 3.1","prevention":"Trace trusted metadata through producer, transport, endpoint authentication, request context, consumer, and reset; target and test each hop.","principle":"Every production hop required by an end-to-end transport acceptance item needs an explicit target and route-level test.","root_cause":"The repair names the execution endpoint in prose but targets only the stdio sender and downstream ContextVar/guard surfaces.","section_id":"3.1","severity":"blocking","suggested_fix":"Add `src/gobby/servers/routes/mcp/endpoints/execution.py` and focused execution-context tests to 3.1; exercise stdio header forwarding, server-side run binding, ContextVar seed/reset, and forged, absent, and mismatched IDs."},{"category":"missing-requirement","causal_finding_id":"F-requirements-source-universe","causal_section_ids":["6.5"],"check_key":"immutable-requirement-source-discovery","description":"Two implementations can recognize different requirement references while both satisfying 6.5.4, so the supposedly authoritative requirements bundle remains implementation-dependent.","finding_id":"F-requirement-marker-grammar","introduced_in_round":4,"location":"§ 6.5","prevention":"For every declared source mechanism, specify syntax, parser ownership, path scope, duplicate rules, fence handling, and malformed/unreadable behavior.","principle":"An immutable source universe is deterministic only when its declaration grammar and malformed-input policy are canonical.","root_cause":"The round-3 repair requires a declared marker without specifying its syntax, placement, fence behavior, parser owner, or invalid-marker outcome.","section_id":"6.5","severity":"blocking","suggested_fix":"Define one exact requirement-source marker and parser owner; fail closed on malformed markers and add canonical, duplicate, fenced-code, outside-project, missing, unreadable, and ordinary-reference fixtures to 6.5.4."},{"category":"traceability","check_key":"immutable-snapshot-consumer-migration","description":"Both adversary definitions and the shared skill currently describe one snapshot call, while the wrapper test expects the whole snapshot in that call. The stage-native definition also still declares live parent-task context canonical. Paging and bundle-only traceability can therefore leave an active reviewer on the first page or mutable requirements.","finding_id":"F-snapshot-consumer-migration","location":"§§ 6.2 and 6.5","participating_section_ids":["6.2","6.5"],"prevention":"For each evidence-shape change, sweep all agent definitions, shared skills, wrappers, tests, and legacy active routes before closing the producer leaf.","principle":"Changing an evidence transport or source-of-truth contract requires migrating every active prompt consumer and its existing contract tests atomically.","root_cause":"Sections 6.2 and 6.5 update the producer, taskless definition, and shared skill incompletely; the stage-native definition and single-call wrapper test retain the old full-snapshot/live-task contracts.","section_id":"6.2","severity":"blocking","suggested_fix":"Make 6.2/6.5 jointly migrate the shared skill, both adversary definitions, and existing wrapper tests to a deterministic `next_offset` loop, page-union/hash verification, and requirement IDs/hashes from the immutable bundle; add stage-native no-live-task coverage."},{"category":"traceability","check_key":"finding-remedy-consumer-fixtures","description":"`tests/review_learning/test_round_diff.py` and `tests/storage/test_stage_review_findings.py` still construct findings with `fix`; the plan requires `minimal_repair` with no alias and runs the review-learning suite. These in-scope producers are absent from the migration.","finding_id":"F-finding-remedy-fixtures","location":"§ 1.1","participating_section_ids":["1.1"],"prevention":"Search production, tests, fixtures, docs, and workflow payloads for every removed field and migrate all literal producers in the same leaf.","principle":"A no-alias schema rename must migrate every concrete producer, including shared fixtures exercised by the required validation matrix.","root_cause":"The target sweep covers production validators and prompts while existing review-learning and stage-review finding fixtures still emit `fix` and blocking records without the new required trace.","section_id":"1.1","severity":"blocking","suggested_fix":"Add all literal finding producers to 1.1's consumer sweep, replace `fix` with `minimal_repair`, add valid `failure_trace` data to blocking fixtures, and retain `suggested_fix` only on lane candidates."},{"category":"unhandled-edge","causal_finding_id":"F-preparation-context-lifecycle","causal_section_ids":["2.1","6.5"],"check_key":"inventory-unavailable-transaction-transition","description":"If index unavailability raises inside preparation, the advertised context record rolls back. If preparation commits it, current coverage/repair-universe prose can treat the missing inventory as an empty universe and proceed. Recovery is unspecified.","finding_id":"F-inventory-unavailable-transition","introduced_in_round":3,"location":"§§ 2.1, 2.6, 5.1, 5.2, and 8.1","participating_section_ids":["2.1","2.6","5.1","5.2","8.1"],"prevention":"For each preparation failure, specify row commit/rollback, caller-visible error, spawn authorization, retry, and abandonment as one state transition.","principle":"A fail-closed branch needs one coherent transaction outcome, durable state, retry path, and spawn gate.","root_cause":"Atomic preparation rollback conflicts with persisting `inventory_unavailable`, while committing the record has no blocked state or named downstream refusal.","section_id":"5.2","severity":"blocking","suggested_fix":"Use the least mechanism: abort and roll back preparation with a typed `inventory_unavailable` error, prohibit spawn, and retry fresh preparation after index recovery. Remove the claim that the unavailable record persists and add a service test for row state plus spawn refusal."},{"category":"unhandled-edge","causal_finding_id":"F-index-token-lifecycle-order","causal_section_ids":["5.2","6.1"],"check_key":"index-token-indexed-state-binding","description":"Index source S, mutate to stable S2 before token capture, and both capture reads agree on S2 while `last_indexed_at` still describes S. Lane `--no-freshness` checks then accept a stale index under a stable token.","finding_id":"F-index-token-index-race","introduced_in_round":4,"location":"§§ 5.2, 6.1, and 8.1","prevention":"Bracket the indexing operation with source digests and retry the whole index-plus-token sequence on mismatch; recheck after the bound inventory read.","principle":"A consistency token must bind the repository digest to the exact source state indexed, across the index operation itself.","root_cause":"The repaired protocol indexes first and then retries only stable digest capture, so a mutation after indexing can pair fresh source bytes with the stale index timestamp.","section_id":"5.2","severity":"blocking","suggested_fix":"In the 5.2 index helper compute D0, run the existing index operation, read its timestamp, compute D1, and accept only D0=D1; otherwise rerun the whole sequence. Recheck after inventory derivation and add the post-index/pre-capture mutation race test."},{"category":"bad-sequencing","causal_finding_id":"F-terminal-telemetry-ordering","causal_section_ids":["7.2"],"check_key":"terminal-transition-result-staging","description":"`src/gobby/agents/agents_termination.py` does not exist; self-termination is under `mcp_proxy/tools`, while timeout/failure flow through `agent_health.py` and 980-line `agent_cleanup.py`. Without staged delivered/enriched validation, the guard may reject partial telemetry, finalization may accept missing daemon fields, or the final call/turn is omitted.","finding_id":"F-terminal-transition-staging","introduced_in_round":4,"location":"§§ 3.1, 6.3, and 7.2","prevention":"Draw success, timeout, and failure through result validation, counter settlement, persistence, evidence expiry/finalization, and direct-parent wake; target the real owner of every transition.","principle":"Reviewer-delivered and daemon-enriched results are distinct states, and all terminal paths must pass through one owned enrichment-before-notification boundary.","root_cause":"The ordering repair names a nonexistent termination target, omits the health/cleanup owners for timeout and failure, and leaves one strict result shape spanning values that do not exist until terminalization.","section_id":"7.2","severity":"blocking","suggested_fix":"Define delivered and daemon-enriched validation states; validate reviewer-owned fields at `end_agent_run`, then use one extracted terminal helper for success/timeout/failure to settle counters, atomically enrich the bound result, expire or finalize as appropriate, and wake only the direct parent. Correct targets, include actual health/cleanup paths, keep `agent_cleanup.py` to delegation, and add its required refactor task."},{"category":"over-engineering","causal_finding_id":"F-page-receipt-consumer-binding","causal_section_ids":["6.2"],"check_key":"parent-reconstruction-receipt-value","description":"The single parent receipt retained after round 3 is stateless ceremony under the proposed offset/limit API. It proves neither complete fetch nor review, and making it causal requires a new stateful protocol with no additional correctness consumer.","finding_id":"F-parent-receipt-ceremony","introduced_in_round":4,"location":"§ 6.2","prevention":"Before adding a receipt, name its authenticated subject, issuance event, replay boundary, verifier, and unique evidence beyond existing hashes.","principle":"A proof token earns its place only when it proves a fact the simpler content hash and citations cannot.","root_cause":"The remaining parent receipt has no traversal-bound issuance state, so it can be echoed after one arbitrary page; adding session bitmaps or continuation state would create machinery that still cannot prove model review.","section_id":"6.2","severity":"nit","suggested_fix":"Drop the reconstruction receipt. Keep deterministic paging, per-page bounds, `next_offset`, the full snapshot hash, exact page-union tests, exhaustive lane section IDs, and hashed citations; locally verify the concatenated snapshot hash before review."}],"reviewer_session":"#9692","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"db9a2280-660b-41b5-8d46-21b6d93e822b","plan_hash":"7d60d07b2b6f50393f936e26ded371631d503c9904d7e08630db72c4c6049059","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1d20ead2bdeb3a4677b79f2e434d5017f9a00d56945fd064daa704e1bd62c637","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":4,"total":6},"evidence_id":"db9a2280-660b-41b5-8d46-21b6d93e822b","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"537adad833ce7c282bb3ab48c36400bb6f921c7f38e1a14a7c784e2eced83fb3","status":"valid"},"source_digest":"7e8eb46785ff1cf51e5c9988877fd9a4f0fb30718156300bd1eb31612f1abed5","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"F-inventory-unavailable-transition","causal_section_ids":["5.2"],"check_key":"inventory-unavailable-transaction-transition","description":"Section 2.1 still tells its isolated implementer to persist an inventory_unavailable record, while 5.2 requires preparation to roll back without any evidence row or persisted unavailable record; the round-4 checkpoint explicitly claims that record was removed.","finding_id":"F-inventory-unavailable-context-regression","introduced_in_round":4,"location":"§§ 2.1, 5.2, and 8.1","prevention":"After changing a failure transition, sweep every storage shape, producer, consumer, integration criterion, and changelog checkpoint for the retired state label.","principle":"One state transition must have one persistence outcome across every self-contained leaf that produces, stores, or consumes it.","root_cause":"The round-4 repair removed the persisted unavailable state from 5.2 but left 2.1 defining that state as durable prior_round_context data.","section_id":"2.1","severity":"blocking","suggested_fix":"Delete “or its inventory_unavailable record” from 2.1 and state that prior_round_context contains a typed inventory only after successful preparation. Keep unavailability solely as 5.2.4's typed rollback error and spawn refusal; add no new state."},{"category":"traceability","check_key":"terminal-registry-constructor-ownership","description":"Acceptance 3.1.4 requires create_agents_registry to inject the evidence store and trusted run-identity getter, but create_agents_registry lives in src/gobby/mcp_proxy/tools/agents_registry.py and that file is absent from 3.1 targets. An isolated 3.1 leaf can therefore satisfy its listed targets without changing the real constructor.","finding_id":"F-terminal-registry-constructor-target","location":"§ 3.1","prevention":"For each production-wiring criterion, resolve the constructor symbol to its definition file and include both definition and focused constructor test in the target inventory.","principle":"Every production constructor named by an acceptance criterion needs its implementation file in that deliverable's target set.","root_cause":"The target sweep covered AgentsRegistryContext and downstream lifecycle code but stopped before the create_agents_registry definition in agents_registry.py.","section_id":"3.1","severity":"blocking","suggested_fix":"Add src/gobby/mcp_proxy/tools/agents_registry.py to 3.1 targets and make 3.1.4 explicitly exercise constructor injection of both dependencies through the production registry."},{"category":"unhandled-edge","causal_finding_id":"F-terminal-transition-staging","causal_section_ids":["7.2"],"check_key":"terminal-transition-result-staging","description":"SESSION_END can call SessionCoordinator.complete_agent_run, mark an active reviewer run successful after nonzero activity, and notify its parent without invoking end_agent_run. Workflow termination provides another success route. Both bypass 3.1's canonical-result guard and 7.2's three targeted enrichment paths.","finding_id":"F-terminal-path-totality","introduced_in_round":4,"location":"§§ 3.1 and 7.2","prevention":"Enumerate terminal transitions from every run-completion caller, then trace result validation, counter settlement, evidence update or expiry, and parent notification for each route.","principle":"Every evidence-bound success transition must validate delivered state and complete enrichment before notifying the parent.","root_cause":"The round-4 repair enumerated self-termination, timeout, and failure as the complete terminal set, while SESSION_END and workflow termination retain independent successful completion routes.","section_id":"7.2","severity":"blocking","suggested_fix":"Use the existing extracted terminal helper for evidence-bound SESSION_END and workflow-terminal completion, or fail and expire those runs unless delivered state already validated. Add the session coordinator, SESSION_END handler, and workflow completion owner to the target inventory and test provider exit plus workflow termination without end_agent_run; leave non-review runs unchanged."},{"category":"bad-sequencing","causal_finding_id":"F-index-token-index-race","causal_section_ids":["5.2"],"check_key":"index-token-indexed-state-binding","description":"Section 5.2 now defines token capture as D0 → index → timestamp → D1, while 6.1 says the coordinator runs an index and then captures the token. It also says to rerun affected lanes on mismatch, but the mismatched token is immutable inside prior_round_context, so the same evidence cannot become valid by rerunning a lane.","finding_id":"F-index-token-recovery-transition","introduced_in_round":4,"location":"§§ 5.2, 6.1, and 8.1","prevention":"Draw index, digest, timestamp, inventory, evidence persistence, spawn, lane checks, mismatch, expiry, and retry as one state machine with exactly one index operation per prepared token.","principle":"An immutable consistency-token mismatch requires fresh evidence; repeating a consumer against the same token cannot repair the state.","root_cause":"The round-4 repair moved bracketed index ownership into 5.2 but left 6.1 specifying a separate coordinator index followed by token capture and lane-only retry on mismatch.","section_id":"6.1","severity":"blocking","suggested_fix":"Make evidence preparation invoke the sole D0 → index → timestamp → D1 capture and remove later pre-spawn indexing language. On before/after mismatch, expire the evidence and prepare a fresh snapshot, inventory, and token before rerunning affected lanes; test mutation and reindex both between preparation and spawn and during a lane."}],"reviewer_session":"#9696","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"da54989e-bbd9-4a00-b50c-73f3107f0c6c","plan_hash":"659f755e141912c2da039e285f806fdc76586207e469db962cf5ae6a096c76d5","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"04b3d4d9faea1aba4a46716743bd5e0fa1ca93f04c6612a580519fbe0a395259","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":8,"total":15},"evidence_id":"da54989e-bbd9-4a00-b50c-73f3107f0c6c","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"5a02854bed05a736b1f1894776e69501470d1bc71f0f0e010595f73447f7044f","status":"valid"},"source_digest":"c0a1e0bf0e4a6fe8a2b441d580e70ee3aeb99b61ad794fe71369dae43cee31f7","version":1},"evidence_id":"da54989e-bbd9-4a00-b50c-73f3107f0c6c","findings":[{"category":"bad-sequencing","causal_finding_id":"F-snapshot-consumer-migration","causal_section_ids":["6.2","6.5"],"check_key":"shared-target-write-order","description":"Section 6.2 now edits `plan-review/SKILL.md` and `plan-adversary-taskless.yaml` but still depends only on 5.2. It is unordered against the existing 4.3 -> 7.1 -> 6.1 -> 6.3 prompt chain, so independently scheduled leaves can overwrite or merge-conflict on the same bundled files before 6.5 and 9.3 run.","finding_id":"F-shared-prompt-write-order","fix":"Make 6.2 depend on 6.3, producing one prompt-owner chain 4.3 -> 7.1 -> 6.1 -> 6.3 -> 6.2 -> 6.5, with 9.3 remaining the final bundled-manifest owner. Re-derive the manifest and assert no shared target has unordered owners.","introduced_in_round":5,"location":"Section 6.2 dependencies and targets; shared prompt owners 4.3, 7.1, 6.1, 6.3, 6.5, and final manifest owner 9.3","participating_section_ids":["4.3","6.1","6.2","6.3","6.5","7.1","9.3"],"prevention":"After any target-list repair, compute shared-target ownership over the derived dependency DAG and reject unordered owning leaves.","principle":"Two independently schedulable deliverables must not own the same file without an explicit dependency order.","root_cause":"The round-5 snapshot-consumer repair expanded 6.2's targets but did not update its dependency edge.","section_id":"6.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-terminal-guard-production-totality","causal_section_ids":["3.1"],"check_key":"terminal-result-branch-schema-parity","description":"The plan adds `needs_requirements` and `inconclusive` but specifies only `evidence_id` plus 'typed reason data'. It never defines exact discriminators, field names, required values, or reason-code enums. Prompt producers, timeout handling, the validator, raw `send_message` storage, and terminal enrichment can therefore implement incompatible envelopes while satisfying their local criteria. Section 7.2 also consumes 3.1's delivered-state guard without a dependency path through 3.1.","finding_id":"F-terminal-branch-schema-parity","fix":"Define one exact raw JSON discriminated union in 3.1 for every verdict and terminal reason, including source drift, missing requirements, index mismatch, and timeout. Make 4.3 depend on 3.1; publish the union verbatim in both adversary producers and the shared skill; pin 6.3 to it; and add producer-to-validator parity tests for every branch.","introduced_in_round":4,"location":"Sections 3.1.2, 3.1.5, 4.3, 6.3, and 7.2","participating_section_ids":["3.1","4.3","6.3","7.2"],"prevention":"Keep the verdict union in one canonical schema and require every producer and terminal consumer to round-trip verbatim fixtures from it.","principle":"Every accepted terminal branch needs one wire contract shared by all producers and consumers.","root_cause":"The round-4 terminal-guard repair added semantic branches without specifying their raw transport schema or dependency order.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"F-requirements-source-universe","causal_section_ids":["6.5"],"check_key":"immutable-requirement-source-discovery","description":"Section 6.5 promises different authoritative sources for task and taskless modes, designated-document inclusion only, content hashes, stable compact IDs, duplicate collapse, and ordinary-reference exclusion. None of its acceptance items directly observes those representation properties, so an incomplete or unstable bundle can satisfy the listed tests.","finding_id":"F-requirements-bundle-representation-coverage","fix":"Add one helper-local 6.5 acceptance item that constructs task and taskless bundles and asserts the exact source set, stable compact IDs, lowercase content SHA-256 values, duplicate collapse, ordinary-reference exclusion, and changed-content identity. Keep 8.1.8 as the service-level persistence and live-source-independence check over the exact helper output.","introduced_in_round":4,"location":"Section 6.5 normative bundle definition and acceptance items 6.5.3-6.5.5; section 8.1.8","participating_section_ids":["2.1","6.2","6.5","8.1"],"prevention":"Translate every normative immutable-data property into one direct helper-level assertion before adding service-level persistence tests.","principle":"Every property of an authoritative immutable representation must be observable in acceptance criteria.","root_cause":"The round-4 requirements-source repair specified the bundle representation in prose but repaired only consumer and persistence tests.","section_id":"6.5","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-integration-manifest-closure","causal_section_ids":["2.1","4.1","4.3","5.2","6.5","7.2","8.1"],"check_key":"integration-owner-leaf-closure","description":"Section 8.1 claims snapshot assembly and all service-level assertions are owned by the integration leaf so upstream helpers can close without `review_evidence.py` wiring. Yet 2.4.1 still asserts the production round-N+1 snapshot payload, and 6.1.4 still asserts service-level evidence expiry plus re-preparation. The manifest therefore retains the ownership ambiguity the round-5 repair was meant to remove.","finding_id":"F-integration-owner-acceptance-closure","fix":"Keep helper-local prior-context construction and mismatch classification upstream, but move the production `snapshot_payload` assertion and evidence expire/reprepare transition to explicit 8.1 acceptance items. Update 8.1.4 to enumerate only the helper-local criteria upstream leaves retain.","introduced_in_round":5,"location":"Sections 2.4.1, 6.1.4, and 8.1.4","participating_section_ids":["2.4","6.1","8.1"],"prevention":"For every integration owner, classify each acceptance item as helper-local or production-call-site and verify its owning deliverable targets that call site.","principle":"Acceptance ownership must match the deliverable that owns the production call site.","root_cause":"The round-5 integration-closure repair moved target ownership in prose without relocating every service-level acceptance item.","section_id":"8.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-index-token-recovery-transition","causal_section_ids":["6.1"],"check_key":"index-token-indexed-state-binding","description":"The new mismatch recovery says to expire evidence, re-prepare snapshot/inventory/token, and rerun affected lanes. The current reviewer is already immutably bound to the old evidence through a unique `dispatch_run_id`, so it cannot bind to the replacement; rerunning only affected lanes can also combine old- and new-evidence lane results.","finding_id":"F-index-mismatch-run-replacement","fix":"On mismatch, the bound child must emit a typed `inconclusive` result for the old evidence and terminate. The parent must expire that evidence, prepare a fresh bundle and token, spawn and bind a new reviewer run, and rerun all three lanes under the new evidence ID.","introduced_in_round":6,"location":"Section 6.1.4 mismatch recovery; sections 3.1 and 8.1 run/evidence binding","participating_section_ids":["3.1","6.1","8.1"],"prevention":"Model recovery transitions across both evidence identity and run identity; never reuse lane results across an immutable evidence replacement.","principle":"An immutable run-to-evidence binding can be replaced only by a new bound run.","root_cause":"The round-6 recovery repair treated evidence regeneration as a lane retry inside the already bound reviewer.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"F-index-generation-proof","causal_section_ids":["6.1"],"check_key":"index-token-verifier-surface","description":"The plan defines preparation-time token production and requires each lane to verify it, but names no canonical callable verifier, exact digest universe, invocation point, or typed mismatch response. Different lanes can therefore compare different state or omit the check while local tests still pass.","finding_id":"F-index-token-verifier-surface","fix":"Add the smallest read-only verifier beside the token producer, reuse the same digest inputs, return an exact typed match/mismatch result, require every lane to invoke it immediately before analysis, and cover match, source mutation, index mutation, and replacement-run recovery through production paths.","introduced_in_round":2,"location":"Sections 5.2.5, 6.1.3-6.1.4, and 8.1","participating_section_ids":["5.2","6.1","8.1"],"prevention":"Any integrity token criterion must identify both its canonical producer and a callable consumer-side verifier with shared fixtures.","principle":"An integrity token is enforceable only when all consumers invoke the same verifier over the same state.","root_cause":"The round-2 index-generation repair specified the proof object but not the executable lane-facing verification surface.","section_id":"6.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-telemetry-authoritative-production","causal_section_ids":["7.2"],"check_key":"telemetry-authoritative-production","description":"Section 7.2 requires reviewer-owned `convergence_telemetry` in the canonical result but targets only validators, storage, and terminal consumers. The shared skill and adversary result producers are omitted. Because `send_message` persists the reviewer payload verbatim, the coordinator cannot reconstruct reviewer-owned telemetry that was never emitted.","finding_id":"F-telemetry-producer-contract","fix":"Add every result-producing prompt surface to 7.2, define exact telemetry fields, provenance, zero-value semantics, and verdict-branch availability in the canonical result contract, order those edits within the shared prompt chain, and test emission through `send_message`, delivery enrichment, and finalization.","introduced_in_round":4,"location":"Section 7.2 targets and acceptance; result producers in the shared plan-review skill and adversary workflows","participating_section_ids":["5.3","7.2","8.1"],"prevention":"For every authoritative output field, trace one production path from prompt/schema owner through transport to terminal consumer and require an end-to-end fixture.","principle":"Reviewer-owned telemetry must be produced by the reviewer contract before downstream enrichment.","root_cause":"The round-4 telemetry repair added downstream authority and storage without adding the upstream result producers.","section_id":"7.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-terminal-path-totality","causal_section_ids":["7.2"],"check_key":"terminal-completion-path-totality","description":"Round 5 says workflow termination delegates to the same evidence-aware terminal helper, but 7.2 still omits the actual workflow completion owner. `EnforcementCompletionMixin._complete_agent_workflow_run` prefers a lifecycle terminalizer and falls back to `complete_and_notify_agent_run`; neither `enforcement_completion.py` nor `run_completion.py` is in the target inventory, so the production fallback can bypass the promised evidence transition.","finding_id":"F-terminal-workflow-owner-target","fix":"Add the actual workflow completion owner and shared fallback helper to 7.2 targets. Route both primary and fallback workflow exits through the evidence-aware terminal helper, and exercise the production fallback in 7.2.7 so delivered results enrich while undelivered results fail and expire evidence.","introduced_in_round":6,"location":"Section 7.2 targets; `src/gobby/workflows/engine/enforcement_completion.py:95`; `src/gobby/agents/run_completion.py:24`","participating_section_ids":["7.2"],"prevention":"Blast-radius review for terminal behavior must enumerate every caller of the shared completion helper and test at least one real fallback path.","principle":"A terminal-path repair is total only when every production completion owner and fallback is routed through it.","root_cause":"The round-6 terminal-totality repair added session-hook targets but did not include the workflow engine owner or its shared fallback.","section_id":"7.2","severity":"blocking"}],"reviewer_session":"#9699","reviewer_session_id":"8b99574d-ce4e-40ec-9d79-2a5106c0fdef","round_number":6,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"19d71bbc-1255-4a6d-a04b-4ee3e61f55ee","plan_hash":"8130674a9154affbcc8b91f5392f4f5b4fc2ecfc9ca79cb8c179b592573da538","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"eedac50696ddfb74ce4f47cd2d87986f1b77d8a5b7b6fabf608432ca775d2ce9","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":6,"total":12},"evidence_id":"19d71bbc-1255-4a6d-a04b-4ee3e61f55ee","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"01c327b800cc2365e67cfe89d33832850f520134b9ebdf7a5db26da5d644fa26","status":"valid"},"source_digest":"af2ab151db9880e9b034c769fb5afc383b9eaf9ee0c38958acad71029ef89757","version":1},"evidence_id":"19d71bbc-1255-4a6d-a04b-4ee3e61f55ee","findings":[{"category":"bad-sequencing","causal_finding_id":"F-telemetry-producer-contract","causal_section_ids":["7.2"],"check_key":"shared-target-write-order","description":"Sections 6.5 and 7.2 are unordered while both edit the shared plan-review skill and both adversary definitions. Because 9.3 depends on 6.5 but not 7.2, it can also refresh the bundled manifest before 7.2's bundled edits land.","finding_id":"F-shared-prompt-order-round7","introduced_in_round":6,"location":"Sections 6.5, 7.2, and 9.3","prevention":"After every target-list repair, recompute unordered shared-target pairs and verify every generated-content owner is transitively downstream of all content producers.","principle":"Every shared-file owner must be dependency-ordered, and a generated bundled manifest must run after every bundled-content editor.","root_cause":"Round 6 added the shared skill and both adversary definitions to 7.2 for telemetry production but did not insert 7.2 into the complete prompt-owner chain or the transitive predecessor set of 9.3.","section_id":"7.2","severity":"blocking","suggested_fix":"Add 7.2 as a dependency of 6.5. This yields one ordered chain through 6.2 -> 7.2 -> 6.5 -> 9.3 and keeps 9.3 transitively last with a single new edge."},{"category":"traceability","causal_finding_id":"F-terminal-branch-schema-parity","causal_section_ids":["3.1"],"check_key":"terminal-result-branch-schema-parity","description":"Section 3.1 requires the terminal union to be published verbatim in the shared skill and both adversary definitions, yet its targets include none of those files. The isolated 3.1 leaf cannot satisfy 3.1.7 before downstream prompt owners execute.","finding_id":"F-terminal-union-producer-targets","introduced_in_round":6,"location":"Section 3.1 targets and acceptance 3.1.7","prevention":"Resolve every producer named by a cross-surface parity criterion to its file and verify the owning leaf can pass that criterion before any downstream leaf runs.","principle":"A deliverable that requires producer-wide wire-contract publication must own every producer surface needed to satisfy its acceptance criteria.","root_cause":"The round-6 repair added producer-parity prose and acceptance 3.1.7 but left all three prompt producers outside 3.1's target inventory.","section_id":"3.1","severity":"blocking","suggested_fix":"Add src/gobby/install/shared/skills/plan-review/SKILL.md and both plan-adversary workflow definitions to 3.1's targets. Existing 3.1 -> 4.3 ordering then places 3.1 at the head of the prompt-owner chain without another dependency edge."},{"category":"weak-testability","causal_finding_id":"F-index-mismatch-run-replacement","causal_section_ids":["6.1"],"check_key":"index-token-indexed-state-binding","description":"The plan now requires the parent to spawn a distinct reviewer and rerun all three lanes under fresh evidence, but no acceptance item observes the new run binding, all-three-lane rerun, or non-reuse of old lane results. A partial implementation can satisfy 6.1.5 and 8.1.14.","finding_id":"F-index-replacement-run-acceptance","introduced_in_round":6,"location":"Section 6.1 recovery protocol and acceptance 8.1.14","prevention":"For every evidence-replacement transition, assert old-child termination, new evidence identity, distinct run binding, complete lane rerun, and zero reuse from the old generation.","principle":"A recovery transition that replaces immutable evidence must be tested across evidence identity, run identity, and every regenerated lane result.","root_cause":"Round 6 repaired the prose state machine to require a new run, while the acceptance set retained only child termination and service-level expire/reprepare assertions.","section_id":"6.1","severity":"blocking","suggested_fix":"Extend 8.1.14 with the production transition: the old child emits typed inconclusive and terminates, old evidence expires, the parent prepares a new evidence ID, binds a distinct reviewer run, reruns all three lanes, and reuses no old result."},{"category":"traceability","causal_finding_id":"F-telemetry-producer-contract","causal_section_ids":["7.2"],"check_key":"telemetry-authoritative-production","description":"The staged adversary does not persist its result through send_message. It calls approve_review/reject_review, whose schemas expose no convergence_telemetry, and task transitions reconstruct round_result without that object. The staged path therefore drops the reviewer-owned telemetry that 7.2 requires.","finding_id":"F-staged-telemetry-transport","introduced_in_round":6,"location":"Section 7.2 staged producer path","prevention":"Trace each authoritative result field from every producer's real emission tool through schema, forwarding, round-result construction, validation, persistence, and finalization.","principle":"Every canonical result producer must carry authoritative reviewer-owned fields through its actual transport and result-construction path.","root_cause":"Round 6 added telemetry to the staged prompt contract but swept only prompt, plan-evidence wrapper, and terminal consumers; the stage-native approve/reject schemas and task transition constructors were omitted.","section_id":"7.2","severity":"blocking","suggested_fix":"Add src/gobby/mcp_proxy/tools/tasks/_stage_review.py and the task-transition result-construction path to 7.2. Carry the strict telemetry object through approve/reject into canonical round_result, extract a small sibling helper so _transitions.py stays below 1,000 lines, and add a stage-native producer-to-finalize test."},{"category":"weak-testability","check_key":"snapshot-paging-direct-consumer-migration","description":"tests/mcp_proxy/test_plans_tools.py directly calls get_plan_review_snapshot with only evidence_id and asserts the whole snapshot is returned inline. Section 6.2 changes that contract to paging but neither targets this file nor names its assertion in acceptance, so focused acceptance can pass while the existing integration test fails.","finding_id":"F-snapshot-wrapper-test-migration","location":"Section 6.2 and tests/mcp_proxy/test_plans_tools.py","participating_section_ids":["6.2"],"prevention":"For every signature or response-shape change, sweep direct symbol usages including tests and list each fixture that asserts the old contract in the owning acceptance item.","principle":"A transport migration must update every direct consumer and every test that pins the retired response shape.","root_cause":"The plan names wrapper-test migration generically and pins a schema test, while the repository sweep omitted the direct integration test that asserts whole-snapshot output.","section_id":"6.2","severity":"blocking","suggested_fix":"Add tests/mcp_proxy/test_plans_tools.py to 6.2's migration scope and extend 6.2.5 to page through next_offset, check per-page size, and verify the reconstructed hash and section union in that direct integration seam."},{"category":"unhandled-edge","check_key":"ledger-canonical-identity-order","description":"Two producers can emit the same participating section set in different orders. The proposed canonical key can then mint two ledger entries for one logical finding, defeating cross-round coalescing and repeated-check convergence accounting.","finding_id":"F-ledger-section-order-identity","location":"Sections 4.1 and 4.2 ledger identity","participating_section_ids":["4.1","4.2"],"prevention":"Exercise canonical keys with reordered and duplicate-equivalent participant inputs and require one stable identity for the same logical section set.","principle":"Canonical identity over set-valued participating sections must be invariant under producer ordering.","root_cause":"The planned key embeds source_section_ids and their hashes without a normalization rule, while the current section-set validator validates uniqueness but preserves caller order.","section_id":"4.1","severity":"blocking","suggested_fix":"Canonicalize unique section IDs to one stable order and pair each ID with its section hash before constructing the key. Add a permutation test proving reordered inputs coalesce to the same ledger_entry_id."}],"reviewer_session":"#9704","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"8164fbea-0977-4335-b381-c64723aeb949","plan_hash":"d34edd261b96fec8ccc775eb073016906de3e3041a3a8362337d9e14b378b431","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2e8108973d449c0ada8c4ef39c1e7e7f64649ca71914ba855987cdac0093908a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":4,"total":12},"evidence_id":"8164fbea-0977-4335-b381-c64723aeb949","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"3c9cf4fded37f9d15c9cceb5f22e3ad4eab933d5d5cb7dac7b210b8fe315206d","status":"valid"},"source_digest":"663ad4da43914ad48a1e8d37c457c351bffa6272eaa4c60df6ab095a95703d16","version":1},"evidence_id":"8164fbea-0977-4335-b381-c64723aeb949","findings":[{"category":"traceability","causal_finding_id":"F-staged-telemetry-transport","causal_section_ids":["7.2"],"check_key":"telemetry-authoritative-production","description":"`approve_review` and `reject_review` forward through `src/gobby/storage/tasks/_transitions_facade.py`, and both canonical round results are built in `src/gobby/storage/tasks/_transitions.py`. Neither file nor a concrete sibling builder is a 7.2 target, so editing `_stage_review.py` cannot carry `convergence_telemetry` into the persisted result.","finding_id":"F-staged-telemetry-transition-ownership-round8","introduced_in_round":7,"location":"Section 7.2 targets, staged transport prose, and acceptance 7.2.9","prevention":"For every staged result-field addition, trace approve and reject from MCP schema through facade signature, transition signature, canonical result construction, finalization, and replay tests.","principle":"Every canonical result field must traverse every fixed-signature producer, forwarding facade, constructor, validator, and persistence boundary.","root_cause":"The round-7 repair added the staged MCP wrapper and an acceptance assertion but did not add the LocalTaskManager forwarding facade, the two storage transition constructors, or a concrete sibling result builder to section 7.2 ownership.","section_id":"7.2","severity":"blocking","suggested_fix":"Add `_transitions_facade.py` and `_transitions.py` to 7.2 for signature forwarding and delegation, name the concrete sibling result-builder path, and make 7.2.9 enter through LocalTaskManager and verify both approve and reject through finalization."},{"category":"bad-sequencing","check_key":"integration-owner-leaf-closure","description":"Acceptance 6.3.2 and 6.3.4 require timeout classification, terminal-result retention, and direct-parent wake behavior implemented through `agent_health.py` and `agent_cleanup.py`. Section 6.3 targets neither file, and downstream 7.2 owns both, so 6.3 cannot close independently.","finding_id":"F-timeout-terminal-leaf-ownership-round8","location":"Acceptance 6.3.2 and 6.3.4 versus section 7.2 terminal-path ownership","participating_section_ids":["6.3","7.2"],"prevention":"Classify every acceptance item as configuration-local, helper-local, production-transition, or integration-level and place it under the leaf that owns the required production files.","principle":"Each manifest leaf must satisfy its acceptance criteria using its own targets and completed dependencies.","root_cause":"Section 6.3 owns runtime timeout transition assertions, while the production transition files are assigned only to section 7.2, which depends on 6.3.","section_id":"6.3","severity":"blocking","suggested_fix":"Move the runtime transition assertions into 7.2 with 7.2.x IDs. Keep 6.3 responsible for timeout configuration, the documented expire-and-retry policy, and the provider capability diagnostic."},{"category":"unhandled-edge","check_key":"terminal-completion-path-totality","description":"`terminalize_killed_agent_run` handles the error branch with `run_storage.fail` plus direct delivery, and `terminalize_cancelled_agent_run` has a lifecycle-less direct transition/delivery fallback. Both bypass the proposed evidence-aware helper, so bound evidence can remain live and daemon aggregates can be missing when the parent wakes.","finding_id":"F-parent-kill-terminal-bypass-round8","location":"Section 7.2 terminal-path matrix; `src/gobby/mcp_proxy/tools/agent_cancellation.py`","participating_section_ids":["7.2"],"prevention":"Enumerate every caller that mutates an AgentRun to terminal state and verify each evidence-bound branch delegates to the same settle-evidence-deliver boundary.","principle":"Every evidence-bound terminal route must settle authoritative counters and evidence state before parent notification.","root_cause":"The terminal-path inventory covered self-termination, health cleanup, SESSION_END, and workflow completion but omitted explicit parent kill and lifecycle-less cancellation.","section_id":"7.2","severity":"blocking","suggested_fix":"Add `src/gobby/mcp_proxy/tools/agent_cancellation.py` to 7.2, route both branches through the extracted terminal helper, and extend 7.2.7 with evidence-bound explicit-error-kill and lifecycle-less-cancellation cases that assert evidence expiry and notification ordering."},{"category":"traceability","check_key":"immutable-requirement-source-discovery","description":"A taskless planning session can contain multiple user turns. With only a session ID, preparation cannot select the initiating request deterministically or prove that round 2 reused the same request after transcript changes or restart; implementers must invent different live-transcript heuristics.","finding_id":"F-taskless-requirement-source-anchor-round8","location":"Sections 2.1, 6.5, and 8.1 taskless requirements lifecycle","participating_section_ids":["2.1","6.5","8.1"],"prevention":"For each taskless authoritative source, trace one stable source identity through command initiation, preparation, evidence storage, restart, later-round reuse, and missing-source failure.","principle":"An immutable requirement source needs a deterministic server-owned identity before its content can be snapshotted and reused across rounds.","root_cause":"The plan names the initiating session request as canonical in taskless mode but neither preparation inputs nor persisted session state identifies which user request initiated `/gobby plan`.","section_id":"6.5","severity":"blocking","suggested_fix":"Capture a provider-neutral server-owned request or turn anchor when taskless `/gobby plan` starts, persist its identity and content in first-round context, reuse that anchored source in later rounds, and add multi-turn plus restart coverage with fail-closed behavior when the anchor is missing."}],"reviewer_session":"#9706","round":8,"round_number":8,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"88d9aba0-18fa-45d2-9803-7349353e549a","plan_hash":"e142c5926ea4069a60e04d1c40165ca138f8b81a079e5bbf8a9d8c0895a98bd8","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"d5a1580ec1ce21644dd4edfb783b19ce30ff7fc7e910a45c914166ec2eb64ea7","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":9,"emitted_findings":3,"total":12},"evidence_id":"88d9aba0-18fa-45d2-9803-7349353e549a","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"2017cd242a6a0c4162775decf97596f5d3391222e4f46abc8f2b85049521237e","status":"valid"},"source_digest":"f3238fa40fbe3b63d2c6a8316d53689584004087779382772fb3a3f5a4d63db7","version":1},"findings":[{"category":"traceability","causal_finding_id":"F-taskless-requirement-source-anchor-round8","causal_section_ids":["6.5"],"check_key":"immutable-requirement-source-discovery","description":"An implementation can satisfy 6.5.7 with helper fixtures while production never creates the request anchor. The server-owned value must exist before first-round preparation, yet every named 6.5 target begins at bundle assembly, evidence IO, or reviewer consumption.","finding_id":"F-taskless-request-anchor-production-path-round9","introduced_in_round":9,"location":"§§ 6.5 and 8.1 taskless preparation lifecycle","prevention":"Trace every taskless source from the exact entry event through persisted state, first preparation, restart before and after preparation, later-round reuse, and missing-source failure.","principle":"An immutable pre-review requirement source needs an authoritative writer and durable handoff before preparation reads it.","root_cause":"The round-8 repair specifies the anchor's identity, content, reuse, and fail-closed behavior after capture, while section 6.5 owns neither the actual taskless plan-entry producer nor the preparation wrapper/transport. The current production coordinator reaches prepare_plan_review_round with session identity and live requirements context only.","section_id":"6.5","severity":"blocking","suggested_fix":"Use the existing persisted session/workflow state as the simplest handoff: add the production taskless plan-entry/capture owner and prepare_plan_review_round wrapper to 6.5, write the provider-neutral request identity and bytes before plan work begins, atomically copy them into prior_round_context on first preparation, and make 6.5.7 enter through that real path across restart. Do not add a new anchor subsystem."},{"category":"bad-sequencing","causal_finding_id":"F-terminal-transition-staging","causal_section_ids":["7.2"],"check_key":"terminal-transition-result-staging","description":"Mailbox delivery can succeed while result persistence fails, leaving end_agent_run unable to observe delivered state. A duplicate or late send racing terminal cleanup can also overwrite an enriched terminal result with the raw reviewer payload after the helper settled daemon aggregates.","finding_id":"F-round-result-delivery-monotonicity","introduced_in_round":9,"location":"§§ 3.1 and 7.2 taskless result delivery","prevention":"Inventory every writer of canonical run results as well as every terminalizer; require one state machine and race each writer against delivery, enrichment, timeout, retry, and parent wake.","principle":"The delivered-to-enriched result transition must be durable and monotonic before any parent notification can release finalization.","root_cause":"The terminal-helper repair inventories terminalizers but omits the independent taskless result writer in agent_messaging.py. That path sends the mailbox message first, then performs a best-effort unconditional raw UPDATE of agent_runs.result, swallowing persistence errors and ignoring run/enrichment state.","section_id":"7.2","severity":"blocking","suggested_fix":"Add src/gobby/mcp_proxy/tools/agent_messaging.py to the existing transition owner. Persist and validate delivered state for the active evidence-bound run before acknowledging parent delivery, use a compare-and-set/state predicate that cannot overwrite enriched or terminal state, and test mailbox-success/write-failure, duplicate retry, and a late send racing enrichment. Reuse the extracted helper/state model; add no second delivery subsystem."},{"category":"weak-testability","check_key":"approval-ledger-production-delivery","description":"All current acceptance items can pass while approval never shows carried major/minor and dismissed ledger entries to the user. Persistence for a future round does not satisfy the stated approval-time surfacing requirement.","finding_id":"F-approval-ledger-delivery-path","location":"§§ 4.1, 4.3, and 8.1 approval output","participating_section_ids":["4.1","4.3","8.1"],"prevention":"For every value described as surfaced or returned, trace server derivation through the canonical result, validator, transport, coordinator display, and an end-to-end assertion containing non-empty data.","principle":"A user-visible approval artifact needs an authoritative producer, validated transport, coordinator consumer, and production-path acceptance test.","root_cause":"Section 4.3 promises the full quality ledger beside the manifest, but 4.3.2 checks documentation and 8.1.9 checks persistence plus next-round reading only. The coordinator approval contract currently enumerates routing_decisions and manifest_entries without a ledger consumer.","section_id":"4.3","severity":"blocking","suggested_fix":"Reuse the existing approved round-result envelope: include the server-derived merged ledger beside the canonical manifest, validate it, target the existing plan coordinator display path (and staged transport if that surface is included), and add one 8.1 production-path acceptance proving non-empty carried entries survive finalization and are shown with the manifest. Add no new API or dashboard."}],"reviewer_session":"#9708","round":9,"round_number":9,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"2227b443-a11f-46e2-9e21-dbf7c9536cce","plan_hash":"eeee853d093f1b0b95742b8861402a142789f3707b1ac6035dbb0fc6da45d847","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"96e262c1262a870933e13fda5de1b5515326018e60276510777d82189bd123be","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":3,"total":9},"evidence_id":"2227b443-a11f-46e2-9e21-dbf7c9536cce","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"fbc2a477360902f56b89fbb723aa2008a3c485553e31252060b9fbf39e5de4b5","status":"valid"},"source_digest":"3f4463be2d4e7da25d5a3e93944220125683266d0fb08eec2784fa12ab6eaaf8","version":1},"findings":[{"category":"traceability","check_key":"snapshot-paging-direct-consumer-migration","description":"The staged dispatcher still decodes and embeds the whole plan snapshot through `dispatch/spawn.py` and `dispatch/prompts.py`, while 6.2 requires the staged adversary to reconstruct paged evidence and 6.5 requires the immutable context bundle. Those production files and their inline-snapshot test are absent from the targets, so implementation can leave two conflicting transports and strand the staged reviewer without `prior_round_context`.","finding_id":"F-stage-native-snapshot-transport-round10","location":"§§ 6.2 and 6.5 stage-native snapshot delivery","participating_section_ids":["6.2","6.5"],"prevention":"For every transport change, sweep tool callers plus prompt embedders, dispatch constructors, fixtures, and tests that consume the same payload without calling the changed tool.","principle":"A transport migration must update every production consumer and remove any contradictory legacy transport.","root_cause":"The plan migrates both adversary definitions to paged evidence reads while omitting the stage-native dispatch path that still injects raw snapshot bytes and a test that requires that legacy shape.","section_id":"6.2","severity":"blocking","suggested_fix":"Use the existing evidence-handle paging protocol for the staged path too: add `src/gobby/dispatch/spawn.py`, `src/gobby/dispatch/prompts.py`, and `tests/storage/test_stage_review_findings.py` to 6.2/6.5 ownership, replace raw snapshot injection with the evidence handle, and test page reconstruction, hash verification, and requirements-context visibility."},{"category":"traceability","causal_finding_id":"F-taskless-request-anchor-production-path-round9","causal_section_ids":["6.5"],"check_key":"immutable-requirement-source-discovery","description":"Editing the plan skill can ask the model to copy prompt text, and the preparation wrapper runs after plan work has begun. Neither surface can create the provider-neutral, server-owned request anchor promised by 6.5, so 6.5.7 can still pass through a model-supplied or fixture-owned value.","finding_id":"F-taskless-anchor-authoritative-producer-round10","introduced_in_round":10,"location":"§ 6.5 taskless request-anchor production path","prevention":"Trace each authoritative request source from the receiving event through pre-work persistence, caller-independent preparation, restart, and later-round reuse.","principle":"A server-owned immutable source must be captured at the event that possesses its authoritative identity and exact bytes.","root_cause":"The round-9 repair added model instructions and the later preparation wrapper, while the actual plan-mode entry rule only loads the skill and the skill/tool contexts expose no initiating request identity or exact request bytes.","section_id":"6.5","severity":"blocking","suggested_fix":"Reuse the existing plan-mode entry path: target `src/gobby/install/shared/workflows/rules/plan-mode/handle-plan-mode-entry.yaml` and the smallest existing turn-start event owner needed to expose the request identity and UTF-8 bytes, persist them in existing session state before `load_skill`, and keep the preparation wrapper as the atomic read/copy boundary. Add no anchor subsystem."},{"category":"weak-testability","causal_finding_id":"F-approval-ledger-delivery-path","causal_section_ids":["4.3","8.1"],"check_key":"approval-ledger-production-delivery","description":"The coordinator plan skill still enumerates findings, routing decisions, and manifest entries without the ledger; staged `approve_review`, its relay, and the task transition result builder also omit it. Acceptance 8.1.15 can therefore pass at the service-finalize seam while the durable approval intent or either coordinator display still drops the carried major/minor and dismissed entries.","finding_id":"F-approval-ledger-production-consumer-round10","introduced_in_round":10,"location":"§§ 4.3 and 8.1 approval ledger delivery","prevention":"Trace every surfaced value through server derivation, pre-finalization intent, checkpoint rendering, finalization, taskless display, staged transport, and a production assertion containing non-empty data.","principle":"A user-visible approval artifact must exist in durable approval intent and traverse every taskless and staged coordinator consumer.","root_cause":"The round-9 repair added envelope and service-level prose while leaving the taskless coordinator contract, staged approval schema/relay, and staged canonical result builder outside 4.3/8.1 ownership; the merged ledger can also be derived only after the supplied approval result is frozen unless apply-time materialization is explicit.","section_id":"4.3","severity":"blocking","suggested_fix":"Add the existing taskless plan skill and staged approval/result-construction owners to the ordered 4.3/8.1 work, materialize the server-derived ledger before manifest apply persists approval intent, and extend 8.1.15 through apply, V1 rendering, finalization, and both existing coordinator displays with the same non-empty ledger. Keep the existing envelope; add no API or dashboard."}],"reviewer_session":"#9710","round":10,"round_number":10,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"63f26797-bdc6-4f06-9958-d32c9d5a6872","plan_hash":"96f35bd85a9ef8b3bf13e4c6ad7c0cf67d92e542108f18c30e5c2aea3eb6ecd8","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2508ec92e231b4086e7cc22ff0c383a3438e963e0f3e9c7a1c65a48a99e544dc","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":6,"total":8},"evidence_id":"63f26797-bdc6-4f06-9958-d32c9d5a6872","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"7f4c2c2332b8e2dc294decab9f696c2fd3c551d86ee701888a7c21181a87d941","status":"valid"},"source_digest":"fa2ab3705d3933566dade9fc4d6835aad0210506032276a8c811d7dae304a29e","version":1},"findings":[{"category":"traceability","causal_finding_id":"F-approval-ledger-production-consumer-round10","causal_section_ids":["4.3","8.1"],"check_key":"approval-ledger-production-delivery","description":"The round-10 repair still stops at the outer staged wrapper. LocalTaskManager's fixed facade and storage transition/result-builder signatures accept no ledger, and the storage transition constructs and persists the canonical round_result before stage approval without one. Section 7.2 owns those files only for telemetry. Consequently 4.3.6 and 8.1.15 can be satisfied at wrapper/service seams while the real staged approval intent, replay value, or displayed result remains ledger-less.","finding_id":"F-approval-ledger-canonical-path-round11","fix":"Extend §4.3 ownership through _transitions_facade.py and _transitions.py or §7.2's concrete sibling result builder. Materialize the server-derived ledger before manifest intent and _stage_states.approve_review, pass one enriched canonical result through apply, replay, finalization, and response, and test via LocalTaskManager that identical non-empty entries reach durable intent, V1, finalized evidence, and both displays; derivation failure must leave stage state unchanged.","introduced_in_round":11,"location":"§4.3 and §8.1; src/gobby/storage/tasks/_transitions_facade.py:296-327 and src/gobby/storage/tasks/_transitions.py:433-619","participating_section_ids":["4.3","7.2","8.1"],"prevention":"For every new canonical approval field, maintain a producer-to-consumer propagation check across producer, MCP schema, facade, result constructor, durable intent, replay, finalization, and display, exercised through the production entry point.","principle":"A server-derived approval field is complete only when the same canonical value crosses every fixed-signature boundary before durable approval state changes.","root_cause":"The repair named the cited wrapper and integration test but omitted the two downstream fixed-signature consumers that own staged result construction and approval ordering.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-stage-native-snapshot-transport-round10","causal_section_ids":["6.2","6.5"],"check_key":"snapshot-transport-sidecar-completeness","description":"The paging repair defines offsets, union completeness, and a reconstruction hash only for plan sections, while §6.5 permits arbitrary repository requirement documents and says their full immutable content rides prior_round_context. Those sidecar records have no byte/record paging, per-source completeness, or response-size contract. A requirement source larger than the 15,000-character offload threshold can therefore recreate the exact offload/truncation failure even though every plan-section page passes 6.2.1-6.2.4.","finding_id":"F-requirements-bundle-paging-round11","fix":"Make the cursor enumerate a deterministic typed stream of plan-section and sidecar records, or add source-ID/offset paging for every requirement and prior-context record. Return total record/byte counts, per-source hashes, next offsets, and a bundle digest. Add staged and taskless integration fixtures whose requirement source and prior ledger/inventory each exceed the offload threshold, asserting every response remains bounded and reconstruction matches all hashes before review begins.","introduced_in_round":11,"location":"§6.2 and §6.5; src/gobby/plans/review_evidence.py:185-199, src/gobby/mcp_proxy/tools/plans/review_evidence.py:66-81, src/gobby/config/features.py:125-148","participating_section_ids":["6.2","6.5","8.1"],"prevention":"Whenever a bounded transport gains sidecar payloads, require the size bound and completeness proof to quantify every record class in the envelope, not only the original primary payload.","principle":"No-live-source traceability requires every immutable requirement byte to be both bounded in transport and covered by a deterministic completeness proof.","root_cause":"The repair generalized the plan transport by cited section pages but treated the newly attached requirements bundle as an unbounded field rather than part of the paged record universe.","section_id":"6.2","severity":"blocking"},{"category":"traceability","check_key":"repair-attestation-production-path","description":"The plan makes resolution records and repair_attestations mandatory inputs to round-N+1 preparation, but supplies no authoritative production producer. The taskless coordinator presents votes, revises, and loops directly to prepare; the staged planner can resubmit review_notes only, and automatic dispatch prepares evidence from task/stage identity without the new records. Service and wrapper tests can pass with injected fixtures while every real second round after a finding is refused.","finding_id":"F-repair-attestation-production-round11","fix":"Add explicit resolution/attestation construction to the taskless plan coordinator from each recorded vote and completed repair. Define one durable staged handoff from planner resubmission through submit_for_review and dispatch into preparation. Both producers must include the actual changed-section diff, changed symbols, consumer and adjacent sweeps, validation, deferred sites, and deviations. Test real round-1 rejection, revision, and successful round-2 preparation/spawn in both modes, plus missing/incomplete refusal.","location":"§2.2, §2.5, and §2.6; src/gobby/install/shared/skills/plan/SKILL.md:79-121, src/gobby/install/shared/workflows/agents/planner.yaml:94-104,181-203, src/gobby/dispatch/spawn.py:74-126","participating_section_ids":["2.2","2.5","2.6"],"prevention":"For every newly required service input, trace an authoritative producer and durable transport on every production caller before accepting injected-unit-fixture coverage.","principle":"A preparation gate is deployable only if every production coordinator can construct and transport its required evidence from authoritative user decisions and actual repairs.","root_cause":"The plan specified validator, persistence, and wrapper schemas but omitted the taskless and staged producers that must create the records.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"approval-ledger-disposition-durability","description":"The declared finalization-time ledger merge requires every current dismissed candidate disposition, but coverage validation canonicalizes those records and returns only counts, booleans, and digests. The canonical round_result carries findings, not full dispositions, and finalization persists only that result. After the coverage call or a restart—especially on needs_review—there is no durable input from which §4.1 can construct dismissed ledger entries.","finding_id":"F-ledger-disposition-durable-handoff-round11","fix":"Create one canonical durable handoff for validated dispositions before finalization: either persist ledger-ready records on the bound evidence row during coverage validation or carry and revalidate them in every verdict branch's round_result. Then make finalization merge only that durable form. Add rejection-plus-restart and approval tests proving identical non-empty dismissed entries reach quality_ledger and the next round without process-local cache or injected helper inputs.","location":"§4.1, §4.2, §4.3, §5.1, and §8.1; src/gobby/plans/review_coverage.py:61-116,382-454 and src/gobby/plans/review_evidence.py:262-294,533-574","participating_section_ids":["4.1","4.2","4.3","5.1","8.1"],"prevention":"For every finalization-time aggregation, enumerate the durable source of each input field and include a restart-between-production-and-consumption test.","principle":"A durable merge cannot depend on rich input that an earlier validation boundary irreversibly reduces to counts.","root_cause":"The plan defined the ledger merge and its final write transition but not the handoff that preserves current-round disposition records until that transition.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-taskless-anchor-authoritative-producer-round10","causal_section_ids":["6.5"],"check_key":"taskless-anchor-entry-branch-coverage","description":"The new producer file is correct in kind but the cited implementation seam is not universal. Web-chat, structured-mode, Codex collaboration-mode, and provider-native branches set plan_mode and return before observer_plan_mode.py's only data.get(\"prompt\") access. A repair at the cited tail can pass one fallback production fixture while common entry branches load the plan skill with no server-owned anchor and then fail preparation closed.","finding_id":"F-request-anchor-branch-coverage-round11","fix":"Read the event-owned prompt/identity before branch-specific returns and persist the anchor exactly once on the false-to-true plan_mode transition. Parameterize the production-path acceptance across web_chat, structured mode, Codex collaboration mode, provider-native mode, and prompt fallback; assert later turns and restart reuse never overwrite the initial bytes.","introduced_in_round":11,"location":"§6.5; src/gobby/workflows/observer_plan_mode.py:58-131,169-189 and src/gobby/workflows/hooks.py:517-612","participating_section_ids":["6.5"],"prevention":"When an observer has multiple authoritative success exits, acceptance must enumerate every exit and verify shared pre-transition side effects before any return.","principle":"The request anchor must be established on every server-recognized plan-mode entry before the load-skill rule observes plan_mode.","root_cause":"The round-10 repair added the right producer file but followed the single prompt-fallback seam cited in prose, leaving earlier authoritative branches outside the repair.","section_id":"6.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"recovery-directive-action-form-coverage","description":"The proposed recovery_directive_suffix extracts a single call_tool-bearing line, but enabled rules use other actionable forms and layouts: require-memory-review-before-status directs set_variable with no call_tool, while block-gobby-tasks-cli contains a call_tool invocation split across physical lines. The implementation can therefore preserve no recovery command or an incomplete, unusable fragment while both 9.1 acceptance tests still pass on a one-line fixture.","finding_id":"F-recovery-directive-extraction-round11","fix":"Define extraction around a complete actionable command or paragraph, not the literal call_tool token on one line. Preserve the existing collapse mechanism and add fixtures for folded one-line call_tool, multiline call_tool, set_variable, skill-load or equivalent actionable directives, and truly non-actionable reasons.","location":"§9.1; src/gobby/workflows/engine/evaluation.py:173-202, src/gobby/workflows/engine/blocked_tool_recovery.py:41-43,109-165, and enabled memory/task enforcement rule reasons","participating_section_ids":["9.1"],"prevention":"Class-wide tests for rendered recovery instructions must inventory enabled action syntaxes and line layouts rather than pinning one cited rule instance.","principle":"A collapsed block reason remains actionable only if it preserves the complete recovery directive independent of tool spelling and formatting.","root_cause":"The repair mechanism was specialized to the observed memory-recall line rather than the repository's full class of enabled recovery directives.","section_id":"9.1","severity":"blocking"}],"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"f5aa61dd-b3e3-4869-b31d-f0a3835057b4","plan_hash":"37159aff15a472bb47f5284f61217d2145cfb61c457d0950e3718cc42ba14939","round_number":12,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"48fc7e8e24c69c27b41eac6d6a0d2f29b04b0de1ec6b1ef93fe07020b7f9ca5a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":10,"total":17},"evidence_id":"f5aa61dd-b3e3-4869-b31d-f0a3835057b4","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"66f3d1eefcf30fced9042049f5f5c0c9fdcf4d74ba804eb9402ea180de4515d9","status":"valid"},"source_digest":"3eae4070a7f3a134a8a10c14100c6eb54531da43d2e5101027f04179be29917e","version":1},"findings":[{"category":"traceability","causal_finding_id":"F-repair-attestation-production-round11","causal_section_ids":["2.2"],"check_key":"repair-attestation-production-path","description":"Section 2.2 still cannot supply staged round-two resolution records or repair attestations. submit_for_review carries only review_notes, stage state stores notes, and dispatch prepares from task/stage identity; the section targets none of those surfaces. Acceptance 2.2.8 can only pass through injected or unowned changes, and a restart before dispatch loses the records.","finding_id":"F-R12-STAGED-REPAIR-TRANSPORT","introduced_in_round":12,"location":"§2.2 staged repair-record production and transport","prevention":"For each new required service input, enumerate producer, fixed-signature forwarders, durable handoff, restart reader, consumer, and production-path test.","principle":"Every required preparation input needs an authoritative producer and durable transport through every production caller.","root_cause":"The round-11 repair added taskless instructions and a preparation wrapper but left the staged submit/facade/stage-state/dispatcher chain outside section ownership and without a typed payload.","section_id":"2.2","severity":"blocking","suggested_fix":"Within §2.2, add the submit_for_review wrapper, LocalTaskManager facade, storage transition/stage-state owner, and dispatcher to the targets. Persist one typed round-bound submission payload in existing stage state, load and consume it idempotently at preparation, and move the end-to-end service criterion to §8.1."},{"category":"traceability","causal_finding_id":"F-ledger-disposition-durable-handoff-round11","causal_section_ids":["4.1"],"check_key":"approval-ledger-disposition-durability","description":"Section 4.1 targets only review_ledger.py, yet 4.1.4 requires coverage validation to persist records onto an evidence row and survive restart through finalization. Current coverage validation is pure and the service returns its attestation directly. Because §8.1 depends on §4.3, which depends on §4.1, the §4.1 leaf cannot close its own production acceptance.","finding_id":"F-R12-DISPOSITION-PRODUCTION-OWNER","introduced_in_round":12,"location":"§4.1.4 and §8.1 integration ownership","prevention":"Classify every acceptance as helper-local or service-level; place production persistence/restart criteria under the integration owner and leave injected-input criteria upstream.","principle":"A production acceptance must live under a leaf that owns every call site needed to satisfy it.","root_cause":"The repair placed restart-spanning coverage persistence in helper-only §4.1 even though §8.1 is the sole review_evidence.py integration owner and runs after §4.1.","section_id":"4.1","severity":"blocking","suggested_fix":"Keep canonicalization and merge tests in §4.1. Return ledger-ready records from the helper, move the coverage write/restart acceptance to a new §8.1 item, and let §8.1 own the service/store call-site wiring."},{"category":"unhandled-edge","causal_finding_id":"F-ledger-disposition-durable-handoff-round11","causal_section_ids":["4.1"],"check_key":"approval-ledger-disposition-attestation-binding","description":"Two valid coverage calls can contain different dismissed identities/rationales while producing identical counts and the same attestation digest. If coverage validation persists the full records separately, a retry can replace the set that finalization merges without changing the canonical round result.","finding_id":"F-R12-DISPOSITION-DIGEST-BINDING","introduced_in_round":12,"location":"§§4.1, 5.1, and 8.1 coverage-to-finalization handoff","prevention":"For each separately persisted validation product, bind its canonical digest into the attestation and test identical retry, conflicting retry, restart, and finalization.","principle":"Durable sidecar evidence must be cryptographically bound to the canonical result that authorizes its later consumption.","root_cause":"Coverage attestation hashes only disposition counts; the proposed full records live separately on the evidence row with no digest or compare-and-set relation to that attestation.","section_id":"4.1","severity":"blocking","suggested_fix":"Canonicalize the complete ledger-ready disposition list, include its SHA-256 digest in the coverage attestation, persist {digest, records} with single-assignment/compare-and-set semantics, and verify that digest in the same transaction that finalization merges the records."},{"category":"unhandled-edge","causal_finding_id":"F-requirements-bundle-paging-round11","causal_section_ids":["6.2"],"check_key":"snapshot-transport-sidecar-completeness","description":"The record cursor still fails on one oversized requirement source. Emitting that record violates the offload threshold; splitting it has no specified fragment identity, offset, length, encoding, or completeness proof. The proposed test can pass with several small records whose aggregate exceeds the threshold.","finding_id":"F-R12-SNAPSHOT-RECORD-CHUNKING","introduced_in_round":12,"location":"§6.2.7 whole-envelope paging","prevention":"Test the largest single logical record, every record class, exact byte bounds, gaps, overlaps, duplicate fragments, encoding, and digest reconstruction.","principle":"A bounded transport must bound serialized bytes even when one logical record exceeds the page limit.","root_cause":"The repair pages a stream of whole records with per-record hashes, while one requirement-source record may contain an arbitrarily large document and no fragment protocol exists.","section_id":"6.2","severity":"blocking","suggested_fix":"Keep one cursor and add canonical UTF-8 fragments carrying record type/ID/hash, total bytes, fragment offset/length, next cursor, and bundle digest. Bound the serialized response after fixed overhead and test a single source larger than both page size and offload threshold."},{"category":"unhandled-edge","causal_finding_id":"F-request-anchor-branch-coverage-round11","causal_section_ids":["6.5"],"check_key":"taskless-anchor-entry-branch-coverage","description":"Round 11 moved anchoring to _apply_resolved_mode, yet the prompt fallback bypasses that helper and directly sets plan_mode in several marker branches. Combining workflow mode and prompt fallback as one acceptance parameter hides this sixth path, so the real marker-only entry can still load the plan skill without an anchor.","finding_id":"F-R12-REQUEST-ANCHOR-FALLBACK","introduced_in_round":12,"location":"§6.5.9 prompt-marker fallback","prevention":"Inventory all writes to the gated state variable and exercise each independent mutation path from a false initial state.","principle":"The request anchor must be written at every real plan_mode false-to-true mutation site.","root_cause":"The repair treats _apply_resolved_mode as the sole mutation site, but prompt-marker fallback delegates to detect_plan_mode_from_context, which writes plan_mode directly.","section_id":"6.5","severity":"blocking","suggested_fix":"Route every plan-mode activation inside detect_plan_mode_from_context through the same anchor-aware transition helper, pass event-owned request bytes to it, and split the marker-only fixture from the workflow-mode fixture in 6.5.9."},{"category":"weak-testability","causal_finding_id":"F-recovery-directive-extraction-round11","causal_section_ids":["9.1"],"check_key":"recovery-directive-action-form-coverage","description":"A balanced-parenthesis implementation can preserve the four listed fixtures while dropping direct get_tool_schema/create_task calls, backticked gcode commands, or approve/reject/escalate alternatives. Those bundled forms still collapse to a non-actionable stub.","finding_id":"F-R12-RECOVERY-DIRECTIVE-INVENTORY","introduced_in_round":12,"location":"§9.1.3 directive-form fixtures","prevention":"Build an actionable-reason syntax inventory before defining fixtures, and fail the inventory test when a new unsupported form is added.","principle":"Class-wide recovery rendering tests must cover the repository's full actionable reason grammar.","root_cause":"The repair expands beyond one-line call_tool but still enumerates only call_tool, set_variable, and skill-load fixtures; bundled rule definitions also use direct tool calls, raw shell/code-index commands, and lifecycle alternatives.","section_id":"9.1","severity":"blocking","suggested_fix":"Expand 9.1.3 from the bundled actionable-reason inventory, covering direct MCP-tool calls, shell commands, alternative command lists, and suggest_next_task-style calls; normalize unsupported reasons to the extractor grammar or retain their complete actionable paragraph."},{"category":"unhandled-edge","check_key":"index-settle-retry-bound","description":"Continuous repository mutation can keep D0→index→timestamp→D1 and inventory rechecks running indefinitely inside preparation. Per-process timeouts do not bound the enclosing loop, and §6.3 cannot recover because no child run exists yet.","finding_id":"F-R12-INDEX-SETTLE-BOUND","location":"§§5.2 and 6.1 pre-spawn index settling","participating_section_ids":["5.2","6.1","8.1"],"prevention":"For each retry, specify attempts, monotonic deadline, backoff, rollback/cancellation behavior, typed exhaustion result, and recovery test.","principle":"Every retry loop needs a concrete attempt/deadline bound and typed exhaustion transition.","root_cause":"Section 5.2 says to rerun the settle sequence and report exhaustion but defines neither maximum attempts nor elapsed-time ceiling; the reviewer timeout begins only after preparation.","section_id":"5.2","severity":"blocking","suggested_fix":"Set max_attempts=3 with an overall monotonic deadline derived from the existing per-index timeout, bounded backoff, typed index_unstable failure, full transaction rollback, and tests for continuous mutation, cancellation, and a later successful preparation."},{"category":"missing-requirement","check_key":"staged-adversary-timeout-parity","description":"The plan claims to bound the adversary run, yet plan-adversary.yaml remains unbounded while only the taskless definition receives 2700 seconds. The stage-native reviewer can therefore hang indefinitely and has no specified evidence-expiry/stage-restoration transition.","finding_id":"F-R12-STAGED-TIMEOUT-PARITY","location":"§6.3 staged and taskless adversary definitions","participating_section_ids":["6.3","7.2"],"prevention":"Inventory every definition and dispatcher path for a bounded agent role; assert identical timeout and recovery semantics for each.","principle":"Operational bounds must cover every production implementation variant of the bounded role.","root_cause":"Section 6.3 targets only plan-adversary-taskless although the stage-native plan-adversary definition is also live and defaults to timeout zero.","section_id":"6.3","severity":"blocking","suggested_fix":"Add plan-adversary.yaml to §6.3 targets, set the same explicit timeout, and specify/test the staged recovery: retain typed timeout result, expire evidence, restore stage state consistently, wake only the direct coordinator, and dispatch fresh evidence."},{"category":"traceability","check_key":"staged-round-result-delivery","description":"After a staged approve_review or reject_review, end_agent_run still has no delivered AgentRun result to validate. The staged wrappers and transitions finalize evidence but never perform the send_message-equivalent delivered-state write required by the structural guard.","finding_id":"F-R12-STAGED-DELIVERED-STATE","location":"§§3.1 and 7.2 staged verdict delivery","participating_section_ids":["3.1","4.3","7.2"],"prevention":"Trace each canonical result through producer, AgentRun writer, evidence writer, terminal guard, enrichment, finalization, and restart on both taskless and staged paths.","principle":"Every terminal guard input needs a production writer on every result-delivery path.","root_cause":"Section 3.1 relies on send_message to write AgentRun.result, while staged reviewers use approve_review/reject_review and §7.2 carries telemetry only into the evidence round result.","section_id":"7.2","severity":"blocking","suggested_fix":"Before any staged stage-state mutation, route approve/reject through the same run/evidence-bound delivered-result validator and compare-and-set writer used by send_message. Add a real staged verdict followed by end_agent_run with no send_message call."},{"category":"bad-sequencing","check_key":"staged-telemetry-finalization-order","description":"The staged path is circular: approve/reject must finalize before end_agent_run, while finalization must wait for aggregates produced by terminal cleanup after end_agent_run. Adding a delivered writer alone leaves this sequencing deadlock intact.","finding_id":"F-R12-STAGED-ENRICHMENT-ORDER","location":"§7.2 delivered→enriched ordering on staged approval/rejection","participating_section_ids":["4.3","7.2","8.1"],"prevention":"Draw the staged verdict timeline and test crashes/retries at delivered intent, terminal counter settlement, enrichment, finalization, stage commit, and parent wake.","principle":"A consumer cannot require post-termination data at a transition that executes before termination.","root_cause":"Staged approve/reject finalizes evidence inside the verdict tool call, while §7.2 declares daemon aggregates authoritative only after the run ends and makes enrichment a prerequisite for finalization.","section_id":"7.2","severity":"blocking","suggested_fix":"Make staged approve/reject record a durable delivered intent only. Let the common terminal helper settle counters, enrich, finalize evidence, commit stage state, and wake the coordinator in one replayable ordered transition; test crashes before and after each boundary."}],"reviewer_session":"#9715","round":12,"round_number":12,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"c7af2b0d-3b09-49a8-9eb4-88df6b050a85","plan_hash":"4a759351062ec5819a9ab1031e67657efb3018527dba6bb2355fb31b2e03b2d3","round_number":13,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"3a531547f22abedbda5267293b1b613f4218c326b7275c84dd3b986e2df47769","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":2,"total":4},"evidence_id":"c7af2b0d-3b09-49a8-9eb4-88df6b050a85","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"63886d0dea916f487ab1ca91e6552f7822738526d1ec33a1ee8c240fb48a380c","status":"valid"},"source_digest":"cf5e14c9d6bcb522e883d7158ec9e07576cbc1a4eb37cddac4bfa966edffced7","version":1},"findings":[{"category":"missing-requirement","check_key":"immutable-requirement-source-authoring-producer","description":"Repository requirement documents have no production authoring path into the new bundle. The interactive plan workflow delegates document construction to plan-draft, whose Constraints contract does not emit requirement-source markers; parser and bundle tests can therefore pass while real plans never designate user-owned requirement documents.","finding_id":"F-R13-REQUIREMENT-SOURCE-AUTHORING-PRODUCER","location":"Section 6.5 requirement-source authoring path","participating_section_ids":["6.5"],"prevention":"For each canonical input, trace user intent through the authoring surface, serialized marker, parser, immutable storage, and consumer before closing the design.","principle":"Every authoritative input grammar needs an owned production author as well as a parser and consumer.","root_cause":"Section 6.5 defines requirement-source as the sole document-designation grammar and owns bundle parsing, while the repository's canonical plan-authoring skill has no rule that emits or preserves that marker and is absent from the deliverable targets.","section_id":"6.5","severity":"blocking","suggested_fix":"Add the existing plan-draft skill to section 6.5 ownership and require it to emit and preserve the existing requirement-source marker only when the user identifies a repository document as canonical. Reuse the current marker, parser, and bundle; add no discovery protocol or transport."},{"category":"unhandled-edge","causal_finding_id":"F-R12-SNAPSHOT-RECORD-CHUNKING","causal_section_ids":["6.2"],"check_key":"snapshot-transport-record-size-bound","description":"A single oversized plan section still cannot fit in a whole-record page. The repository's plan parser and section-manifest builder enforce structure and hashes without a byte limit, so the transport can reproduce the same unpageable-record failure that the new requirement-source bound closes.","finding_id":"F-R13-SNAPSHOT-RECORD-SIZE-BOUND","introduced_in_round":13,"location":"Section 6.2 whole-envelope paging and single-record bound","prevention":"For every bounded record stream, test the largest single record in each record class against the serialized response budget before testing aggregate paging.","principle":"Every logical record in a whole-record bounded transport must itself have an enforced serialized-size bound.","root_cause":"The round-12 repair bounds only designated requirement sources and assumes plan sections are bounded by grammar, while parse_plan and build_section_manifest accept arbitrary section byte spans.","section_id":"6.2","severity":"blocking","suggested_fix":"Delete the unsupported bounded-by-grammar/construction assertion and apply the same existing preparation-time serialized-size rejection to every record class, including plan sections. Add an oversized-plan-section fixture. Keep the one-record transport; add no fragments, digest, or state machine."}],"reviewer_session":"#9718","round":13,"round_number":13,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"88e205ff-60a9-458c-bb9a-333b4c3e6418","plan_hash":"290dd177303e61bed2c5ca1eec7e4396b3549321bd6106d1aa469a747bf34e7b","round_number":14,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2ddb15a15f6f53324050463c27593088f449280d3c0624022609c0309fdc66fe","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":3,"total":8},"evidence_id":"88e205ff-60a9-458c-bb9a-333b4c3e6418","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"725d37f8b0e0df79fe6a533343d855fdfb35f71cc8044df0b6cb5faef9ea2ae8","status":"valid"},"source_digest":"4173dfa3e1cdc5a66fed19ebea51ce2c4618e4262b7eb7b4a7d479513ea91092","version":1},"evidence_id":"88e205ff-60a9-458c-bb9a-333b4c3e6418","findings":[{"category":"traceability","check_key":"staged-timeout-restoration-ownership","description":"An implementer receiving only section 7.2 can satisfy every listed runtime acceptance criterion without restoring the timed-out stage. That leaves the required staged retry path without an implementation owner even though section 6.3 promises the behavior.","finding_id":"F-R14-STAGED-TIMEOUT-RESTORATION-OWNER","location":"Sections 6.3 and 7.2, staged timeout recovery and terminal integration","participating_section_ids":["6.3","7.2"],"prevention":"For every terminal variant, trace result classification through evidence expiry, stage reset, fresh preparation and dispatch, and direct-parent wake under the runtime owner.","principle":"Every required recovery-state mutation must live in the self-contained runtime owner and be observed by that owner's acceptance criteria.","root_cause":"Section 6.3 promises that a staged timeout restores stage state but owns definition and configuration work; section 7.2 owns the terminal runtime paths yet omits restoration from both its body and acceptance item 7.2.11.","section_id":"7.2","severity":"blocking","suggested_fix":"Extend the existing section 7.2 body and 7.2.11 to require stage restoration, expiry of the old evidence, preparation and dispatch of fresh evidence, and direct coordinator wake. Reuse the existing terminal helper and its tests; add no new state machine."},{"category":"over-engineering","check_key":"shadow-derivation-token-proportionality","description":"The proposed token adds a second identity mechanism around a derivation already keyed by evidence and reviewed routing decisions. No downstream requirement needs a separate token, and ordinary cache-miss rederivation already covers the only stated behavior.","finding_id":"F-R14-DERIVATION-TOKEN-NO-CONSUMER","location":"Section 6.2 manifest derivation cache and coverage-validation input","participating_section_ids":["5.3","6.2","8.1"],"prevention":"Before adding a protocol field, identify its producer, consumer, failure lifecycle, and capability beyond the existing hashed and persisted structures; delete it when those structures already suffice.","principle":"A cross-call token needs a concrete consumer and capability not already supplied by an existing validated key.","root_cause":"The evidence_id plus reviewed routing decisions already identify the canonical server-side manifest derivation and cache entry, so the optional derivation token and acceptance item 6.2.3 duplicate the existing validated carrier without an issuer, lifecycle, or distinct consumer.","section_id":"6.2","severity":"blocking","suggested_fix":"Delete the derivation-token option and acceptance item 6.2.3. Continue accepting the reviewed routing decisions and use the existing evidence-plus-routing cache key, with a cache miss rederiving the canonical manifest."},{"category":"traceability","check_key":"approval-ledger-server-authority","description":"The approval ledger has conflicting authorities: review_ledger is supposed to derive it from persisted history and the delivered round_result, while the staged facade accepts it from its caller. The plan therefore does not guarantee that the persisted and displayed ledger is the server-derived canonical value.","finding_id":"F-R14-SERVER-DERIVED-LEDGER-ECHO","location":"Sections 4.1, 4.3, 7.2, and 8.1, terminal review transition and approval-ledger propagation","participating_section_ids":["4.1","4.3","7.2","8.1"],"prevention":"Trace each canonical value from its authoritative producer through merge, persistence, canonical result, and display, and remove caller-supplied copies of server-owned state.","principle":"A server-derived canonical field must be produced at the authoritative transition, not accepted as caller authority.","root_cause":"The plan calls the approval ledger server-derived, but section 4.3 exposes it as an input to staged approve and reject and tests only forwarding through LocalTaskManager. A trusted input permits substitution, omission, or races; a rederived input is a redundant echo.","section_id":"4.3","severity":"blocking","suggested_fix":"Remove the public approval-ledger input from staged approve and reject and from the facade. In the existing terminal or finalization transition, derive it once with review_ledger from the persisted prior ledger and canonical delivered round_result before stage mutation, then persist and surface that same value. Reuse the existing helper and result carrier; add no new store or protocol."}],"plan_hash":"290dd177303e61bed2c5ca1eec7e4396b3549321bd6106d1aa469a747bf34e7b","reviewer_session":"#9719","round":14,"round_number":14,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"80f2a4df-4550-4800-b8d0-189bcb9f3583","plan_hash":"36b5e6feb884aee868d9067df4f445f112703414ec23ed28c14d3aa386510005","round_number":15,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"5ef8c8fc984026dabaefe25fde79bb7715947662d97a9353e525077a36d3a561","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":10,"emitted_findings":8,"total":18},"evidence_id":"80f2a4df-4550-4800-b8d0-189bcb9f3583","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":25,"manifest_digest":"d0b377fcc5ab3deec4f4e35c0e1262f36675d943bfe6e992b8bffdfa467f27f2","status":"valid"},"source_digest":"4e9eaad8197ce86452a7c6c7f4febd09196519b12a5ebbfe4e32ef6b2a4dd037","version":1},"findings":[{"category":"missing-requirement","check_key":"immutable-requirement-authoritative-cutoff","description":"The repository Task model makes title, description, and validation_criteria distinct authoritative fields, while the planning skill explicitly confirms decisions during planning. An implementer following 6.5 literally can construct a hash-valid immutable bundle that omits mandatory acceptance criteria or a later confirmed user requirement, allowing the traceability lane to approve a plan that dropped them.","finding_id":"F-R15-IMMUTABLE-REQUIREMENTS-CUTOFF","location":"Sections 6.5 and 8.1; src/gobby/storage/tasks/_models.py Task fields and src/gobby/install/shared/skills/plan/SKILL.md Decision Record workflow.","participating_section_ids":["6.5","8.1"],"prevention":"Trace each authoritative requirement producer through a deterministic cutoff into the existing hashed bundle, its restart path, and every lane consumer; cover a task whose acceptance exists only in validation criteria and a taskless plan whose requirement is confirmed after entry.","principle":"Immutable traceability evidence must contain every server-owned requirement source fixed at a deterministic approval boundary.","root_cause":"Section 6.5 defines the exact requirements universe as only the task description or the initial taskless request. That excludes task title and mandatory validation criteria, and it has no deterministic way to include requirements confirmed during taskless planning before review begins.","section_id":"6.5","severity":"blocking","suggested_fix":"Extend the existing requirements bundle instead of creating another store: for task-bound reviews hash title, description, and validation_criteria; for taskless reviews reuse the existing entry anchor as the lower bound and freeze the ordered server-owned user requirement turns at the first adversarial-review preparation/approval cutoff. Persist that bundle in the existing prior-round context and page it through 6.2; exclude subsequent review-loop chatter."},{"category":"missing-requirement","check_key":"repair-overlap-authoritative-universe","description":"The research source requires overlap analysis across shared sections, files/resources, contracts, and checks. Restricting the graph to shared sections misses exactly the cross-section repair conflicts the graph is intended to expose, so a fixer can make two locally valid but mutually incompatible changes.","finding_id":"F-R15-REPAIR-OVERLAP-UNIVERSE","location":"Sections 2.2 and 2.6; docs/research/adversarial-plan-review.md overlap and cross-section interaction requirements.","participating_section_ids":["2.2","2.6"],"prevention":"For each repair, test adjacency through section, check key, changed contract, and target/resource identity, then verify that every discovered edge is visible before edit and represented in the existing coverage disposition.","principle":"Repair synthesis must derive cross-finding interactions from every stated shared identity, not a single proxy.","root_cause":"Section 2.6 makes shared section_id the repair-graph join, although the plan's own evidence model also identifies overlap through check_key, changed contracts, targets, resources, and acceptance criteria. Findings on different sections can therefore mutate the same runtime contract without becoming adjacent.","section_id":"2.6","severity":"blocking","suggested_fix":"Broaden the existing 2.6 repair graph to the union of shared check_key, changed contract, target/resource, and section identities. Expose that same graph before edits in 2.2 and record each edge through the existing attestation/disposition structure; do not introduce a second graph protocol."},{"category":"traceability","causal_finding_id":"F-R14-SERVER-DERIVED-LEDGER-ECHO","causal_section_ids":["4.3"],"check_key":"approval-ledger-server-authority-acceptance-parity","description":"The two adjacent acceptance clauses instruct an implementer to build mutually exclusive interfaces. The current wrappers have no ledger parameter, so following 4.3.5 would reintroduce the mechanism that 4.3.6 explicitly removed.","finding_id":"F-R15-LEDGER-ACCEPTANCE-CONTRADICTION","introduced_in_round":15,"location":"Section 4.3 acceptances 4.3.5 and 4.3.6; src/gobby/tools/plan_approval.py staged/taskless approval wrappers.","prevention":"Whenever authority moves from caller to server, sweep the complete producer/consumer acceptance chain and delete all caller-forwarding clauses before accepting the repair.","principle":"A server-derived value cannot simultaneously remain a caller-forwarded input in the same acceptance contract.","root_cause":"Round 14 removed approval_ledger from the caller contract and moved its single derivation to the terminal transition, but 4.3.5 still requires taskless and staged callers to forward the ledger into subsequent calls while 4.3.6 rejects any caller-provided ledger.","section_id":"4.3","severity":"blocking","suggested_fix":"Delete the stale ledger-forwarding requirement from 4.3.5. Require staged display to render the ledger from the server-derived approved envelope, keep 4.3.6 limited to rejecting ledger input, and leave terminal persistence with 7.2/8.1."},{"category":"over-engineering","check_key":"coverage-shadow-echo-contract-parity","description":"An implementer following 5.3 can preserve the 42 KB caller-supplied shadow echo even though 6.2 makes the server-owned routing decisions authoritative. That retains redundant protocol surface and two potentially divergent representations of the same manifest.","finding_id":"F-R15-SHADOW-ECHO-SCHEMA-STALE","location":"Sections 5.3, 6.2, and 8.1; src/gobby/tools/plan_review.py coverage wrapper shadow-manifest boundary.","participating_section_ids":["5.3","6.2","8.1"],"prevention":"After deleting a caller echo, trace the removed field through wrapper inventory, validator schema, test fixtures, and attestation output, distinguishing input removal from compact output status.","principle":"A server-derived artifact should not remain a large caller echo when validated persisted routing inputs already key its derivation.","root_cause":"Section 6.2 correctly removes the echoed shadow manifest and derives it from evidence_id plus reviewed routing decisions, but 5.3 still specifies shadow_manifest_status as a strict coverage-validator input and inventories the old wrapper signature.","section_id":"5.3","severity":"blocking","suggested_fix":"Update 5.3's inventory and validator contract to accept routing_decisions, remove shadow_manifest_status as an input, and retain only the compact server-derived shadow status in the output attestation. Reuse the existing evidence cache and routing decisions; add no derivation token, state store, or cache-key protocol."},{"category":"bad-sequencing","check_key":"integration-owner-leaf-closure","description":"The manifest emitter creates a task per leaf with literal target ownership. These upstream leaves would therefore be blocked on behavior located only in 8.1, or would need to violate ownership and create reverse dependencies, defeating the plan's own integration boundary.","finding_id":"F-R15-INTEGRATION-LEAF-ACCEPTANCE-LEAK","location":"Sections 2.2, 5.2, 6.5, 7.2, and 8.1; manifest_emitter and _contract task generation ownership.","participating_section_ids":["2.2","5.2","6.5","7.2","8.1"],"prevention":"Compile every acceptance against the emitted leaf's exact target-file set and dependency predecessors; any production assertion requiring a downstream service file must move to the integration leaf.","principle":"Each manifest leaf must be completable using only its owned files before the downstream integration owner runs.","root_cause":"Section 8.1 says production service integration belongs there, but upstream acceptances 2.2.7-2.2.8, 5.2.9, 6.5.7, and 7.2.13 still require real prepare, rollback, finalize, or terminal service behavior that their manifest-owned helper/schema files cannot implement.","section_id":"8.1","severity":"blocking","suggested_fix":"Move the remaining production prepare/rollback/finalize/terminal clauses into 8.1 and delete the duplicates from upstream leaves. Keep upstream tests limited to their helper/provider/schema contracts and injected callbacks so every leaf closes before integration without reverse dependencies."},{"category":"weak-testability","check_key":"minimal-repair-scope-discriminated-schema","description":"A competent implementation cannot enforce the stated rule deterministically from minimal_repair prose: equivalent prose can describe either branch, and the validator cannot know when a new-deliverable justification is mandatory. This leaves a required validator input with no producer.","finding_id":"F-R15-MINIMAL-REPAIR-SCOPE-SCHEMA","location":"Sections 1.1, 4.3, 5.3, and 7.1; src/gobby/models/review_findings.py review finding and round-result schemas.","participating_section_ids":["1.1","4.3","5.3","7.1"],"prevention":"For every conditional validation rule, identify the discriminant, all producers, schema transport, and negative cases before assigning the validator acceptance.","principle":"A validator-required conditional branch needs a machine-distinguishable input and an owned producer.","root_cause":"Section 7.1 adds only a free-form minimal_repair string while requiring the validator to distinguish repairs within existing deliverables from justified new deliverables. Neither the branch discriminator nor its producer is specified in taskless, staged, or contract schemas.","section_id":"7.1","severity":"blocking","suggested_fix":"Add one discriminated repair_scope field with values existing_sections or new_deliverable. Reuse section_id and participating_section_ids for the existing branch, and require new_deliverable_justification only for the new branch. Carry these fields through the already planned taskless, staged, and contract schemas in their existing owner order; add no subsystem."},{"category":"traceability","check_key":"end-to-end-verification-manifest-owner","description":"The plan calls E1 required end-to-end verification, but the compiled task graph cannot schedule or close it. Individual unit and integration leaves can all close while the stated fixer-induced classification, repeated-key, wall-time, ledger telemetry, and live-loop comparison is never run.","finding_id":"F-R15-E1-VERIFICATION-UNOWNED","location":"E1 and Sections 6.4, 7.2, 8.1, 9.1, 9.2, and 9.3; src/gobby/tools/manifest_emitter.py and src/gobby/tools/_contract.py.","participating_section_ids":["6.4","7.2","8.1","9.1","9.2","9.3"],"prevention":"Before expansion, map every required verification command and evidence artifact to a manifest entry, dependency closure, and explicit acceptance criterion.","principle":"Required live or manual verification in an expandable plan needs a manifest-owned deliverable with executable acceptance.","root_cause":"E1 is marked only as a verification section without acceptance criteria, while manifest derivation emits tasks only for deliverables and compilation creates one task per manifest entry. Its required live comparison and telemetry checks therefore have no task owner.","section_id":"8.1","severity":"blocking","suggested_fix":"Promote E1 into one final category:test deliverable depending on the relevant P6/P7/P8/P9 leaves, with acceptance that writes the concrete comparison artifact and records the listed telemetry. Keep E1's existing commands as its implementation detail; this single explicit regression task is the required owner."},{"category":"bad-sequencing","causal_finding_id":"F-R12-STAGED-ENRICHMENT-ORDER","causal_section_ids":["7.2"],"check_key":"staged-intent-post-verdict-side-effect-order","description":"Following the listed transition literally leaves existing effects either before durable intent, where a crash can expose uncommitted verdict state, or after terminal commit without replay ownership. The current workflow's lesson-mint wait can also deadlock because the terminal owner has not run yet.","finding_id":"F-R15-STAGED-INTENT-SIDE-EFFECT-ORDER","introduced_in_round":15,"location":"Section 7.2; src/gobby/tools/plan_approval.py and src/gobby/install/shared/workflows/adversarial_plan_review.yaml.","prevention":"Inventory every existing verdict-dependent effect and wait condition for both approval and rejection, then test crash/replay at each boundary using the existing recovery/checkpoint path.","principle":"When verdict commit moves to terminal cleanup, every side effect that depends on that verdict must move behind the same durable boundary and all pre-terminal consumers must be removed.","root_cause":"Section 7.2 enumerates settle, enrich, finalize, commit, and wake, but the current staged wrappers also mint/backfill lessons, link persisted artifacts, release claims, notify or relay, and tick workflow state; the workflow can wait on lesson_mint_status before end_agent_run. The plan does not order those effects relative to the new durable intent.","section_id":"7.2","severity":"blocking","suggested_fix":"Route the existing verdict-dependent side-effect bundle through the common terminal helper after enrichment and stage commit, and change the YAML path to durable intent followed directly by end_agent_run. Reuse the existing mint recovery/checkpoint for replay and cover both verdicts plus crash points; do not add another state machine."}],"reviewer_session":"#9720","round":15,"round_number":15,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"d8bfe602-ce1b-4feb-8edd-e97729c220b2","plan_hash":"e089c90328bf98b7a56bd169924ff8afb81c0cb1bc06173780f92bf816d6cd3e","round_number":16,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"470cc1970e06e64286b11dca7bb4f6007993dea96eb5e319dbf34816f36da295","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":6,"total":10},"evidence_id":"d8bfe602-ce1b-4feb-8edd-e97729c220b2","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":26,"manifest_digest":"88e682437672cfb37aafba1d846d209c5d9192aa0145af390ffc5be032e71a80","status":"valid"},"source_digest":"9409f60d162caf5adda6c4ec035972e285d49b81065951e5794fe730190bcdaf","version":1},"findings":[{"category":"traceability","check_key":"staged-repair-producer-state-seam","description":"An implementer can satisfy the helper assertions with injected records while the real staged planner still submits only review notes and the public stage-state API has no typed payload. A round-one rejection followed by planner revision and restart therefore has no production seam that atomically carries the required resolution records and repair attestations into round-two preparation.","finding_id":"F-R16-STAGED-REPAIR-PRODUCER-SEAM","location":"§2.2; src/gobby/install/shared/workflows/agents/planner.yaml:90-106,180-204; src/gobby/mcp_proxy/tools/tasks/_stage_review.py:213-281; src/gobby/storage/tasks/_stage_states.py:172-190,339-372","participating_section_ids":["2.2","2.5","2.6","8.1"],"prevention":"For every new preparation input, trace producer, public schema, fixed-signature forwarders, atomic durable write, restart reader, consumer, and a fixture-free production test.","principle":"Every mandatory round-two repair input needs an authoritative production producer and one atomic durable transport on every coordinator path.","root_cause":"Section 2.2 describes a staged producer but does not own the actual planner or plan-draft producer contracts, and the public stage-state submit seam accepts only notes even though the private transition can persist artifact updates.","section_id":"2.2","severity":"blocking","suggested_fix":"Add planner.yaml and plan-draft/SKILL.md to §2.2 ownership, specify the exact typed payload constructed at the real submit call, and thread it through the existing public stage-state submit method into the private artifact_updates seam for one atomic write. Extend the production rejection → revision → restart → round-two test so no fixture supplies the payload."},{"category":"bad-sequencing","check_key":"repair-universe-pre-attestation-visibility","description":"The first call containing any newly derived site cannot succeed: the coordinator cannot attest to a site it has not seen, and failed preparation persists no context from which a retry can learn it. This leaves a required attestation input with no successful production sequence.","finding_id":"F-R16-REPAIR-UNIVERSE-VISIBILITY","location":"§§2.2,2.6,5.2,8.1; .gobby/plans/adversary-convergence-improvements.md:660-673; src/gobby/plans/review_evidence.py:49-177; src/gobby/plans/consumer_sweep.py:88-122","participating_section_ids":["2.2","2.5","2.6","5.2","5.3","8.1"],"prevention":"For each pre-spawn proof, order authoritative derivation, producer visibility, attestation construction, server rederivation, mismatch handling, persistence, and retry.","principle":"A producer must observe the complete obligation universe before attesting that it swept that universe.","root_cause":"The plan derives the changed-contract/target consumer inventory inside round-N+1 preparation, validates already-submitted attestations against it in the same call, and returns the inventory only after successful preparation while explicitly rejecting a preview transition.","section_id":"5.2","severity":"blocking","suggested_fix":"Reuse the existing deterministic consumer-sweep/repair-universe helper before submission so taskless and staged producers can see the exact site graph, then have preparation rederive and compare it against current repository state. Reject drift and rerun construction; do not add a second persistent preview state machine."},{"category":"traceability","check_key":"staged-coverage-record-handoff","description":"The stated ledger merge requires dismissed identities, rationales, and structured sweep records, yet the production verdict boundaries carry only findings and the compact attestation. An implementation can therefore validate exhaustive coverage and still discard the records before the terminal helper and ledger consume them.","finding_id":"F-R16-COVERAGE-RECORD-HANDOFF","location":"§§4.1,5.1,5.3,7.2,8.1; src/gobby/plans/review_coverage.py:61-116,382-454; src/gobby/mcp_proxy/tools/tasks/_stage_review.py:302-442,476-584","participating_section_ids":["4.1","5.1","5.3","7.2","8.1"],"prevention":"Trace every finalization input through validator output, taskless result, staged public schema, facade, result builder, durable intent, terminal revalidation, restart, and ledger merge.","principle":"Every record required by finalization must have one canonical producer-to-consumer transport that preserves its digest-bound identity across both verdicts and restart.","root_cause":"The plan puts sweep and disposition records in canonical round_result, but it never assigns a validator return shape or staged approve/reject/result-builder path that carries those rich records; the current validation boundary reduces them to coverage_attestation.","section_id":"5.1","severity":"blocking","suggested_fix":"Have §5.1 return one canonical validated coverage-record bundle and include that same bundle in round_result. Thread it through the existing taskless result and staged approve/reject, facade/result-builder, delivered-intent, and terminal-revalidation paths without a sidecar store; test non-empty approval and rejection with a restart before terminalization."},{"category":"traceability","check_key":"immutable-requirement-citation-contract","description":"Task title, description, validation criteria, and taskless request-anchor requirements have no repository path, so a compliant immutable requirement-ID citation is rejected by the existing closed shape. Using a path instead violates the plan's immutable-bundle rule. The mandatory traceability evidence therefore has no accepted wire representation.","finding_id":"F-R16-REQUIREMENT-CITATION-CONTRACT","location":"§6.5; .gobby/plans/adversary-convergence-improvements.md:1096-1106; src/gobby/plans/review_coverage.py:345-379,457-559","participating_section_ids":["1.1","2.4","4.1","4.2","5.1","5.3","6.2","6.5","8.1"],"prevention":"For each evidence source class, round-trip one citation through lane candidate, failure trace, coverage validation, source digest, disposition, and ledger staleness handling.","principle":"Every required citation source needs a typed carrier accepted by every producer, validator, digest, and ledger consumer.","root_cause":"Section 6.5 requires traceability lanes to cite immutable requirement IDs, while the canonical citation validator accepts only repository path/SHA citations and rehashes those paths from the live worktree.","section_id":"6.5","severity":"blocking","suggested_fix":"Extend the existing citation type into one discriminated union: repository citations keep path, SHA-256, and span; requirement citations carry requirement_id, content_sha256, and an optional immutable-content span and validate against the bound requirements bundle. Reuse that union for lane candidates and failure traces and update existing strict schemas and digest consumers."},{"category":"unhandled-edge","causal_finding_id":"F-R15-STAGED-INTENT-SIDE-EFFECT-ORDER","causal_section_ids":["7.2"],"check_key":"snapshot-transport-record-size-bound","description":"The complete §7.2 section is 15,819 characters and 15,895 bytes before JSON envelope overhead, so the proposed fail-on-oversize rule cannot transport this bound snapshot. An implementer following §6.2 would make round preparation reject the very plan being implemented.","finding_id":"F-R16-OVERSIZED-SNAPSHOT-SELF-HOST","introduced_in_round":16,"location":"§6.2 and §7.2; .gobby/plans/adversary-convergence-improvements.md:858-922,1147-1323; src/gobby/config/features.py:126-148; src/gobby/plans/parser.py:163-313","prevention":"For every bounded transport, test the largest single valid instance of every record class and verify the reviewed plan itself round-trips below the serialized response budget.","principle":"A bounded evidence transport must carry every valid evidence record accepted by the plan grammar, including the plan that defines the transport.","root_cause":"Section 6.2 makes logical records indivisible and fails preparation when one exceeds a page, but valid plan sections have no size bound; the round-16 §7.2 record is already larger than the configured 15,000-character offload threshold.","section_id":"6.2","severity":"blocking","suggested_fix":"Page the canonical serialized evidence-envelope byte stream with the existing offset, limit, next_offset, and snapshot hash, aligning chunks to UTF-8 code-point boundaries and verifying the full hash before parsing. Delete the single-record rejection; do not add a separate record-fragment protocol."},{"category":"weak-testability","causal_finding_id":"F-R15-E1-VERIFICATION-UNOWNED","causal_section_ids":["E1.1"],"check_key":"convergence-regression-pass-criteria","description":"E1.1 can pass after recording unchanged or worse rounds-to-approval, repeated keys, same-class tail, and fixer-induced findings. The referenced target table requires single-digit approval rounds, zero exact check-key repeats, zero consecutive same-class findings, and a tail that decays to zero, but none is asserted.","finding_id":"F-R16-CONVERGENCE-ASSERTION","introduced_in_round":16,"location":"§E1.1; .gobby/plans/adversary-convergence-improvements.md:1507-1524; docs/plans/adversary-convergence.md:258-262","prevention":"For every claimed success metric, name its persisted producer, exact threshold or baseline relation, executable assertion, and failure output in the final verification owner.","principle":"A required end-to-end regression must fail when the intended outcome regresses; writing observations alone is not verification.","root_cause":"The round-15 repair promoted E1 to an executable deliverable but gave it only artifact-writing acceptance, with no pass/fail comparison against the plan's stated convergence targets.","section_id":"E1.1","severity":"blocking","suggested_fix":"Keep the comparison artifact and add executable assertions over persisted telemetry for the stated convergence targets, while separately bounding wall-time variance and proving exhaustive coverage is unchanged. The deliverable must fail when those criteria are not met."}],"round_number":16,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"c175c4d4-a50b-453a-aa9f-4230be0f0374","plan_hash":"20957747e402a22272687edecf922a1f1d29c1e3504a29376bf3b19b450f6a1d","round_number":17,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"995ad2f08fa58f28adbe7c750181e9b1e19402403c2d64729d0a70f31171bd68","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":5,"total":9},"evidence_id":"c175c4d4-a50b-453a-aa9f-4230be0f0374","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":26,"manifest_digest":"6897dd100f0c9fcae9473e760a712fc348f0a4c041f1283eebc61840e94a7969","status":"valid"},"source_digest":"78485e29f8842c3bd1747f34f63e941f53f239b7ec77358a65c4de5cc89632df","version":1},"evidence_id":"c175c4d4-a50b-453a-aa9f-4230be0f0374","findings":[{"category":"traceability","causal_finding_id":"F-R16-REQUIREMENT-CITATION-CONTRACT","causal_section_ids":["6.5"],"check_key":"immutable-requirement-citation-owner","description":"Section 6.5.12 requires repository/requirement citations to round-trip through lane candidates, failure traces, strict schemas, and digest consumers. Its Target list omits `review_coverage.py`, which owns `_citation_list` and `_rehash_sources`, and `review_findings.py`, which section 1.1 makes the blocking failure-trace validator. The manifest leaf and its machine-derived target inventory can therefore omit the two consumers that must implement the union.","finding_id":"F-R17-CITATION-UNION-OWNER","introduced_in_round":17,"location":"§6.5 lines 1033-1158; src/gobby/plans/review_coverage.py:345-379,457-559; src/gobby/plans/review_findings.py:49-158","prevention":"For every evidence-shape change, map the canonical type through all producers, validators, rehash/digest functions, schemas, and target ownership before accepting the repair.","principle":"A discriminated evidence carrier must name and order every validator, digest consumer, and producer that implements it.","root_cause":"The round-16 repair widened citation semantics in section 6.5 without adding the current repository-citation owner or the failure-trace consumer to that leaf's owned target surface.","section_id":"6.5","severity":"blocking","suggested_fix":"Add `src/gobby/plans/review_coverage.py` and `src/gobby/plans/review_findings.py` to section 6.5's Target list and existing shared-file order. Reuse one citation-union validator across those consumers and keep 6.5.12 as the cross-carrier test; add no parallel citation channel."},{"category":"bad-sequencing","causal_finding_id":"F-R16-REPAIR-UNIVERSE-VISIBILITY","causal_section_ids":["2.6"],"check_key":"repair-universe-producer-callable-seam","description":"The taskless coordinator currently proceeds directly to `prepare_plan_review_round`, the staged planner proceeds to `submit_for_review`, and the canonical consumer-sweep helper's production callers are the CLI and expansion gate. No read-only Gobby operation exposes the helper to either producer. Section 5.2 simultaneously says the inventory first appears inside preparation and could not have been shown beforehand. Following the plan leaves the required pre-attestation graph without a callable producer sequence.","finding_id":"F-R17-REPAIR-UNIVERSE-PRODUCER-SEAM","introduced_in_round":17,"location":"§2.6 lines 339-350; §5.2 lines 691-700; plan/SKILL.md:79-87; planner.yaml:80-103; review-evidence MCP wrapper:29-48","prevention":"Trace pre-spawn proof generation through callable derivation, producer visibility, attestation construction, canonical digest, preparation rederivation, drift rejection, and retry on every coordinator path.","principle":"A producer must observe the authoritative obligation universe through a callable production surface before it can attest to that universe.","root_cause":"The round-16 repair says model-driven producers call a Python helper before submission, but it adds no operation they can invoke and leaves section 5.2's contrary first-derivation-inside-preparation sequence intact.","section_id":"2.6","severity":"blocking","suggested_fix":"Expose the existing deterministic helper through one stateless read-only `gobby-plans` operation returning the typed graph and canonical digest. Instruct both existing producers to call it before editing/submission, have preparation rederive and compare that digest, and delete section 5.2's stale no-prior-visibility paragraph. Keep the rejected persistent preview state machine deleted."},{"category":"unhandled-edge","check_key":"terminal-verdict-effect-idempotency","description":"`_relay_signoff_to_build_coordinator_sync` calls `InterSessionMessageManager.create_message`, which generates a fresh UUID and performs an unconditional INSERT on every invocation. A crash after that INSERT and before the terminal helper records completion duplicates the relay on replay, even though 7.2.13 promises every injected crash boundary converges to the same durable state. The same proof is missing for the other verdict-dependent effects moved into the bundle.","finding_id":"F-R17-TERMINAL-EFFECT-IDEMPOTENCY","location":"§7.2 lines 1328-1339 and acceptance 7.2.13; src/gobby/mcp_proxy/tools/tasks/_stage_review.py:107-174; src/gobby/storage/inter_session_messages.py:141-194","participating_section_ids":["7.2","8.1"],"prevention":"For every terminal effect, inject a crash immediately after its durable write and before its checkpoint, then assert replay produces exactly one durable effect and one wake.","principle":"Every durable effect in a crash-replayable terminal transition must be idempotent across the interval after effect commit and before replay checkpoint commit.","root_cause":"Section 7.2 moves the full verdict-dependent effect bundle into a replayable helper but names recovery only for lesson minting; the current signoff relay has no stable identity.","section_id":"7.2","severity":"blocking","suggested_fix":"Reuse the existing evidence/run identity plus effect kind and target as a stable idempotency key in the existing effect APIs and recovery checkpoint. Make the signoff insert idempotent and prove artifact linking, claim release, lesson minting, relay/notification, workflow tick, and wake each survive the post-write/pre-checkpoint crash exactly once. Introduce no separate outbox unless an existing effect API cannot accept the stable key."},{"category":"weak-testability","causal_finding_id":"F-R16-CONVERGENCE-ASSERTION","causal_section_ids":["E1.1"],"check_key":"convergence-regression-class-identity-producer","description":"The evidence source explicitly says its same-class grouping is a human reading because nothing in the schema makes classes comparable. E1.1.3 now requires the live test to compare check-key classes across consecutive rounds using persisted telemetry, while 7.2 records exact/repeated check keys and classifications but defines no class field or canonical mapping. An implementer must invent the metric, so conforming implementations can disagree on pass/fail.","finding_id":"F-R17-CONVERGENCE-CLASS-PRODUCER","introduced_in_round":17,"location":"§7.2 lines 1197-1225; §E1.1 lines 1568-1569; docs/plans/adversary-convergence.md:98-105,251-267","prevention":"For every success metric, trace exact semantics through producer, strict schema, persistence, comparison reader, threshold, and negative regression fixture.","principle":"Every executable regression predicate over persisted telemetry needs a canonical machine-readable input with an authoritative producer.","root_cause":"The round-16 repair copied a qualitative same-class target into an executable assertion without defining class identity in the telemetry schema or producer contracts.","section_id":"E1.1","severity":"blocking","suggested_fix":"Add one canonical `check_key_class` field with contributing finding/ledger IDs and classification inputs to the existing reviewer-emitted `convergence_telemetry` object and producer contracts. Persist it through the existing `round_result`, then have E1.1.3 compare consecutive class sets. Add no new store or telemetry subsystem."},{"category":"weak-testability","check_key":"convergence-regression-fixture-path","description":"E1.1.1 and the live-regression command name `.gobby/plans/context-mode-borrowings.md`, which does not exist and no deliverable creates or copies. The tracked fixture is `.gobby/plans/completed/context-mode-borrowings.md`. Following the plan makes the required final regression fail before it measures convergence.","finding_id":"F-R17-REGRESSION-FIXTURE-PATH","location":"§E1.1 lines 1552-1577; .gobby/plans/completed/context-mode-borrowings.md:1-3","prevention":"Resolve and hash every external fixture path during repository blast-radius review, and require a producer for each path that does not already exist.","principle":"Every executable regression fixture must resolve to a durable repository artifact or have an explicit producer before the test runs.","root_cause":"E1.1 retained the plan's pre-completion location after the historical fixture moved into the tracked completed-plan directory.","section_id":"E1.1","severity":"blocking","suggested_fix":"Replace both E1.1 references with the existing tracked `.gobby/plans/completed/context-mode-borrowings.md` path. Add no fixture copy or setup mechanism."}],"plan_hash":"20957747e402a22272687edecf922a1f1d29c1e3504a29376bf3b19b450f6a1d","reviewer_session":"#9722","round":17,"round_number":17,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"ab14cbe7-951e-4935-8edb-7e278ffee660","plan_hash":"c99de32d0cf52345b644820079d2abe89b2dc0162d47535dccc5ed548e47c373","round_number":18,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"319cd0722c1f84fd6bb1f1498d18d2b2e569dc5f8442495c87b3c3d1335aa99a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":6,"total":13},"evidence_id":"ab14cbe7-951e-4935-8edb-7e278ffee660","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":26,"manifest_digest":"cb1b07b25bc88ac488e529f24befdd649b49ac8385d61004c0548d91d6a4959c","status":"valid"},"source_digest":"1808205f4b29ba47ab09587499c84d8810df0fb225dc9f4c86bc5abd068810ae","version":1},"evidence_id":"ab14cbe7-951e-4935-8edb-7e278ffee660","findings":[{"category":"bad-sequencing","causal_finding_id":"F-R17-REPAIR-UNIVERSE-PRODUCER-SEAM","causal_section_ids":["2.6","5.2"],"check_key":"repair-universe-producer-callable-seam","description":"The taskless and staged producers still have no manifest-owned gobby-plans wrapper or prompt surface for the new operation. The stated pre-edit call also cannot produce the digest preparation rederives from the post-edit revision, and a staged mismatch occurs after submission with no active producer named to reconstruct the attestation.","finding_id":"F-R18-REPAIR-UNIVERSE-PRODUCTION-SEQUENCE","introduced_in_round":18,"location":"§2.6 lines 334-385; §2.2 producer paths; §5.2 inventory derivation","prevention":"Trace edit, callable derivation, producer visibility, attestation construction, submission, preparation rederivation, drift rejection, and producer recovery across both coordinator paths.","principle":"An attestation can bind only an obligation universe derived from the repository state after the repair edit, and every drift refusal needs a reachable producer retry.","root_cause":"The round-17 repair added a stateless operation in prose while leaving section 2.6 owned only by review_repair.py and ordering the authoritative call before the edit that determines changed sections, symbols, targets, and contracts.","section_id":"2.6","severity":"blocking","suggested_fix":"Keep the one stateless helper. Apply the edit first, call the registered read-only operation over that resulting worktree, construct attestations, and submit without further edits. Add the existing review-evidence wrapper plus taskless plan skill and staged planner targets to §2.6, and reuse the current failed-dispatch rollback to restore the revision/resubmission producer on digest drift."},{"category":"missing-requirement","check_key":"request-anchor-byte-owner-totality","description":"Section 6.5 claims event-owned request bytes are available to all six plan-mode exits, yet the existing managed-web-chat fixture activates plan mode from persisted metadata with no event data or prompt. That branch cannot write the required exact initiating request without guessing, reading mutable state later, or fabricating an empty anchor.","finding_id":"F-R18-REQUEST-ANCHOR-BYTES-PRODUCER","location":"§6.5 lines 1067-1111 and 1169-1174; observer_plan_mode.py:58-131","participating_section_ids":["6.5","8.1"],"prevention":"For every authoritative activation branch, prove the capture point receives non-empty exact request bytes and identity; test metadata-only, persisted-mode, transcript-mode, structured-mode, and prompt-fallback events.","principle":"An immutable request anchor must be captured at a boundary where authoritative request identity and exact content coexist.","root_cause":"The selected plan-mode observer owns mode resolution, but several activation branches own only metadata, persisted mode, or transcript-derived mode and can return before any request content is read.","section_id":"6.5","severity":"blocking","suggested_fix":"Capture provider-neutral request identity and exact content at the existing ingress event that actually owns them, then bind or reuse that persisted anchor when plan mode activates. Mode-only branches must reuse an existing anchor or fail closed; they must not manufacture bytes. Exercise the real metadata-only and transcript-only paths in §6.5.9 and §8.1.16."},{"category":"unhandled-edge","causal_finding_id":"F-R17-TERMINAL-EFFECT-IDEMPOTENCY","causal_section_ids":["7.2"],"check_key":"terminal-verdict-effect-idempotency","description":"The signoff relay cannot meet exactly-once replay: a crash after create_message commits and before the terminal checkpoint makes retry insert a second message because each call gets a fresh UUID. The declared evidence/run/effect/target key never reaches a durable uniqueness boundary.","finding_id":"F-R18-SIGNOFF-IDEMPOTENCY-CARRIER","introduced_in_round":18,"location":"§7.2 lines 1362-1372 and acceptance 7.2.14; _stage_review.py:107-174; inter_session_messages.py:141-194","prevention":"For every verdict-dependent effect, follow the stable key through caller, persistence API, unique carrier, conflict verification, and post-write/pre-checkpoint replay.","principle":"A stable effect identity prevents replay duplicates only when the durable writer accepts and enforces that identity atomically.","root_cause":"The round-17 repair added stable-key prose and acceptance above the storage boundary, while InterSessionMessageManager.create_message still generates uuid4 and performs an unconditional INSERT and is absent from §7.2 targets.","section_id":"7.2","severity":"blocking","suggested_fix":"Reuse the existing message primary key as the carrier: derive a deterministic ID from evidence ID, run ID, effect kind, and target, pass it through create_message, and conflict-verify/reuse the existing row. Add inter_session_messages.py and its focused tests to §7.2. Keep the outbox fallback deleted unless an existing effect API truly cannot accept the key."},{"category":"unhandled-edge","check_key":"round-result-delivery-monotonicity","description":"send_message persists the parent mailbox message before it writes the delivered AgentRun.result. If the mailbox commit succeeds and the result write fails, the promised retry invokes the mailbox path again with a new ID, producing duplicate canonical round-result messages even though the result CAS is monotonic.","finding_id":"F-R18-ROUND-RESULT-MAILBOX-IDEMPOTENCY","location":"§7.2 lines 1317-1325 and acceptance 7.2.12; agent_messaging.py:175-209; sessions/mailbox.py:110-198","participating_section_ids":["3.1","7.2"],"prevention":"Inject crashes after each durable write in reviewer-result delivery and require one stable identity across mailbox insertion, result delivery state, retry, enrichment, and parent observation.","principle":"A multi-write delivery is retry-safe only when every earlier durable write has the same stable identity as the later compare-and-set state.","root_cause":"Section 7.2 guards AgentRun.result against regression but leaves the preceding mailbox insert on a fresh UUID, so the result CAS cannot identify or reuse the message committed before it.","section_id":"7.2","severity":"blocking","suggested_fix":"Reuse one deterministic run/evidence/parent/delivery-kind identity in the existing mailbox carrier, or perform the mailbox insert and delivered-state CAS in one existing database transaction. Add the mailbox persistence owner to §7.2 and extend 7.2.12 with post-mailbox/pre-result and post-result/pre-ack crash cases."},{"category":"traceability","causal_finding_id":"F-R17-CONVERGENCE-CLASS-PRODUCER","causal_section_ids":["7.2"],"check_key":"convergence-regression-class-identity-producer","description":"An implementer following “Each finding also carries a check_key_class” will emit a top-level finding key that validate_plan_review_findings rejects. An implementer following the target list will place it in convergence_telemetry instead. Both readings satisfy different parts of the section and produce incompatible round results.","finding_id":"F-R18-CHECK-KEY-CLASS-CARRIER","introduced_in_round":18,"location":"§7.2 lines 1226-1246; review_findings.py:24-46,117-127; E1.1.3","prevention":"For each new telemetry field, name its exact JSON path and trace that path through producer contract, closed validator, persistence, comparison reader, and negative schema fixture.","principle":"A machine-readable metric needs one unambiguous carrier whose owning validator and consumers agree on the field location.","root_cause":"The round-17 repair says each finding carries check_key_class while §7.2 otherwise defines it as reviewer-emitted convergence telemetry; the closed finding validator rejects the top-level field and is absent from §7.2 ownership.","section_id":"7.2","severity":"blocking","suggested_fix":"Use the carrier already present: state that each convergence_telemetry classification record carries check_key_class alongside its contributing finding or ledger IDs and inputs, and remove the claim that it is a top-level finding field. Pin that exact JSON path in 7.2.2, 7.2.8, and E1.1.3; add no second schema field."},{"category":"unhandled-edge","check_key":"terminal-run-transition-totality","description":"_deferred_tmux_health_check directly marks the run failed and delivers it. Dispatch binds plan-review evidence immediately after spawn_agent_impl returns, so the asynchronous zero-delay task can run after binding and bypass settle, enrichment or expiry, canonical-result enforcement, and the direct-parent wake order.","finding_id":"F-R18-IMMEDIATE-HEALTH-TERMINAL-BYPASS","location":"§7.2 terminal-route invariant and 7.2.10; spawn_agent/_health.py:92-164; dispatch/spawn.py:328-367","participating_section_ids":["3.1","7.2"],"prevention":"Search every run_storage.fail, run_storage.complete, lifecycle terminalizer, and direct terminal delivery call; test each on both sides of the evidence-bind race.","principle":"Every terminalizer that can observe bound review evidence must enter the single evidence-aware terminal helper before mutating run state or delivering completion.","root_cause":"The terminal-route sweep covered the long-running health monitor but missed the separate zero-delay tmux health task in spawn_agent/_health.py.","section_id":"7.2","severity":"blocking","suggested_fix":"Add spawn_agent/_health.py to §7.2 targets and route its post-bind terminal decision through the extracted helper before run_storage.fail or delivery. Preserve the current pre-bind spawn-failure branch, and add this module to 7.2.10 with a race test covering health completion immediately before and after bind_evidence_run."}],"plan_hash":"c99de32d0cf52345b644820079d2abe89b2dc0162d47535dccc5ed548e47c373","reviewer_session":"#9723","round":18,"round_number":18,"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

```json plan-review-round
{"evidence_id":"4390f45c-8924-4b0f-9045-17f9758b7dc0","plan_hash":"c524144dc03db8e00c64677f7c178bc2d9ac9792c45c4efe182d53a34778f58c","round_number":19,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"78e9d5481000d8223ce74954f5cef2639b4f782edf374e017f9c3e699752fdb1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":9,"emitted_findings":10,"total":19},"evidence_id":"4390f45c-8924-4b0f-9045-17f9758b7dc0","lanes":[{"candidate_count":10,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":26,"manifest_digest":"a5f2c667b21464b1e14b3b869005a7a0482615af738af6072faf357300e0d852","status":"valid"},"source_digest":"e9702aee21f4e295022ad9bd21c3ae0855a52008242cb9e477ec9ad6c83f95f9","version":1},"findings":[{"category":"bad-sequencing","check_key":"shared-target-write-order","description":"Sections 2.6 and 4.3 both own edits to the plan-review skill and review-evidence wrapper, but the plan leaves them unordered. Parallel expansion can conflict or make later edits overwrite the reviewed-routing behavior.","finding_id":"F-R19-SHARED-TARGET-ORDERING","fix":"Make section 4.3 depend on 2.6, then rederive the manifest and verify reachability for every shared target pair.","location":".gobby/plans/adversary-convergence-improvements.md:334 and :564","prevention":"Build a target-to-section map during manifest derivation and reject shared writers lacking directed reachability.","principle":"Independently schedulable deliverables that write the same owned files must be transitively ordered.","root_cause":"Round 18 added the plan-review skill and wrapper to section 2.6 without adding a dependency edge to the 4.3/6.2 shared-target chain.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R18-REQUEST-ANCHOR-BYTES-PRODUCER","causal_section_ids":["6.5"],"check_key":"request-anchor-content-availability","description":"The plan forces the managed web-chat and Codex branches to reuse a persisted anchor or fail closed, although both branches receive the exact event prompt. On first entry this rejects a valid request instead of hashing the bytes already present.","finding_id":"F-R19-ANCHOR-CONTENT-AVAILABILITY","fix":"Capture the event prompt together with its turn/request identity on the false-to-true transition; use persisted-anchor reuse or fail-closed behavior only when that concrete payload is actually absent.","introduced_in_round":19,"location":".gobby/plans/adversary-convergence-improvements.md:1094; src/gobby/workflows/observer_plan_mode.py:58; src/gobby/servers/websocket/chat/backends/codex.py:146; src/gobby/adapters/codex_impl/client_notifications.py:97; src/gobby/adapters/codex_impl/app_server_adapter.py:626","prevention":"For every transition branch, inventory prompt bytes and identity fields and test the first-entry/no-persisted-anchor case.","principle":"Request-anchor policy must be based on the actual event payload, not a blanket source or mode classification.","root_cause":"The round-18 remedy treats managed web-chat and Codex BEFORE_AGENT transitions as contentless even though their event data carries the prompt.","section_id":"6.5","severity":"blocking"},{"category":"traceability","check_key":"deviation-proof-wire-schema","description":"Taskless findings, staged findings, rendering, and coverage validation can each implement a different deviation proof while still following the prose, so the mandatory escalation evidence is not interoperable end to end.","finding_id":"F-R19-DEVIATION-WIRE-SCHEMA","fix":"Define one exact five-key deviation_from_minimal_repair object and reuse that schema unchanged in every producer, validator, renderer, and parity test.","location":".gobby/plans/adversary-convergence-improvements.md:275","prevention":"Require every cross-surface proof carrier to reference one server-owned schema and parity test.","principle":"A value required across multiple strict producers and validators needs one closed, named wire schema.","root_cause":"The plan requires deviation_from_minimal_repair evidence but does not pin its exact object keys, types, nullability, or reuse point.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"integration-owner-acceptance-parity","description":"The two acceptance contracts leave no coherent owner for the production preparation behavior: satisfying one section's stated boundary violates the other's. A competent implementer cannot close both leaves as written.","finding_id":"F-R19-INTEGRATION-OWNER-CLOSURE","fix":"Move the production registered-operation preparation and recovery assertions to section 8.1; keep section 2.6 acceptance local to the helper and wrapper contract.","location":".gobby/plans/adversary-convergence-improvements.md:334 and :1440","prevention":"Assign every integration assertion to exactly one leaf and reject contradictory ownership exclusions.","principle":"Leaf ownership and acceptance boundaries must agree about who proves production integration.","root_cause":"Section 2.6.4 requires production registered-operation preparation and recovery on both routes, while section 8.1.4 says its acceptance does not cover upstream production preparation.","section_id":"8.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"terminal-route-inventory-completeness","description":"Unregister can terminalize a run without executing the planned mailbox/result helper, so the claimed one-identity terminal delivery invariant is false on an existing production route.","finding_id":"F-R19-UNREGISTER-TERMINAL-BYPASS","fix":"Route unregister cancellation through the existing evidence-aware cancellation helper and expand the route-table test to cover unregister, cancel, timeout, cleanup, and health-check paths.","location":"src/gobby/mcp_proxy/tools/agents_query_tools.py:616; src/gobby/agents/runner_queries.py:59; src/gobby/storage/agents/_lifecycle.py:339","prevention":"Trace every cancel, timeout, cleanup, unregister, and health-check entry point to storage before declaring the route table complete.","principle":"Every terminal transition must pass through the same evidence-aware result-delivery route.","root_cause":"The terminal-route inventory omits unregister cancellation, which calls AgentRunner.cancel_run and reaches storage cancellation before generic terminal delivery.","section_id":"7.2","severity":"blocking"},{"category":"traceability","check_key":"consumer-sweep-caller-contract","description":"The new typed error can escape the plan gate before guarded spawning, producing an unstructured failure instead of the intended deterministic no-spawn result.","finding_id":"F-R19-CONSUMER-SWEEP-CALLER-CONTRACT","fix":"Catch the typed sweep error in the existing plan gate, translate it to the existing structured no-spawn outcome, and pin both CLI and spawn-gate callers with parity tests.","location":"src/gobby/plans/consumer_sweep.py:88; src/gobby/tasks/expansion/_plan_gate.py:30 and :93; src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:181","prevention":"Use caller enumeration plus negative-path tests whenever a shared result contract becomes exceptional.","principle":"Changing a shared helper from a value result to a typed failure requires updating every current caller's control-flow contract.","root_cause":"The plan changes an unavailable consumer sweep from skip to typed error but omits the spawn plan gate, which assumes a result and catches only database errors.","section_id":"5.2","severity":"blocking"},{"category":"traceability","check_key":"repair-scope-producer-parity","description":"Enforcing repair_scope as planned will reject at least one existing findings producer, or producers will invent divergent shapes, because no leaf owns producing the new values across both routes.","finding_id":"F-R19-REPAIR-SCOPE-PRODUCER-PARITY","fix":"Add repair_scope and its conditional justification under the existing taskless and staged producer owners, then reuse the shared validator and parity tests without adding a second protocol.","location":".gobby/plans/adversary-convergence-improvements.md:1198; src/gobby/plans/review_findings.py:1 and :117","prevention":"For every required closed-schema field, map producer, validator, renderer, persistence carrier, and parity test to owned deliverables.","principle":"A new required field in a closed schema needs explicit producer ownership before consumer enforcement.","root_cause":"Section 7.1 requires repair_scope and conditional deviation justification through taskless and staged findings but targets only the shared validator/skill and assigns no acceptance to the existing producers.","section_id":"7.1","severity":"blocking"},{"category":"traceability","check_key":"index-token-verifier-invocation","description":"The review workflow cannot perform its own mandated token verification, so an engineer must either violate the acceptance contract or invent an unplanned access path.","finding_id":"F-R19-INDEX-VERIFIER-CALLABLE","fix":"Expose a thin stateless read-only wrapper around the existing verifier, add only that wrapper to the review allowlists, and test before/after lane invocation.","location":".gobby/plans/adversary-convergence-improvements.md:822; src/gobby/agents/code_index.py:1","prevention":"For each required workflow action, validate callable-surface reachability and allowlist membership before accepting the workflow.","principle":"A mandatory cross-lane verification step must be callable through the surfaces available to those lanes.","root_cause":"The plan creates an internal verify_index_token helper and requires every lane to call it before and after research, but exposes no MCP or CLI seam available to the taskless reviewer.","section_id":"6.1","severity":"blocking"},{"category":"over-engineering","check_key":"wake-delivery-semantics","description":"The durable inter-session result message can be deduplicated, but the live wake cannot be proven exactly once across cleanup/restart with the existing carriers. Implementing the prose would require an unjustified durable wake protocol.","finding_id":"F-R19-WAKE-EXACTLY-ONCE","fix":"Bind correctness to one deterministic durable result message with idempotent recipient consumption; make the live wake best-effort or at-least-once and delete the outbox/exactly-once nudge requirement.","location":".gobby/plans/adversary-convergence-improvements.md:1224; src/gobby/events/completion_registry.py:32; src/gobby/agents/agent_cleanup.py:171; src/gobby/events/wake.py:120, :485, and :538","prevention":"Separate durable correctness from best-effort notification semantics and test the crash boundary before claiming exactly-once delivery.","principle":"Exactly-once live side effects across crashes require durable consumer state; when durable identity already exists, correctness should rest on it rather than a new delivery protocol.","root_cause":"The plan demands exactly one live wake while the selected completion registry and debounce state are process-local and are cleared during cleanup or restart.","section_id":"7.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"dismissal-reopen-enforcement","description":"A reviewer can reopen an unchanged dismissed candidate and still obtain a valid coverage attestation, defeating the plan's convergence guarantee.","finding_id":"F-R19-DISMISSAL-REOPEN-GATE","fix":"Reuse the existing coverage validator to reject an unchanged ledger identity under the same bound plan/source hashes; permit reopening only through the typed changed-hash path.","location":".gobby/plans/adversary-convergence-improvements.md:550; src/gobby/plans/review_coverage.py:61; src/gobby/plans/review_evidence.py:262","prevention":"Put stateful no-reopen invariants in the server-owned validator and test unchanged, changed-source, and changed-plan cases.","principle":"A persisted dismissal invariant must be enforced at the acceptance boundary, not only supplied as advisory prompt context.","root_cause":"Section 4.2 injects the dismissed-candidate ledger into reviewer context, but the coverage validator never compares emitted candidates with ledger identity and bound hashes.","section_id":"4.2","severity":"blocking"}],"verdict":"needs_review"},"session_id":"ec2116d4-00d1-45fc-a323-f713633f4dd7"}
```

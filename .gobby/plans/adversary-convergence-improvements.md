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

Target: `src/gobby/plans/review_repair.py` (new module)

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

**Acceptance:**

- 2.3.1 - A deviating attestation without the three proof fields is rejected; one with them passes. test: `tests/plans/test_review_repair.py::test_deviation_requires_proof`.
- 2.3.2 - A deviation without counterexample validation evidence or without an explicit `accepted_risk` value is rejected. test: `tests/plans/test_review_repair.py::test_deviation_counterexample_and_risk`.

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

- 2.4.1 - Snapshot payload for round N+1 carries `prior_round_context` with prior finding IDs, check keys, attestations, changed acceptance-item IDs, and changed section targets. test: `tests/plans/test_review_evidence.py::test_snapshot_carries_prior_round_context`.
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

Target: `src/gobby/plans/review_repair.py`

Attestation sweep fields are proof only if the obligation set they discharge
is derived by the server, never by the payload being validated. From the
finalized prior round and the 5.2 consumer-site inventory, derive per repair:
required consumer sites, adjacent variants (check-key expansion over the
inventory), changed contracts, and repair-bundle interaction edges (accepted
findings whose repairs touch shared sections must carry a cross-repair sweep
record). Validate each attestation's `consumer_sites_swept`,
`adjacent_variants_swept`, and `deferred_sites` against that universe: every
derived site is swept, deferred with a reason, or explicitly dispositioned;
zero-result sweep claims carry query evidence (the search that returned
nothing); anything else refuses preparation. This closes the pre-spawn hole
that post-spawn coverage (5.1) cannot reach.

**Acceptance:**

- 2.6.1 - The sweep universe is derived server-side from the prior round plus the site inventory; an attestation covering a strict subset with no deferrals is refused naming the missing sites. test: `tests/plans/test_review_repair.py::test_sweep_universe_subset_refused`.
- 2.6.2 - A zero-result sweep claim without query evidence is refused. test: `tests/plans/test_review_repair.py::test_zero_result_requires_query_evidence`.
- 2.6.3 - Two accepted findings with overlapping repair sections require a cross-repair interaction record; its absence refuses preparation. test: `tests/plans/test_review_repair.py::test_repair_bundle_interaction_edges`.

## P3: Terminal-completion guard
`kind: framing`

**Goal**: A plan reviewer cannot terminate cleanly having delivered nothing.

### 3.1 end_agent_run completion guard and session-resolution fix [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py`, `src/gobby/plans/review_evidence_store.py`, `src/gobby/mcp_proxy/tools/agents_context.py`, `src/gobby/mcp_proxy/stdio_proxy.py`, `src/gobby/servers/routes/mcp/endpoints/execution.py`, `src/gobby/mcp_proxy/tools/agents_registry.py`, `src/gobby/utils/session_context.py`, `src/gobby/plans/review_evidence_models.py`

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
   `needs_requirements` and `inconclusive` (source drift, and the 6.3 timeout
   classification) are *non-attested terminal branches* — a reviewer whose
   snapshot drifted under it never completed lanes and structurally cannot
   produce a coverage attestation — so they require the bound `evidence_id`
   plus typed reason data instead. Demanding coverage from every verdict
   would trap exactly the runs that most need to exit cleanly, which is the
   failure this section exists to prevent.
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
`(kind, check_key, source_section_ids, section_hashes_at_entry)`, and retain
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

**Acceptance:**

- 4.1.1 - Ledger module merges, carries, and stales entries across three synthetic rounds. test: `tests/plans/test_review_ledger.py::test_merge_and_staleness_across_rounds`.
- 4.1.2 - Ledger entry validation shares category and check-key vocabulary with findings. symbol: `gobby.plans.review_ledger`.
- 4.1.3 - A reworded finding with a new round-local ID coalesces into its existing `ledger_entry_id`; a source-hash change stales the old entry and creates a fresh one. test: `tests/plans/test_review_ledger.py::test_canonical_coalescing_and_hash_split`.

### 4.2 Dismissal do-not-reopen injection [category: code] (depends: 2.4, 4.1)
`kind: deliverable`

Target: `src/gobby/plans/review_ledger.py`, `src/gobby/plans/review_evidence_io.py`

Inject ledger `dismissed` entries into the next round's `prior_round_context`
(from 2.4) so a later reviewer does not re-derive and re-dismiss the same
candidate. Reopen is permitted only when a named source hash or plan section
changed — the ledger carries the hashes to prove it.

**Acceptance:**

- 4.2.1 - A dismissed candidate appears in the next snapshot's context and is marked reopenable only on hash change. test: `tests/plans/test_review_ledger.py::test_dismissal_injection_and_reopen_rule`.

### 4.3 Approval condition and surfacing [category: code] (depends: 4.1, 5.1)
`kind: deliverable`

Target: `src/gobby/plans/review_coverage.py`, `src/gobby/review_learning/round_diff.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `docs/contracts/plan-coverage.md`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`

Approval becomes **zero remaining `blocking` findings** (equivalently: no
finding carrying a required failure trace). The ledger is surfaced in full at
approval alongside the manifest, so declining to fix a `major` is an explicit
recorded choice rather than an omission. A ledger entry carried unresolved
across three or more rounds feeds `record_review_lesson` with
`decision="no-fix-policy"`, `guardrail_target="checklist"` — the existing
promotion-ladder rung, reused rather than rebuilt. Lesson-minting eligibility
(`round_diff.py:53` requires `severity == "blocking"` today) is extended
**narrowly** for this path: `decision="no-fix-policy"` plus
`rounds_carried >= 3` plus a quality-ledger source entry; ordinary
finding-derived lessons keep the blocking requirement.

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

### 5.2 Machine-generated consumer inventory [category: code] (depends: 4.2)
`kind: deliverable`

Target: `src/gobby/plans/consumer_sweep.py`, `src/gobby/plans/review_evidence_io.py`, `src/gobby/agents/code_index.py`

`run_consumer_sweep` (`consumer_sweep.py:88-122`) queries the code index only
against declared section targets, from two call sites (`cli/plans.py:230` and
the spawn-time gate `tasks/expansion/_plan_gate.py:113`). Extend it to derive
changed acceptance items, targets, symbols, and contracts from the
inter-round revision and emit a typed candidate site inventory that travels
into the next-round snapshot's `prior_round_context` (2.4) through evidence
preparation and `review_evidence_io.py` — the inventory has a named producer,
transport, and consumer, not just a generator.

Derived sites are **not** gated on coordinator pre-disposition. Preparation
computes the inventory inside its own transaction (2.1), so there is no point
at which the coordinator could have seen and dispositioned sites it has not
been shown — a refusal gate there would make every non-empty inventory
unsatisfiable on its first call, and recovering by retrying an errored
preparation would be an undocumented preview protocol. Instead each derived
site joins the **required sweep universe** that 2.6 already validates:
attestations must sweep it, defer it with a reason, or disposition it, and
that check runs where the coordinator has the inventory in hand.

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
inside `prior_round_context`. Ownership sits here rather than in 6.1 because
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
- 5.2.4 - An unavailable index rolls preparation back with a typed `inventory_unavailable` error, leaves no evidence row behind, refuses spawn, and a fresh preparation succeeds after recovery. test: `tests/plans/test_consumer_sweep.py::test_inventory_unavailable_aborts_and_retries`.
- 5.2.5 - Token capture accepts only when the digests bracketing the index operation agree; a mutation landing between the index and the digest read forces a rerun, the token is rechecked after inventory derivation, and retry exhaustion is reported rather than silently accepted. test: `tests/plans/test_consumer_sweep.py::test_index_token_brackets_index_operation`.
- 5.2.7 - A changed target in a language the sweep cannot resolve is recorded as `language_unsupported` instead of being omitted from the inventory. test: `tests/plans/test_consumer_sweep.py::test_unsupported_language_marked_not_omitted`.

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
The file is 369 lines; schema constants may need a sibling module if the
result approaches the 1000-line limit.

**Acceptance:**

- 5.3.1 - All five payload parameters carry nested schemas with properties and required lists. file: `src/gobby/mcp_proxy/tools/plans/review_evidence.py`.
- 5.3.2 - A malformed disposition record is rejected at the schema layer with a field-level diagnostic. test: `tests/mcp_proxy/test_review_evidence_schemas.py::test_disposition_schema_rejects_malformed`.
- 5.3.3 - `prepare_plan_review_round` publishes strict schemas for its `repair_attestations` and resolution-record parameters, and a wrapper round trip carries both through to the service boundary unchanged. test: `tests/mcp_proxy/test_review_evidence_schemas.py::test_preparation_payload_schema_round_trip`.

## P6: Orchestration cost
`kind: framing`

**Goal**: Cut per-round wall time through the levers Gobby actually controls
(constraint 3).

### 6.1 Pin one code-index generation by protocol [category: config] (depends: 5.2, 7.1)
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

On mismatch the evidence is **expired and re-prepared**, not patched. The
token is immutable inside `prior_round_context`, so a stale-index mismatch
cannot be cleared by rerunning a lane against the same evidence — the snapshot,
inventory, and token must all be regenerated together before affected lanes
rerun. Still no `index_generation` counter (constraint 4): the token uses
existing state, zero schema change, zero Rust change.

This deliverable owns lane-instruction edits in the review skill, including
the constraint-2 statement that protocol implementors remain a prompt
obligation in the `repository_blast_radius` lane (from 5.2).

**Acceptance:**

- 6.1.1 - The plan skill's spawn step relies on preparation's single indexing site and instructs no separate pre-spawn `gcode index`. file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 6.1.2 - The adversary definition and review skill instruct `--no-freshness` on all lane searches. file: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`.
- 6.1.3 - The review skill and adversary definition instruct lanes to verify the snapshot's `index_token` before and after searching and to report a mismatch rather than proceeding. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 6.1.4 - A repository mutation or reindex between preparation and spawn, and one during a lane, are both detected; recovery expires the evidence and re-prepares snapshot, inventory, and token together before affected lanes rerun. test: `tests/plans/test_review_evidence.py::test_index_token_mismatch_expires_and_reprepares`.

### 6.2 Fix snapshot transport [category: code] (depends: 5.2)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/plans/review_evidence_io.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`

Changing the transport changes the consumer contract, so this deliverable
migrates the readers with it: the shared plan-review skill and **both**
adversary definitions (staged and taskless) describe a single whole-snapshot
call today, and existing wrapper tests assert that shape. Leaving them
unmigrated strands a live reviewer on page one — a worse failure than the
truncated envelope this fixes.

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
- Cache the derived manifest keyed on `(evidence_id, routing_digest)` —
  `validate_plan_review_coverage` re-derives it on every call, spinning up a
  `TemporaryDirectory` and re-rendering (`review_evidence.py:212-215, 278`).
- Accept `routing_decisions` or a derivation token in place of the echoed
  42 KB shadow manifest; derive the canonical shadow once, server-side.

New logic goes in `review_evidence_io.py` or a new module, not
`review_evidence.py` (constraint 5).

**Acceptance:**

- 6.2.1 - Every page of a 140 KB snapshot stays under the offload threshold and carries `snapshot_hash`, `total_sections`, and `next_offset`. test: `tests/plans/test_snapshot_transport.py::test_paged_fetch_under_threshold`.
- 6.2.2 - Repeated coverage validation with unchanged routing reuses the cached manifest instead of re-rendering. test: `tests/plans/test_snapshot_transport.py::test_manifest_cache_hit`.
- 6.2.3 - Coverage validation accepts a derivation token in place of the echoed shadow manifest. test: `tests/plans/test_snapshot_transport.py::test_shadow_derivation_token`.
- 6.2.4 - The page union equals the section manifest exactly and the concatenated pages hash to `snapshot_hash`, so a reader detects a missed or duplicated page locally. test: `tests/plans/test_snapshot_transport.py::test_page_union_and_local_hash_verification`.
- 6.2.5 - The shared plan-review skill and both adversary definitions drive a deterministic `next_offset` loop to exhaustion with local hash verification, and existing wrapper tests that assume a single whole-snapshot call are migrated. test: `tests/mcp_proxy/test_review_evidence_schemas.py::test_snapshot_consumers_page_to_exhaustion`.

### 6.3 Bound the adversary run [category: code] (depends: 6.1)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/agents/spawn_executor.py`, `src/gobby/install/shared/skills/plan/SKILL.md`

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

- 6.3.1 - The adversary definition carries a nonzero `timeout:` enforced by the health monitor. file: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`.
- 6.3.2 - A timed-out adversary run is classified inconclusive/timeout, and the documented coordinator transition is expire-then-fresh-retry with no checkpoint consumer. test: `tests/agents/test_adversary_timeout.py::test_timeout_classified_inconclusive`.
- 6.3.3 - Spawning a claude-provider agent whose definition relies on native subagents logs an explicit diagnostic. test: `tests/agents/test_spawn_executor.py::test_native_subagent_strip_warns`.
- 6.3.4 - Timeout retains the run's terminal result and wakes only the direct parent session. test: `tests/agents/test_adversary_timeout.py::test_wake_isolation_and_result_retention`.

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

### 6.5 Immutable requirements bundle [category: code] (depends: 6.2, 6.3)
`kind: deliverable`

Target: `src/gobby/plans/review_requirements.py` (new module), `src/gobby/plans/review_evidence_io.py`, `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`, `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`, `src/gobby/install/shared/skills/plan-review/SKILL.md`

Canonical requirements live outside the plan artifact and today none of them
are part of the hashed evidence, so the `requirements_traceability` lane can
attest plan-to-prompt parity while never seeing user-owned requirements, or
attest against sources that changed mid-round.

The **authoritative source universe** is defined here, or two conforming
implementations snapshot different requirements and both pass their tests. It
is exactly: (a) the parent task's description, or for a taskless interactive
review the initiating session request, plus (b) documents the plan explicitly
designates as requirement sources — a plan cites many research documents,
sibling plans, contracts, and test files that are references, not
requirements, and nothing may promote a reference into a requirement
implicitly.

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

**Acceptance:**

- 6.5.3 - Both adversary definitions and the review skill direct the traceability lane to the bundle's requirement IDs, and the staged definition no longer declares live parent-task context canonical. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.
- 6.5.4 - The marker parser is exercised against canonical, duplicate, fenced-code, outside-project, missing, unreadable, malformed, and ordinary-reference fixtures; only declared sources enter the bundle and malformed markers fail preparation closed. test: `tests/plans/test_review_requirements.py::test_requirement_marker_grammar`.
- 6.5.5 - A stage-native review with no live task access still traces requirements from the immutable bundle. test: `tests/plans/test_review_requirements.py::test_stage_native_no_live_task_access`.

## P7: Minimal repair and telemetry
`kind: framing`

**Goal**: Remedies are sized to the defect, and convergence becomes measurable.

### 7.1 Split defect from minimal repair [category: code] (depends: 1.1, 4.3)
`kind: deliverable`

Target: `src/gobby/plans/review_findings.py`, `src/gobby/install/shared/skills/plan-review/SKILL.md`

8 of 17 remedies in one wiki-plan round were larger than the minimal correct
repair (artifact grew 28 → 38 sections), while `#9602` fixed a fence with
less mechanism than proposed. In the finding schema, require
`minimal_repair`: the smallest edit to **existing** sections that closes the
failure trace. A remedy proposing a new deliverable must justify why no
existing host section can own the obligation. Update the skill's finding
schema accordingly.

**Acceptance:**

- 7.1.1 - Findings carry a validated `minimal_repair` field; a new-deliverable remedy without host-section justification is rejected. test: `tests/plans/test_review_findings.py::test_minimal_repair_required`.

### 7.2 Convergence telemetry [category: code] (depends: 4.3, 5.3, 6.3, 7.1)
`kind: deliverable`

Target: `src/gobby/plans/review_telemetry.py` (new module), `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, `src/gobby/mcp_proxy/tools/agents_termination.py`, `src/gobby/agents/agent_health.py`, `src/gobby/agents/agent_cleanup.py`, `src/gobby/hooks/session_coordinator.py`, `src/gobby/hooks/event_handlers/_session_end.py`

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
  finding/ledger IDs and the inputs behind the classification. These ride a
  `convergence_telemetry` object inside the canonical `round_result` under a
  strict published schema (extending 5.3's wrapper), validated by
  `review_telemetry.py` at the evidence-bound finalize.
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
is woken.

Two further routes reach terminal state **without** `end_agent_run` and would
otherwise bypass both this enrichment and 3.1's canonical-result guard
entirely: `SessionCoordinator.complete_agent_run`
(`hooks/session_coordinator.py:498`) marks a run successful on SESSION_END
after nonzero activity and notifies the parent, and workflow termination
completes a run through its own path. A reviewer whose provider simply exits
would therefore be recorded as a clean success having delivered nothing —
exactly the hole P3 exists to close, reached by a different door. Both routes
delegate to the same terminal helper for evidence-bound runs: if the result is
already in the delivered state it is enriched normally, otherwise the run fails
and its evidence expires. Runs with no bound plan-review evidence keep their
current behavior unchanged.

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
- 7.2.6 - The delivered state validates reviewer-owned fields only, so the completion guard accepts a result before enrichment; finalization requires the enriched state and rejects missing daemon fields. Every terminal path routes through the one extracted helper. test: `tests/plans/test_review_telemetry.py::test_delivered_and_enriched_states`.
- 7.2.7 - A reviewer whose provider exits at SESSION_END without calling `end_agent_run`, and one completed by workflow termination, both fail and expire their evidence rather than being recorded as successes; a run with no bound evidence is unaffected. test: `tests/agents/test_terminal_paths.py::test_session_end_and_workflow_termination_do_not_bypass_guard`.

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
- 8.1.4 - Every upstream deliverable's acceptance passes against its module and the MCP wrapper alone, with this file's wiring absent; the check enumerates each upstream leaf. test: `tests/plans/test_review_evidence.py::test_upstream_leaves_close_independently`.
- 8.1.5 - Preparation persists the whole `prior_round_context` atomically; a failure mid-preparation leaves no partially populated context and no evidence row, and a later read reconstructs it from the row alone after the live sources have changed. test: `tests/plans/test_review_evidence.py::test_prior_round_context_atomic_and_source_independent`.
- 8.1.6 - A non-empty consumer inventory succeeds on its first preparation call with no coordinator pre-disposition, and its sites appear in the required sweep universe. test: `tests/plans/test_review_evidence.py::test_inventory_first_call_succeeds`.
- 8.1.7 - The settled `index_token` is stored in `prior_round_context` and survives a restart round-trip; a mismatch between inventory time and verification time is detected. test: `tests/plans/test_review_evidence.py::test_index_token_persistence`.
- 8.1.8 - Preparation snapshots the requirements bundle into the evidence row, and traceability succeeds from that bundle alone when live task access is unavailable. test: `tests/plans/test_review_evidence.py::test_requirements_bundle_persisted_and_sufficient`.
- 8.1.9 - Finalization persists the merged quality ledger and the next round's preparation reads it back. test: `tests/plans/test_review_evidence.py::test_ledger_round_trip_through_finalize`.
- 8.1.10 - The evidence-bound finalize path rejects a round result whose findings fail the finding validator and refuses approval while any blocking finding remains. test: `tests/plans/test_review_evidence.py::test_finalize_validates_findings_and_blocks_approval`.
- 8.1.11 - Each finalized round validates and persists the `convergence_telemetry` object in `round_result` and reads it back durably, with repeated check keys counted across rounds. test: `tests/plans/test_review_evidence.py::test_telemetry_persisted_at_finalize`.

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
is a new helper colocated there that lifts the `call_tool(...)`-bearing
directive line out of the original full reason before the collapse discards
it. Rejected alternatives: exempting whole rules from collapse (loses the
savings on genuinely repetitive gates); an `always_verbose:` block-effect
field (more surface than the problem warrants).

**Acceptance:**

- 9.1.1 - A collapsed repeat block still carries its one-line recovery directive. test: `tests/workflows/test_block_rendering.py::test_collapsed_reason_keeps_directive`.
- 9.1.2 - Reasons without an actionable directive collapse to the bare stub unchanged. test: `tests/workflows/test_block_rendering.py::test_no_directive_collapses_clean`.

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

Unit and integration suites named per deliverable, plus:

- **Live regression target:** run `/gobby plan` against
  `.gobby/plans/context-mode-borrowings.md` — the plan that ran 22 rounds
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

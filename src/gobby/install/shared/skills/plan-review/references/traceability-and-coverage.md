# Traceability and coverage

## Review Stance

The review's job is to find what the drafter missed — missing requirements,
bad sequencing, unhandled edge cases, weak testability, traceability gaps —
not to rubber-stamp the plan.

The adversary mandate is semantic and architectural work. Mechanical validator
classes belong to the deterministic sweep; surface one only when the supplied
report is stale, incomplete, or contradicted by a spot-check.

Use a precise, professional tone. No profanity, no personal attacks, no editorial
filler. Every finding must be concrete and actionable.

### No finding quotas

Do not manufacture findings to hit a target count. If a methodical walk of the
Method and Traceability sections finds nothing, approve the plan cleanly — that
is the correct outcome. Equally, finish the walk rather than stopping at
"enough" findings.

### Bias toward "what's missing"

The drafter already knows what they wrote. You add value by surfacing what they
**did not** write:

- Requirements from the plan's governing context — repository documents,
  Gobby tasks, the plan's own Overview and Constraints — that the body never
  addresses.
- Edge cases or failure paths the plan never handles.
- Steps that assume a precondition the plan never establishes.
- Tests or observability the plan silently omits.

### Recalled-lesson pass

At review start, call
`gobby-review-learning.recall_review_lessons_by_class` with
`lesson_domain=plan` and `lesson_types=["reviewer-miss"]`. Treat every recalled
lesson as a mandatory extra review pass after the standard checks: apply its
`check_key` to the current plan and emit any resulting finding in the typed
schema below. Complete this pass even when ordinary review finds no defects.

---

## Method: Walk Every Branch

Review the plan **mechanically**, not by intuition. Enumerate and walk:

1. **Control-flow branches** implied by the plan — conditionals, loops, error
   handlers, early returns, retries, timeouts. For each one, ask: does the plan
   say what happens in the un-happy case?
2. **Boundary transitions** — phase-to-phase handoffs, data-shape changes
   between steps, state transitions, concurrency boundaries. For each one, ask:
   does the plan specify the interface/contract, or does it paper over the seam?
3. **Input ranges** — empty, null, very large, malformed, adversarial,
   duplicate. For each one, ask: does the plan guard the input or implicitly
   trust it?
4. **Failure modes** — what happens if a dependency is down, a prior phase
   failed, a migration is partially applied, a race loses?
5. **Scope collisions** — two tasks that both touch the same file, two phases
   that both assume they own the same resource, locks without owners.

Catalog **only un-addressed paths**. Silently discard anything the plan already
handles — do not list "the plan correctly handles X" findings.

---

## Traceability

Cross-reference the plan against its governing requirements context, read
directly from the repository and Gobby tasks.

- **Sources of truth:** the plan artifact's own `## Overview` and
  `## Constraints`, every repository document the plan cites (design contracts,
  `docs/contracts/*`, `.impeccable.md`, and similar), and — when the round is
  bound to a task — the parent task's fields via `gobby-tasks:get_task`. You
  have free read access to all of them; use it. A requirements gap, ambiguity,
  or conflict you cannot resolve from those sources is a finding, never a
  reason to halt.
- For every governing requirement, find the plan item(s) that address it.
Requirements with **no matching task** are `missing-requirement` findings.
- For every `### N.N` task in the plan, name the requirement it satisfies.
Tasks with **no corresponding requirement** are `traceability` findings —
either the requirement is implicit (and needs to be added) or the task is
out of scope.
- Walk phase dependencies the same way: cross-phase `(depends: Phase N)` links
  must correspond to a real precondition — flag gratuitous phase gating or
  missing gating where Phase K obviously requires Phase J output.

---

## Gobby-specific Checks

These are contract-level and fail-fast. Flag any of them as `blocking`:

- **No duplicate/filler TDD-wrapper test tasks** — drafts must not contain
  `[TDD]` / `[IMPL]` / `[REF]` prefixes, "Write tests for...", "Ensure tests
  pass", or sibling test tasks whose only purpose is testing a `code` or
  `config` deliverable. Expansion emits one leaf per manifest entry and uses
  `tdd: true` metadata plus the `test-driven-development` skill for required
  TDD work. Standalone
  `category: test` deliverables are valid explicit test tasks only when they
  carry their own target, acceptance criteria, and test-infrastructure, parity,
  characterization, or regression purpose.
- **Concrete symbol-scoped Targets** — every deliverable specifies each changed
  file in its Targets block. Existing symbol-bearing files use exact indexed
  qualified names regardless of category, or one justified `::*` entry. Vague
  tasks like "update the backend" are un-actionable.
- **Valid expansion categories** — every `### N.N` implementation-plan
  deliverable carries one of: `code`, `config`, `docs`, `refactor`, `test`.
  `research`, `planning`, and `manual` are valid for direct task creation
  outside expansion manifests, but approved-plan expansion must be
  development-forward.
- **Code domain routing** — every code deliverable must be resolvable to
  `implementation_domain: backend | frontend | fullstack` in the final manifest.
  Missing domains block expansion because agent routing is deterministic.
- **Phase heading syntax** — every phase uses the canonical `## P<N>: Name`
  form. Headings such as `## Phase 1: Name` or `## 1: Name` are **silently
  skipped** by the expansion parser — the phase's tasks disappear.
- **Acyclic, well-referenced dependency tree** — `(depends: X)` refs must point
  at phases or tasks that actually exist in this plan; cycles are rejected.
- **Self-contained task sections** — each `### N.N` body must contain enough
  detail (file paths, code examples, behavioral specs) for an agent who sees
  ONLY that section to do the work. The implementing agent does **not** get
  the full plan document. A section that says "see Phase 1 for context" is a
  blocking finding.

---

## Proportionality (Over-Engineering) Check

The checks above surface what the plan *omits* (missing requirements, edge
cases, weak tests) and what it puts *out of scope* (traceability). You must also
check the **opposite** failure: a deliverable that builds **more mechanism than
its goal justifies** — a Rube Goldberg machine. This is the `over-engineering`
finding category, and it closes the gap where plan-review historically caught
under-built plans but never over-built ones.

The adversary agent loads the shared **`proportionality`** skill alongside this
one (`get_skill(name="proportionality")`). That skill is the single source of
truth for the criterion; apply it here at **plan altitude**, where the unit
under review is a **deliverable section**.

**Criterion — justification, not minimization.** You are not asking "is this the
smallest possible plan?" Ambition, size, and large or complex epics are **never
findings on their own** — do not punish reach. Flag only machinery with **no
concrete consumer or stated requirement anywhere in the plan**:

- a deliverable that builds a subsystem, registry, service, factory, framework,
  or abstraction layer for a caller that does not exist in this plan;
- config fields, flags, profile knobs, or env settings introduced with exactly
  one value and no second consumer named;
- indirection (wrappers, adapters, event hops) that adds call depth without
  adding a capability, boundary, or testability anyone in the plan needs;
- a new dependency where an existing Gobby utility already covers every case.

Apply the justification test per deliverable: is there a concrete consumer in
*this* plan? is the simplest direct approach provably insufficient? does intent
reach effect in ≤2–3 indirection hops? is it explainable without "we might
later…"? Any "no" → emit an `over-engineering` finding and **name the simpler
form explicitly** ("replace the `FooRegistry` with a module-level dict", "drop
the `enable_x` flag and inline the behavior"). A finding that does not name the
simpler alternative is incomplete.

**Severity:**

- `blocking` for a **structural** Rube Goldberg — a speculative subsystem,
  framework, or abstraction the rest of the plan would build on. Simplify these
  **before expansion**, because every downstream leaf inherits the over-built
  shape.
- `nit` for **ceremony** — a one-off knob, a single redundant wrapper, mild
  gold-plating that is cheap to simplify in place and does not distort the rest
  of the plan.

**Do not over-flag.** A false over-engineering finding discourages legitimate
ambition, which is the harm to avoid. A large-but-justified epic where every
deliverable names its consumer produces **zero** proportionality findings.
Justified complexity — error handling for real failure modes, structure a stated
requirement demands, extensibility with a named future consumer — is retained,
never flagged. When justification is plausible, do not flag.

---

## Exhaustive Three-Lane Coverage

Every round completes exactly three lanes against the immutable evidence
snapshot:

1. `requirements_traceability` — requirements, acceptance coverage,
   dependencies, and manifest parity.
2. `repository_blast_radius` — spot-check the deterministic sweep report against
   exact file-qualified Targets and a bounded sample of gcode callers, consumers,
   implementations, test seams, registry inventories, and source-size constraints.
   Mark this lane `delegated-verified` only after its report and sample agree.
3. `runtime_invariants` — inputs, outcomes, state transitions, wrappers,
   sync/async boundaries, retries, races, bounds, serialization, and recovery.

Call `get_plan_review_snapshot(evidence_id)` once. It returns one complete,
decoded, immutable snapshot containing the plan sections and review metadata.
Do not reread the plan file during the round.

Run the three lanes concurrently as read-only provider-native internal
subagents through the current CLI/runtime's collaboration facility. Give each
lane only its scope, the immutable snapshot, direct read access to repository
and task context, and the candidate schema below. Forbid file edits,
task/service mutation, findings, manifests, evidence finalization, and
verdicts. Never use `gobby-agents:spawn_agent` for lane research. Capacity
shortage, subagent failure, or malformed output moves only that lane to
sequential parent review; the round still completes.

Internal subagents return candidates; the parent adversary is the sole evidence,
finding, manifest, coverage-attestation, and verdict owner.
The requirements and runtime lanes return `status: completed`; the repository
lane returns `status: delegated-verified`. Each lane names every deliverable in
`section_ids_checked`, supplies hashed source citations, and returns candidate issues with stable
candidate id, affected section ids, violated invariant, suggested fix,
adjacent sites checked, confidence, and citations. The parent must:

1. Verify and deduplicate every candidate.
2. Record exactly one `emitted_finding` or `dismissed` entry in
   `candidate_dispositions`, with a reason.
3. Perform a cross-lane interaction pass.
4. Complete a class-wide adjacent-variant sweep.

Call `derive_plan_review_manifest` during every round, including rejection.
Then call `validate_plan_review_coverage` with the three lane results, all
candidate dispositions, and the exact shadow-manifest status returned by
derivation. The returned `coverage_attestation` is mandatory in
`round_result`; it contains two completed lanes plus the delegated-verified
repository lane, the source digest,
disposition counts, cross-lane and adjacent-variant completion, and
shadow-manifest status.

**Verbatim or fail loudly.** Embed the exact JSON object
`validate_plan_review_coverage` returned — `version`, lane objects carrying
`lane_id`/`status`/`candidate_count`, `source_digest`, `disposition_counts`,
`cross_lane_interaction_complete`, `adjacent_variant_complete`,
`shadow_manifest_status`, and `attestation_digest`, byte-for-byte. A
hand-built or summarized attestation — lanes as plain strings, a
`sections_checked` count, a missing `attestation_digest` — is not a lesser
attestation; it is an invalid round result. The coordinator's finalization
rejects it with `invalid_coverage_attestation`, the round never counts toward
`completed_plan_review_rounds`, and the evidence is expired, discarding the
review's entire cost. When `validate_plan_review_coverage` cannot be
completed, never substitute a reconstruction: return a protocol-failure
result with no `verdict`, a `protocol_failure` field naming the failed tool
call and its exact error, and your draft findings, so the coordinator can
expire the attempt honestly while salvaging the analysis.

When `validate_plan_review_coverage` succeeds, the terminal result is one JSON
object with `verdict: approved` or `verdict: needs_review`, plus `findings` and
`coverage_attestation`. Approved results also include the exact
`manifest_entries` and `routing_decisions`. A protocol-failure terminal result
omits `verdict` and retains its `protocol_failure` field, exact tool error, and
draft findings.

---

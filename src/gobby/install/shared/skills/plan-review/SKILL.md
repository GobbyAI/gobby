---
name: plan-review
description: Review a gobby plan document for missing requirements, bad sequencing, unhandled edge cases, weak testability, and traceability gaps. Use when asked to review or critique a plan.
version: "1.4.0"
category: methodology
internal: true
triggers: plan review, plan critique, adversarial review, plan audit
metadata:
  gobby:
    audience: all
    depth: 0
---

# plan-review — Gobby Plan Adversarial Review Methodology

> Internal methodology skill; loaded with `get_skill(name="plan-review")` from `/gobby plan` and autonomous agents. Not a user-facing command.
>
> Load `restraint` first (`get_skill(name="restraint")`) alongside
> `proportionality`. Every proposed `fix` walks the restraint decision ladder
> and names the lowest rung that fully repairs the defect; a fix that adds
> mechanism around an already-sufficient design is an `over-engineering`
> finding, never a repair.

This skill is the single source of truth for **how to review a gobby plan**.

It is consumed from two places:

- **Interactive:** the `plan` skill uses this methodology during its
  adversarial review loop.
- **Autonomous:** the spawned `plan-adversary-taskless` agent loads this as its
  first action so every adversary run uses the same heuristics. The legacy
  `plan-adversary` lifecycle agent keeps the same qualitative review rules for
  older stage-native planning flows.

A plan that passes this review is ready for `gobby build` handoff.

---

## The Canonical Artifact

`.gobby/plans/<slug>.md` is the plan. Nothing else is.

Review the artifact at the path the coordinator supplies, and confirm it is the
canonical one before reading it. Scratchpad copies, provider plan-mode files
(for example `~/.claude/plans/*.md`), pasted plan bodies, and any file outside
the project's `.gobby/plans/` directory are display or working copies. They go
stale silently — a mirror drifting dozens of lines behind the artifact while
both look complete is the normal failure, not an exotic one. Reviewing one
produces findings against text nobody will ship. If the supplied path is not a
canonical artifact, say so and stop rather than reviewing the copy.

Preparation pins this for you: `prepare_plan_review_round` normalizes the plan
path inside the project root, rejects symlinks and escapes, and binds the
evidence row to exactly one repository-relative path, so a later call naming a
different file is refused. Your reviewed bytes come from that pinned path.

**You never edit the plan.** Not the artifact, not a copy, not to demonstrate a
fix. You return findings; the coordinator applies them and owns every byte that
changes. Editing the artifact mid-round also invalidates the round, because
approval re-verifies the reviewed sections against the sealed snapshot.

---

## Plan-Coverage Contract Gate

Mechanical parser rejection happens upstream of the adversary. Plan-authoring
sessions run `uv run gobby plans validate <plan-file>` before resubmission;
the planner/adversary spawn gate also calls the same internal validator before
every adversary spawn. The validator calls `parse_plan(..., parse_mode="draft")`
internally and blocks the spawn on any contract violation. By the time the
adversary is invoked, the typed grammar has already passed the draft-mode
contract gate — re-running the parser pre-verdict is structural duplication
that wastes a spawn round on syntax the planner already cleared.

The adversary's approval gate validates the derived typed manifest entries
against the expansion contract before returning them (see Manifest Handoff
below). The coordinator's `apply_plan_review_manifest` call performs the
authoritative render plus expansion parse before its atomic write.

The rejection-message vocabulary and post-parse semantic checks below remain
authoritative for qualitative findings. When surfacing a contract violation
the planner gate missed — or one the parser cannot detect mechanically
(table-row decomposition, traceability gaps) — cite the exact rejection cause
from the table.

The planner-side gate and `gobby plans validate` also run deterministic semantic
lint. `target-coverage` and conservative `table-row-decomposition` failures are
mechanical validator failures, not qualitative review findings. If one appears
in your prompt or task history, require the planner to check the whole plan for
that same failure class before resubmission.

Project-aware validation also requires
`symbol_validation.status: passed`. It hashes each existing target file and
compares that SHA-256 with the index before trusting symbols. Exact Targets must
equal gcode `qualified_name` values in the declared file; bare paths are limited
to new or zero-symbol files; `::*` requires a non-empty `scope-reason`; symbol
UUIDs and wildcard/exact mixtures are rejected. `planner` and `plan-enhancer`
may receive these diagnostics so they can repair the artifact.
`plan-adversary`, expansion, and execution stay blocked until they pass.

When checking a changed symbol, resolve the plan's exact file-qualified Target
first with `gcode search-symbol`. Confirm the displayed `qualified_name` in that
file before using `gcode usages` or `gcode blast-radius` for consumer and
blast-radius research. A broad graph search cannot repair an unresolved
canonical Target.

The canonical heading regex and full contract grammar live in the `plan-draft`
skill (the contract's authoring surface) and `docs/contracts/plan-coverage.md`
— do not restate them here; load `plan-draft` when a finding requires quoting
grammar.

The documented rejection message MUST name the failing cause. Reject on these
nine cases:

| Cause | Rejection message |
| --- | --- |
| missing ID | `Plan-Coverage Contract rejection: missing ID` |
| missing kind | `Plan-Coverage Contract rejection: missing kind` |
| missing acceptance | `Plan-Coverage Contract rejection: missing acceptance` |
| ID collision | `Plan-Coverage Contract rejection: ID collision` |
| malformed item ID | `Plan-Coverage Contract rejection: malformed item ID` |
| malformed deferral | `Plan-Coverage Contract rejection: malformed deferral` |
| zero artifact references | `Plan-Coverage Contract rejection: zero artifact references` |
| phases missing | `Plan-Coverage Contract rejection: phases missing` |
| table-row decomposition | `Plan-Coverage Contract rejection: table-row decomposition` |

Mechanical parser-level rejection covers the first seven cases (enforced
upstream by the draft-mode contract gate; the full grammar lives in
`plan-draft` and `docs/contracts/plan-coverage.md`):

- A heading at level `##` through `######` does not match the canonical regex
  in strict implementation-parse mode.
- A section has no `kind:` front-matter line.
- A `deliverable` section has no `**Acceptance:**` block.
- An acceptance item has zero artifact references. At least one of `file:`,
  `symbol:`, `test:`, or `behavior:` is required.
- An acceptance item ID does not dotted-prefix-match its section ID.
- A duplicate section ID appears anywhere in the document.
- A `deferred` section has a malformed deferral object. Required fields are
  `task_ref`, `reason`, `owner`, and `original_acceptance_items`; the referenced
  task must be open and carry `deferred-from:<plan-id>:<section-id>`.

The eighth rejection ("phases missing") is a post-parse semantic check, not a
parser-level rejection: in `parse_mode="draft"` the parser silently drops
headings that do not match the canonical regex, so a plan authored to the
pre-contract template (`## Phase 1: Setup`) parses without error but produces
zero phase sections. After parsing, count sections whose ID matches the
contract phase regex `^P\d+$` (`_CONTRACT_PHASE_ID_RE` in
`src/gobby/tasks/expansion/_common.py`). The expansion compiler cannot build
the phase hierarchy without phases, so `validate_plan_file`
(`src/gobby/tasks/expansion/_validate.py`) blocks adversary spawn for any plan
with one or more `kind: deliverable` sections but zero phase sections.

The ninth rejection is a conservative semantic-lint check. Any `deliverable`
section whose body uses a markdown table to enumerate work items MUST emit one
acceptance item per table data row with stable IDs. The validator blocks a
deliverable whose acceptance-item count is lower than its table data-row count.
The rejection should name the missing rows so the planner can add the omitted
acceptance items instead of rewriting unrelated table text.
For ambiguous tables the validator does not hard-block, but the qualitative
review should still cite "table-row decomposition" when the plan under-specifies
work rows.

---

## Review Stance

The review's job is to find what the drafter missed — missing requirements,
bad sequencing, unhandled edge cases, weak testability, traceability gaps —
not to rubber-stamp the plan.

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
2. `repository_blast_radius` — resolve exact file-qualified Targets first, then
   use gcode to sweep callers, consumers, constructors, destructures,
   implementations, fakes, exhaustive matches, test seams, migration/registry
   inventories, and source-size constraints.
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
Each completed lane names every deliverable in `section_ids_checked`, supplies
hashed source citations, and returns candidate issues with stable
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
`round_result`; it contains exactly three completed lanes, the source digest,
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

## Manifest Handoff on Approval

`## M1 Task Manifest` is the typed bridge between deliverable sections and the
leaves the deterministic compiler emits. The adversary records reviewed routing
decisions. `derive_plan_review_manifest` generates canonical entries, including
titles, source sections, exact covers labels, and validation criteria. Each
criteria string contains every covered acceptance item in source order as
`<item-id>: <full acceptance text>`.

See `docs/contracts/plan-coverage.md` (§ "Task Manifest") for the entry schema
and parser-enforced invariants. This skill covers the adversary's handoff
responsibility; the coordinator owns compare-and-apply.

### Plan Identity Precondition

Before any manifest handoff or approval, verify the plan text outside fenced code
blocks contains a real `**Plan ID:** <id>` marker. Missing, blank, or literal
`unknown` Plan IDs are blocking findings because they generate
`covers:unknown:*` labels. Reject any existing or generated manifest that
contains a label beginning `covers:unknown:` before approving the plan.

### Sequence on Clean Review

When no blocking findings remain (zero findings or only nits):

1. Re-check the Plan Identity Precondition above.
2. Record routing decisions for each deliverable: category, task type,
dependencies, TDD, and assigned agent or implementation domain.
3. Call `derive_plan_review_manifest` with those decisions. Treat typed
diagnostics as rejection evidence. Never summarize or hand-author server-owned
labels or validation criteria.
4. Call `validate_plan_review_coverage`, then return `verdict: approved` with
the exact routing decisions, derived `manifest_entries`, and canonical
`coverage_attestation`. Entry count alone is insufficient.

### Plan-File Write Scope

The adversary never writes the plan file. `apply_plan_review_manifest`
re-derives entries from the evidence snapshot and routing decisions, rejects
any differing payload, revalidates freshness, and performs the only manifest
write. Rejection returns typed findings plus shadow-manifest diagnostics; typed
`repairs` on those findings are payload, written only by the coordinator's
`apply_plan_review_repairs` after the vote and the finalized checkpoint. The
coordinator owns `## V1 Plan Changelog`, writing round entries only through
`append_plan_changelog_round`, and the planner owns revisions.

## Escalation Policy

Findings carry one severity:

- `blocking` — plan expansion must wait for a repair.
- `nit` — useful non-blocking guidance.

Escalate **only when context is insufficient or a true human-intervention blocker exists**.
For routine revision rounds, return a non-approval verdict instead:

- If ≥1 `blocking` finding after the second pass → return
  `verdict: needs_review` with formatted findings.
- If only `nit` findings remain → return `verdict: approved`.
- If zero findings after the second pass → approve cleanly.

Non-blocking nits never trigger escalation on their own.

---

## Output Format

When rejecting review, return findings under a **round-scoped** heading:

```text
## Adversary Findings — Round N
```

`N` is the **display round** (1-indexed, matching the adversary prompt and the
UI). First round is `Round 1`, second is `Round 2`, etc. Include
`round_number: N` in the structured taskless result so the parent can record it
without parsing prose.

### Preserve prior rounds

**Do not overwrite or delete previous rounds' sections.** The parent session
preserves every round in `## V1 Plan Changelog` for audit.

### Finding schema

Each finding is one typed attestation with these fields:

- **finding_id** — stable across retries of the same round.
- **section_id** — primary section anchor; it must exist in the prepared
  evidence manifest.
- **check_key** — stable review check identity. Reuse keys returned by
  `list_check_keys`.
- **severity** — `blocking` or `nit`.
- **category** — one of:
- `missing-requirement`
- `bad-sequencing`
- `unhandled-edge`
- `weak-testability`
- `traceability`
- `over-engineering`
- `gobby-format`
- **principle** / **root_cause** — at least one must be non-empty. Both may be
  present; each has one distinct wire field.
- **prevention** — concrete checklist action that would catch recurrence.
- **location** — human-readable phase/task reference.
- **description** — one short paragraph; what is wrong or missing.
- **fix** — one short paragraph; what the drafter should add or change.

When claiming `reviewer-miss`, add non-empty
`participating_section_ids` containing every section that participates in the
missed defect. When claiming `fixer-induced-defect`, add
`introduced_in_round`, `causal_finding_id`, and non-empty
`causal_section_ids` containing every section changed by the causal fix.
`causal_finding_id` names the prior round's causal finding; never overload this
finding's own `finding_id`.

Section sets carry ids only. Hashes remain server-resolved. Reject an attestation
when any id is absent from the prepared evidence manifest or when a
class-required set is empty. A finding may carry both classes only when both
evidence bundles are complete.

### Repair class vs design class

A finding is **repair class** when its fix is a mechanical plan edit the
coordinator can apply after the vote: a missing Targets entry, a missing
`(depends: …)` edge, or a missing acceptance item. Everything else is
**design class** and stays prose: the planner decides how to redesign,
resequence, or scope it in the next round.

Vote outcomes at disposition are `accept`, `decline`, and
`decline: over-mechanism`. The last is judged on the `restraint` decision
ladder by whoever votes — the user interactively, the coordinator unattended:
a finding whose fix adds mechanism around a design that already fully solves
the problem is declined with the rung it fails at, and the declined finding is
recorded in the checkpoint like any other. The usual trigger is a
fixer-induced chain — a finding on the previous round's repair that asks for
more machinery around it.

Repair-class findings carry an optional `repairs` list. The category matrix
governs which kinds a category may carry; a category absent from the table
forbids `repairs` entirely, and the validator rejects any violation with
`invalid_round_result`:

| Category | Allowed repair kinds |
| --- | --- |
| `traceability` | `add_targets`, `add_acceptance` |
| `bad-sequencing` | `add_dependency` |
| `weak-testability` | `add_acceptance` |
| `gobby-format` | `add_targets`, `add_dependency`, `add_acceptance` |
| `missing-requirement`, `unhandled-edge`, `over-engineering` | none — design class |

Payload schema (every `section_id` must exist in the evidence manifest; it may
differ from the finding's own `section_id`):

- `{kind: add_targets, section_id, entries: [<Targets line>]}` — each entry
  parses as exactly one target with zero issues (`path::qualified_name`,
  `path::* — scope-reason: …`, or a bare path for a new file); entries are
  unique by reference.
- `{kind: add_dependency, section_id, on: [<section-id>]}` — unique refs from
  the manifest, none equal to `section_id`.
- `{kind: add_acceptance, section_id, items: [{prose, artifact}]}` — both
  single-line and non-empty; `artifact` starts with `file:`, `symbol:`,
  `test:`, or `behavior:`.

Closed loop: a repair satisfies the reviewer's own check, so the next round's
fresh reviewer re-runs that check against the repaired artifact rather than
trusting the repair. Design-class repairs never ride on `repairs`; write them
in `fix` for the planner. The adversary never applies repairs —
`apply_plan_review_repairs` is coordinator-only and runs after the rejection
checkpoint is finalized, so the checkpoint records the reviewed artifact and
the next snapshot records the repaired one.

Example repair-class finding:

```yaml
finding_id: F3
section_id: "1.2"
check_key: targets-complete
severity: blocking
category: traceability
root_cause: Only directly edited files were inventoried.
prevention: Run gcode usages for every exact symbol Target.
location: Phase 1 / § 1.2 Targets
description: The consumer of `build_widget` is missing from Targets.
fix: Add the consumer file and an acceptance item covering its update.
repairs:
  - kind: add_targets
    section_id: "1.2"
    entries: ["`src/module/consumer.py::use_widget`"]
  - kind: add_acceptance
    section_id: "1.2"
    items:
      - prose: Consumer updated for the new signature
        artifact: "test: `tests/test_consumer.py::test_use_widget`"
```

### Example

````markdown
## Adversary Findings — Round 1

### F1 — blocking — unhandled-edge — Phase 2 / § 2.4

```yaml
finding_id: F1
section_id: "2.4"
check_key: lock-contention
severity: blocking
category: unhandled-edge
principle: Every reachable lock outcome needs an explicit policy.
root_cause: The task specified only the successful acquisition path.
prevention: Check success, contention, timeout, and dependency-failure paths.
location: Phase 2 / § 2.4
description: The lock-held and timeout branches are unspecified.
fix: Add retry, bail-out, and caller-visible failure behavior.
```
````

---

## Halt Conditions

Preparation rejects a missing, empty, or structurally invalid plan before the
adversary runs. Requirements context that remains unresolved after direct
repository and task inspection becomes a blocking `missing-requirement`
finding carrying the specific questions the plan must answer.

Do **not** approve a plan you do not understand. When in doubt, emit blocking
findings with your specific unanswered questions rather than manufacturing
findings or rubber-stamping.

## Autonomous Exit

When running as spawned `plan-adversary-taskless`, send the exact JSON result
to the parent first. Legacy `plan-adversary` runs finish the stage-native
verdict first (`approve_review`, `reject_review`, or `escalate_task`), then send
the exact JSON result returned or assembled for that verdict to the parent.
`send_message` durably binds that result to the run. In both paths, call
`end_agent_run` on `gobby-agents` with **no arguments** only after delivery.

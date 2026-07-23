---
name: plan-review
description: Review a gobby plan document for missing requirements, bad sequencing, unhandled edge cases, weak testability, and traceability gaps. Use when asked to review or critique a plan.
version: "1.1.0"
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
lint. `target-coverage`, conservative `table-row-decomposition`, and
index-proven `consumer-sweep` failures are mechanical validator failures, not
qualitative review findings. If one appears in your prompt or task history,
require the planner to sweep the whole plan for that same failure class before
resubmission.

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
(`src/gobby/tasks/expansion/_compile.py`) blocks adversary spawn for any plan
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

## Role & Attitude

You are a rigorous plan reviewer. Your job is to find what the drafter missed —
**missing requirements, bad sequencing, unhandled edge cases, weak testability,
traceability gaps** — not to rubber-stamp the plan.

Use a precise, professional tone. No profanity, no personal attacks, no editorial
filler. Every finding must be concrete and actionable.

### No finding quotas

Do **not** manufacture findings to hit a target count. If the first review pass
finds nothing, do a second pass methodically (walk the Method and Traceability
sections again end-to-end). If the second pass still finds nothing, approve the
plan cleanly — that is the correct outcome.

Conversely, do not stop early because you found "enough." Finish the walk.

### Bias toward "what's missing"

The drafter already knows what they wrote. You add value by surfacing what they
**did not** write:

- Requirements from the parent task that the plan never addresses.
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

Cross-reference the plan against its parent task.

- **Source of truth:** the parent task's description plus any docs it references
  (runbooks, linked issues, design docs). You do **not** require a literal
  `## Requirements` heading in the parent — the description itself is canonical,
  matching the contract in `planner.yaml` and `plan-adversary.yaml`.
- For every requirement in the parent, find the plan item(s) that address it.
  Requirements with **no matching task** are `missing-requirement` findings.
- For every `### N.N` task in the plan, find the requirement it satisfies.
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
- **Concrete target file paths** — every `code` / `config` task must specify a
  file path (`Target: src/foo/bar.py` or inline). Vague tasks like
  "update the backend" are un-actionable.
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

## Manifest Handoff on Approval

`## M1 Task Manifest` is the typed bridge between deliverable sections and the
leaves the deterministic compiler emits. The approving adversary derives the
full typed entries and returns them in `round_result.manifest_entries`. Preserve
complete category and implementation-domain decisions already present in the
narrative.

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
2. Derive one full typed manifest entry per `kind: deliverable` section. Every
entry carries `title`, `source_section`, `covers`, `category`, `priority`,
`task_type`, `tdd`, `labels`, `description`, and `validation_criteria`;
`category: code` also carries
`implementation_domain: backend | frontend | fullstack`.
3. Validate the entries against the plan-coverage contract: every acceptance
item is covered exactly once, dependencies resolve, labels use the real Plan
ID, and every code domain is explicit. Return `verdict: needs_review` with
specific entry errors when this check fails.
4. On success, return `verdict: approved`, the full typed `manifest_entries`
array, and the complete typed findings/attestations for the round. Entry count
alone is insufficient.

### Plan-File Write Scope

The adversary never writes the plan file. Approval returns full typed entries;
the coordinator calls `apply_plan_review_manifest`, which revalidates freshness
and performs the only manifest write. Rejection returns typed findings. The
coordinator owns `## V1 Plan Changelog`, and the planner owns revisions.

---

## Escalation Policy

Findings carry a **severity**:

- `blocking` — the plan should not be expanded until this is fixed.
- `nit` — worth noting, but not a blocker on its own.

Escalate **only when context is insufficient or a true human-intervention blocker exists**.
For routine revision rounds, return a non-approval verdict instead:

- If ≥1 `blocking` finding after the second pass → return
  `verdict: needs_review` with formatted findings.
- If only `nit` findings remain → record them in the findings section so the
  drafter can see them, but return `verdict: approved`.
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
- **suggested fix** — one short paragraph; what the drafter should add or change.

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
suggested_fix: Add retry, bail-out, and caller-visible failure behavior.
```
````

---

## Halt Conditions

Stop and **escalate with `needs_requirements: <concrete missing questions>`**
when:

- The plan artifact file is missing or empty.
- The plan has no canonical `## P<N>` phase sections.
- The parent task description (and any docs it references) does not give you
  enough context to judge whether the plan is correct — write the specific
  questions you cannot answer and escalate.

The `needs_requirements:` escalation contract matches the one `planner.yaml`
uses on the drafting side and remains stable for the stage-native planning flow.

Do **not** approve a plan you do not understand. When in doubt, escalate with
specific questions rather than manufacturing findings or rubber-stamping.

## Autonomous Exit

When running as spawned `plan-adversary-taskless`, send the structured result
to the parent first. Legacy `plan-adversary` runs finish the stage-native
verdict first (`approve_review`, `reject_review`, or `escalate_task`). In both
paths, call `end_agent_run` on `gobby-agents` with **no arguments** to finish
the run.

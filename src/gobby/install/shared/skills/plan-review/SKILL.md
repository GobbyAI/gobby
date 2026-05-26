---
name: plan-review
description: Review a gobby plan document for missing requirements, bad sequencing, unhandled edge cases, weak testability, and traceability gaps. Use when asked to review or critique a plan.
version: "1.0.0"
category: core
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

The adversary's only mechanical gate is the post-approval
`parse_mode="expansion"` self-check on manifest write (see Manifest Emission
below). It validates a different invariant: the appended `## M1 Task Manifest`
is present, schema-correct, and covers every acceptance item exactly once.

The rejection-message vocabulary, canonical heading regex, and post-parse
semantic checks below remain authoritative for qualitative findings. When
surfacing a contract violation the planner gate missed — or one the parser
cannot detect mechanically (table-row decomposition, traceability gaps) —
cite the exact rejection cause from the table.

The planner-side gate and `gobby plans validate` also run deterministic semantic
lint. `target-coverage`, conservative `table-row-decomposition`, and
index-proven `consumer-sweep` failures are mechanical validator failures, not
qualitative review findings. If one appears in your prompt or task history,
require the planner to sweep the whole plan for that same failure class before
resubmission.

Canonical heading regex:

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
```

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

Mechanical parser-level rejection covers the first seven cases:

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

## Manifest Emission on Approval

`## M1 Task Manifest` is the typed bridge between deliverable sections and the
leaves the deterministic compiler emits. The approving adversary may write it;
if the planning agent has already supplied complete category and implementation
domain decisions, preserve those decisions. The deterministic manifest emitter
is only a fallback for missing manifests or legacy drafts where the planning
agents did not assign enough category/domain data.

See `docs/contracts/plan-coverage.md` (§ "Task Manifest") for the entry schema
and parser-enforced invariants. This skill covers the adversary's emission
**responsibility**; the schema lives in the contract.

### Sequence on Clean Review

When no blocking findings remain (zero findings or only nits):

1. Append a `## M1 Task Manifest` section to the end of the plan file with
   `kind: manifest` and a YAML block carrying one entry per `kind: deliverable`
   section. The `M1` heading ID is required by the canonical heading regex.
   Every `category: code` entry must include
   `implementation_domain: backend | frontend | fullstack`.
2. Self-check via `parse_plan(plan_path, parse_mode="expansion")`. Strict
   expansion validates that the manifest is present, schema-correct, and that
   every acceptance item is covered by exactly one entry.
3. On `PlanParseError`, fix the manifest in-place and re-self-check. Cap is
   **3 retries**.
4. If the cap is exhausted in the taskless path, return
   `verdict: needs_review` with the parser error details. Do not approve. The
   parent session records the failed manifest attempt in `## V1 Plan Changelog`
   and revises the plan interactively.
5. On success, return a structured `verdict: approved` result that documents
   the manifest outcome, including entry count and any fallback emitter use.

### Plan-File Write Scope

`Edit` and `Write` are permitted ONLY for the supplied plan file path. Writing
to any other path violates the agent contract. The legitimate plan-file writes
are appending or repairing `## M1 Task Manifest` on approval.

**Rejection rounds MUST NOT edit the plan file.** Plan edits between rounds
are the parent planner's responsibility. When emitting findings, return them in
the structured taskless result so the parent can append them to
`## V1 Plan Changelog`.

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

Each finding is a fenced block (or bullet entry) with these fields:

- **severity** — `blocking` or `nit`
- **category** — one of:
  - `missing-requirement`
  - `bad-sequencing`
  - `unhandled-edge`
  - `weak-testability`
  - `traceability`
  - `gobby-format`
- **location** — phase/task reference (e.g., `Phase 2 / § 2.3` or `Phase header`)
- **description** — one short paragraph; what is wrong or missing.
- **suggested fix** — one short paragraph; what the drafter should add or change.

### Example

```markdown
## Adversary Findings — Round 1

### F1 — blocking — unhandled-edge — Phase 2 / § 2.4

Task 2.4 calls `acquire_lock` but does not describe what happens if the lock
is already held or times out. Both cases are reachable from normal traffic.

**Suggested fix:** add a "Lock contention" subsection to 2.4 specifying the
retry / bail-out policy and the surfacing of the failure to the caller.

### F2 — blocking — gobby-format — Phase header

`## Phase 3 — Wire-up` is not a canonical expansion phase heading. The
canonical form is `## P3: Wire-up`. Update before approval.
```

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

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

> Internal methodology skill; invoked via `get_skill` from `/gobby plan` and autonomous agents. Not a user-facing command.

This skill is the single source of truth for **how to review a gobby plan**.

It is consumed from two places:

- **Interactive:** the `plan` skill loads this during its adversarial review loop.
- **Autonomous:** the spawned `plan-adversary` agent (`plan-adversary.yaml`)
  loads this as its first action so every adversary run uses the same heuristics.

A plan that passes this review is ready for `/gobby expand`.

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

- **No explicit test tasks** — drafts must not contain `[TDD]` / `[IMPL]` /
  `[REF]` prefixes, "Write tests for…", "Ensure tests pass", etc. Expansion
  auto-inserts TDD wrappers. Explicit test tasks break the sandwich and
  duplicate work.
- **Concrete target file paths** — every `code` / `config` task must specify a
  file path (`Target: src/foo/bar.py` or inline). Vague tasks like
  "update the backend" are un-actionable.
- **Valid categories** — every `### N.N` task carries one of: `code`, `config`,
  `docs`, `refactor`, `test`, `research`, `planning`, `manual` (the enum-backed
  canonical set from `VALID_CATEGORIES`). Anything else is silently rejected at
  `create_task` time.
- **Phase heading syntax** — every `## Phase N` uses the canonical
  `## Phase N: Name` form (colon), or one of the tolerated dashes
  (`—`, `–`, `-`). Anything else is **silently skipped** by the expansion
  parser — the phase's tasks disappear.
- **Acyclic, well-referenced dependency tree** — `(depends: X)` refs must point
  at phases or tasks that actually exist in this plan; cycles are rejected.
- **Self-contained task sections** — each `### N.N` body must contain enough
  detail (file paths, code examples, behavioral specs) for an agent who sees
  ONLY that section to do the work. The implementing agent does **not** get
  the full plan document. A section that says "see Phase 1 for context" is a
  blocking finding.

---

## Escalation Policy

Findings carry a **severity**:

- `blocking` — the plan should not be expanded until this is fixed.
- `nit` — worth noting, but not a blocker on its own.

Escalate **only when context is insufficient or a true human-intervention blocker exists**.
For routine revision rounds, reject review instead:

- If ≥1 `blocking` finding after the second pass → call
  `mark_task_review_rejected(task_id=<planning_task>, rejection_notes="<formatted findings>", round=N)`.
  Use the Output Format below for `rejection_notes`; the tool appends the
  `## Adversary Findings — Round N` section and returns the task to `open`.
- If only `nit` findings remain → record them in the findings section so the
  drafter can see them, but **approve** the plan with
  `mark_task_review_approved(task_id=<planning_task>, approval_notes="...")`.
- If zero findings after the second pass → approve cleanly.

Non-blocking nits never trigger escalation on their own.

---

## Output Format

When rejecting review, pass findings in `rejection_notes` so
`mark_task_review_rejected` can append them under a **round-scoped** heading:

```text
## Adversary Findings — Round N
```

`N` is the **display round** (1-indexed, matching the adversary prompt and the
UI). The interactive planner passes `display_round = planning_round_label + 1`
in the prompt — use that exact number. First round is `Round 1`, second is
`Round 2`, etc.

### Preserve prior rounds

**Do not overwrite or delete previous rounds' sections.** The rejection tool
appends the new `## Adversary Findings — Round N` section below any prior ones.
Previous rounds stay in the description for audit.

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

### F2 — nit — gobby-format — Phase header

`## Phase 3 — Wire-up` uses an em-dash. Expansion tolerates it but the
canonical form is `## Phase 3: Wire-up`. Update for consistency.
```

---

## Halt Conditions

Stop and **escalate with `needs_requirements: <concrete missing questions>`**
when:

- The plan artifact file is missing or empty.
- The plan has no `## Phase` sections.
- The parent task description (and any docs it references) does not give you
  enough context to judge whether the plan is correct — write the specific
  questions you cannot answer and escalate.

The `needs_requirements:` escalation contract matches the one `planner.yaml`
uses on the drafting side — the interactive planner and autonomous front-half
both branch on that prefix.

Do **not** approve a plan you do not understand. When in doubt, escalate with
specific questions rather than manufacturing findings or rubber-stamping.

## Autonomous Exit

When running as spawned `plan-adversary`, finish the verdict first
(`mark_task_review_approved` or `escalate_task`), then call
`end_agent_run` on `gobby-agents` with **no arguments** to finish the run.

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

- **Interactive:** the `plan` skill loads this during its adversarial review loop.
- **Autonomous:** the spawned `plan-adversary` agent (`plan-adversary.yaml`)
  loads this as its first action so every adversary run uses the same heuristics.

A plan that passes this review is ready for `/gobby expand`.

---

## Plan-Coverage Contract Gate

Mechanical parser rejection happens upstream of the adversary. The interactive
planner and autonomous front-half both run `validate_plan_file`
(`src/gobby/tasks/expansion/_compile.py`) before every adversary spawn; that
helper calls `parse_plan(..., parse_mode="draft")` internally and blocks the
spawn on any contract violation. By the time the adversary is invoked, the
typed grammar has already passed the draft-mode contract gate — re-running the
parser pre-verdict is structural duplication that wastes a spawn round on
syntax the planner already cleared.

The adversary's only mechanical gate is the post-approval
`parse_mode="expansion"` self-check on manifest write (see Manifest Emission
below). It validates a different invariant: the appended `## M1 Task Manifest`
is present, schema-correct, and covers every acceptance item exactly once.

The rejection-message vocabulary, canonical heading regex, and post-parse
semantic checks below remain authoritative for qualitative findings. When
surfacing a contract violation the planner gate missed — or one the parser
cannot detect mechanically (table-row decomposition, traceability gaps) —
cite the exact rejection cause from the table.

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
`src/gobby/tasks/expansion/_common.py`). The expansion compiler cannot anchor
TDD wrappers without phases, so `validate_plan_file`
(`src/gobby/tasks/expansion/_compile.py`) blocks adversary spawn for any plan
with one or more `kind: deliverable` sections but zero phase sections.

The ninth rejection is qualitative because table intent cannot be parsed
reliably. Any `deliverable` section whose body uses a markdown table to
enumerate work items MUST emit one acceptance item per table data row with
stable IDs. Reject a deliverable whose acceptance-item count is lower than its
table data-row count, cite the missing rows, and name "table-row decomposition"
in the finding.

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

## Manifest Emission on Approval

Plan-adversary is the **sole writer** of the `## M1 Task Manifest` section.
Planners author narrative only; the adversary commits the typed bridge between
deliverables and expansion leaves on the same call where it approves. The act
of emitting the manifest is what forces the adversary to confront ambiguity it
might otherwise wave through — if a manifest entry cannot be written for a
deliverable, the plan is not ready.

See `docs/contracts/plan-coverage.md` (§ "Task Manifest") for the entry schema
and parser-enforced invariants. This skill covers the adversary's emission
**responsibility**; the schema lives in the contract.

### Sequence on Clean Review

When no blocking findings remain (zero findings or only nits):

1. Append a `## M1 Task Manifest` section to the end of the plan file with
   `kind: manifest` and a YAML block carrying one entry per `kind: deliverable`
   section. The `M1` heading ID is required by the canonical heading regex.
2. Self-check via `parse_plan(plan_path, parse_mode="expansion")`. Strict
   expansion validates that the manifest is present, schema-correct, and that
   every acceptance item is covered by exactly one entry.
3. On `PlanParseError`, fix the manifest in-place and re-self-check. Cap is
   **3 retries**.
4. After the cap is exhausted, behavior splits by yolo state on the planning
   anchor task:
   - **non-yolo**: call
     `escalate_task(reason="needs_human:manifest_emission_failure:<details>")`
     with the parser error details. Do NOT approve.
   - **yolo**: do NOT call `escalate_task` — yolo NEVER escalates on this
     path (top-level invariant). Instead:
     - Append a `## Yolo Fallbacks` audit section to the planning anchor's
       description (NOT the plan file) documenting the failure mode.
     - Fall back to the deterministic stub-manifest path. Direct Python form:
       `from gobby.plans.manifest_emitter import emit_stub_manifest`, then
       call `emit_stub_manifest(plan_path)`. If direct imports are unavailable
       in the current surface, call the MCP/tooling wrapper that exposes
       `emit_stub_manifest(plan_path)`. Re-run `parse_plan(plan_path,
       parse_mode="expansion")`.
     - If the stub also fails (the deliverable schema in the plan is malformed
       beyond the emitter's reach), append a second `## Yolo Fallbacks` audit
       marker and force-approve with `mark_task_review_approved` whose
       `approval_notes` document that downstream `gobby expand` will reject
       the plan and require human intervention at expansion time.
5. On success (any path that yields a parser-clean manifest, or the yolo
   force-approve path), call `mark_task_review_approved` with `approval_notes`
   that document the manifest outcome (e.g. "approved with N manifest
   entries").

### Plan-File Write Scope

`Edit` and `Write` are permitted ONLY for the plan file at
`task_artifacts.plan_file_path`. Writing to any other path violates the agent
contract. The only legitimate plan-file write is appending the
`## M1 Task Manifest` section on approval.

**Rejection rounds MUST NOT edit the plan file.** Plan edits between rounds
are the planner's responsibility (§2.23). When emitting findings, route them
through `mark_task_review_rejected(rejection_notes=...)` only — the rejection
tool appends `## Adversary Findings — Round N` to the anchor task description
without touching the plan.

---

## Escalation Policy

Findings carry a **severity**:

- `blocking` — the plan should not be expanded until this is fixed.
- `nit` — worth noting, but not a blocker on its own.

**Anchor-task contract**: the spawn prompt names the **per-round anchor task** the parent created for this review (a child of the planning epic). Mark verdict on that anchor — NOT on the planning epic itself, NOT on the parent epic. The parent reads the anchor's terminal state on daemon-wake and routes from there.

Escalate **only when context is insufficient or a true human-intervention blocker exists**.
For routine revision rounds, reject review instead:

- If ≥1 `blocking` finding after the second pass → call
  `mark_task_review_rejected(task_id=<anchor_task_id>, rejection_notes="<formatted findings>", round_number=N)`.
  Use the Output Format below for `rejection_notes`; the tool appends the
  `## Adversary Findings — Round N` section to the anchor description and
  returns the anchor to `open`. The parent closes the anchor on next wake.
- If only `nit` findings remain → record them in the findings section so the
  drafter can see them, but **approve** the plan with
  `mark_task_review_approved(task_id=<anchor_task_id>, approval_notes="...")`.
- If zero findings after the second pass → approve cleanly on the anchor.

Use the `anchor_task_id` value passed in the spawn prompt; do not infer or fall back to the planning epic.

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
(`mark_task_review_approved`, `mark_task_review_rejected`, or `escalate_task`), then call
`end_agent_run` on `gobby-agents` with **no arguments** to finish the run.

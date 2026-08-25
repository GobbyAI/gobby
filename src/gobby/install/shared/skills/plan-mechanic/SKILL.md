---
name: plan-mechanic
description: Use when a gobby plan fails `gobby plans validate`, or after any pass that edited the plan (draft, enhancement fold-in, semantic repair, cut), and it needs bounded mechanical repair before the next adversary round.
version: "1.0.0"
category: methodology
internal: true
triggers: plan mechanic, mechanical plan repair, plan validate failure, fix plan targets, plan-mechanic
metadata:
  gobby:
    audience: all
    depth: 0
---

# plan-mechanic — Bounded Mechanical Plan Repair

> Internal methodology skill; loaded with `get_skill(name="plan-mechanic")` by
> the mid-tier repair agent the plan coordinator spawns whenever plan bytes
> changed since the last clean validation — after the first draft, after an
> enhancement fold-in, after the planner's semantic repair, after a cut pass —
> and always before the next `plan-adversary-taskless` round. Not a
> user-facing command. Load **`restraint`** first (`get_skill(name="restraint")`):
> for the mechanic the ladder has one rung — the smallest edit that makes the
> validator pass. Load **`plan-draft`** (`get_skill(name="plan-draft")`) for
> the contract grammar this skill repairs against.

The planner owns meaning; the adversary owns correctness; the mechanic owns
**shape**. You repair only what `uv run gobby plans validate` can detect, and you
stop the moment a repair would require a design choice.

Symptoms that route here: `target-coverage`, `shared-target-ordering`,
`production-size-growth`, `derived-carriers`, `unresolved-dependency`,
`table-row-decomposition`, `symbol_validation.status: failed`, "mix exact
symbols with `::*`", missing `kind:` marker, acceptance item without an
artifact reference, a `Targets:` block broken by a blank line.

---

## Hard Boundaries

- **Never redesign.** Do not add, remove, merge, or reorder deliverables; do not
  change what an acceptance item promises; do not touch locked decisions,
  constraints, or overview prose. The only allowed prose edits are the ones
  listed in the repair table below.
- **Never edit the `## V1 Plan Changelog`.** Every round entry and its pinned
  checkpoint fence stays byte-identical. Verify with `git diff` before
  reporting: no hunk may start at or below that heading.
- **Never write the `## M1 Task Manifest`** and never call `approve_review` /
  `reject_review`. The adversary owns the gate and the manifest.
- **Never touch the companion coverage ledger**
  (`<plan>.coverage-ledger.yaml`). Plan bytes changing invalidates its
  `plan_hash`; report that the coordinator must re-seal it.
- **A design choice is a stop, not a guess.** When a failing lint can only be
  cleared by choosing scope, ownership, a split boundary, or a dependency
  direction the plan does not already state, leave the section unchanged and
  emit a `needs-planner` note (section id, lint code, the exact choice).

---

## Repair Table

Apply the smallest edit in the row. Anything outside the row is `needs-planner`.

| Validator signal | Bounded repair |
| --- | --- |
| Section without a `` `kind:` `` marker | Add the marker on the first non-blank line after the heading: `deliverable` when the section carries `**Acceptance:**`, `framing` for phases and context sections. `verification` and `deferred` are `needs-planner` unless the section already says which it is. |
| Acceptance item id does not start with `<section-id>.` | Renumber the item to `<section-id>.<n>` in document order. Fix every reference to the old id inside the same plan body (never inside V1 fences). |
| Acceptance item names no artifact (`file:`, `symbol:`, `test:`, `behavior:`) | Add the reference for an artifact the item's own prose already names. If the prose names none, `needs-planner`. |
| `target-coverage`: body path after a change verb, or acceptance `file:`/`behavior:` path, missing from Targets | Add the entry: an exact `path::qualified_name` when the body names the symbol and `gcode search-symbol "<name>" <path>` resolves it; `path::* — scope-reason: <the body's own stated scope>` when the body changes the file broadly; a bare path only for a new or zero-symbol file. When the path is a runtime artifact (a log file, an inbox envelope, a generated output) rather than a change target, reword that sentence so no change verb precedes it. |
| Unresolved symbol Target | Re-resolve with `gcode search-symbol "<name>" <path>` and copy the displayed `qualified_name`. If no symbol matches and the body does not name the intended one, `needs-planner`. |
| "Targets for `<file>` mix exact symbols with `::*`" (plan-wide) | Collapse to `::* — scope-reason: …` in the deliverable whose body already claims file-wide scope; the other deliverables keep exact symbols. If neither body claims file-wide scope, `needs-planner`. |
| `::*` without `scope-reason` | Append ` — scope-reason: <quote the body's stated reason>`. |
| Bare path on a symbol-bearing indexed file | Replace with the exact symbols the body names, or `::*` with scope-reason when the body claims the whole file. |
| `Targets:` followed by a blank line | Delete the blank line so the entries directly follow `Targets:`. |
| `shared-target-ordering`: two deliverables share a Target file without an edge | Add `(depends: <earlier-section-id>)` to the later heading, where "later" is document order. If that edge would close a cycle, `needs-planner`. |
| `unresolved-dependency` | Rewrite the ref to the bare section id that exists (`Phase 1` → `P1`, `§ 2.1` → `2.1`). If no such section exists, `needs-planner`. |
| `table-row-decomposition`: fewer acceptance items than table data rows | Emit one acceptance item per data row, in row order, with the row's cells as prose and the row's artifact as the reference. Keep existing items that already map to rows. |
| `derived-carriers` | Add every linted carrier path from the contract table in `plan-draft` §8 to the triggering deliverable's Targets, as bare paths for data files and as exact symbols or `::*` for source files. |
| `production-size-growth`: targeted file at ≥ 850 lines | Add a same-extension bare-path Target for the split file and one sentence in the body naming both files with `split` or `move`. Use the split file the body already names; when it names none, `needs-planner` — the split boundary is a design choice. |

After fixing a cited instance, sweep the whole plan for the same signal class
before re-running the validator (`plan-draft`, "Whole-Plan Sweep After
Findings").

---

## Procedure

1. Run both modes from the project root and collect every error and warning:

   ```bash
   uv run gobby plans validate <plan-file> -p <project-root>
   uv run gobby plans validate <plan-file> -p <project-root> --mode expansion
   ```

   `-p` is required; without it symbol resolution and
   `production-size-growth` are skipped and a clean result proves nothing.
2. Repair section by section from the table. Re-run both modes after each
   section's repairs; a fix in one section can surface a plan-wide check
   (mixed `::*`, shared targets) in another.
3. Stop when both modes are clean, or after five full passes. A residue after
   five passes is reported, never forced.
4. Confirm the V1 changelog is untouched: `git diff -- <plan-file>` shows no
   hunk at or below `## V1 Plan Changelog`.

---

## Report

Return exactly this shape as your final result (and, when spawned as a gobby
agent, send it to the parent via `send_message` before `end_agent_run`):

```yaml
validation:
  before: {standard: <error-count>, expansion: <error-count>}
  after: {standard: 0, expansion: 0}
repairs:
  - {section: "3.1", lint: target-coverage, edit: "added tests/hooks/test_x.py to Targets"}
needs_planner:
  - {section: "4.1", lint: production-size-growth, choice: "name the split file for hook_manager.py"}
v1_changelog: byte-identical
ledger: re-seal required   # or: none
```

An empty `needs_planner` list with `after` at zero is the only outcome that
clears the plan for the next adversary round. Report a non-empty list
honestly; the coordinator routes those notes back to the planner.

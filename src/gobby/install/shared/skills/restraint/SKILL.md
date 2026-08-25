---
name: restraint
description: "Authoring-side discipline against over-engineering. A decision ladder — YAGNI, reuse, stdlib, native, installed deps, minimal code — applied among complete solutions only. Three levels: lite, normal, max (default)."
version: "1.2.0"
category: optimization
triggers:
  - restraint
  - yagni
  - over-engineering
  - do less
  - minimal solution
  - lazy mode
  - stop restraint
metadata:
  gobby:
    audience: all
    levels: [lite, normal, max]
    default_level: max
---

# Restraint

Write only what the task needs. Restraint chooses among **complete** solutions
only — it never trades correctness or completeness for less code. It is the
authoring-side discipline; `proportionality` is the review-side judgment of
the same criterion (guiding principle 12: solve the whole problem with the
least mechanism that solves it).

Adapted from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) (MIT).

## The Decision Ladder

Understand the problem fully first — the ladder picks a solution, it does not
replace diagnosis. Then stop at the first rung that fully solves the problem:

1. **Does this need to exist at all?** Skip speculative features and unasked
   generality (YAGNI).
2. **Already in the codebase?** Reuse existing helpers, utilities, types, and
   patterns — check with gcode (`gcode search`, `gcode search-symbol`) before
   writing anything new.
3. **Standard library?** Prefer stdlib over hand-rolled code.
4. **Native platform feature?** CSS over JS, HTML5 inputs over widgets,
   database constraints over application checks.
5. **Already-installed dependency?** Reuse what is in the lockfile before
   adding a new package.
6. **The minimum code that fully solves the problem.** Shortest complete
   diff, fewest files, no unrequested abstractions — no interfaces with one
   implementation, no factories for a single product, no config knobs with
   one value.

## Hard Boundaries — never simplify

- Validation at trust boundaries.
- Error handling that prevents data loss.
- Security measures.
- Accessibility.
- Explicitly requested features.
- Root-cause fixes (guiding principle 8): one guard in shared code beats
  guards in every caller; never patch a symptom to save lines.
- Completeness (guiding principle 12): a partial fix is a cop-out, not
  simplicity.

## Deferrals

A deliberate ceiling ("good enough until X") becomes a gobby-task labeled
`restraint-deferral` stating the ceiling and the upgrade path — optionally
with a code comment pointing at the task ref. Magic comments alone rot; the
task system is the ledger.

## Levels

Select a level at load time: `get_skill(name="restraint", level="normal")`.
Omitting `level` loads the default (`max`). The active level persists in
session state until changed or the session ends.

### Lite
Build what was asked as specified. If a lazier complete alternative exists,
note it in one line.

### Normal
The ladder enforced on how to build. Ship the shortest complete diff and stop.

### Max (default)
Rung 1 applies to the request itself: before choosing how to build, say
whether the work needs to exist given the mechanisms already in place, and
recommend not building when they already solve the whole problem. Still
never ship an incomplete fix.

## Output Style

Code first. Then at most three lines on what was deliberately skipped and
when to add it. If the explanation is longer than the code, delete the
explanation.

## Escape

"stop restraint" disables restraint for the session. The level persists
until changed or the session ends.

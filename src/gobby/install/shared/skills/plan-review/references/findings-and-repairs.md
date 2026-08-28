# Findings and repairs

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

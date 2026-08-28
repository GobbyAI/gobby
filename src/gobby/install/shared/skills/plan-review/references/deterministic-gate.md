# Deterministic review gate

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
sessions run project-aware base and expansion validation before resubmission;
the planner/adversary spawn gate also calls the same internal validator before
every adversary spawn. The validator calls `parse_plan(..., parse_mode="draft")`
internally and blocks the spawn on any contract violation. By the time the
adversary is invoked, the typed grammar has already passed the draft-mode
contract gate — re-running the parser pre-verdict is structural duplication
that wastes a spawn round on syntax the planner already cleared.

The coordinator supplies the final clean deterministic sweep report from those
two commands. Validator residue is repaired by the bounded plan-mechanic loop or
returned to the planner before evidence preparation. Treat the report as delegated
mechanical evidence: verify its artifact identity and spot-check its claims; never
repeat the whole deterministic sweep inside the adversary.

### Measured deterministic-gate trial

Session #11061 trialed this split during rounds 5–10 of the
`grok-hook-deferred-materialization` review. Both validator modes reported zero
residue in six consecutive rounds, and zero validator-class findings reached the
adversary, compared with 3 of 10 blind findings in round 4. The semantic review
still reached the ten-round cap with 8 blocking semantic findings in round 10.
This trial validates the mechanical gate as a finding-class filter; it did not
establish semantic convergence. Adversary token delta was not measured, and the
mid-tier mechanic was not exercised because every deterministic gate was clean.

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

# Verification and revision

## Verification Checklist (run BEFORE presenting the plan)

Before handing the plan off for review or expansion, confirm ALL of the following:

After this narrative checklist passes, the orchestrating `plan` workflow MUST
run `uv run gobby plans validate <plan-file>` before adversary review or
expansion. Use `gobby tasks expand validate-plan` only from task-expansion
workflows or when debugging the expansion CLI path itself.

### 1. No Explicit Test Tasks

Scan headings and bullets for forbidden duplicate TDD-wrapper patterns from the
TDD Compatibility section above. Remove filler "write tests for X" tasks that
duplicate skill-backed TDD required on the implementation leaf. Keep standalone
`category: test`
deliverables when they have their own target, acceptance criteria, and
behavior-pinning or test-infrastructure purpose.

### 2. Dependency Tree Valid

- No circular dependencies (A → B → A is invalid).
- No missing references (if `(depends: 1.1)`, task `1.1` must exist).
- Phase dependencies (`depends: P<N>`) reference existing phases by their
  letter-prefixed section_id. `(depends: Phase 1)` does not resolve.
- Leaf tasks are concrete implementation work, not meta-tasks like "coordinate
  phase 2".

### 3. Categories Assigned Correctly

Every `### N.M` heading carries a `[category: X]` tag and `X` is one of the
canonical categories above. Fix any missing or unknown category.

### 4. Phase Heading Syntax

Every phase heading uses the canonical `## P<N>: Name` form (letter-prefixed
section_id matching `^P\d+$`). Headings like `## Phase 1: Name`, `## 1: Name`,
or dash-separator variants are **silently dropped** by the parser — the
phase will not be recognized and its tasks will be lost at expansion. Pair
each phase heading with `` `kind: framing` `` on the next non-blank line.

### 5. Sections Are Self-Contained

Spot-check three `### N.N` sections: does each one contain enough detail (file
paths, code examples, behavioral specs) that an agent who sees ONLY that section
can do the work without outside context?

### 6. Symbol Targets Resolve

- Every existing symbol-bearing file uses exact gcode `qualified_name` Targets,
  or one justified `::* — scope-reason: ...` entry.
- Rust references retain `Type::method` after the file separator.
- Bare paths are new or zero-symbol files in the fresh index.
- No target contains a symbol UUID or line number.
- No file mixes exact symbols with `::*`.
- Final project-aware validation reports `symbol_validation.status: passed`.

### 7. Consumer Sweep Recorded

- For every exact symbol Target, run `gcode usages <symbol-id>` or
  `gcode blast-radius <name>`.
- Every owned consumer file in production or tests appears in some
  deliverable's Targets. Exclude vendor and generated files.
- When the index does not cover the planned branch, such as the worktree
  overlay gap tracked by #20664, record the literal-sweep commands and their
  complete hit lists in `## Constraints` or the owning deliverable body. Run
  commands such as `gcode grep -F "Symbol(" src/ tests/` and
  `gcode grep -w symbol` so the adversary and close-gate judge can verify the
  sweep against the planned branch checkout.

### 8. Derived Carriers Included

Apply every matching row. The `derived-carriers` lint enforces only the
carriers marked *linted*; the rest are advisory and the adversary checks them
by reading the plan.

| Trigger | Required Targets |
| --- | --- |
| Any path under `crates/gcore/assets/schema/migrations/`, `crates/gcore/assets/schema/baseline.sql`, or `crates/gcore/src/schema/assets.rs` | *Linted:* `crates/gcore/assets/schema/catalog.manifest.json`<br>`crates/gcore/src/grant/bundle.rs`<br>`crates/gcore/tests/schema_contract.rs`<br>`crates/gdaemon/tests/cli_contract.rs`<br>`src/gobby/storage/schema_expected_identity.json`<br>*Advisory (not linted):* `tests/runtime_grants/golden/*.json` |
| Any `.py` under `src/gobby/config/` | *Linted:* `crates/gcore/assets/config/runtime_config_contract.json`<br>*Advisory (not linted):* the matching `tests/config/` test |
| A model field is removed | *Advisory (not linted):* every constructor and assertion site found by a literal sweep |
| A wire or protocol field is added | *Advisory (not linted):* every golden fixture plus `crates/gcore/src/grant/tests.rs` and `tests/runtime_grants/test_golden_vectors.py` |

### 9. Shared Targets Are Ordered

When two deliverables list the same Target file, the later deliverable carries
an explicit `(depends: <earlier-section-id>)` edge. Prose sequencing is not
enforceable; the compiled manifest edge is.

### 10. Production Size Checked

For every targeted hand-maintained production file currently at 850 lines or
more, record its current line count. Any deliverable that targets the file adds
a new same-extension bare-path Target and says `split` or `move` in the body
paragraph that names both files; that is what the `production-size-growth` lint
checks, and it keeps the file below the 1,000-line ceiling.

### Verification Output

Report:

```
Plan Verification:
✓ No explicit test tasks found
✓ Dependency tree is valid (no cycles, all refs exist)
✓ Categories assigned correctly
✓ Phase headings use canonical syntax
✓ Task sections are self-contained
✓ Symbol Targets resolve against a fresh gcode index
✓ Consumer sweep recorded
✓ Derived carriers included for every triggered contract
✓ Shared targets are ordered
✓ Production size checked

Ready for review.
```

Or, if issues were fixed:

```
Plan Verification:
✗ Found 2 explicit test tasks (removed):
  - "Add tests for user authentication" → REMOVED
  - "Ensure all tests pass" → REMOVED
✓ Dependency tree is valid
✓ Categories assigned correctly
✓ Phase headings use canonical syntax
✓ Task sections are self-contained
✓ Symbol Targets resolve against a fresh gcode index
✓ Consumer sweep recorded
✓ Derived carriers included for every triggered contract
✓ Shared targets are ordered
✓ Production size checked

Plan updated. Ready for review.
```

If any check still fails after attempted fixes, **do not present the plan yet** —
revise until every check passes.

---

## Revision Round Mandate

Planner revision rounds run in fresh context. Read the current plan file,
the cumulative `## V1 Plan Changelog`, and the latest taskless adversary
findings supplied by the coordinator.

Every adversary round entry embeds the pinned V1 checkpoint fence rendered by
`append_plan_changelog_round`, the coordinator's atomic changelog write (its
fence bytes match `render_plan_changelog_round` output).
Preserve those bytes verbatim. The canonical JSON
contains exactly `evidence_id`, `round_number`, `plan_hash`, `session_id`, and
`round_result`; surrounding prose bullets are only a projection of that
payload. Never reconstruct, normalize, or reformat the fence.

Apply surgical fixes: missing acceptance items, ambiguous wording, stale file
paths, missing dependency annotations, and contradictions with the codebase.
Do not redesign the plan in response to adversary findings. When a finding
requires rejecting the premise or re-engineering a section, return a note to
the coordinator naming the section id and the specific premise conflict
instead of folding a redesign into a revision.

After a revision, rerun plan validation and hand the artifact plus resolution
notes for the round back to the coordinator, who records the round entry via
`append_plan_changelog_round` before the next taskless review round.

First drafts author narrative sections only. The `## M1 Task Manifest` is
emitted by `plan-adversary-taskless` or the interactive coordinator on approval.

<!-- markdownlint-disable MD013 -->

# Plan-Coverage Contract

This page is the canonical reading order for the plan-coverage contract:

1. `CLAUDE.md` for repo-wide agent requirements.
2. `src/gobby/install/shared/skills/plan/SKILL.md` for interactive authoring.
3. `src/gobby/install/shared/skills/plan-draft/SKILL.md` for the typed grammar.
4. `src/gobby/install/shared/skills/plan-review/SKILL.md` for adversarial review.
5. `src/gobby/install/shared/skills/plan-enhance/SKILL.md` for the constructive
   pre-adversary enhancement pass.
6. `src/gobby/install/shared/skills/proportionality/SKILL.md` for the shared
   over-engineering / right-sizing criterion used by plan, epic, and leaf review.
7. `src/gobby/install/shared/skills/expand/SKILL.md` for expansion obligations.
8. `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` for QA gating.

Parser and coverage implementation surfaces live under `gobby.plans.parser`,
`gobby plan coverage`, and the evidence resolver used by the coverage matrix.

## Canonical Heading Regex

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?)(?:\.(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?))*)(?=\s|[).:-]|$)
```

The regex accepts heading levels `##` through `######`, optional `§`, numeric
section IDs of any depth, alpha-prefixed IDs such as `A10`, and an optional
letter suffix on every segment.

## Section Kinds

Every section carries first-line front matter:

```markdown
`kind: deliverable | framing | verification | deferred`
```

- `deliverable` sections require an `**Acceptance:**` block with at least one
  acceptance item.
- `framing` sections carry context or non-goals and no acceptance items.
- `verification` sections summarize end-to-end checks and no acceptance items.
- `deferred` sections require a typed deferral object.

## Acceptance Items

Acceptance item IDs are formed by appending `.<n>` to the section's own ID.
The section ID is the prefix, verbatim — no synthetic letters are added.
The parser enforces this with `item_id.startswith(f"{section_id}.")` in
`_build_acceptance_item` (`src/gobby/plans/parser.py`).

Concretely:

- Section `A1` (letter-prefixed) emits `A1.1`, `A1.2`, and so on. The `A` in
  the items is **part of the section ID**, not a synthetic prefix added to
  acceptance items.
- Section `A1.7` emits `A1.7.1`, `A1.7.2`, and so on.
- Section `1.1` (purely numeric) emits `1.1.1`, `1.1.2`, and so on — **no `A`
  prefix**. Items like `A1.1.1` for a section `1.1` are rejected by the
  parser because `A1.1.1` does not start with `1.1.`.

The `A<section>.<n>` shorthand used elsewhere in this contract is read as
"section ID dot n" — for purely numeric sections that means a numeric item
ID with no letter prefix.

Each item names at least one artifact kind: `file`, `symbol`, `test`, or
`behavior`. `symbol` values use the code index's `qualified_name` contract:
top-level symbols are leaf names and nested class/type members are
container-qualified within one file, such as `Symbol.make_id`. They are not
module- or package-qualified import paths; include a `file` artifact when a
symbol name needs disambiguation.

```markdown
**Acceptance:**  (under section `A1`, letter-prefixed)

- A1.1 - <prose>. file: `src/module.py`.
- A1.2 - <prose>. symbol: `Symbol`.
- A1.3 - <prose>. test: `tests/test_module.py::test_behavior`.
- A1.4 - <prose>. behavior: "documented behavior" in `docs/contract.md`.
```

```markdown
**Acceptance:**  (under section `1.1`, purely numeric)

- 1.1.1 - <prose>. file: `src/module.py`.
- 1.1.2 - <prose>. symbol: `Symbol.make_id`.
```

## Target Inventory

Every `deliverable` section declares the files it changes in a `Target:` or
`Targets:` inventory in its body, before `**Acceptance:**`. The `target-coverage`
semantic lint (`src/gobby/plans/semantic_lint.py`) fails a section when a
concrete file path appears in the body after a change-intent verb — `add`,
`create`, `delete`, `edit`, `expose`, `extract`, `implement`, `modify`, `move`,
`refactor`, `register`, `remove`, `rename`, `replace`, `split`, `touch`,
`update`, `wire` — or in a `file:`/`behavior:` acceptance ref, and that path is
not in the inventory.

Each inventory entry uses one of these forms:

- `path/to/file.py::qualified_name` targets one exact indexed symbol. The
  qualified name must equal gcode's indexed value for that file. Parsing splits
  only the first `::`, so Rust targets retain names such as `Type::method`.
- `path/to/file.py::*` — `scope-reason: <non-empty explanation>` targets every
  indexed symbol in one file. The reason stays on the same line and is valid only
  for a `::*` target.
- `path/to/file.py` targets a genuinely new file or a file whose fresh index has
  no symbol-bearing records.

Symbol-qualified scope is mandatory whenever the fresh index reports symbols,
including for newly created files after indexing. A file may have multiple exact
symbol targets. It may not mix exact targets with `::*`. Symbol UUIDs and line
numbers are invalid target references.

**Block format is load-bearing.** `iter_target_block_lines` reads the inventory
as a contiguous block: the `Targets:` line itself, then every immediately
following line. **A blank line ends the block**, as does the next heading, a
`kind:` marker, `**Acceptance:**`, another `Target:` line, or any line that is
neither a bullet nor contains a backtick or `/`. Inventory bullets separated
from their `Targets:` line by a blank line are silently not part of the
inventory, and the section then fails for paths that visually appear to be
listed.

```markdown
Targets:
- `src/gobby/plans/symbol_targets.py::validate_symbol_targets`
- `src/gobby/plans/semantic_lint.py::*` — scope-reason: update every semantic lint
- `docs/contracts/plan-coverage.md`

Update `src/gobby/plans/symbol_targets.py` and
`src/gobby/plans/semantic_lint.py`, then document the contract in
`docs/contracts/plan-coverage.md`.
```

The single-entry form
`Target: \`src/gobby/plans/symbol_targets.py::validate_symbol_targets\`` puts the
target on the header line itself; both forms may appear in one section, and their
entries merge.

The `plan-draft` Verification Checklist also requires consumer-sweep evidence
for every exact symbol Target. Record `gcode usages <symbol-id>` or
`gcode blast-radius <name>` results and place every owned production or test
consumer, excluding vendor and generated files, in some deliverable's Targets.
When the index does not cover the planned branch, including the worktree overlay
gap tracked by #20664, record literal-sweep commands such as
`gcode grep -F "Symbol(" src/ tests/` and `gcode grep -w symbol` with their hit
lists in `## Constraints` or the owning deliverable body, and run them from the
planned branch checkout. This authoring evidence adds no semantic-lint behavior.

**Matching is basename-aware in one direction only** (`_path_covered_by_targets`):

- A mentioned path containing `/` must match a target's normalized file path
  **exactly**. `web/src/app.tsx` is not covered by a target for `app.tsx`.
- A mentioned bare filename matches **any** target file path sharing that
  basename. `stages.yaml` is covered by a target for
  `src/gobby/install/shared/registry/stages.yaml`.

A bare extension such as `.tsx` is not a path and never requires an entry.

### Validator Lints

Plan validation applies five additional inventory lints:

| Code | Trigger | Required disposition |
| --- | --- | --- |
| `unresolved-dependency` | A `(depends: ...)` reference on a deliverable heading, or on a phase heading above it, names neither a deliverable section nor a phase that contains deliverables. | Use the bare section id of an existing deliverable or phase. |
| `shared-target-ordering` | The same primary path appears in two or more deliverable Targets inventories. Matching is path-exact and ignores symbol scope. | Every pair of owners must have a dependency path in either direction. A `(depends: P<N>)` reference expands to every deliverable in that phase, and a `(depends: ...)` annotation on a phase heading applies to every deliverable in that phase. |
| `production-size-growth` | A targeted hand-maintained `.py`, `.ts`, `.tsx`, `.css`, `.rs`, `.js`, `.mjs`, `.cjs`, or `.sh` file currently has at least 850 lines. | Add a new bare-path Target with the same extension and name the split or move in the deliverable body. The production ceiling remains 1,000 lines. Files under `tests/`, `fixtures/`, `vendor/`, or `node_modules/`; files whose stem is `conftest` or `tests`, starts with `test_`, or ends with `_test`, `_tests`, `.test`, or `.spec`; and files whose first five lines carry `@generated`, `DO NOT EDIT`, `auto-generated`, or `Generated by` are excluded. A Rust file's line count stops at its first `#[cfg(test)]` line. |
| `derived-carriers` | A Target matches one of the source rows below. | Target every required carrier in the same deliverable or in a deliverable that transitively depends on it. |
| `consumer-coverage` | An exact symbol Target has owned call or import consumers in the code index. | Target every same-repository consumer, including tests. Vendor, `node_modules`, and generated files are excluded. Missing consumers warn in standard validation and error in expansion validation. |

`production-size-growth` deliberately uses a simple decomposition heuristic. The
exemption is per large file: one body paragraph (lines up to the next blank
line) must contain `split` or `move`, name the large file by path or basename,
and name a new same-extension bare-path Target that does not exist yet. When
validation runs without a project root, the lint is skipped and the validator
emits a `production-size-growth skipped: no project root` warning.

The three inventory lints above read the **primary path** of each Targets entry —
the first backticked span, else the first path token, ignoring anything after
` — ` — so a path mentioned in a scope-reason or a trailing comment never
counts as a Target. `target-coverage` keeps reading every path on the line.

`derived-carriers` uses this static trigger table:

| Source Target | Required carrier Targets |
| --- | --- |
| Any path under `crates/gcore/assets/schema/migrations/`; `crates/gcore/assets/schema/baseline.sql`; or `crates/gcore/src/schema/assets.rs` | `crates/gcore/assets/schema/catalog.manifest.json`; `crates/gcore/src/grant/bundle.rs`; `crates/gcore/tests/schema_contract.rs`; `crates/gdaemon/tests/cli_contract.rs`; `src/gobby/storage/schema_expected_identity.json` |
| Any `.py` under `src/gobby/config/` | `crates/gcore/assets/config/runtime_config_contract.json` |

Consumer coverage uses active `code_calls` rows plus `code_imports` rows whose
importer mentions the symbol's bare name as a whole word in its indexed content
(which also catches string sites such as `patch("module.symbol")`). Importing
the module alone is not a usage. In a worktree overlay, usages are unioned
across the overlay and parent indexes. When the index is unavailable or its
recorded root does not cover the plan checkout, the validator emits one
non-blocking `consumer-coverage skipped: <reason>` warning and emits no consumer
omissions. Worktree-root mismatches cite the overlay visibility gap tracked by
#20664.

Known consumer blind spots: a consumer that reaches the symbol through a
re-export from another module (the importer names the re-exporting module, not
the defining one), and dynamic access by string that does not spell the bare
name. The `plan-draft` consumer sweep covers these with literal `gcode grep`
evidence.

## Deferrals

`kind: deferred`

Typed deferral object:

```yaml
deferral:
  task_ref: "#12345"
  reason: "Why this work is outside the current epic."
  owner: "team-or-agent"
  original_acceptance_items:
    - A7.3
```

The referenced task must be open and carry
`deferred-from:<plan-id>:<section-id>` provenance. A closed task fails the gate.

External prerequisites — work gated on another epic, plan, or task — are never
expressed as prose blockers or manifest `depends_on` edges; `depends_on` resolves
only to sibling manifest entries, so an external wait carried in prose is
unenforceable. Such work MUST be a `kind: deferred` section instead.

The deferral task is parented under the plan's own epic as tail work; it never
floats as an orphan follow-up. The task carries the actual ordering edges: a
`blocked-by` dependency on each external prerequisite, plus dependencies on any
internal leaves the deferred work needs (for example the audit leaf it follows).

The task is created at expansion or finalization, never while the plan is being
drafted, enhanced, or adversarially reviewed — plans may change or be abandoned
before then. A dangling `task_ref` in an unfinalized plan is expected and does
not fail base validation; the open-task, provenance, and dependency-closure
gates above apply from expansion validation onward.

## Coverage Records

Leaves emit structured labels:

```text
covers:<plan-id>:<section-id>:<item-id>
```

Free-form `plan-ref:` labels are not honored; only structured `covers:` labels
are valid coverage signal.

## Retired Classification Escape Hatches

Grandfather and legacy classification escape hatches are retired. The typed
plan-coverage contract is the single routing source for plan expansion,
coverage evidence, and plan-kind interpretation.

No install-shared state file may override that contract. Reintroducing hidden
grandfather state or legacy classification files under `src/gobby/install/shared/`
is a contract violation and must fail pre-flight validation.

## Task Manifest

Implementation plans carry a single `## M1 Task Manifest` section at the end of
the document. The manifest is the typed bridge between the plan's deliverable
sections and the leaves the deterministic compiler emits at expansion time. The
section heading uses the canonical ID `M1` so it satisfies the section-ID
regex; `kind: manifest` exempts it from the `**Acceptance:**` requirement.

````markdown
## M1 Task Manifest
`kind: manifest`

```yaml
- title: <human-readable title>
  category: <code|config|docs|refactor|test>
  task_type: <feature|bug|chore|...>
  depends_on: [<section-id>, ...]
  validation_criteria: <one-line pass/fail>
  labels:
    - covers:<plan-id>:<section-id>:<item-id>
  implementation_domain: <backend|frontend|fullstack>  # required for code
  assigned_agent: <agent-name>  # optional privileged/manual override
  tdd: <true|false>
  source_section: <section-id>
```
````

Entry schema (one entry per `kind: deliverable` section):

| Field | Type | Notes |
| --- | --- | --- |
| `title` | str | Human-readable title for the synthesized leaf |
| `category` | enum | One of the development-forward categories: `code`, `config`, `docs`, `refactor`, `test` |
| `task_type` | enum | Task-type tag for the synthesized leaf |
| `depends_on` | list[str] | References `source_section` IDs of other manifest entries |
| `validation_criteria` | str | One-line pass/fail |
| `labels` | list[str] | Exactly one `covers:<plan-id>:<section-id>:<item-id>` label per acceptance item in the source section |
| `implementation_domain` | enum\|null | Required for `category: code`; one of `backend`, `frontend`, `fullstack` |
| `assigned_agent` | str\|null | Optional privileged/manual route override |
| `tdd` | bool | True adds `test-driven-development`, `tdd:required`, and TDD evidence criteria on the leaf |
| `source_section` | str | Must reference a `kind: deliverable` section ID |

`depends_on` values name leaf deliverable dependencies by manifest
`source_section` ID. They must resolve to another manifest entry. Phase IDs
such as `P0` are invalid because phases are not implementation leaves.

### Phase Sub-Epic Titles

For multi-phase Plan-Coverage expansion, compiled `phases[].title` values and
created phase sub-epic titles preserve the canonical marker in the exact format
`P<N>: <parsed title>`. The stable phase ID remains `phase-p<N>`. The visible
marker does not add a task label; generated phase sub-epics retain only their
existing expansion provenance label. This applies to future compilation and
apply runs without backfilling existing task trees.

### Category/TDD Policy

The single category policy is:

- `code` entries must include `implementation_domain`. Expansion maps
  `backend` to `backend-developer`, `frontend` to `frontend-developer`, and
  `fullstack` to `fullstack-developer` unless a privileged/manual override is
  present.
- `code` and `config` are TDD-eligible. Manifest entries for these categories
  may set `tdd: true`, which makes expansion emit one implementation leaf with
  `additional_skills: ["test-driven-development"]`, label `tdd:required`, and
  validation criteria requiring red, green, refactor/final-green, exact command,
  and test-quality audit evidence.
- `test`, `refactor`, and `docs` are not TDD-eligible. Manifest entries for
  these categories must use `tdd: false`.
- `research`, `planning`, and `manual` are valid direct task categories, but
  expansion manifests reject them.
- `category: test` is valid for standalone test infrastructure,
  characterization, parity, or regression suites that are deliverables in
  their own right. These entries expand as single tasks and must carry their
  own acceptance criteria.
- Filler tasks such as "write tests for X" are rejected when they duplicate
  TDD work required on a `code` or `config` deliverable.

### Parser-Enforced Invariants

When the manifest is present, these invariants are checked regardless of mode:

- Schema-check every entry against the table above.
- Reject `category: code` without `implementation_domain`.
- Reject `tdd: true` unless the entry category is `code` or `config`.
- Every `kind: deliverable` section has exactly one manifest entry referencing
  it via `source_section` (1:1 invariant).
- Every `depends_on` value resolves to another manifest entry's
  `source_section` ID.
- Every `covers:` label resolves to a real acceptance item under the entry's
  `source_section`.
- No orphan manifest entries — every entry's `source_section` resolves to a
  real deliverable section.

A malformed manifest fails the parser in any mode. Only the missing-manifest
behavior differs by mode.

### Parser Modes

`gobby.plans.parser.parse_plan` accepts a `parse_mode` parameter that selects
validation strictness:

| Mode | Manifest | Used by | Behavior |
| --- | --- | --- | --- |
| `parse_mode="draft"` | optional | `validate_plan_file` (planner-side gate run before every adversary spawn); `/gobby plan` Phase 3a; `gobby plan coverage` against drafts | Manifest tolerated absent. If present, schema and 1:1 invariants still apply — a malformed draft manifest still fails. |
| `parse_mode="expansion"` | required | `gobby expand` deterministic compile path; taskless adversary/coordinator post-approval self-check | Raises `PlanParseError("missing manifest")` if the section is absent or any deliverable has no entry. |
| `parse_mode="strict"` (default) | required | callers that want full validation regardless of context | Same strict invariants as `expansion`; default so any caller that omits `parse_mode` keeps full validation. |

The deadlock between "review the plan" and "manifest must exist" is resolved by
construction: the planner-side `validate_plan_file` gate parses in `draft` mode
before each taskless adversary spawn, the adversary then runs qualitative review without
re-parsing, writes the manifest on clean review, self-checks in `expansion`
mode, and downstream `gobby expand` parses in `expansion` against the
now-manifest-bearing plan.

### Manifest-on-Approval Contract

First drafts are narrative-only. The approving `plan-adversary-taskless` run or
interactive coordinator writes `## M1 Task Manifest` after user-approved review.
If the planning agent already supplied complete category and implementation
domain decisions, preserve them. The deterministic manifest emitter is fallback
only for missing manifests or legacy drafts where planning agents did not assign
enough category/domain data.

Sequence on clean review (no blocking findings):

1. Append or repair the `## M1 Task Manifest` section in the plan file.
2. Self-check via `parse_plan(plan_path, parse_mode="expansion")`.
3. On `PlanParseError`, fix the manifest in-place and retry up to 3 times.
4. After the cap is exhausted, return `verdict: needs_review` with the parser
   details. Do not approve.
5. On success, return `verdict: approved` with manifest entry count and whether
   fallback emission was used.

On rejection rounds the adversary MUST NOT edit the plan file — plan edits
between rounds are the parent planner's responsibility. Findings are recorded in
`## V1 Plan Changelog` by the coordinator via the `append_plan_changelog_round`
gobby-plans tool, which renders the canonical round fence daemon-side and
atomically inserts the round entry (prose + fence) at the end of the changelog;
coordinators never hand-edit round fences.

Repair-class findings (`traceability`, `bad-sequencing`, `weak-testability`,
and `gobby-format`, per the category matrix in `plan-review`) may carry typed
`repairs` — `add_targets`, `add_dependency`, or `add_acceptance` payloads whose
every `section_id` comes from the evidence manifest. Those repairs are payload,
never writes: the adversary only returns them, and the coordinator's
`apply_plan_review_repairs` gobby-plans tool is the sole writer. It runs only
after the interactive vote and only on a finalized `needs_review` checkpoint,
so the checkpoint records the reviewed artifact and the next round's snapshot
records the repaired one. The tool is idempotent and all-or-nothing — repairs
already present are skipped, a repair that would leave the plan unparseable or
change an unrepaired section's hash fails with `invalid_repair` and leaves the
file untouched — and it returns the unified diff with the plan hashes before
and after. Design-class findings stay prose; the planner owns those edits.

### Enhancement And Over-Engineering Vocabulary

A constructive `plan-enhancer` pass runs before the adversary gate (default on
for interactive `/gobby plan`, opt-in via `--plan-enhancement-rounds` for
autonomous `gobby build`). It loads `plan-enhance` and `proportionality` and
emits ranked Better/Bigger suggestions. The enhancer is advisory only: it never
approves, rejects, edits the plan file, or writes the manifest. Fold-ins are the
planner's or coordinator's responsibility, and every suggestion must still pass
the adversary. The manifest stays adversary-owned regardless of enhancement.

The adversary's review vocabulary gains an `over-engineering` dimension, scored
against the shared `proportionality` justification test: flag mechanism with no
concrete consumer or stated requirement in the work under review (speculative
abstraction, a subsystem where a function would do, single-value config or
flags, indirection without payoff) and name the simpler alternative. Size,
ambition, and large-but-justified epics are never findings on their own.
Structural over-engineering is `blocking` ("simplify before expansion");
ceremony is a `nit`. This dimension only *adds* to review — the adversary keeps
sole correctness-gate authority and its write-scope invariant.

## Review Severity and Approval

The shared normative severity matrix is:

| Severity | Decision boundary | Required disposition |
| --- | --- | --- |
| blocking | Demonstrated violation of a required obligation, backed by the complete failure trace. | Repair before approval. |
| major | Material non-gating quality or operability risk. | Record an explicit quality-ledger decision. |
| minor | Localized hardening with bounded effect. | Carry in the quality ledger until resolved or explicitly accepted. |
| nit | Cosmetic issue with no behavioral effect. | Carry in the quality ledger; it never blocks approval. |

Boundary examples are table-driven:

| Candidate | Boundary fact | Severity |
| --- | --- | --- |
| A required rollback path leaves a durable partial write and includes the reproducible trace. | Required obligation is demonstrably violated. | blocking |
| Retry behavior works, but operator-visible diagnosis is materially incomplete. | Operability risk is material and non-gating. | major |
| One validated example omits an adjacent bounded hardening case. | Effect is localized and bounded. | minor |
| Heading punctuation differs from house style. | Effect is cosmetic. | nit |

Approval requires zero `blocking` findings. Open `major`, `minor`, and `nit`
entries remain visible in the server-derived quality ledger carried beside the
canonical manifest in the approved result envelope.

## Coverage CLI

```bash
gobby plan coverage \
  --plan <path> \
  --plan-id <id> \
  --plan-hash <sha256> \
  --task-tree <db|matrix-file> \
  [--root-task <ref>] \
  [--project-id <id>] \
  [--matrix-file <path>] \
  [--evidence <kind>] \
  [--manifest <path>] \
  [--regenerate]
```

Required flags: `--plan`, `--plan-id`, `--plan-hash`, `--task-tree`.

Optional flags: `--root-task`, `--project-id`, `--matrix-file`, `--evidence`,
`--manifest`, `--regenerate`.

Task-tree modes:

- `db` reads the live task database. It requires `--root-task <ref>` and
  `--project-id <uuid>` and evaluates tasks in that root's scoped tree.
- `matrix-file` reads `--matrix-file <path>`. The file is a YAML or JSON
  coverage matrix with a `header` (including the current `plan_hash`) and
  `rows`; do not pass DB-only root or project scope flags in this mode.

Both modes write the canonical coverage manifest, either to `--manifest` or
under `.gobby/plans/coverage/<project>/<root>/<plan>.coverage.yaml`, print that
path, and use the exit-code contract below. `jsonl` and arbitrary path values
are not task-tree modes; task JSONL is export material, not coverage input.

Exit codes: `0`, `2`, `3`, `4`, `5`, `6`, `7`, `8`.

Evidence kinds: `commits | task-diff | worktree-diff | coverage-matrix | none`.

## Bootstrap Ledger

Every new epic plan ships a `.coverage-ledger.yaml` companion file,
adversary-reviewed before expansion, until the contract tooling is mature. The
ledger enumerates deliverable acceptance items and expected implementation
leaves so expansion and `gobby plan coverage` can compare manifest rows against
the plan. Parent close does not consult the ledger: an epic is closable when it
has no open children.

## Plan Storage

The `plans` table is the authoritative registry of plan state. The file system
holds plan markdown content; `gobby-plans` MCP tools and the `gobby plans` CLI
are the read/write surfaces for plan metadata. Each row carries:

- `plan_id` — stable identifier matching the plan filename stem.
- `project_id` — owning project UUID.
- `root_task_ref` — the epic seq_num as a string.
- `plan_path` — path relative to the project root.
- `plan_hash` — sha256 of the current plan file content.
- `plan_kind` — one of `implementation`, `strategy`.
- `state` — one of `active`, `archived`.

`plan_kind` controls how the plan participates in coverage verification:

- `implementation` — parsed in strict mode; every active entry MUST have a
  generated manifest at the canonical scoped path, the manifest's `plan_hash`
  MUST match the on-disk plan, and every row MUST resolve to
  `status: covered`. This is the default for new epic plans.
- `strategy` — parsed in permissive mode (canonical headings without `kind:`
  default to `framing`). No manifest is required or permitted; manifests
  pointing at strategy entries fail orphan detection.

Plan archive is system-managed: archiving flips `state` to `archived`, moves
the plan file to `.gobby/plans/completed/`, records `archived_at`, and removes
the managed coverage manifest.

## Table-Row Decomposition

Any `deliverable` section whose body uses a markdown table to enumerate work
items MUST emit one acceptance item per table data row with stable IDs.
Plan-adversary qualitatively rejects deliverables that enumerate work in tables
without per-row acceptance items. This rule closes the #12725 missing-section
failure mode for future plans.

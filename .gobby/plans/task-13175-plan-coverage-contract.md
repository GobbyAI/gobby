# Plan-Coverage Contract — Epic 1 Implementation Plan

> Implementation-grade detail plan for Epic 1 of the post-#12725
> lifecycle-dispatch recovery. Strategy:
> [`task-13173-lifecycle-dispatch-recovery.md`](./task-13173-lifecycle-dispatch-recovery.md),
> Phase A, sections A1–A10 (approved after five rounds of plan-adversary
> review).
>
> **Plan ID:** `task-13175-plan-coverage-contract`.
> **Root task:** `#13175`.
> **Project:** `d45545c5-ded5-4335-b115-0245752edacf` (gobby).
>
> This plan is the FIRST plan written under the new contract. The tooling
> that would mechanically validate it (A4) does not yet exist — Epic 1
> builds it. The plan is therefore hand-validated against A1's canonical
> regex (see §A12 for the validation summary) and re-validated by the new
> tooling once A4 lands (blocking gate before Epic 1 closes; see A8).

## A0 Context and scope

`kind: framing`

Epic #12725 ("lifecycle-dispatch") merged with only the storage / build /
config foundation actually landed; ~30 of 36 plan sections were missing
or partial. Root cause is structural: the expansion → expansion-qa
contract has no input-coverage requirement and no plan-section
provenance, so sections vanish silently. The strategy plan splits the
recovery into two epics:

- **Epic 1 — Plan-Coverage Contract (this plan).** Fixes the contract:
  typed plan grammar, parser, coverage library, expansion-qa integration,
  evidence-based holistic-review gate, #12725 retrofit, bootstrap ledger,
  CI gate, documentation.
- **Epic 2 — #12725 Gap Recovery.** Closes the implementation gaps under
  the new contract. Out of scope here. Gated on Epic 1.

Sections A1–A10 below mirror the strategy's section numbering exactly.
Deviations from strategy A1–A10 require user approval; this plan does
not redesign them.

The plan-author task tree under `#13175` will be expanded after the
bootstrap ledger (A8) is adversary-approved. Until A4 lands, the
ledger is the only mechanically-checkable manifest of Epic 1's expected
coverage; once A4 lands, the ledger is re-validated against the
generated manifest, and a mismatch blocks Epic 1 close (A8.7).

**Out of scope** is enumerated explicitly in §A11.

## A1 Plan-format spec (typed grammar)

`kind: deliverable`

Update the planning skill chain and the planner / plan-adversary agent
definitions so plans MUST emit a typed structure the A2 parser can
consume. The grammar covers heading levels `##`–`######`, numeric and
alpha-prefixed section IDs of any depth with optional letter suffix on
the last segment, an optional `§` prefix, and bare or titled headings.

The canonical regex is frozen at strategy level and pinned as a literal
string in the plan skill, the plan-draft skill, the plan-review skill,
the planner agent prompt, the plan-adversary agent prompt, and the A2
parser module:

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
```

The trailing `|$` lookahead allows bare end-of-line headings (`### 1.1a`,
`## A10`) — both styles appear in real plans (see §A2 fixtures).

**Section kind enum.** Every section carries front-matter
`kind: deliverable | framing | verification | deferred`:

- `deliverable` — concrete implementation work; **must** carry an
  `**Acceptance:**` block with one or more numbered acceptance items.
- `framing` — context, scope, out-of-scope. No acceptance items.
- `verification` — end-to-end / acceptance summary. No acceptance items
  (the items it summarizes live in their `deliverable` sections).
- `deferred` — explicitly out-of-scope-for-this-epic with a typed
  deferral object (see A3) pointing at a real open task with
  `deferred-from:<plan-id>:<section-id>` provenance.

**Front-matter syntax.** A line of the form `` `kind: <value>` ``
(backtick-quoted to render distinctly in markdown viewers) directly
under the heading, optionally followed by a blank line. A2's parser
reads the first non-blank line under each heading and matches against
the kind grammar; missing/unrecognized kind raises `PlanParseError`.

**Acceptance-item shape.** A `**Acceptance:**` block under each
deliverable section, one bullet per item:

```
**Acceptance:**

- A1.1 — <prose>. <artifact-kind>: `<artifact-ref>`.
- A1.2 — ...
```

Item IDs are dotted-suffix on the section ID (section `A1` → items
`A1.1`, `A1.2`, …; section `A1.7` → items `A1.7.1`, `A1.7.2`, …).
Each item names **at least one** concrete artifact (matching strategy
A1) — over-specifying is allowed; the parser counts the first
matching artifact reference for coverage, additional references in
the same item's prose are accepted but informational:

- `file: <path>` — a file the leaf must create or modify.
- `symbol: <module>.<symbol>` — a function, class, type, or constant.
- `test: <path>::<name>` — a named test that must exist.
- `behavior: "<documented behavior>"` — a documented behavior in a
  named file (file path included after the prose).

**Plan-adversary rejection (mechanical, before qualitative review).**
The plan-adversary agent loads A2's parser as its first action and
rejects with a structured failure message before applying its
qualitative checklist when the plan exhibits any of:

- A heading at level `##`–`######` that does not match the canonical
  regex AND does not carry `kind: framing` (strict / implementation
  parse mode only; strategy plans are surveyed under permissive mode).
- A section without a `kind:` front-matter line.
- A `deliverable` section without an `**Acceptance:**` block.
- An `**Acceptance:**` item with zero artifact references (the rule:
  at least one of `file:`, `symbol:`, `test:`, `behavior:` per item;
  the parser uses the first matching reference as the canonical
  artifact for coverage matching).
- A `deliverable` section whose body uses a markdown table to
  enumerate work items but whose `**Acceptance:**` block has fewer
  acceptance items than the table has data rows. Tables that
  enumerate deliverables MUST be decomposed into one acceptance
  item per row with stable IDs (this is the failure mode that
  caused #12725's missing sections; strategy A1 makes per-row
  decomposition load-bearing). Plan-adversary detects this case
  qualitatively because parser-level table-vs-prose intent
  detection is intractable; the rule applies to every plan
  authored under the contract.
- An item ID that does not dotted-prefix-match its section ID.
- A duplicate section ID anywhere in the document.
- A `deferred` section whose deferral object fails A3 validation
  (parser-level: missing fields; library-level: task lookup, recovery-
  epic dependency / cited-out-of-scope-parent check).

**Files to modify:**

- `src/gobby/install/shared/skills/plan/SKILL.md` — add a
  "Plan-Coverage Contract Grammar" section at the top of the drafting
  guidance referencing plan-draft for the schema.
- `src/gobby/install/shared/skills/plan-draft/SKILL.md` — pin the
  canonical regex verbatim in a code block; document the kind enum;
  document the acceptance-item shape; document the deferral object;
  document the structured `covers` record format; document the
  table-row decomposition rule (one acceptance item per data row
  in any deliverable-enumeration table).
- `src/gobby/install/shared/skills/plan-review/SKILL.md` — instruct
  plan-adversary to load `gobby.plans.parser` and reject mechanically
  before qualitative review; enumerate the rejection cases above;
  add a qualitative-checklist item: reject any deliverable section
  whose body contains a markdown table enumerating work items but
  whose acceptance-item count is less than the table's data-row
  count (table-row decomposition rule).
- `src/gobby/install/shared/workflows/agents/planner.yaml` — prompt
  appendix that requires the typed grammar.
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml` —
  allowed-tool list includes the A2 parser entry point; prompt
  appendix references plan-review skill content.

**Tests to write:**

- `tests/skills/test_plan_skill_grammar.py` — load the plan-draft skill
  via the skills loader, assert the canonical regex literal appears
  verbatim, assert the kind enum is documented, assert the
  acceptance-item shape is documented, assert the deferral object spec
  appears, assert the covers-record format appears.
- `tests/skills/test_plan_adversary_rejection.py` — fixture-driven
  test feeding the plan-adversary skill content a malformed plan for
  each rejection case; assert the documented rejection message names
  the cause.
- `tests/workflows/test_planner_grammar_prompt.py` — assert the planner
  agent's compiled prompt contains the typed-grammar requirement
  string.

**Acceptance:**

- A1.1 — `src/gobby/install/shared/skills/plan-draft/SKILL.md` ships
  the canonical regex literal verbatim inside a fenced code block.
  file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- A1.2 — plan-draft SKILL.md documents the four-value kind enum
  (`deliverable | framing | verification | deferred`) and the
  per-kind acceptance/no-acceptance rule. file:
  `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- A1.3 — plan-draft SKILL.md specifies the acceptance-item shape with
  the four artifact kinds (`file | symbol | test | behavior`) and the
  dotted-suffix item-ID rule. file:
  `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- A1.4 — plan-draft SKILL.md specifies the typed deferral object shape
  (see A3) and the structured `covers:<plan-id>:<section-id>:<item-id>`
  record format. file:
  `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- A1.5 — plan-review SKILL.md documents mechanical rejection by the
  plan-adversary using the A2 parser before qualitative review, and
  enumerates the eight rejection cases (the seven mechanical
  parser-level cases plus the qualitative table-row decomposition
  case from A1.11). file:
  `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- A1.6 — `src/gobby/install/shared/workflows/agents/planner.yaml`
  prompt includes the typed-grammar requirement (canonical regex,
  kind enum, acceptance-item shape, deferral object). file:
  `src/gobby/install/shared/workflows/agents/planner.yaml`.
- A1.7 — `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`
  allowed-tool list includes the A2 parser callable and the prompt
  references plan-review skill. file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.
- A1.8 — test:
  `tests/skills/test_plan_skill_grammar.py::test_canonical_regex_pinned`
  asserts string equality between the regex literal in plan-draft
  SKILL.md and the regex constant exported by `gobby.plans.parser`
  (A2.2). test:
  `tests/skills/test_plan_skill_grammar.py::test_canonical_regex_pinned`.
- A1.9 — `tests/skills/test_plan_adversary_rejection.py::test_rejects_each_case`
  asserts the plan-adversary skill's documented rejection
  message fires for each of the eight cases: the seven
  mechanical parser-level cases (missing ID, missing kind,
  missing acceptance, ID collision, malformed item ID, malformed
  deferral, zero artifact references on an acceptance item) AND
  the qualitative table-row decomposition case from A1.11
  (deliverable section whose body contains a markdown
  enumeration table with N data rows but fewer than N
  acceptance items). test:
  `tests/skills/test_plan_adversary_rejection.py::test_rejects_each_case`.
  Companion test:
  `tests/skills/test_plan_adversary_rejection.py::test_rejects_table_row_decomposition_violation`
  feeds plan-adversary a fixture deliverable with a 5-row
  enumeration table and only 2 acceptance items; asserts the
  documented rejection message names "table-row decomposition"
  and cites the missing rows.
- A1.10 — test:
  `tests/workflows/test_planner_grammar_prompt.py::test_planner_prompt_contains_grammar`
  asserts the planner agent's compiled prompt includes the
  typed-grammar requirement marker. test:
  `tests/workflows/test_planner_grammar_prompt.py::test_planner_prompt_contains_grammar`.
- A1.11 — plan-draft, plan-review, planner.yaml, and
  plan-adversary.yaml all document the **table-row decomposition
  rule** (strategy A1): a `deliverable` section whose body uses a
  markdown table to enumerate work items MUST emit one
  acceptance item per data row with stable IDs (e.g., `A7.4.1`,
  `A7.4.2`, … per row). Plan-adversary qualitatively rejects any
  deliverable with fewer acceptance items than table data rows.
  This closes the failure mode that produced #12725's missing
  sections. test:
  `tests/skills/test_plan_skill_grammar.py::test_table_row_decomposition_rule_documented`
  asserts the rule appears in plan-draft SKILL.md, plan-review
  SKILL.md, and the compiled planner/plan-adversary YAML
  prompts.

## A2 Plan parser library

`kind: deliverable`

A new module `gobby.plans.parser` exposes a pure-Python parser that
turns a plan markdown file into a `PlanDocument` AST. No DB calls. The
parser is the single source of truth for the canonical regex; the plan
skill content (A1) imports the same literal string and A1.8 asserts
string equality, so changes to the grammar fail loudly in both places.

**Files to create:**

- `src/gobby/plans/__init__.py` — empty namespace marker.
- `src/gobby/plans/parser.py` — parser implementation, types,
  exceptions, regex constants.
- `tests/plans/__init__.py` — empty.
- `tests/plans/test_parser.py` — fixture tests.
- `tests/plans/test_parser_grammar.py` — pinned-string tests.

**Symbols / types / signatures:**

```python
import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

PLAN_HEADING_REGEX: re.Pattern[str] = re.compile(
    r"^#{2,6}\s+(?:§\s*)?(?P<section_id>"
    r"(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?)"
    r")(?=\s|[).:-]|$)"
)

class Kind(StrEnum):
    deliverable = "deliverable"
    framing = "framing"
    verification = "verification"
    deferred = "deferred"

class ArtifactKind(StrEnum):
    file = "file"
    symbol = "symbol"
    test = "test"
    behavior = "behavior"

@dataclass(frozen=True)
class AcceptanceItem:
    item_id: str
    prose: str
    artifact_kind: ArtifactKind
    artifact_ref: str
    source_line: int

@dataclass(frozen=True)
class Deferral:
    task_ref: str
    reason: str
    owner: str
    original_acceptance_items: tuple[AcceptanceItem, ...]
    raw_block: str

@dataclass(frozen=True)
class PlanSection:
    section_id: str
    parent_id: str | None
    heading_level: int
    title: str
    kind: Kind
    acceptance_items: tuple[AcceptanceItem, ...]
    deferral: Deferral | None
    source_span: tuple[int, int]   # (start_line, end_line) inclusive, 1-indexed

@dataclass(frozen=True)
class PlanDocument:
    plan_id: str | None            # parsed from optional `**Plan ID:** ...` blockquote in front-matter; None if absent
    source_path: Path
    source_hash: str               # sha256 hex of file bytes at parse time
    sections: tuple[PlanSection, ...]
    framing_headings: tuple[tuple[int, str, int], ...]  # (line, raw, level) for each non-canonical heading recorded as framing

class PlanParseError(ValueError):
    """Raised on any structural violation. .errors is a list of (line, message)."""
    def __init__(self, errors: list[tuple[int, str]], source_path: Path) -> None: ...

class PlanKind(StrEnum):
    implementation = "implementation"   # strict mode; non-canonical headings without explicit kind: framing raise
    strategy = "strategy"               # permissive; non-canonical headings without explicit kind default to framing_headings

def parse_plan(path: Path, *, plan_kind: PlanKind = PlanKind.implementation) -> PlanDocument: ...
```

**Behavior contract:**

- `source_hash` is `hashlib.sha256(path.read_bytes()).hexdigest()`,
  computed once at parse time. Every downstream artifact (manifest,
  expansion output, evidence row) records this hash; mismatch
  invalidates the artifact (A4, A9).
- A heading is parsed by matching `PLAN_HEADING_REGEX` against the
  raw heading line. A heading that fails to match AND whose first
  non-blank line below is `` `kind: framing` `` is recorded in
  `framing_headings` but does not contribute to `sections` and is not
  an error.
- For `plan_kind=implementation` (strict, the default): a heading that
  fails to match AND whose kind is anything else (or absent) is a
  `PlanParseError`. This is the parse mode used by A5 expansion-qa,
  A7 retrofit assertions, and A9 CI for implementation plans.
- For `plan_kind=strategy` (permissive): both non-canonical and
  canonical headings missing `kind:` front-matter are tolerated.
  Specifically:
  - A non-canonical heading without `kind:` is recorded in
    `framing_headings`.
  - A canonical heading (matches the regex) without `kind:` is
    parsed into a `PlanSection` whose `kind` defaults to
    `Kind.framing` and whose `acceptance_items` is empty. The
    `section_id` is captured normally so fixture tests can assert
    presence (e.g., A1, A10, D0.1, B5, etc.). No `**Acceptance:**`
    block is required.
  - A canonical heading WITH an explicit `kind:` line still
    follows the strict rules (deliverable requires acceptance,
    item IDs must dotted-prefix-match, etc.).
  This mode exists so the strategy doc
  `task-13173-lifecycle-dispatch-recovery.md` (which carries
  canonical IDs `### A1.`, `### D0.1`, `### B5`, etc. without
  `kind:` lines, plus narrative headings such as `## Context`,
  `## Phase A — ...`, `## Adversary Review Log`,
  `## Verification`, `## Out of Scope (filed as follow-ups)`) can
  be parsed without a retrofit pass. Strategy plans are excluded
  from A9 manifest/hash/zero-row CI by their `plan_kind` entry in
  `.gobby/plans/index.yaml` (see A9). The strategy fixture in A2
  asserts only `section_id` presence, not deliverable/acceptance
  shape, since strategy plans are not implementation contracts.
- A heading that matches but is missing `kind:` front-matter is a
  `PlanParseError`.
- Duplicate `section_id` anywhere is a `PlanParseError`.
- `deliverable` without `**Acceptance:**` block is a `PlanParseError`.
- Acceptance item with item ID that does not dotted-prefix-match its
  section ID is a `PlanParseError`.
- Acceptance item with **zero** artifact references
  (`file: ...`, `symbol: ...`, `test: ...`, or `behavior: "..."` —
  at least one per item; the FIRST match in document order is the
  canonical artifact for coverage matching, additional references
  in the same item's prose are accepted but informational) is a
  `PlanParseError`. This is the load-bearing rule that backs A1's
  "every item names at least one concrete artifact" claim and the
  plan-adversary's mechanical rejection (A1.9). The "at least one"
  cardinality matches strategy A1; over-specifying with multiple
  artifacts is permitted because acceptance items often verify
  more than one observable surface.
- `deferred` section without a parseable deferral object is a
  `PlanParseError`. Deferral object syntax: a fenced YAML block of the
  form

  ```yaml
  task_ref: "#NNNN"
  reason: "<reason>"
  owner: "<agent or session ref>"
  original_acceptance_items:
    - item_id: A?.?
      prose: "..."
      artifact_kind: file|symbol|test|behavior
      artifact_ref: "..."
  ```

  inside the deferred section's body. Parser populates `Deferral`;
  library-level validation (A3) is separate.
- The parser does **not** call out to the task store; A3 does. This
  keeps A2 a pure function.
- **Fenced code blocks are masked from all structural scans.** Before
  any heading, `kind:` line, `**Acceptance:**` block, acceptance bullet,
  or deferral-YAML detection runs, the parser computes a code-fence
  mask using **CommonMark fence-matching semantics**: a fenced code
  block opens on a line whose left-trimmed prefix is three or more
  consecutive ` ` ` (backticks) or three or more consecutive `~`
  (tildes), with optional info string. The block closes on a
  subsequent line whose left-trimmed prefix is **the same fence
  character** with length **greater than or equal to** the opener's
  fence length, and an empty (whitespace-only) info string after the
  delimiter. A shorter same-character fence inside the block does
  NOT close it; a different-character fence (e.g., `~~~` inside a
  backtick-fenced block, or `` ``` `` inside a tilde-fenced block)
  does NOT close it. Indentation up to 3 spaces is permitted on
  either fence. All lines while the state is "fenced" are excluded
  from structural matching. The single
  exception is the deferral YAML block intentionally consumed by a
  `deferred` section: when the parser is in deferral-capture mode
  for a section, the first fenced YAML block inside that section is
  read as the deferral object (matching the established `deferred`
  syntax above) and not masked. This rule is load-bearing because
  plan files (including this one) contain fenced examples with
  fake `**Acceptance:**` blocks and fake acceptance bullets;
  without masking, `test_parses_self` would either fail or its
  passing/failing would depend on implementation detail.

**Tests:**

- `tests/plans/test_parser.py::test_parses_task_12725_lifecycle_dispatch`
  — parses `.gobby/plans/task-12725-lifecycle-dispatch.md`; asserts
  presence of section IDs `1.1`, `1.1a`, `1.1b`, `1.1c`, `1.1d`,
  `1.2`, `1.3`, `1.3a`, `1.4`, `1.5`, `1.6`, `1.7`, `1.8`, `1.9`,
  `1.10`, `2.1`, `2.2`, `2.3`, `2.4`, `2.5`, `2.6`, `2.7`, `2.8`,
  `2.8a`, `2.8b`, `2.9`, `2.10`, `3.1`, `3.2`, `3.3`, `3.4`, `4.1`,
  `4.2`, `4.3`, `4.4`, `4.5`. (#12725 may not be retro-conformed yet
  at parser-test time; the test runs against the post-A7 form, so it
  is gated on A7.1–A7.4 — see A7 dependency note.)
- `tests/plans/test_parser.py::test_parses_task_13173_recovery`
  — parses `.gobby/plans/task-13173-lifecycle-dispatch-recovery.md`
  with `plan_kind=strategy` (permissive); asserts presence of
  `section_id` for `A1`–`A10`, `D0.1`–`D0.9`, `B1`–`B5`, `C1`–`C6`,
  `D1`–`D8`, `F1`–`F4` (the parser handles the trailing-period
  delimiter via the `[).:-]` lookahead). Each canonical heading
  parses into a `PlanSection` whose `kind` defaults to
  `Kind.framing` and whose `acceptance_items` is empty (the
  strategy doc carries no `kind:` lines and no `**Acceptance:**`
  blocks; the test asserts only ID presence, not deliverable
  shape). The strategy doc's narrative headings (`## Context`,
  `## Phase A — ...`, `## Adversary Review Log`,
  `## Verification`, `## Out of Scope (filed as follow-ups)`,
  `### Round N — REJECTED`, etc.) are recorded in
  `framing_headings` rather than raising. The strategy doc is
  excluded from A9 manifest/hash CI by its
  `plan_kind: strategy` entry in `.gobby/plans/index.yaml` (see
  A9 for the schema and discovery flow).
- `tests/plans/test_parser.py::test_parses_self`
  — parses this very plan
  (`.gobby/plans/task-13175-plan-coverage-contract.md`); asserts
  presence of `A0`–`A12`; asserts every deliverable section has at
  least one acceptance item; asserts the source_hash matches a
  freshly computed sha256.
- `tests/plans/test_parser.py::test_bare_and_titled_headings`
  — fixture file with `### 1.1a\n` (bare end-of-line) and
  `### 1.1a Title here\n`; asserts both parse to the same
  section_id.
- `tests/plans/test_parser.py::test_alpha_and_numeric_ids`
  — fixture file with `## A1`, `## A10`, `### D0.1`, `### B5`,
  `### 1.1a`, `### 2.8b`; asserts each parses with the documented
  section_id.
- `tests/plans/test_parser.py::test_framing_without_id_is_recorded`
  — fixture with `## Phase A — Free Form\n` followed by
  `` `kind: framing` ``; asserts no error and presence in
  `framing_headings`.
- `tests/plans/test_parser.py::test_framing_without_id_no_kind_raises`
  — same heading without `kind: framing`; asserts `PlanParseError`.
- `tests/plans/test_parser.py::test_duplicate_section_id_raises` —
  fixture with `## A1` twice; asserts `PlanParseError`.
- `tests/plans/test_parser.py::test_deliverable_without_acceptance_raises`
  — fixture deliverable section without `**Acceptance:**`; asserts
  `PlanParseError`.
- `tests/plans/test_parser.py::test_acceptance_item_id_must_prefix_section`
  — fixture with section `A1` and item `A2.1`; asserts
  `PlanParseError`.
- `tests/plans/test_parser.py::test_deferred_without_object_raises`
  — fixture deferred section without YAML deferral block; asserts
  `PlanParseError`.
- `tests/plans/test_parser.py::test_acceptance_item_without_artifact_raises`
  — fixture deliverable section with an acceptance bullet whose
  prose contains no `file:`, `symbol:`, `test:`, or `behavior:`
  reference; asserts `PlanParseError`.
- `tests/plans/test_parser.py::test_acceptance_item_with_multiple_artifacts_uses_first`
  — fixture deliverable section with an acceptance bullet whose
  prose contains two `test:` refs (or any combination of artifact
  kinds); parses cleanly and produces an `AcceptanceItem` whose
  `artifact_kind` and `artifact_ref` come from the FIRST match in
  document order. Asserts no raise and exact field values.
- `tests/plans/test_parser.py::test_strategy_kind_permissive_no_raise_on_narrative_headings`
  — fixture file with `## Context\n` and no `kind:` line; parses
  cleanly with `plan_kind=PlanKind.strategy`; the heading appears
  in `framing_headings`. Same fixture with
  `plan_kind=PlanKind.implementation` raises `PlanParseError`.
- `tests/plans/test_parser.py::test_strategy_kind_permissive_canonical_heading_no_kind`
  — fixture file with `### A1. Plan format spec (typed grammar)\n`
  followed by prose (no `kind:` line, no `**Acceptance:**`);
  parses cleanly with `plan_kind=PlanKind.strategy` and yields a
  `PlanSection(section_id="A1", kind=Kind.framing,
  acceptance_items=())`. Same fixture with
  `plan_kind=PlanKind.implementation` raises `PlanParseError`
  citing missing `kind:` front-matter.
- `tests/plans/test_parser.py::test_source_hash_is_sha256_of_bytes`
  — assert `parse_plan(p).source_hash == hashlib.sha256(p.read_bytes()).hexdigest()`.
- `tests/plans/test_parser.py::test_source_span_is_inclusive_1_indexed`
  — fixture with known line ranges; assert `source_span` matches.
- `tests/plans/test_parser_grammar.py::test_regex_pinned_strings`
  — table-driven test of (input, expected_section_id) pairs:

  | Input | Expected |
  |---|---|
  | `### 1.1a` | `1.1a` |
  | `### 1.1a Lifecycle enum and automation fields` | `1.1a` |
  | `### 1.1d` | `1.1d` |
  | `### 2.8a` | `2.8a` |
  | `### 2.8b` | `2.8b` |
  | `## A1` | `A1` |
  | `## A1 Plan format spec (typed grammar)` | `A1` |
  | `## A10` | `A10` |
  | `### D0.1` | `D0.1` |
  | `## B5` | `B5` |
  | `## §1.7 Decision rules` | `1.7` |
  | `### D0.8 Dispatcher slot reservation primitive (F11)` | `D0.8` |
  | `### A1. Plan format spec (typed grammar)` | `A1` |
- `tests/plans/test_parser_grammar.py::test_negative_framing_heading`
  — `## Phase A — Fix the Expansion/QA Contract` does not match the
  canonical regex.
- `tests/plans/test_parser_grammar.py::test_h1_not_subject_to_regex`
  — `# Title` is not matched (regex starts at `##`).
- `tests/plans/test_parser.py::test_fenced_headings_are_masked`
  — fixture file with a real `## A1\n` followed by `kind:
  deliverable\n**Acceptance:**\n- A1.1 — real item. file: a.py.\n`
  AND a fenced ``` ```markdown\n## A2\nkind: deliverable\n``` ```
  block. Parses without error; `PlanDocument.sections` contains
  `A1` only (the fenced `A2` is masked), no duplicate-ID raise,
  no missing-kind raise.
- `tests/plans/test_parser.py::test_fenced_acceptance_bullets_are_masked`
  — fixture with one real deliverable section whose `**Acceptance:**`
  block has a single real `- A1.1 — real item. file: a.py.` bullet,
  followed by a fenced ``` ```\n**Acceptance:**\n- A1.2 — fake. file:
  b.py.\n``` ``` block in the same section's body. Parser produces
  `len(sections[0].acceptance_items) == 1` (the fenced bullet does
  not contribute) and the `A1.2` ID does not appear anywhere.
- `tests/plans/test_parser.py::test_fenced_deferral_yaml_outside_deferred_is_ignored`
  — fixture `deliverable` section whose body contains a stray fenced
  YAML block whose contents look like a deferral object; the parser
  does not treat it as a deferral (the section is not `kind:
  deferred`) and the YAML is masked from all structural scans.
- `tests/plans/test_parser.py::test_tilde_fence_also_masks`
  — fixture using ` ~~~ ` fence delimiters (instead of triple
  backticks) around fake headings; the parser masks them identically.
- `tests/plans/test_parser.py::test_fence_closes_with_longer_delimiter`
  — fixture opens with three backticks and closes with four
  backticks (CommonMark legal: closer length >= opener length);
  the block is masked correctly and any structural content after
  the closing fence is parsed normally.
- `tests/plans/test_parser.py::test_shorter_inner_fence_does_not_close`
  — fixture opens with four backticks; a three-backtick line
  inside the block does NOT close it (closer length < opener
  length); content after the inner short fence remains masked
  until a real four-or-more-backtick closer.
- `tests/plans/test_parser.py::test_different_fence_char_does_not_close`
  — fixture opens with ` ``` `; an inner `~~~` line does NOT
  close it (closer character differs); the block continues until
  a real backtick closer.
- `tests/plans/test_parser.py::test_indented_fence_up_to_3_spaces`
  — fixture opens with 3-space-indented ` ``` `; the parser
  recognizes it as a valid fence (per CommonMark, up to 3 leading
  spaces are allowed); 4-space-indented opener does NOT (it's a
  code block by indentation, not a fence).

**Acceptance:**

- A2.1 — `src/gobby/plans/parser.py` exports `PlanDocument`,
  `PlanSection`, `AcceptanceItem`, `Deferral`, `Kind`, `ArtifactKind`,
  `PlanParseError`, `parse_plan`, `PLAN_HEADING_REGEX`. file:
  `src/gobby/plans/parser.py`.
- A2.2 — `PLAN_HEADING_REGEX` is the literal pattern from A1, exported
  as a module constant. symbol: `gobby.plans.parser.PLAN_HEADING_REGEX`.
- A2.3 — `PlanDocument.source_hash` is sha256 hex of the file bytes
  at parse time. test:
  `tests/plans/test_parser.py::test_source_hash_is_sha256_of_bytes`.
- A2.4 — parser handles every section ID shape in
  `task-12725-lifecycle-dispatch.md` after retrofit (post-A7.1–A7.4).
  test: `tests/plans/test_parser.py::test_parses_task_12725_lifecycle_dispatch`.
- A2.5 — parser parses every canonical section ID in
  `task-13173-lifecycle-dispatch-recovery.md` under
  `plan_kind=PlanKind.strategy`; canonical headings without
  `kind:` lines yield `PlanSection(kind=Kind.framing,
  acceptance_items=())`; narrative headings appear in
  `framing_headings`. The test asserts ID presence only, not
  deliverable/acceptance shape. test:
  `tests/plans/test_parser.py::test_parses_task_13173_recovery`.
- A2.6 — parser handles bare and titled forms of the same heading.
  test: `tests/plans/test_parser.py::test_bare_and_titled_headings`.
- A2.7 — parser records framing headings without canonical IDs in
  `PlanDocument.framing_headings` and raises if `kind: framing` is
  absent. test:
  `tests/plans/test_parser.py::test_framing_without_id_is_recorded`,
  `tests/plans/test_parser.py::test_framing_without_id_no_kind_raises`.
- A2.8 — parser raises `PlanParseError` on duplicate section IDs.
  test:
  `tests/plans/test_parser.py::test_duplicate_section_id_raises`.
- A2.9 — `AcceptanceItem.artifact_kind` is one of the four
  `ArtifactKind` values; parser raises on any other. symbol:
  `gobby.plans.parser.ArtifactKind`.
- A2.10 — `PlanSection.source_span` is `(start_line, end_line)`
  inclusive, 1-indexed. test:
  `tests/plans/test_parser.py::test_source_span_is_inclusive_1_indexed`.
- A2.11 — pinned-strings grammar test asserts each row in the
  test table matches with the expected `section_id`. test:
  `tests/plans/test_parser_grammar.py::test_regex_pinned_strings`.
- A2.12 — `parse_plan` is a pure function; no DB calls. behavior:
  "parser does not import gobby.storage" — verified by
  `tests/plans/test_parser.py::test_parser_module_does_not_import_storage`.
- A2.13 — `Deferral` is parsed from the YAML block in deferred
  sections; library-level validation is in A3. behavior:
  "Deferral.task_ref, reason, owner, original_acceptance_items
  populated from YAML" in
  `tests/plans/test_parser.py::test_deferred_without_object_raises` and
  the positive-case fixture in
  `tests/plans/test_parser.py::test_deferred_object_parsed`.
- A2.14 — parser raises `PlanParseError` on an acceptance item with
  zero artifact references (at least one of
  `file:`, `symbol:`, `test:`, `behavior:` per item; the FIRST
  match in document order is the canonical artifact for coverage
  matching, additional references are accepted as informational
  prose). test:
  `tests/plans/test_parser.py::test_acceptance_item_without_artifact_raises`
  asserts the zero-artifact raise;
  `tests/plans/test_parser.py::test_acceptance_item_with_multiple_artifacts_uses_first`
  asserts multi-artifact items parse cleanly and the
  `AcceptanceItem.artifact_kind` / `artifact_ref` come from the
  first match in document order.
- A2.15 — `parse_plan(path, plan_kind=PlanKind.strategy)` is
  permissive on both kinds of missing-`kind:` headings:
  non-canonical narrative headings record into
  `PlanDocument.framing_headings`, AND canonical headings without
  `kind:` lines yield `PlanSection(kind=Kind.framing,
  acceptance_items=())`. `plan_kind=PlanKind.implementation` (the
  default) raises `PlanParseError` on either case. symbol:
  `gobby.plans.parser.PlanKind`. test:
  `tests/plans/test_parser.py::test_strategy_kind_permissive_no_raise_on_narrative_headings`,
  `tests/plans/test_parser.py::test_strategy_kind_permissive_canonical_heading_no_kind`.
- A2.16 — parser masks fenced code blocks from all structural
  scans (heading, `kind:`, `**Acceptance:**`, acceptance bullets,
  and deferral-YAML detection except a deferral block
  intentionally consumed inside a `deferred` section), using
  **full CommonMark fence-matching semantics**: open with ≥3
  consecutive ` ` ` or `~`, close with the **same character** at
  length **≥ opener's length**; a shorter same-character fence
  inside the block does NOT close it; a different-character
  fence inside does NOT close it; up to 3 leading spaces
  permitted on either fence. Fenced examples that look like
  real headings or acceptance bullets do not contribute to
  `PlanDocument.sections` or any `PlanSection.acceptance_items`.
  behavior: "fenced markdown blocks are excluded from structural
  parsing per CommonMark fence-matching rules (closer length ≥
  opener length, same character)" in
  `gobby.plans.parser.parse_plan`. test:
  `tests/plans/test_parser.py::test_fenced_headings_are_masked`,
  `tests/plans/test_parser.py::test_fenced_acceptance_bullets_are_masked`,
  `tests/plans/test_parser.py::test_fenced_deferral_yaml_outside_deferred_is_ignored`,
  `tests/plans/test_parser.py::test_tilde_fence_also_masks`,
  `tests/plans/test_parser.py::test_fence_closes_with_longer_delimiter`,
  `tests/plans/test_parser.py::test_shorter_inner_fence_does_not_close`,
  `tests/plans/test_parser.py::test_different_fence_char_does_not_close`,
  `tests/plans/test_parser.py::test_indented_fence_up_to_3_spaces`.
- A2.17 — `test_parses_self` is load-bearing under A2.16: this very
  plan contains fenced examples (A1 acceptance-bullet examples, A3
  deferral-block examples, A4 manifest snippets) and parses cleanly
  with `PlanDocument.sections` containing exactly the real A0–A12
  IDs. test: `tests/plans/test_parser.py::test_parses_self` —
  asserts that no fenced fake `Aₙ.ₙ` ID leaks into
  `PlanDocument.sections` or any section's `acceptance_items`.

## A3 Typed deferral and structured covers contract

`kind: deliverable`

Two related typed contracts replace free-form labels:

- **Deferral object** (parsed by A2; validated here against the task
  store).
- **Structured `covers` records** carried as task labels of the form
  `covers:<plan-id>:<section-id>:<item-id>` on every leaf claiming
  coverage of an acceptance item.

A leaf may carry multiple `covers` labels; an acceptance item may be
covered by multiple leaves. Free-form `plan-ref:` labels are not
honored — A10 documents the deprecation.

**Files to create / modify:**

- `src/gobby/plans/coverage.py` (new — also used by A4) — covers-record
  parsing and validation.
- `src/gobby/plans/deferral.py` (new) — deferral validation.
- `tests/plans/test_deferral.py` (new).
- `tests/plans/test_covers.py` (new).

**Symbols / types / signatures:**

```python
# src/gobby/plans/coverage.py — covers record portion (rest of file is A4)
@dataclass(frozen=True)
class CoversRecord:
    plan_id: str
    section_id: str
    item_id: str

COVERS_LABEL_REGEX: re.Pattern[str] = re.compile(
    r"^covers:(?P<plan_id>[A-Za-z0-9._-]+):"
    r"(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?)):"
    r"(?P<item_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))$"
)

class InvalidCoversLabelError(ValueError): ...

def parse_covers_label(label: str) -> CoversRecord: ...

@dataclass(frozen=True)
class CoversValidationResult:
    record: CoversRecord
    leaf_task_ref: str
    status: Literal["valid", "missing_section", "missing_item", "artifact_not_referenced"]
    detail: str

def validate_covers(
    record: CoversRecord,
    leaf_validation_criteria: str,
    leaf_task_ref: str,
    plan_doc: PlanDocument,
) -> CoversValidationResult: ...

# src/gobby/plans/deferral.py
@dataclass(frozen=True)
class DeferralValidationResult:
    deferral: Deferral
    section_id: str
    plan_id: str
    status: Literal[
        "valid",
        "task_missing",
        "task_closed",
        "missing_provenance_label",
        "validation_criteria_does_not_duplicate",
        "missing_reason_or_owner",
        "missing_dependency_or_cited_parent",
    ]
    detail: str

class TaskStoreProtocol(Protocol):
    def get_task(self, task_ref: str) -> dict | None: ...
    def get_task_labels(self, task_ref: str) -> list[str]: ...
    def get_task_dependencies(self, task_ref: str) -> list[str]: ...   # task refs this task depends on; reverse direction also reachable via the recovery_epic side

def validate_deferral(
    deferral: Deferral,
    plan_id: str,
    section_id: str,
    task_store: TaskStoreProtocol,
    *,
    recovery_epic_ref: str,
) -> DeferralValidationResult: ...
```

**Behavior contract:**

- `parse_covers_label` returns `CoversRecord` on match, raises
  `InvalidCoversLabelError(label)` otherwise. The plan_id grammar is
  the same allowlist as A4's `_sanitize` allowlist (`[A-Za-z0-9._-]`)
  so `coverage_manifest_path(...)` cannot be tricked into a path
  outside the canonical scope by a crafted plan_id label.
- `validate_covers` resolves `record.section_id` and `record.item_id`
  against `plan_doc`; if either misses, status is
  `missing_section` / `missing_item`. If both resolve, the leaf's
  `validation_criteria` text is checked for either:
  - exact substring match of the artifact_ref, or
  - regex match of the artifact_ref escaped (for paths and
    symbol names where dots, slashes, colons appear).
  If neither matches, status is `artifact_not_referenced`. The
  matcher is per-artifact-kind:
  - `file`: validation_criteria contains the full path or the path
    relative to repo root.
  - `symbol`: validation_criteria contains the full dotted name OR
    the trailing component (e.g., `parse_plan` matches
    `gobby.plans.parser.parse_plan`).
  - `test`: validation_criteria contains the full `path::name` form
    OR the test name alone with the test file path elsewhere in
    validation_criteria.
  - `behavior`: validation_criteria contains the documented behavior
    string verbatim (case-insensitive substring), AND the named file
    path appears.
- `validate_deferral` checks (in order, short-circuit on first
  failure):
  1. `task_store.get_task(deferral.task_ref)` returns a non-None task.
  2. Task `status` is in `{open, in_progress, needs_review, review_approved, escalated}` (not `closed`).
  3. Labels include exactly `f"deferred-from:{plan_id}:{section_id}"`.
  4. Task `validation_criteria` contains, for each item in
     `deferral.original_acceptance_items`, the artifact_ref of that
     item (substring match per artifact-kind rules above).
  5. `deferral.reason` and `deferral.owner` are both non-empty.
  6. Task is **either** a transitive dependency of the
     `recovery_epic_ref` (reachable via `get_task_dependencies`)
     **or** carries a `cited-parent:<parent_ref>` label whose
     target task **simultaneously**:
     - resolves to a non-closed task,
     - carries an `out-of-scope-for:<recovery_epic_ref>` label, AND
     - is NOT a transitive dependency of `recovery_epic_ref`
       (i.e., is genuinely outside the recovery epic's
       dependency closure).
     Failure status: `missing_dependency_or_cited_parent`. This
     closes the F2 hole where a deferred section could point at an
     unrelated open task with the right label and still pass.
     The `out-of-scope-for:` label is the explicit-proof
     requirement: a deferral whose cited-parent lacks it (or whose
     parent has it but is also reachable from the recovery epic)
     fails the gate.
- A4's coverage evaluator calls `validate_deferral` for every
  `deferred` section and `validate_covers` for every `covers` label
  on every leaf in scope.

**Tests:**

- `tests/plans/test_covers.py::test_parse_valid_label` — fixtures
  for each ID shape combination (numeric section + numeric item;
  alpha section + alpha item; mixed).
- `tests/plans/test_covers.py::test_parse_rejects_malformed`
  — fixtures: missing prefix (`coverage:...`), missing parts (`covers:a:b`),
  trailing whitespace, embedded slash, embedded colon in plan_id,
  empty plan_id (`covers::A1:A1.1`).
- `tests/plans/test_covers.py::test_validate_covers_missing_section`
- `tests/plans/test_covers.py::test_validate_covers_missing_item`
- `tests/plans/test_covers.py::test_validate_covers_artifact_referenced_by_path`
- `tests/plans/test_covers.py::test_validate_covers_artifact_referenced_by_symbol_short_name`
- `tests/plans/test_covers.py::test_validate_covers_artifact_referenced_by_test_path_and_name`
- `tests/plans/test_covers.py::test_validate_covers_artifact_referenced_by_behavior_substring`
- `tests/plans/test_covers.py::test_validate_covers_overbroad_rejected` —
  leaf claims `covers:plan:A1:A1.1` but its validation_criteria
  contains only generic phrases ("the parser", "the library") with
  no artifact_ref; status is `artifact_not_referenced`.
- `tests/plans/test_deferral.py::test_validate_task_missing` (FakeStore returns None).
- `tests/plans/test_deferral.py::test_validate_task_closed` (FakeStore returns task with status=closed).
- `tests/plans/test_deferral.py::test_validate_missing_provenance_label`
  (task exists, status=open, but labels exclude `deferred-from:...`).
- `tests/plans/test_deferral.py::test_validate_criteria_does_not_duplicate`
  (task exists with provenance label but validation_criteria does
  not contain the deferred section's acceptance artifact_refs).
- `tests/plans/test_deferral.py::test_validate_missing_reason_or_owner`.
- `tests/plans/test_deferral.py::test_validate_missing_dependency_or_cited_parent`
  — task exists, open, has provenance label, criteria duplicates,
  reason+owner non-empty, but is NOT a dependency of
  `recovery_epic_ref` and has no `cited-parent:` label; asserts
  status `missing_dependency_or_cited_parent`.
- `tests/plans/test_deferral.py::test_validate_dependency_path`
  — task is reachable via `get_task_dependencies` from
  `recovery_epic_ref`; asserts status `valid`.
- `tests/plans/test_deferral.py::test_validate_cited_parent_path`
  — task carries `cited-parent:<ref>` label whose target is open,
  carries `out-of-scope-for:<recovery_epic>`, and is not
  reachable from recovery_epic dependencies; asserts status
  `valid`.
- `tests/plans/test_deferral.py::test_validate_cited_parent_without_out_of_scope_label_rejected`
  — cited-parent target is open but lacks
  `out-of-scope-for:<recovery_epic>`; asserts status
  `missing_dependency_or_cited_parent` (closes F3: open parent
  alone is not enough).
- `tests/plans/test_deferral.py::test_validate_cited_parent_inside_dependency_closure_rejected`
  — cited-parent target carries the label but is also a
  transitive dependency of recovery_epic; asserts status
  `missing_dependency_or_cited_parent` (the parent must be
  genuinely out-of-scope, not reachable).
- `tests/plans/test_deferral.py::test_validate_happy_path`.

**Acceptance:**

- A3.1 — `src/gobby/plans/coverage.py` exports `CoversRecord`,
  `parse_covers_label`, `validate_covers`, `COVERS_LABEL_REGEX`,
  `CoversValidationResult`, `InvalidCoversLabelError`. file:
  `src/gobby/plans/coverage.py`.
- A3.2 — `parse_covers_label` parses
  `"covers:task-12725-lifecycle-dispatch:1.7:1.7.3"` to
  `CoversRecord(plan_id="task-12725-lifecycle-dispatch",
  section_id="1.7", item_id="1.7.3")`. test:
  `tests/plans/test_covers.py::test_parse_valid_label`.
- A3.3 — `parse_covers_label` rejects malformed labels with
  `InvalidCoversLabelError`. test:
  `tests/plans/test_covers.py::test_parse_rejects_malformed`.
- A3.4 — `COVERS_LABEL_REGEX` plan_id grammar is exactly
  `[A-Za-z0-9._-]+` (same allowlist as A4's `_sanitize`). symbol:
  `gobby.plans.coverage.COVERS_LABEL_REGEX`.
- A3.5 — `validate_covers` returns `missing_section` when
  `record.section_id` does not resolve in `plan_doc`. test:
  `tests/plans/test_covers.py::test_validate_covers_missing_section`.
- A3.6 — `validate_covers` returns `missing_item` when section
  resolves but item does not. test:
  `tests/plans/test_covers.py::test_validate_covers_missing_item`.
- A3.7 — `validate_covers` returns `artifact_not_referenced` when
  leaf validation_criteria does not name the artifact under any of
  the four artifact-kind matching rules. test:
  `tests/plans/test_covers.py::test_validate_covers_overbroad_rejected`.
- A3.8 — `src/gobby/plans/deferral.py` exports `validate_deferral`,
  `DeferralValidationResult`, `TaskStoreProtocol`. file:
  `src/gobby/plans/deferral.py`.
- A3.9 — `validate_deferral` short-circuits on each of the six
  failure cases in the order documented. test:
  `tests/plans/test_deferral.py::test_validate_task_missing`,
  `test_validate_task_closed`,
  `test_validate_missing_provenance_label`,
  `test_validate_criteria_does_not_duplicate`,
  `test_validate_missing_reason_or_owner`,
  `test_validate_missing_dependency_or_cited_parent`.
- A3.10 — `validate_deferral` returns `valid` only when all six
  conditions hold. test:
  `tests/plans/test_deferral.py::test_validate_happy_path`.
- A3.11 — `validate_deferral` accepts a kwarg-only
  `recovery_epic_ref: str` and `TaskStoreProtocol` exposes
  `get_task_dependencies(task_ref) -> list[str]` to enable the
  dependency check. symbol:
  `gobby.plans.deferral.validate_deferral`.
- A3.12 — `validate_deferral` accepts the deferral when the target
  task is a transitive dependency of `recovery_epic_ref`, OR when
  the target's `cited-parent:<ref>` label resolves to an open
  task that simultaneously carries
  `out-of-scope-for:<recovery_epic_ref>` AND is NOT reachable
  from `recovery_epic_ref` dependencies. test:
  `tests/plans/test_deferral.py::test_validate_dependency_path`,
  `tests/plans/test_deferral.py::test_validate_cited_parent_path`,
  `tests/plans/test_deferral.py::test_validate_cited_parent_without_out_of_scope_label_rejected`,
  `tests/plans/test_deferral.py::test_validate_cited_parent_inside_dependency_closure_rejected`.

## A4 Coverage library and `gobby plan coverage` CLI

`kind: deliverable`

The deterministic gate consumed by expansion-qa (A5),
holistic-review (via A6 evidence), and CI (A9). All three call this
library; none re-implement parsing or matching.

**Files to create / modify:**

- `src/gobby/plans/coverage.py` (extends A3's covers-record portion).
- `src/gobby/plans/coverage_manifest.py` (new — manifest read/write,
  identity-tuple keying, sanitization helper).
- `src/gobby/cli/plan.py` (new — `gobby plan coverage` Click
  command).
- `src/gobby/cli/__init__.py` (modify — register the `plan` subgroup).
- `tests/plans/test_coverage.py` (new).
- `tests/plans/test_coverage_signature.py` (new).
- `tests/plans/test_coverage_manifest_path.py` (new — sanitization
  edge cases).
- `tests/plans/test_coverage_cli.py` (new).
- `tests/plans/test_coverage_identity.py` (new — duplicate-identity
  rejection).

**Symbols / types / signatures:**

```python
# src/gobby/plans/coverage.py
class CoverageStatus(StrEnum):
    covered = "covered"
    deferred = "deferred"
    missing = "missing"
    invalid = "invalid"

class EvidenceKind(StrEnum):
    commits = "commits"
    task_diff = "task-diff"
    worktree_diff = "worktree-diff"
    coverage_matrix = "coverage-matrix"
    none = "none"

class TaskTreeSource(StrEnum):
    db = "db"
    jsonl = "jsonl"
    matrix_file = "matrix-file"

@dataclass(frozen=True)
class CoverageRowLeaf:
    leaf_task_ref: str
    validation_criteria_snippet: str
    matched_artifact_ref: str

@dataclass(frozen=True)
class CoverageRow:
    section_id: str
    item_id: str
    status: CoverageStatus
    leaves: tuple[CoverageRowLeaf, ...]
    deferral_target: str | None        # task_ref if deferred
    evidence: tuple[EvidenceRow, ...]  # from A6 resolver
    detail: str                        # human-readable failure reason if status != covered

@dataclass(frozen=True)
class CoverageHeader:
    plan_id: str
    plan_hash: str
    root_task_ref: str | None     # None if task_tree=matrix_file
    project_id: str | None        # None if task_tree=matrix_file
    generated_at: str             # ISO 8601 UTC
    task_tree_source: TaskTreeSource
    task_tree_source_hash: str    # hash of the task-tree slice (DB query result, jsonl content, or matrix file bytes)
    evidence_summary: str         # single-line summary of evidence kind+ref

@dataclass(frozen=True)
class CoverageReport:
    header: CoverageHeader
    rows: tuple[CoverageRow, ...]

class MissingScopeError(TypeError):
    """Raised at runtime when task_tree=db|jsonl is used without scope inputs."""

class StaleHashError(ValueError): ...
class IdentityCollisionError(ValueError): ...
class EmptyComponentError(ValueError): ...

# Library entry point — the type annotations make the scope rule load-bearing.
@overload
def evaluate(
    *,
    plan_path: Path,
    plan_hash: str,
    plan_id: str,
    task_tree: Literal[TaskTreeSource.db, TaskTreeSource.jsonl],
    root_task_ref: str,            # required
    project_id: str,               # required
    evidence: str | None = None,
    manifest_path: Path | None = None,
) -> CoverageReport: ...
@overload
def evaluate(
    *,
    plan_path: Path,
    plan_hash: str,
    plan_id: str,
    task_tree: Literal[TaskTreeSource.matrix_file],
    matrix_file: Path,             # required
    root_task_ref: None = None,
    project_id: None = None,
    evidence: str | None = None,
    manifest_path: Path | None = None,
) -> CoverageReport: ...
def evaluate(*, plan_path, plan_hash, plan_id, task_tree, **kwargs) -> CoverageReport: ...
```

The `@overload` chain enforces the scope rule at type-checking time
(mypy); the implementation also enforces it at runtime (raises
`MissingScopeError` when `task_tree in {db, jsonl}` and any of
`root_task_ref`, `project_id` is missing). A4's tests exercise both
paths.

```python
# src/gobby/plans/coverage_manifest.py

# Allowlist exactly: ASCII letters, digits, period, underscore, hyphen.
SAFE_COMPONENT_REGEX: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+$")
COMPONENT_MAX_LEN: int = 64
TRUNCATE_HASH_LEN: int = 7  # hex chars
WINDOWS_RESERVED: frozenset[str] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

def _sanitize(component: str, *, kind: Literal["project_id", "root_task_ref", "plan_id"]) -> str:
    """Portable filesystem sanitization. Raises EmptyComponentError on empty.

    Rules (applied in this order):
    1. If kind=="root_task_ref", drop a single leading "#".
    2. Replace every character outside SAFE_COMPONENT_REGEX with "-".
    3. Strip leading and trailing "-", ".", "_".
    4. If the result is empty, raise EmptyComponentError.
    5. If the result.upper() (ASCII uppercase) is in WINDOWS_RESERVED,
       append "_" so "CON" -> "CON_" (still ≤ COMPONENT_MAX_LEN unless
       step 6 triggers).
    6. If len(result) > COMPONENT_MAX_LEN, replace with
       `result[:COMPONENT_MAX_LEN - 1 - TRUNCATE_HASH_LEN] + "-" +
       sha256(original_pre_step_2).hexdigest()[:TRUNCATE_HASH_LEN]`.
       The hash is computed on the **pre-replacement** input so two
       distinct raws collapsing to the same step-3 component still
       diverge after truncation.
    """

def coverage_manifest_path(project_id: str, root_task_ref: str, plan_id: str) -> Path:
    """Single source of truth for manifest paths.

    Returns `.gobby/plans/coverage/<sani(project_id)>/<sani(root_task_ref)>/<sani(plan_id)>.coverage.yaml`.
    Reject empty inputs via _sanitize.
    """

@dataclass(frozen=True)
class ManifestIdentity:
    project_id: str
    root_task_ref: str
    plan_id: str
    plan_hash: str

def read_manifest(path: Path) -> ManifestIdentity | None: ...
def write_manifest(path: Path, report: CoverageReport, *, regenerate: bool = False) -> None: ...
```

`write_manifest` reads any existing manifest at `path`. If the
existing manifest's `(project_id, root_task_ref, plan_id)` matches and
its `plan_hash` matches the report header, it overwrites in place. If
the identity matches but the hash differs, it raises
`IdentityCollisionError(existing_hash, new_hash)` unless
`regenerate=True`, in which case it overwrites and writes a single
audit line to `.gobby/plans/coverage/.regenerate.log` containing
`<UTC ISO> <project_id> <root_task_ref> <plan_id> <old_hash> -> <new_hash>`.

**Path-collision write protection (F5 + R7/F1).** Before writing,
`write_manifest` performs **full-path identity verification at every
component**, not just the leaf filename. The check covers four
distinct collision modes:

1. **Exact-path identity mismatch.** If a manifest exists at the
   exact target `path` and its
   `(project_id, root_task_ref, plan_id)` identity differs from the
   new identity, `write_manifest` raises
   `PathIdentityMismatchError(existing_path, existing_identity,
   new_identity)` regardless of `regenerate=`. (`regenerate=True`
   is scoped to same-identity hash collisions only — it cannot be
   used to overwrite a different-identity manifest.)

2. **Casefold-equal final filename.** If a sibling in the immediate
   parent directory has a casefold-equal name to the target leaf
   filename and holds a manifest with a different identity, raise
   `PathIdentityMismatchError`. This covers macOS HFS+/APFS
   default and Windows NTFS case-insensitive semantics on the leaf.

3. **Casefold-equal ancestor directory.** Walk every ancestor
   directory under `.gobby/plans/coverage/` from root toward
   target. At each level, enumerate siblings of the next component
   and casefold-compare. If a casefold-equal sibling exists whose
   resolved subtree contains a manifest with a different identity,
   raise `PathIdentityMismatchError`. This catches collisions where
   the project-id or root-task-ref components differ only by case
   or normalization, and a case-insensitive filesystem on a
   different machine would otherwise conflate them.

4. **Sanitization-collapse path collision.** Two distinct raw
   inputs that `_sanitize` reduces to the same canonical component
   are detected by the parent-dir enumeration in (2)/(3) — the
   writer reads the manifest header at the existing path and
   compares its identity to the new write. Same-path different-
   identity always raises `PathIdentityMismatchError`. Examples
   covered: `project_id="foo/bar"` vs `"foo-bar"`, `root_task_ref="#abc"`
   vs `"abc"`, and casefold-collapsing project/root components
   such as `"ABC"` vs `"abc"`.

Reader-side warning (deprecated to defense-in-depth) is preserved
for legacy manifests that predate this writer rule. `regenerate=True`
applies ONLY to same-identity hash refresh; it cannot bypass any
`PathIdentityMismatchError`. Exit code `8` is the canonical surface
for `PathIdentityMismatchError`; CI distinguishes this from
identity-hash collisions (exit `5`).

**`gobby plan coverage` CLI** (Click command in `src/gobby/cli/plan.py`):

```bash
gobby plan coverage \
  --plan PATH \
  --plan-id ID \
  --plan-hash SHA256 \
  --task-tree {db,jsonl,matrix-file} \
  [--root-task REF] \
  [--project-id ID] \
  [--matrix-file PATH] \
  [--evidence SPEC] \
  [--manifest PATH] \
  [--regenerate]
```

Exit codes:

- `0` — every deliverable acceptance item is `covered` or `deferred`.
- `2` — at least one row has status `missing`.
- `3` — at least one row has status `invalid`.
- `4` — `StaleHashError` (plan_hash on disk differs from --plan-hash).
- `5` — `IdentityCollisionError` (existing manifest at path with
  different plan_hash and `--regenerate` not passed).
- `6` — `MissingScopeError` (db/jsonl without scope inputs), surfaced
  by the CLI Click validation layer with exit code 6.
- `7` — `EmptyComponentError` (sanitization rejected an input).
- `8` — `PathIdentityMismatchError` (any path-component collision
  — exact-path, casefold-equal leaf filename, casefold-equal
  ancestor directory, or sanitization-collapse — produces a
  manifest at the target with a different
  `(project_id, root_task_ref, plan_id)` identity).

**Manifest YAML schema** (the file `<plan_id>.coverage.yaml`):

```yaml
header:
  plan_id: "<id>"
  plan_hash: "<sha256>"
  root_task_ref: "<ref or null>"
  project_id: "<id or null>"
  generated_at: "<UTC ISO>"
  task_tree_source: "db | jsonl | matrix-file"
  task_tree_source_hash: "<sha256 of slice bytes>"
  evidence_summary: "<single-line summary>"
rows:
  - section_id: "<id>"
    item_id: "<id>"
    status: "covered | deferred | missing | invalid"
    detail: "<human-readable reason>"
    leaves:
      - leaf_task_ref: "<ref>"
        validation_criteria_snippet: "<text>"
        matched_artifact_ref: "<ref>"
    deferral_target: "<task_ref or null>"
    evidence:
      - kind: "commits | task-diff | worktree-diff | coverage-matrix | none"
        ref: "<ref>"
        status: "resolved | invalid"
        reason: "<text>"
```

**Tests:**

- `tests/plans/test_coverage.py::test_evaluate_db_happy_path` — fake
  task store with leaves carrying valid `covers:` labels covering
  every acceptance item; assert all rows `covered`.
- `tests/plans/test_coverage.py::test_evaluate_db_missing_item` — one
  acceptance item uncovered; assert row `missing`.
- `tests/plans/test_coverage.py::test_evaluate_db_invalid_covers` —
  leaf carries valid label but validation_criteria does not name the
  artifact; assert row `invalid` with the leaf cited.
- `tests/plans/test_coverage.py::test_evaluate_db_deferred` —
  deferred section with valid deferral; assert row `deferred` with
  `deferral_target` set.
- `tests/plans/test_coverage.py::test_evaluate_db_deferred_invalid` —
  deferred section whose task_ref points at a closed task; assert
  row `invalid` (the deferral itself failed validation).
- `tests/plans/test_coverage.py::test_evaluate_jsonl` — same surface
  via `.gobby/tasks.jsonl`.
- `tests/plans/test_coverage.py::test_evaluate_matrix_file` — read a
  pre-generated manifest as the task-tree source.
- `tests/plans/test_coverage.py::test_evaluate_stale_hash` —
  `--plan-hash` differs from the on-disk hash; assert `StaleHashError`
  / exit 4.
- `tests/plans/test_coverage.py::test_evaluate_root_scope_excludes_other_subtree`
  — leaves outside `--root-task` subtree are ignored even if they
  carry valid `covers:` labels for this plan.
- `tests/plans/test_coverage_signature.py::test_db_without_root_task_raises`
  — calling `evaluate(task_tree=db, plan_path=..., plan_hash=..., plan_id=..., project_id=...)`
  without `root_task_ref` raises `MissingScopeError`.
- `tests/plans/test_coverage_signature.py::test_db_without_project_raises`.
- `tests/plans/test_coverage_signature.py::test_jsonl_without_scope_raises`.
- `tests/plans/test_coverage_signature.py::test_matrix_file_without_path_raises`.
- `tests/plans/test_coverage_signature.py::test_matrix_file_rejects_root_task_ref`
  — calling `evaluate(task_tree=TaskTreeSource.matrix_file,
  matrix_file=<path>, root_task_ref="#13175", ...)` raises
  `MissingScopeError`/`TypeError` (matrix-file mode rejects
  scope inputs).
- `tests/plans/test_coverage_signature.py::test_matrix_file_rejects_project_id`
  — same with `project_id`.
- `tests/plans/test_coverage_signature.py::test_mypy_overload_rejects_db_without_scope`
  — runs `mypy --strict` on a fixture file that calls
  `evaluate(task_tree=TaskTreeSource.db, ...)` without scope inputs;
  asserts mypy flags it.
- `tests/plans/test_coverage_manifest_path.py::test_canonical_form` —
  `coverage_manifest_path("d45...", "13175", "task-13175-plan-coverage-contract")`
  yields the documented path.
- `tests/plans/test_coverage_manifest_path.py::test_drops_leading_hash`
  — `_sanitize("#12725", kind="root_task_ref")` returns `"12725"`.
- `tests/plans/test_coverage_manifest_path.py::test_strips_punct`
  — `_sanitize(".__weird-_-.", kind="plan_id")` strips both ends.
- `tests/plans/test_coverage_manifest_path.py::test_replaces_disallowed_chars`
  — `_sanitize("foo bar/baz", kind="plan_id")` returns `"foo-bar-baz"`.
- `tests/plans/test_coverage_manifest_path.py::test_rejects_empty_post_sanitize`
  — `_sanitize("///", kind="plan_id")` raises `EmptyComponentError`.
- `tests/plans/test_coverage_manifest_path.py::test_rejects_path_traversal`
  — `_sanitize("../etc/passwd", kind="plan_id")` produces a non-`..`
  string; verify the resulting `coverage_manifest_path` resolves
  inside `.gobby/plans/coverage/`.
- `tests/plans/test_coverage_manifest_path.py::test_windows_reserved_disambiguated`
  — `_sanitize("CON", kind="plan_id")` returns `"CON_"`;
  case-insensitive: `"con"` → `"con_"`; `"Lpt9"` → `"Lpt9_"`.
- `tests/plans/test_coverage_manifest_path.py::test_truncate_with_hash`
  — input length 100 produces `_sanitize` output of length
  `COMPONENT_MAX_LEN`, ending in `-` followed by 7 hex chars.
- `tests/plans/test_coverage_manifest_path.py::test_truncate_hash_disambiguates_collisions`
  — two distinct 100-char inputs whose first 56 chars differ by one
  letter produce distinct truncated outputs.
- `tests/plans/test_coverage_manifest_path.py::test_truncate_hash_uses_pre_replacement_input`
  — two inputs that collapse to the same step-3 component but differ
  before character replacement still produce distinct outputs.
- `tests/plans/test_coverage_manifest_path.py::test_case_collision_warning`
  — on a case-insensitive filesystem fixture, two plan_ids that
  differ only in case both resolve to the same path (since
  `_sanitize` is case-preserving); the manifest reader detects this
  and the test asserts a documented warning is emitted.
- `tests/plans/test_coverage_cli.py::test_cli_args_required` — Click
  rejects missing `--plan`, `--plan-id`, `--plan-hash`, `--task-tree`.
- `tests/plans/test_coverage_cli.py::test_cli_db_requires_scope` —
  `--task-tree db` without `--root-task` and `--project-id` exits 6.
- `tests/plans/test_coverage_cli.py::test_cli_matrix_file_requires_path`
  — `--task-tree matrix-file` without `--matrix-file` exits 6.
- `tests/plans/test_coverage_cli.py::test_cli_exit_codes_per_status` —
  table-driven: `0` for all-covered, `2` for missing, `3` for invalid,
  `4` for stale hash, `5` for identity collision, `7` for empty
  component.
- `tests/plans/test_coverage_identity.py::test_identity_collision_blocks_overwrite`
  — write manifest with hash A; call `write_manifest` with hash B;
  raises `IdentityCollisionError`.
- `tests/plans/test_coverage_identity.py::test_regenerate_overwrites_and_audits`
  — with `regenerate=True`, overwrites and appends one line to
  `.regenerate.log`.
- `tests/plans/test_coverage_identity.py::test_same_plan_two_root_tasks_distinct_manifests`
  — `coverage_manifest_path(p, "12725", "task-12725-foo")` and
  `coverage_manifest_path(p, "13175", "task-12725-foo")` are
  distinct files.
- `tests/plans/test_coverage_identity.py::test_two_plans_one_root_distinct_manifests`
  — same root, different plan_ids.
- `tests/plans/test_coverage_identity.py::test_casefold_leaf_collision_raises_path_identity_mismatch`
  — under the same `(project_id, root_task_ref)`, two plan_ids
  `task-13175-Plan-Coverage-Contract` and
  `task-13175-plan-coverage-contract` produce paths that casefold-
  equal each other; `write_manifest` raises
  `PathIdentityMismatchError` and writes nothing on the second
  call.
- `tests/plans/test_coverage_identity.py::test_path_identity_mismatch_emits_exit_8`
  — CLI invocation triggering `PathIdentityMismatchError` exits
  with code 8.
- `tests/plans/test_coverage_identity.py::test_casefold_protection_works_on_case_sensitive_fs`
  — fixture forces case-sensitive directory enumeration; the
  writer-side `casefold()` comparison still fires on the second
  write attempt regardless of FS case sensitivity.
- `tests/plans/test_coverage_identity.py::test_exact_path_different_identity_raises`
  — write manifest A at canonical path; call `write_manifest`
  with a manifest carrying different
  `(project_id, root_task_ref, plan_id)` but the same rendered
  target path (constructed by feeding distinct raw inputs that
  collapse via sanitization to the same canonical components);
  raises `PathIdentityMismatchError` even with `regenerate=True`.
- `tests/plans/test_coverage_identity.py::test_casefold_ancestor_dir_collision_raises`
  — write manifest under
  `coverage/<project_id_caseA>/<root_task_ref>/<plan_id>.coverage.yaml`;
  on second write, use a casefold-equal `<project_id_caseB>` (e.g.,
  `"ABC"` vs `"abc"`); writer walks ancestors and detects the
  casefold-equal sibling directory whose subtree contains a
  manifest with different identity; raises
  `PathIdentityMismatchError`.
- `tests/plans/test_coverage_identity.py::test_root_task_ref_hash_strip_collision_raises`
  — first write uses `root_task_ref="#abc"`, second write uses
  `root_task_ref="abc"`; both sanitize to the same canonical
  component; the writer detects existing manifest at the target
  path with different identity and raises
  `PathIdentityMismatchError`.
- `tests/plans/test_coverage_identity.py::test_sanitize_collapse_collision_raises`
  — first write uses `project_id="foo/bar"`, second write uses
  `project_id="foo-bar"`; both sanitize to `foo-bar`; existing
  manifest detected, raises `PathIdentityMismatchError`.
- `tests/plans/test_coverage_identity.py::test_regenerate_does_not_bypass_path_identity_mismatch`
  — even with `regenerate=True`, `PathIdentityMismatchError` is
  raised when the existing manifest at target has a different
  identity. `regenerate=True` is scoped to same-identity hash
  refresh only.

**Acceptance:**

- A4.1 — `src/gobby/plans/coverage.py` exports `evaluate`,
  `CoverageReport`, `CoverageRow`, `CoverageRowLeaf`,
  `CoverageHeader`, `CoverageStatus`, `EvidenceKind`,
  `TaskTreeSource`, `MissingScopeError`, `StaleHashError`. file:
  `src/gobby/plans/coverage.py`.
- A4.2 — `evaluate(task_tree=TaskTreeSource.db|jsonl, ...)` requires
  `plan_id`, `root_task_ref`, `project_id` at type-check time
  (mypy `@overload`) and at runtime (`MissingScopeError`). symbol:
  `gobby.plans.coverage.evaluate`. test:
  `tests/plans/test_coverage_signature.py::test_db_without_root_task_raises`,
  `test_db_without_project_raises`,
  `test_jsonl_without_scope_raises`,
  `test_mypy_overload_rejects_db_without_scope`.
- A4.3 — `evaluate(task_tree=TaskTreeSource.matrix_file, ...)`
  accepts only `matrix_file`; passing `root_task_ref` or
  `project_id` is rejected at type-check time (mypy `@overload`)
  and at runtime (`MissingScopeError` / `TypeError`). test:
  `tests/plans/test_coverage_signature.py::test_matrix_file_without_path_raises`,
  `tests/plans/test_coverage_signature.py::test_matrix_file_rejects_root_task_ref`,
  `tests/plans/test_coverage_signature.py::test_matrix_file_rejects_project_id`.
- A4.4 — `src/gobby/cli/plan.py` defines the `gobby plan coverage`
  Click command with all ten flags: four required
  (`--plan`, `--plan-id`, `--plan-hash`, `--task-tree`) and six
  optional (`--root-task`, `--project-id`, `--matrix-file`,
  `--evidence`, `--manifest`, `--regenerate`). file:
  `src/gobby/cli/plan.py`. test:
  `tests/plans/test_coverage_cli.py::test_cli_help_lists_exact_ten_flags`
  asserts `gobby plan coverage --help` lists exactly those ten
  options — no extras, no omissions — with the four required
  options marked as such.
- A4.5 — CLI exit codes match the documented table (0, 2, 3, 4, 5,
  6, 7, 8). test:
  `tests/plans/test_coverage_cli.py::test_cli_exit_codes_per_status`.
- A4.6 — `coverage_manifest_path(project_id, root_task_ref, plan_id)`
  returns `Path(".gobby/plans/coverage") / sani(project) /
  sani(root) / f"{sani(plan_id)}.coverage.yaml"`. symbol:
  `gobby.plans.coverage_manifest.coverage_manifest_path`.
- A4.7 — `_sanitize` allowlist is exactly `[A-Za-z0-9._-]`; rejects
  empty post-sanitize with `EmptyComponentError`. test:
  `tests/plans/test_coverage_manifest_path.py::test_replaces_disallowed_chars`,
  `test_rejects_empty_post_sanitize`.
- A4.8 — `_sanitize(..., kind="root_task_ref")` drops a single
  leading `#`. test:
  `tests/plans/test_coverage_manifest_path.py::test_drops_leading_hash`.
- A4.9 — `_sanitize` strips leading and trailing `-`, `.`, `_`. test:
  `tests/plans/test_coverage_manifest_path.py::test_strips_punct`.
- A4.10 — `_sanitize` truncates to `COMPONENT_MAX_LEN` with
  `-<sha256[:7]>` suffix on overflow; hash uses the pre-replacement
  input. test:
  `tests/plans/test_coverage_manifest_path.py::test_truncate_with_hash`,
  `test_truncate_hash_uses_pre_replacement_input`,
  `test_truncate_hash_disambiguates_collisions`.
- A4.11 — `_sanitize` disambiguates Windows reserved names
  (case-insensitive: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
  `LPT1`–`LPT9`) by appending `_`. test:
  `tests/plans/test_coverage_manifest_path.py::test_windows_reserved_disambiguated`.
- A4.12 — sanitization edge-case test suite covers case-collision,
  Windows reserved, slash/path traversal, distinct raws collapsing,
  truncate-with-hash determinism. file:
  `tests/plans/test_coverage_manifest_path.py`.
- A4.13 — `write_manifest` raises `IdentityCollisionError` when
  identity matches and hash differs without `regenerate=True`;
  with `regenerate=True` it overwrites and appends one audit line
  to `.gobby/plans/coverage/.regenerate.log`. test:
  `tests/plans/test_coverage_identity.py::test_identity_collision_blocks_overwrite`,
  `test_regenerate_overwrites_and_audits`.
- A4.14 — `CoverageReport.header` includes `plan_id`, `plan_hash`,
  `root_task_ref`, `project_id`, `generated_at`,
  `task_tree_source`, `task_tree_source_hash`, `evidence_summary`.
  symbol: `gobby.plans.coverage.CoverageHeader`.
- A4.15 — `CoverageRow.status ∈ CoverageStatus` and `leaves` carries
  `(leaf_task_ref, validation_criteria_snippet, matched_artifact_ref)`
  per matched leaf. symbol: `gobby.plans.coverage.CoverageRow`.
- A4.16 — multi-plan-multi-root path scheme: same plan reused under
  two roots produces two distinct manifest paths; two plans under
  the same root produce two distinct manifest paths. test:
  `tests/plans/test_coverage_identity.py::test_same_plan_two_root_tasks_distinct_manifests`,
  `test_two_plans_one_root_distinct_manifests`.
- A4.17 — `evaluate` excludes leaves outside `--root-task` subtree
  even if they carry matching `covers:` labels. test:
  `tests/plans/test_coverage.py::test_evaluate_root_scope_excludes_other_subtree`.
- A4.18 — `write_manifest` raises `PathIdentityMismatchError` and
  writes nothing on any path-component collision (exact-path
  identity mismatch, casefold-equal leaf filename, casefold-equal
  ancestor directory, or sanitization-collapse) that produces a
  manifest at the target path with a different
  `(project_id, root_task_ref, plan_id)` identity. `regenerate=True`
  is scoped to same-identity hash refresh only and CANNOT bypass
  this error. CLI exit code `8` surfaces the failure. symbol:
  `gobby.plans.coverage_manifest.PathIdentityMismatchError`. test:
  `tests/plans/test_coverage_identity.py::test_casefold_leaf_collision_raises_path_identity_mismatch`,
  `tests/plans/test_coverage_identity.py::test_path_identity_mismatch_emits_exit_8`,
  `tests/plans/test_coverage_identity.py::test_casefold_protection_works_on_case_sensitive_fs`,
  `tests/plans/test_coverage_identity.py::test_exact_path_different_identity_raises`,
  `tests/plans/test_coverage_identity.py::test_casefold_ancestor_dir_collision_raises`,
  `tests/plans/test_coverage_identity.py::test_root_task_ref_hash_strip_collision_raises`,
  `tests/plans/test_coverage_identity.py::test_sanitize_collapse_collision_raises`,
  `tests/plans/test_coverage_identity.py::test_regenerate_does_not_bypass_path_identity_mismatch`.

## A5 Expansion-QA integration

`kind: deliverable`

Update the expansion-qa agent so validation calls the A4 library
mechanically; replace any pre-existing ad-hoc parsing or matching.

The expansion-qa agent today carries its prompt/instructions inline
in `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`
(verified: no `src/gobby/install/shared/skills/expansion-qa/`
directory exists). All Epic 1 contract content for expansion-qa
lands in the agent YAML's `instructions:` block plus the workflow
step list — there is no separate SKILL.md.

**Files to modify:**

- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` —
  workflow steps invoke the A4 library / CLI, persist manifest,
  reject on missing/invalid rows. The agent's `instructions:` block
  documents the mechanical-rejection contract (replacing the
  earlier "skill SKILL.md" target).

**Files to create:**

- `tests/workflows/test_expansion_qa_coverage_call.py`.
- `tests/workflows/test_expansion_qa_rejection.py`.
- `tests/workflows/test_expansion_qa_persists_manifest.py`.

**Symbols / behavior contract:**

**Sequencing (F6).** A5 reads `task_artifacts.plan_file_hash` and
writes `coverage_matrix_path` through the artifact MCP surface.
`plan_file_hash` is added by A6's migration (A6.8 / A6.9) and the
existing artifact MCP tools' schemas are extended in A6.14 to surface
it. A5's leaves therefore depend on A6.8, A6.9, A6.14, and A6.15
landing first; the bootstrap ledger encodes this as an explicit
`depends_on` edge in the `notes:` field of A5.1, A5.4, A5.5, and
A5.7 expected_leaves and the Epic 1 expansion sequences leaves
accordingly. Implementation work on A5 cannot start until A6's
storage column and MCP schema changes are merged.

The expansion-qa workflow gains a deterministic step that:

1. Reads the epic's `task_artifacts.plan_file_path`,
   `task_artifacts.plan_file_hash` (added in A6's migration if not
   already present — see A6 for plan_file_hash story; A5 documents
   the read contract), `tasks.id` (= `root_task_ref`), and the
   project_id from session context.
2. Re-computes `plan_hash = sha256(read_bytes(plan_file_path))`. If
   `plan_hash != plan_file_hash`, fails the run with reason
   `plan_hash_drift` and writes the new hash to artifacts (operator
   intervention required).
3. Calls the A4 library:

   ```python
   evaluate(
       plan_path=plan_file_path,
       plan_hash=plan_hash,
       plan_id=plan_id,             # derived from filename
       task_tree=TaskTreeSource.db,
       root_task_ref=epic_task_ref,
       project_id=project_id,
       evidence="none",
       manifest_path=coverage_manifest_path(project_id, epic_task_ref, plan_id),
   )
   ```

   The library signature **rejects** `db`/`jsonl` without scope at
   type/test time (A4.2), so a stale call site fails noisily.
4. If any row has status `missing` or `invalid`, calls
   `gobby-tasks:mark_task_review_rejected` with `rejection_notes`
   that include, for each failing row,
   `(section_id, item_id, status, detail)` and the leaves that
   claimed-but-failed coverage. The rejection message is
   structured (one bullet per row) so the planner agent can act on
   it.
5. Persists the manifest at the canonical scoped path; writes the
   path to `task_artifacts.coverage_matrix_path` via
   `gobby-tasks-ops:set_artifact` (the new MCP tool added in
   #12725's §1.1d — note this dependency: §1.1d landed with #12725's
   storage foundation; A5 just consumes the existing tool).
6. On success (zero `missing|invalid` rows), calls
   `gobby-tasks:mark_task_review_approved` with `approval_notes`
   citing the manifest path.

The skill content emphasizes: this step is **mechanical**, not
LLM-judged. The workflow YAML invokes the library/CLI directly
(via a dedicated `gobby plan coverage` CLI step or a
`gobby-tasks-ops` thin wrapper if that path is preferred for
in-process execution; either way the implementation is one of the
two and the test fixture asserts the chosen path).

**Tests:**

- `tests/workflows/test_expansion_qa_coverage_call.py::test_workflow_calls_evaluate_with_full_scope`
  — fixture workflow execution; assert the A4 library is called
  with all four scope inputs (plan_id, plan_hash, root_task_ref,
  project_id) and `task_tree=db`.
- `tests/workflows/test_expansion_qa_coverage_call.py::test_workflow_uses_coverage_manifest_path_helper`
  — assert the manifest path written to artifacts equals
  `coverage_manifest_path(...)` rather than an ad-hoc string.
- `tests/workflows/test_expansion_qa_rejection.py::test_missing_row_triggers_rejection`
  — fixture with one uncovered acceptance item; assert
  `mark_task_review_rejected` is invoked with rejection_notes
  citing `(section_id, item_id, "missing", detail)`.
- `tests/workflows/test_expansion_qa_rejection.py::test_invalid_row_triggers_rejection`
  — fixture with a leaf carrying valid covers label but no
  artifact reference; assert rejection cites the leaf.
- `tests/workflows/test_expansion_qa_rejection.py::test_zero_missing_invalid_triggers_approval`
  — fixture with full coverage; assert
  `mark_task_review_approved` invoked with manifest path.
- `tests/workflows/test_expansion_qa_persists_manifest.py::test_manifest_written_at_canonical_path`
  — assert manifest file exists at the path returned by
  `coverage_manifest_path` and the YAML is well-formed.
- `tests/workflows/test_expansion_qa_persists_manifest.py::test_artifact_pointer_written`
  — assert `task_artifacts.coverage_matrix_path` equals the
  manifest path.
- `tests/workflows/test_expansion_qa_persists_manifest.py::test_plan_hash_drift_fails`
  — fixture where `task_artifacts.plan_file_hash` does not match
  the on-disk file; assert workflow fails with reason
  `plan_hash_drift`.

**Acceptance:**

- A5.1 — expansion-qa workflow step calls the configured coverage
  execution path.
  `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`
  workflow step calls the A4 coverage library or `gobby plan
  coverage` CLI with: `--plan` from `task_artifacts.plan_file_path`,
  `--plan-id` (derived from filename), `--plan-hash` (recomputed),
  `--root-task` (= epic ref), `--project-id` (from session
  context), `--task-tree db`. file:
  `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`.
- A5.2 — the `instructions:` block in
  `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`
  documents the mechanical-rejection contract: any deliverable
  acceptance item with status `missing` or `invalid` rejects the
  run. file:
  `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`.
- A5.3 — rejection_notes cite each failing row by
  `(section_id, item_id, status, detail)` and the leaves that
  claimed-but-failed coverage. test:
  `tests/workflows/test_expansion_qa_rejection.py::test_missing_row_triggers_rejection`.
- A5.4 — manifest is persisted to
  `coverage_manifest_path(project_id, root_task_ref, plan_id)`;
  the path is written to `task_artifacts.coverage_matrix_path`
  via `gobby-tasks-ops:set_artifact`. test:
  `tests/workflows/test_expansion_qa_persists_manifest.py::test_manifest_written_at_canonical_path`,
  `test_artifact_pointer_written`.
- A5.5 — A4 library is invoked with all four scope inputs
  (`plan_id`, `plan_hash`, `root_task_ref`, `project_id`) — never
  partial. test:
  `tests/workflows/test_expansion_qa_coverage_call.py::test_workflow_calls_evaluate_with_full_scope`.
- A5.6 — successful run (zero `missing|invalid` rows) calls
  `gobby-tasks:mark_task_review_approved`. test:
  `tests/workflows/test_expansion_qa_rejection.py::test_zero_missing_invalid_triggers_approval`.
- A5.7 — plan-hash drift between `task_artifacts.plan_file_hash`
  and the recomputed file hash fails the run with reason
  `plan_hash_drift`. test:
  `tests/workflows/test_expansion_qa_persists_manifest.py::test_plan_hash_drift_fails`.

## A6 Evidence-based holistic-review gate

`kind: deliverable`

Phase A ships the gate library; the holistic-review skill / agent
that consumes it qualitatively are Epic 2 (Phase E) work. Epic 1's
deliverable is the evidence resolver, the schema additions, and the
isolation-base capture path.

**Files to create / modify:**

- `src/gobby/plans/evidence.py` (new — evidence resolver).
- `src/gobby/storage/migrations.py` (modify — additive migration:
  `task_artifacts.base_commit_sha` nullable; `task_artifacts.plan_file_hash`
  nullable if not already present).
- `src/gobby/storage/baseline_schema.sql` (modify — add the same
  columns with the same nullable semantics for fresh installs).
- `src/gobby/storage/tasks/_artifacts.py` (modify — extend
  `set_artifacts_atomic` and `_validate_constraints` for app-level
  enforcement; add `clear_isolation_pair` clearing
  `base_commit_sha` atomically).
- `src/gobby/agents/isolation.py` (modify — capture
  `base_commit_sha` via `git rev-parse HEAD` immediately after
  worktree/clone creation; populate via `set_artifacts_atomic`).
- `src/gobby/mcp_proxy/tools/tasks/_artifacts.py` (modify — extend
  the existing `get_artifacts`, `set_artifact`,
  `set_artifacts_atomic`, and `clear_isolation_pair` MCP tool
  schemas to surface the new `base_commit_sha` and
  `plan_file_hash` columns with explicit nullability). The artifact
  tools were added under #12725's §1.1d and live in the modular
  `tasks/` package — no `tasks_ops.py` file exists; Epic 1 extends
  the existing surface, it does not introduce new tools or files.

**Files to create:**

- `tests/plans/test_evidence.py`.
- `tests/plans/test_evidence_worktree_diff.py`.
- `tests/storage/tasks/test_artifacts_isolation_base.py`.
- `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py`.
- `tests/agents/test_isolation_base_capture.py`.
- `tests/storage/tasks/test_artifacts_plan_file_hash.py`.

**Symbols / types / signatures:**

```python
# src/gobby/plans/evidence.py
class EvidenceResolveStatus(StrEnum):
    resolved = "resolved"
    invalid = "invalid"

@dataclass(frozen=True)
class EvidenceRow:
    kind: EvidenceKind
    ref: str
    status: EvidenceResolveStatus
    detail: str
    artifacts_touched: tuple[str, ...]   # files / symbols / tests detected in the change

@dataclass(frozen=True)
class EvidenceBundle:
    rows: tuple[EvidenceRow, ...]
    summary: str

class InvalidEvidenceError(ValueError): ...
class MissingIsolationBaseError(ValueError): ...

class EvidenceContextProtocol(Protocol):
    repo_root: Path
    def get_task_diff(self, task_ref: str) -> str: ...
    def get_artifacts(self, task_ref: str) -> dict | None: ...
    def get_commit_range_diff(self, range_: str) -> str: ...

def resolve_evidence(spec: str, *, ctx: EvidenceContextProtocol) -> EvidenceBundle: ...
```

`spec` grammar:

- `commits:<range>` — e.g., `commits:abc123..def456` or
  `commits:HEAD~10..HEAD`.
- `task-diff:<task_ref>` — e.g., `task-diff:#12725`.
- `worktree-diff:<artifact_ref>` — `<artifact_ref>` is a task ref;
  the resolver reads `task_artifacts` for that task and selects the
  active isolation family (`worktree_path` or `clone_path`) and
  `base_commit_sha`.
- `coverage-matrix:<path>` — read a manifest file at `<path>`.
- `none` — emits `EvidenceRow(kind=none, status=resolved, detail="explicit operator override")`.

**`worktree-diff` resolution behavior** (the F10/F15/F17 closure):

1. Look up `task_artifacts` for `<artifact_ref>`. If missing, return
   `EvidenceRow(kind=worktree_diff, status=invalid,
   detail="no artifacts row for <ref>")`.
2. Pick the active isolation family:
   - If `worktree_path` set, use it.
   - Else if `clone_path` set, use it.
   - Else return `invalid` with detail
     `"no isolation path on artifacts row for <ref>"`.
3. Read `base_commit_sha`. If `NULL` (legacy row from before this
   migration), return `invalid` with detail
   `"missing base_commit_sha; rerun gobby build to recapture base
   or use set_artifact(base_commit_sha=...) if you can recover it
   out-of-band"`.
4. Run `git -C <isolation_path> rev-parse <base_commit_sha>`. If
   non-zero exit, return `invalid` with detail
   `"base_commit_sha <sha> does not resolve in <isolation_path>"`.
5. Run `git -C <isolation_path> diff <base_commit_sha>...HEAD`.
   Return `EvidenceRow(kind=worktree_diff, status=resolved,
   detail=summary, artifacts_touched=(parsed file list))`.

The resolver does **not** silently degrade to no-evidence on any
failure — every failure path returns an `invalid` row.

**Schema addition** (additive migration):

```sql
-- Migration: add base_commit_sha (nullable) and plan_file_hash (nullable) to task_artifacts.
ALTER TABLE task_artifacts ADD COLUMN base_commit_sha TEXT;
ALTER TABLE task_artifacts ADD COLUMN plan_file_hash TEXT;

-- The existing CHECK constraint on task_artifacts (from #12725 §1.1b/§1.2)
-- is augmented with one additional predicate:
--   base_commit_sha IS NULL when both isolation families are NULL
-- (forward-compat: a non-null base_commit_sha with no isolation family is
-- nonsense). This is added by dropping and recreating the table only if
-- SQLite's ALTER doesn't allow CHECK augmentation in place; A6's migration
-- includes a SQLite-portable rebuild path with full row preservation.
```

App-level enforcement is in `set_artifacts_atomic` and
`_validate_constraints`:

- A **new write** that sets `worktree_path` or `clone_path` AND does
  not also set `base_commit_sha` raises
  `MissingIsolationBaseError` BEFORE touching the DB.
- An **update** to an existing row whose `base_commit_sha` is `NULL`
  (legacy row) is permitted as long as the update does not modify
  the isolation family columns. Modifying isolation columns on a
  legacy row requires `base_commit_sha` to be set in the same
  atomic write.
- `clear_isolation_pair(task_id, family)` clears the family's
  `(path, id)` pair AND clears `base_commit_sha` atomically.

**Tests:**

- `tests/plans/test_evidence.py::test_resolve_commits_range`
  — fixture git repo with commits in a range; assert
  `artifacts_touched` lists the changed files.
- `tests/plans/test_evidence.py::test_resolve_task_diff`
  — fixture context with `get_task_diff` returning a known diff;
  assert parse and rows.
- `tests/plans/test_evidence.py::test_resolve_coverage_matrix`
  — fixture YAML manifest; assert rows are loaded.
- `tests/plans/test_evidence.py::test_resolve_none_emits_audit_row`.
- `tests/plans/test_evidence_worktree_diff.py::test_resolves_with_base_sha`
  — fixture worktree with known base sha; assert diff range.
- `tests/plans/test_evidence_worktree_diff.py::test_invalid_when_artifacts_missing`.
- `tests/plans/test_evidence_worktree_diff.py::test_invalid_when_no_isolation_path`.
- `tests/plans/test_evidence_worktree_diff.py::test_invalid_when_base_sha_null` —
  legacy row; assert `invalid` with the documented repair detail.
- `tests/plans/test_evidence_worktree_diff.py::test_invalid_when_base_sha_unresolvable`
  — `base_commit_sha` set to a nonexistent sha; assert `invalid`
  with the documented detail.
- `tests/plans/test_evidence_worktree_diff.py::test_picks_worktree_over_clone_when_both_present`
  — schema invariant blocks this state, but in a fixture that
  bypasses the CHECK (raw SQL), assert resolver picks `worktree_path`
  consistently.
- `tests/storage/tasks/test_artifacts_isolation_base.py::test_migration_adds_column_nullable`
  — apply migration to a fresh DB; assert column exists and is
  nullable.
- `tests/storage/tasks/test_artifacts_isolation_base.py::test_migration_preserves_legacy_rows`
  — fixture DB with a pre-existing artifacts row (no base);
  apply migration; assert row preserved with `base_commit_sha=NULL`.
- `tests/storage/tasks/test_artifacts_isolation_base.py::test_baseline_schema_matches_post_migration`
  — fresh-install schema from `baseline_schema.sql` matches the
  end-state of running the migration on a pre-#12725 DB.
- `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_new_isolation_write_without_base_raises`.
- `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_legacy_row_update_other_field_permitted`.
- `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_legacy_row_isolation_modify_requires_base`.
- `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_clear_isolation_pair_clears_base`.
- `tests/agents/test_isolation_base_capture.py::test_worktree_handler_captures_base`.
- `tests/agents/test_isolation_base_capture.py::test_clone_handler_captures_base`.
- `tests/agents/test_isolation_base_capture.py::test_base_captured_before_first_agent_run`.
- `tests/storage/tasks/test_artifacts_plan_file_hash.py::test_set_artifact_plan_hash_round_trips`.
- `tests/storage/tasks/test_artifacts_plan_file_hash.py::test_mcp_get_artifacts_includes_plan_file_hash`.

**Acceptance:**

- A6.1 — `src/gobby/plans/evidence.py` exports `resolve_evidence`,
  `EvidenceBundle`, `EvidenceRow`, `EvidenceResolveStatus`,
  `InvalidEvidenceError`, `MissingIsolationBaseError`,
  `EvidenceContextProtocol`. file: `src/gobby/plans/evidence.py`.
- A6.2 — `resolve_evidence("commits:<range>")` returns one row per
  commit with `artifacts_touched` populated from the diff. test:
  `tests/plans/test_evidence.py::test_resolve_commits_range`.
- A6.3 — `resolve_evidence("task-diff:<task_ref>")` reads through
  `EvidenceContextProtocol.get_task_diff` (which wraps
  `gobby-tasks:get_task_diff`). test:
  `tests/plans/test_evidence.py::test_resolve_task_diff`.
- A6.4 — `resolve_evidence("worktree-diff:<artifact_ref>")` computes
  `git diff <base_commit_sha>...HEAD` against the active isolation
  path; uses `base_commit_sha` and **never** `target_branch`. test:
  `tests/plans/test_evidence_worktree_diff.py::test_resolves_with_base_sha`.
- A6.5 — `resolve_evidence("coverage-matrix:<path>")` loads the
  manifest YAML and projects rows. test:
  `tests/plans/test_evidence.py::test_resolve_coverage_matrix`.
- A6.6 — `resolve_evidence("none")` emits an explicit-override
  audit row with `status=resolved`, `detail="explicit operator
  override"`. test:
  `tests/plans/test_evidence.py::test_resolve_none_emits_audit_row`.
- A6.7 — every failure path on `worktree-diff` returns
  `EvidenceRow(status=invalid, detail=...)` with the documented
  repair instructions; never silently no-evidence. test:
  `tests/plans/test_evidence_worktree_diff.py::test_invalid_when_artifacts_missing`,
  `test_invalid_when_no_isolation_path`,
  `test_invalid_when_base_sha_null`,
  `test_invalid_when_base_sha_unresolvable`.
- A6.8 — `src/gobby/storage/migrations.py` adds an additive
  migration adding `task_artifacts.base_commit_sha TEXT` (nullable)
  and `task_artifacts.plan_file_hash TEXT` (nullable). file:
  `src/gobby/storage/migrations.py`.
- A6.9 — `src/gobby/storage/baseline_schema.sql` includes
  `base_commit_sha` and `plan_file_hash` with the same nullable
  semantics. file: `src/gobby/storage/baseline_schema.sql`.
- A6.10 — migration preserves legacy rows: a row created before
  the migration retains its original column values and gains
  `base_commit_sha=NULL`. test:
  `tests/storage/tasks/test_artifacts_isolation_base.py::test_migration_preserves_legacy_rows`.
- A6.11 — `set_artifacts_atomic` raises
  `MissingIsolationBaseError` if a NEW write sets
  `worktree_path` or `clone_path` without `base_commit_sha`. test:
  `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_new_isolation_write_without_base_raises`.
- A6.12 — `src/gobby/agents/isolation.py` captures
  `base_commit_sha` via `git -C <path> rev-parse HEAD` immediately
  after worktree/clone creation, before any agent runs. test:
  `tests/agents/test_isolation_base_capture.py::test_worktree_handler_captures_base`,
  `test_clone_handler_captures_base`,
  `test_base_captured_before_first_agent_run`.
- A6.13 — `clear_isolation_pair(task_id, family)` clears
  `(<family>_path, <family>_id, base_commit_sha)` atomically.
  test:
  `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_clear_isolation_pair_clears_base`.
- A6.14 — MCP tool schemas (`get_artifacts`, `set_artifact`,
  `set_artifacts_atomic`, `clear_isolation_pair`) include
  `base_commit_sha` and `plan_file_hash` with explicit nullability
  in their JSON schemas. behavior: "MCP schemas list
  base_commit_sha and plan_file_hash with type [string, null]" in
  `src/gobby/mcp_proxy/tools/tasks/_artifacts.py`. test:
  `tests/storage/tasks/test_artifacts_plan_file_hash.py::test_mcp_get_artifacts_includes_plan_file_hash`.
- A6.15 — `plan_file_hash` round-trips through `set_artifact`
  and `get_artifacts`. test:
  `tests/storage/tasks/test_artifacts_plan_file_hash.py::test_set_artifact_plan_hash_round_trips`.

> **Note on lifecycle linkage points:** The strategy lists
> `link_commit`, `mark_task_needs_review`, and merge-finalize as the
> three points where worktree-local commits become linked-task
> evidence. `link_commit` and `mark_task_needs_review` are existing
> tools (no Epic 1 modification); the evidence resolver consumes
> their output via `EvidenceContextProtocol.get_task_diff`. The
> merge-finalize linkage is Epic 2's merge agent (Phase E §2.10);
> Epic 1 ships only the resolver and schema substrate.

## A7 #12725 retrofit

`kind: deliverable`

Two-step retrofit. Step 1 lands during Epic 1 implementation
expansion as a planning/refactor leaf; Step 2 generates the
compliance manifest using A4's CLI and is gated on A4 being green.
Both steps are Epic 1 implementation deliverables — the briefing's
phrase "deferred to Epic 1 implementation" means "not part of plan
authoring (the present task #13175); produced by Epic 1's expanded
leaves."

**Files to modify:**

- `.gobby/plans/task-12725-lifecycle-dispatch.md` — retrofit to
  conform to A1.

**Files to generate (Step 2, after A4 lands):**

- `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch.coverage.yaml`
  — produced by:

  ```bash
  gobby plan coverage \
    --plan .gobby/plans/task-12725-lifecycle-dispatch.md \
    --plan-id task-12725-lifecycle-dispatch \
    --plan-hash <recomputed sha256> \
    --root-task '#12725' \
    --project-id d45545c5-ded5-4335-b115-0245752edacf \
    --task-tree db \
    --evidence 'commits:<merged-range>' \
    --manifest .gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch.coverage.yaml
  ```

**Retrofit scope** (Step 1, applied to the existing #12725 plan):

The `task-12725-lifecycle-dispatch.md` plan already uses canonical
section IDs of the form `### 1.1`, `### 1.1a`, `### 1.1b`, … through
`### 4.5` (35 numeric IDs). The retrofit must:

- Add `kind:` front-matter to every section. Most existing sections
  are deliverable; the `## Overview`, `## Pipeline Architecture`,
  `## Constraints`, `## Adversary Review Log`,
  `### Round 1 — REJECTED` … `### Round 8 …`,
  `### Source-Grounding Summary (pre-Round 3)`, `## Phase 1: …`
  (and other Phase headings), `## Task Mapping`,
  `## Verification`, `## Out of Scope (filed as follow-ups)` are
  framing or verification.
- Bare `## Phase N: …` headings and `### Round N — …` headings
  do not match the canonical regex; they must be either retitled to
  carry an ID (e.g., `## P1 Phase 1: Foundation …`) or marked
  `kind: framing` and recorded in `framing_headings`. The retrofit
  task will choose `kind: framing` for round-log subheadings and
  add explicit `## P1`–`## P4` IDs for phase headings. Verification
  list items become numbered acceptance items inside their parent
  deliverable section.
- Add `**Acceptance:**` blocks to every deliverable section. Each
  item must name a concrete artifact (file path, symbol, test, or
  documented behavior). The existing prose in §1.1, §1.1a–§1.1d,
  §1.2, §1.3 already names files and symbols clearly; the retrofit
  task converts those into numbered acceptance items.
- Convert the table-sourced deliverables (Phase 2 §2.1–§2.10's
  agent table; Phase 4 task-mapping table) to per-row acceptance
  items with stable IDs.

**Acceptance:**

- A7.1 — `.gobby/plans/task-12725-lifecycle-dispatch.md` is
  retro-conformed: every `##`–`######` heading either matches A1's
  canonical regex OR carries `kind: framing` (verified by feeding
  the file through `gobby.plans.parser.parse_plan` without raising).
  file: `.gobby/plans/task-12725-lifecycle-dispatch.md`. test:
  `tests/plans/test_parser.py::test_parses_task_12725_lifecycle_dispatch`.
- A7.2 — every retrofitted section in
  `task-12725-lifecycle-dispatch.md` has `kind:` front-matter
  (`deliverable`, `framing`, `verification`, or `deferred`).
  behavior: "every parsed section has a non-None kind" in
  `.gobby/plans/task-12725-lifecycle-dispatch.md`. test:
  `tests/plans/test_parser.py::test_task_12725_every_section_has_kind`.
- A7.3 — every deliverable section in the retrofitted plan has
  an `**Acceptance:**` block with at least one acceptance item
  whose `artifact_ref` is non-empty. test:
  `tests/plans/test_parser.py::test_task_12725_every_deliverable_has_acceptance`.
- A7.4 — table-sourced deliverables (Phase 2 §2.1–§2.10 agent
  table; Phase 4 task-mapping table) are decomposed into per-row
  acceptance items with stable IDs. behavior: "Phase 2 agent
  table converted to acceptance items A2.1.1–A2.1.N or
  equivalent" in `.gobby/plans/task-12725-lifecycle-dispatch.md`.
  test:
  `tests/plans/test_parser.py::test_task_12725_table_rows_are_acceptance_items`.
- A7.5 — after A4 lands, running

  ```bash
  gobby plan coverage \
    --plan .gobby/plans/task-12725-lifecycle-dispatch.md \
    --plan-id task-12725-lifecycle-dispatch \
    --plan-hash <recomputed> \
    --root-task '#12725' \
    --project-id d45545c5-ded5-4335-b115-0245752edacf \
    --task-tree db \
    --evidence 'commits:<merged-range>' \
    --manifest .gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch.coverage.yaml
  ```

  exits cleanly (any exit code: success path is informational; the
  file is the gap inventory). file:
  `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch.coverage.yaml`.
- A7.6 — the generated manifest is the authoritative gap inventory
  and Epic 2's acceptance checklist. behavior: "manifest header
  records plan_id, plan_hash, root_task_ref='12725', project_id,
  generated_at; rows enumerate every acceptance item with status"
  in
  `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch.coverage.yaml`.

> **Implementation ordering:** A7.1–A7.4 (Step 1) can run before A4
> lands — they are pure markdown edits. A7.5–A7.6 (Step 2) require
> A4. The Epic 1 expansion will sequence the leaves so that A4
> lands first; A7 leaves carry `depends_on` edges accordingly.

## A8 Bootstrap coverage ledger

`kind: deliverable`

The bootstrap coverage ledger protects this very plan
(`task-13175-plan-coverage-contract`) from the same failure mode that
sank #12725: the tooling that would mechanically catch missing
sections does not yet exist. The ledger is the manual stand-in,
adversary-reviewed before any Epic 1 implementation work begins, and
re-validated by A4's tooling once it lands.

**Files to create (this task creates the ledger as part of plan
authoring):**

- `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`
  — the bootstrap ledger.

**Ledger schema** (the file referenced above):

```yaml
project_id: d45545c5-ded5-4335-b115-0245752edacf
plan_id: task-13175-plan-coverage-contract
plan_hash: <sha256 of plan file at ledger-write time>
root_task_ref: "13175"
sections:
  A1:
    acceptance_items:
      A1.1:
        expected_leaves:
          - title: <leaf title>
            owner_agent: <agent name or "unspecified">
            validation_criteria_summary: <short description>
        notes: <optional>
      A1.2: ...
      ...
  A2: ...
  ...
  A10: ...
```

Sections that are `framing` or `verification` (`A0`, `A11`, `A12`)
are intentionally absent from the ledger — they have no acceptance
items.

**Acceptance:**

- A8.1 — file
  `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`
  exists and parses as YAML. file:
  `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`.
- A8.2 — ledger header includes `project_id`
  (= `d45545c5-ded5-4335-b115-0245752edacf`), `plan_id`
  (= `task-13175-plan-coverage-contract`), `plan_hash` (sha256 of
  the plan file), `root_task_ref` (= `"13175"`). behavior:
  "header fields present and match plan identity" in
  `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`.
- A8.3 — every deliverable section A1–A10 of this plan appears as a
  key under `sections:` in the ledger. behavior: "ledger.sections
  has keys A1, A2, A3, A4, A5, A6, A7, A8, A9, A10" in
  `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`.
- A8.4 — every acceptance item in those sections appears as a key
  under `sections.<X>.acceptance_items` (e.g., `A1.1`, `A1.2`,
  …, `A10.5`). behavior: "every plan acceptance item ID appears in
  the ledger" in
  `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`.
- A8.5 — each acceptance item has `expected_leaves: list[<leaf>]`
  where each leaf has `title`, `owner_agent`,
  `validation_criteria_summary`. behavior: "every ledger
  acceptance_item has a non-empty expected_leaves list with the
  three required fields" in
  `.gobby/plans/task-13175-plan-coverage-contract.coverage-ledger.yaml`.
- A8.6 — plan-adversary review of the ledger artifact (separate
  spawn from the plan-adversary review of the plan itself) is
  approved before any Epic 1 implementation work begins. behavior:
  "ledger reviewed by plan-adversary; verdict review_approved
  recorded on root task #13175 with adversary findings cleared" in
  task `#13175` description.
- A8.7 — Epic 1 closure gate: after A4 lands, running

  ```bash
  gobby plan coverage \
    --plan .gobby/plans/task-13175-plan-coverage-contract.md \
    --plan-id task-13175-plan-coverage-contract \
    --plan-hash <recomputed> \
    --root-task '#13175' \
    --project-id d45545c5-ded5-4335-b115-0245752edacf \
    --task-tree db \
    --evidence none \
    --manifest .gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/13175/task-13175-plan-coverage-contract.coverage.yaml
  ```

  produces a manifest whose rows match the ledger's
  `expected_leaves`; mismatch (a deliverable section in the ledger
  that has zero matching leaves in the manifest, or vice versa)
  blocks closing root task `#13175`. The gate is implemented as a
  helper `verify_bootstrap_ledger(db, task_id)` in
  `src/gobby/plans/bootstrap_ledger.py` (new) and called from the
  close-task transition path at
  `src/gobby/storage/tasks/_transitions.py:close_task` (existing
  function — extended with a pre-close hook that, when the task is
  the root of a plan whose `<plan_id>.coverage-ledger.yaml`
  companion exists, calls `verify_bootstrap_ledger`; on mismatch
  the close raises `BootstrapLedgerMismatchError` and the MCP
  wrapper at
  `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:close_task`
  surfaces the structured error to the caller). symbol:
  `gobby.plans.bootstrap_ledger.verify_bootstrap_ledger`. test:
  `tests/plans/test_bootstrap_ledger_revalidation.py::test_close_blocked_on_ledger_mismatch`,
  `tests/plans/test_bootstrap_ledger_revalidation.py::test_close_succeeds_on_ledger_match`,
  `tests/storage/tasks/test_transitions_ledger_gate.py::test_close_task_invokes_verify_when_companion_exists`,
  `tests/storage/tasks/test_transitions_ledger_gate.py::test_close_task_skips_verify_when_no_companion`.
- A8.8 — `.gobby/plans/.grandfathered` mechanism is **not** used
  for Epic 1; documentation in A10 records that
  `.grandfathered` is reserved for already-merged epics (e.g.,
  #12725 between merge and A7 retrofit completion) and that any
  addition to it requires a co-located removal task. behavior:
  "CLAUDE.md states .grandfathered is reserved for already-merged
  epics and additions require co-located removal task" in
  `CLAUDE.md`.

## A9 Repo-wide CI test

`kind: deliverable`

A single CI test that walks every active plan, asserts a manifest
exists, asserts the manifest's `plan_hash` matches the on-disk
plan, asserts zero `missing|invalid` rows, and asserts no orphan
manifests or un-paired `.grandfathered` entries.

**Files to create:**

- `tests/plans/test_plan_coverage_ci.py` — the CI test.
- `tests/plans/conftest.py` (modify if exists; create if not) —
  fixtures for plan discovery and `.grandfathered` index parsing.
- `src/gobby/cli/plan_snapshots.py` — implements the
  `gobby plan grandfathered-refresh` and `gobby plan
  legacy-classification-refresh` Click subcommands that
  regenerate `.gobby/plans/.grandfathered-task-state.yaml` and
  `.gobby/plans/.legacy-classification.yaml` from the live task
  DB. Wired into the main `gobby plan` CLI group registered by
  A4.
- `tests/plans/test_plan_snapshots_cli.py` — tests for the two
  refresh subcommands (determinism, schema preservation, CLI
  registration).
- `tests/plans/test_plan_snapshots_hook.py` — tests for the
  pre-commit hook (stale-snapshot rejection, fresh-snapshot
  pass).

**Files to modify:**

- `.gobby/plans/.grandfathered` (create if absent) — initially
  empty (Epic 1 does not grandfather any plan; A7 retrofit makes
  #12725 conformant).
- `.gobby/plans/.legacy-classification.yaml` (create if absent) —
  committed self-describing snapshot for every `plan_kind: legacy`
  index entry, recording its root-task open/closed state and the
  retrofit/non-retrofit disposition. Required so legacy
  classification cannot silently exempt active plan files from
  the manifest gate (closes the F1/Round 5 hole). Format:

  ```yaml
  # Self-describing snapshot for A9.13 verification under
  # GOBBY_LIVE_DB=0 (A9.9). One row per plan_kind: legacy entry
  # in .gobby/plans/index.yaml. The pre-commit hook in A9.13
  # regenerates this file from the live task DB so committed
  # entries always reflect the snapshot author's verified state.
  generated_at: "<ISO-8601 UTC at last edit>"
  generator: "gobby plan legacy-classification-refresh"  # CLI in A4
  entries:
    - plan_id: task-12068-skillsmp-install-rewrite
      root_task_ref: "12068"
      root_open: true                # snapshot of live DB at edit time
      root_title: "<root task title at snapshot time>"
      legacy_reason: "<prose: why this plan is classified legacy>"
      retrofit_target: "#NNNN"       # required iff root_open: true
      retrofit_target_exists: true   # snapshot of live DB at edit time
      retrofit_target_open: true     # snapshot of live DB at edit time
      retrofit_target_title: "<task title at snapshot time>"
      # OR (mutually exclusive with retrofit_target):
      # non_retrofit_acknowledgment: "#NNNN"
      # non_retrofit_acknowledgment_exists: true
      # non_retrofit_acknowledgment_open: true
      # non_retrofit_acknowledgment_title: "<task title>"
    - plan_id: task-12725-lifecycle-dispatch
      root_task_ref: "12725"
      root_open: false               # closed root: no retrofit_target needed
      root_title: "Lifecycle-state-driven agent dispatch"
      legacy_reason: "Epic 0 — merged before contract; A7 retrofit handles its conformance separately."
  ```

  Schema requirements: every `plan_kind: legacy` entry in
  `.gobby/plans/index.yaml` MUST have a matching row here.
  When `root_open: true`, the row MUST carry exactly one of
  `retrofit_target` or `non_retrofit_acknowledgment`, and that
  field's `*_exists` and `*_open` snapshot fields MUST both be
  `true` (so A9.13 can verify open-task status under
  `GOBBY_LIVE_DB=0` without the live DB; A9.9). The `*_title`
  snapshot is informational but required for drift detection
  when the live DB is available. When `root_open: false`,
  neither retrofit field nor its snapshot triplet is required
  (a closed root is auto-exempt). `legacy_reason` and
  `root_title` are always required and must be non-empty prose.

- `.gobby/plans/.grandfathered-task-state.yaml` (create if absent) —
  committed self-describing snapshot of every `# remove-by:` task
  ref's open/closed state, refreshed when `.grandfathered` is
  edited. Format:

  ```yaml
  # Self-describing snapshot for A9.7 verification under
  # GOBBY_LIVE_DB=0 (A9.9). One row per remove-by task ref appearing
  # in .gobby/plans/.grandfathered. The pre-commit hook in A9.7
  # regenerates this file from the live task DB so committed entries
  # always reflect the snapshot author's verified state.
  generated_at: "<ISO-8601 UTC at last edit>"
  generator: "gobby plan grandfathered-refresh"  # CLI in A4
  refs:
    - task_ref: "#NNNN"
      exists: true
      open: true
      title: "<task title at snapshot time>"
  ```

  Initially empty (`refs: []`) since Epic 1 does not grandfather
  any plan.
- `pyproject.toml` (modify — ensure the test discovery pattern
  picks up `tests/plans/`).
- `.pre-commit-config.yaml` (modify if exists; create if not) —
  add a `gobby-plan-snapshots-refresh` hook that runs both
  refresh subcommands in `--check` mode when
  `.gobby/plans/.grandfathered`, `.gobby/plans/index.yaml`, or
  either snapshot YAML is modified. Hook fails on diff and
  cites the exact subcommand to run.

**Behavior contract:**

The test:

1. Walks `.gobby/plans/*.md`, excluding `.coverage-ledger.yaml`
   and any non-plan markdown. **Asserts every plan file has an
   entry in `.gobby/plans/index.yaml`** (closes the unindexed-
   plan-silently-skipped hole). Unindexed plan files fail CI
   citing the missing entry.
2. Reads `.gobby/plans/index.yaml`; for each entry it knows
   `plan_kind ∈ {implementation, strategy, legacy}`,
   `status ∈ {active, merged, archived}`, and the
   `(project_id, root_task_ref)` identity.
3. For each plan file:
   - Parses with
     `gobby.plans.parser.parse_plan(path, plan_kind=<from index>)`.
     Implementation entries parse strict; strategy and legacy
     entries parse permissively (canonical headings without
     `kind:` yield `Kind.framing`, narrative headings go to
     `framing_headings`).
   - Skips plans whose `plan_kind ∈ {legacy}` AND `status ==
     archived` from the manifest gate (they are surveyed only for
     parse-without-raise and index-entry presence).
   - Skips plans whose epic `status == merged` AND whose plan_id
     appears in `.gobby/plans/.grandfathered`.
   - For each `(project_id, plan_id, root_task_ref)` identity
     **whose `plan_kind == implementation`**, asserts the
     manifest exists at
     `coverage_manifest_path(project_id, root_task_ref, plan_id)`.
     Strategy and legacy entries are exempt from manifest/hash/
     zero-row checks but must still parse without raising under
     `plan_kind=PlanKind.strategy`.
4. For implementation entries: re-computes `plan_hash` from the
   on-disk plan file and asserts string equality with the
   manifest's `plan_hash`. On mismatch the test fails citing both
   hashes.
5. For implementation entries: invokes `gobby plan coverage
   --task-tree matrix-file --matrix-file <manifest>` and asserts
   zero `missing|invalid` rows. The library re-validation against
   `db` runs at expansion-qa time, not in CI; CI uses the
   committed manifest.
6. Walks every manifest under `.gobby/plans/coverage/` and
   asserts each resolves to a `plan_kind: implementation` entry
   in `.gobby/plans/index.yaml`. Orphan manifests fail the test
   citing the path. Manifests pointing at `plan_kind: strategy`
   or `plan_kind: legacy` entries are also treated as orphan
   (only implementation plans have manifests).
7. Reads `.gobby/plans/.grandfathered`; for each entry asserts
   the line includes a `# remove-by: <task-ref>` annotation;
   asserts the named task is recorded as `exists: true` and
   `open: true` in the committed `.gobby/plans/.grandfathered-task-state.yaml`
   snapshot. CI never skips this check; the snapshot is the
   self-describing source of truth under `GOBBY_LIVE_DB=0`.
   When the live DB is available, A9 also asserts the snapshot
   matches live state (drift fails CI citing the divergent ref).
   New entries since the last signed-off commit (detected via
   `git diff HEAD` or against the signed-off baseline file)
   require both the `# remove-by:` annotation AND a corresponding
   `refs[]` entry in the snapshot file.

**Files to create alongside:**

- `.gobby/plans/index.yaml` — `plan_index` mapping; Epic 1 ships
  with entries for `task-12725-lifecycle-dispatch` (status:
  `merged`) and `task-13175-plan-coverage-contract` (status:
  `active`). Format:

  ```yaml
  entries:
    # Implementation plans (subject to manifest/hash/rows gate)
    - plan_id: task-12725-lifecycle-dispatch
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12725"
      plan_kind: implementation
      status: merged
    - plan_id: task-13175-plan-coverage-contract
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "13175"
      plan_kind: implementation
      status: active
    # Strategy plan (parser-permissive, exempt from manifest CI)
    - plan_id: task-13173-lifecycle-dispatch-recovery
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "13173"
      plan_kind: strategy
      status: active
    # Legacy plans (pre-Plan-Coverage-Contract; parser-permissive,
    # exempt from manifest CI; tracked here so A9 cannot silently
    # skip them. Every entry below corresponds to a real
    # .gobby/plans/<plan_id>.md file at Epic 1 ship time — the
    # reciprocal-existence assertion in A9.12 forbids stale rows.)
    - plan_id: task-12068-skillsmp-install-rewrite
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12068"
      plan_kind: legacy
      status: archived
    - plan_id: task-12746-neo4j-falkordb-swap
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12746"
      plan_kind: legacy
      status: archived
    - plan_id: task-12761-postgres-hub-migration
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12761"
      plan_kind: legacy
      status: archived
    - plan_id: task-12898-memory-recall-helper
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12898"
      plan_kind: legacy
      status: archived
    - plan_id: task-12902-pipeline-runs-rename
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12902"
      plan_kind: legacy
      status: archived
    - plan_id: task-12910-drawbridge-ui-batch
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12910"
      plan_kind: legacy
      status: archived
    - plan_id: task-12948-codex-retry-verbatim-removal
      project_id: d45545c5-ded5-4335-b115-0245752edacf
      root_task_ref: "12948"
      plan_kind: legacy
      status: archived
  ```

  Schema requirements: `plan_kind ∈ {implementation, strategy,
  legacy}`, `status ∈ {active, merged, archived}`, and entries
  whose `plan_kind ∈ {strategy, legacy}` must NOT have a
  corresponding manifest (the CI orphan-manifest check rejects
  strategy-/legacy-plan manifests).

  **`plan_kind: legacy`** is for pre-Epic-1 plans authored before
  the Plan-Coverage Contract that should not be retrofitted (they
  belong to merged or archived work and the cost of conforming
  them is not justified). Legacy plans:
  - parse under `PlanKind.strategy` (permissive),
  - are exempt from manifest/hash/zero-row CI,
  - are surveyed by A9 only for index-entry presence,
  - and have `status: archived` to signal their historical
    nature.

  Epic 1 ships index entries for every `.gobby/plans/task-*.md`
  file currently in the repo. **The exhaustive inventory at Epic
  1 ship time is 10 plan files: 2 implementation
  (`task-12725-lifecycle-dispatch`,
  `task-13175-plan-coverage-contract`), 1 strategy
  (`task-13173-lifecycle-dispatch-recovery`), and 7 legacy
  (`task-12068-skillsmp-install-rewrite`,
  `task-12746-neo4j-falkordb-swap`,
  `task-12761-postgres-hub-migration`,
  `task-12898-memory-recall-helper`,
  `task-12902-pipeline-runs-rename`,
  `task-12910-drawbridge-ui-batch`,
  `task-12948-codex-retry-verbatim-removal`).** Every legacy
  entry above is `plan_kind: legacy, status: archived`; the
  strategy plan is `plan_kind: strategy, status: active`; the
  implementation plans are `plan_kind: implementation` with
  their actual status (`merged` for #12725 post-A7 retrofit,
  `active` for #13175). Future plans MUST be added to
  `index.yaml` at authoring time; A9 CI rejects any unindexed
  `.gobby/plans/task-*.md` file (A9.11) and equally rejects any
  index entry whose `plan_id` does not have a matching plan file
  in `.gobby/plans/` (A9.12).

**Tests (this section's own test file is the deliverable; below are
the assertions that file makes):**

- `tests/plans/test_plan_coverage_ci.py::test_every_active_plan_has_manifest`.
- `tests/plans/test_plan_coverage_ci.py::test_manifest_plan_hash_matches_on_disk`.
- `tests/plans/test_plan_coverage_ci.py::test_zero_missing_invalid_rows`.
- `tests/plans/test_plan_coverage_ci.py::test_no_orphan_manifests`.
- `tests/plans/test_plan_coverage_ci.py::test_grandfathered_entries_require_remove_by_annotation`.
- `tests/plans/test_plan_coverage_ci.py::test_grandfathered_target_task_exists_and_open`.
- `tests/plans/test_plan_coverage_ci.py::test_no_unauthorized_grandfathered_additions`.
- `tests/plans/test_plan_coverage_ci.py::test_index_file_present_and_well_formed`.
- `tests/plans/test_plan_coverage_ci.py::test_index_inventory_matches_repo`.
- `tests/plans/test_plan_coverage_ci.py::test_every_plan_file_has_index_entry`.
- `tests/plans/test_plan_coverage_ci.py::test_every_index_entry_has_plan_file`.
- `tests/plans/test_plan_coverage_ci.py::test_grandfathered_target_task_exists_and_open_via_snapshot`.
- `tests/plans/test_plan_coverage_ci.py::test_grandfathered_snapshot_matches_live_db_when_available`.
- `tests/plans/test_plan_coverage_ci.py::test_ci_runs_under_no_live_db_with_no_skipped_checks`.
- `tests/plans/test_plan_coverage_ci.py::test_strategy_plans_have_no_manifests`.
- `tests/plans/test_plan_coverage_ci.py::test_legacy_plans_have_no_manifests`.
- `tests/plans/test_plan_coverage_ci.py::test_parse_plan_dispatch_by_plan_kind`.
- `tests/plans/test_plan_coverage_ci.py::test_every_active_implementation_plan_has_manifest`.
- `tests/plans/test_plan_coverage_ci.py::test_every_legacy_entry_has_classification_row`.
- `tests/plans/test_plan_coverage_ci.py::test_open_root_legacy_requires_retrofit_or_acknowledgment_with_open_snapshot`.
- `tests/plans/test_plan_coverage_ci.py::test_legacy_classification_snapshot_matches_live_db_when_available`.

**Acceptance:**

- A9.1 — `tests/plans/test_plan_coverage_ci.py` walks every plan
  file under `.gobby/plans/*.md` and asserts each has an entry
  in `.gobby/plans/index.yaml`; unindexed plan files fail CI
  citing the missing entry. file:
  `tests/plans/test_plan_coverage_ci.py`. test:
  `tests/plans/test_plan_coverage_ci.py::test_every_plan_file_has_index_entry`.
- A9.2 — for each plan, `plan_id`, `root_task_ref`, and
  `plan_kind` resolve from `.gobby/plans/index.yaml`; the index
  schema requires `plan_kind ∈ {implementation, strategy,
  legacy}` and `status ∈ {active, merged, archived}` per entry.
  Epic 1 ships index entries for all 10 current plan files (2
  implementation, 1 strategy, 7 legacy) — the inventory matches
  `.gobby/plans/task-*.md` exactly at ship time. file:
  `.gobby/plans/index.yaml`. test:
  `tests/plans/test_plan_coverage_ci.py::test_index_file_present_and_well_formed`
  asserts the field is present and validated, AND
  `tests/plans/test_plan_coverage_ci.py::test_index_inventory_matches_repo`
  asserts the index entry-set equals the on-disk plan-file set
  (no stale rows, no unindexed files).
- A9.3 — for each `(project_id, plan_id, root_task_ref)` whose
  `plan_kind == implementation`, the test asserts a manifest
  exists at the path returned by `coverage_manifest_path`.
  Strategy entries are exempt. test:
  `tests/plans/test_plan_coverage_ci.py::test_every_active_implementation_plan_has_manifest`.
- A9.4 — for implementation entries the test re-computes
  `plan_hash` from the plan file and asserts equality with the
  manifest's `plan_hash`; mismatch fails. Strategy entries are
  not hash-checked. test:
  `tests/plans/test_plan_coverage_ci.py::test_manifest_plan_hash_matches_on_disk`.
- A9.5 — for implementation entries the test invokes `gobby plan
  coverage --task-tree matrix-file --matrix-file <manifest>` and
  asserts zero `missing|invalid` rows. test:
  `tests/plans/test_plan_coverage_ci.py::test_zero_missing_invalid_rows`.
- A9.6 — every manifest under `.gobby/plans/coverage/` resolves
  to a live `plan_index.yaml` entry whose `plan_kind ==
  implementation`; orphan manifests AND manifests pointing at
  strategy or legacy entries all fail. test:
  `tests/plans/test_plan_coverage_ci.py::test_no_orphan_manifests`,
  `tests/plans/test_plan_coverage_ci.py::test_strategy_plans_have_no_manifests`,
  `tests/plans/test_plan_coverage_ci.py::test_legacy_plans_have_no_manifests`.
- A9.7 — every `.gobby/plans/.grandfathered` entry has a paired
  `# remove-by: <task-ref>` annotation; the named task is
  recorded as `exists: true, open: true` in the committed
  `.gobby/plans/.grandfathered-task-state.yaml` snapshot. The
  snapshot is the self-describing source of truth so this check
  works under `GOBBY_LIVE_DB=0` (A9.9). When the live DB is
  available, A9 also asserts the snapshot matches live state.
  file: `.gobby/plans/.grandfathered-task-state.yaml`. test:
  `tests/plans/test_plan_coverage_ci.py::test_grandfathered_entries_require_remove_by_annotation`,
  `tests/plans/test_plan_coverage_ci.py::test_grandfathered_target_task_exists_and_open_via_snapshot`,
  `tests/plans/test_plan_coverage_ci.py::test_grandfathered_snapshot_matches_live_db_when_available`.
- A9.8 — new `.grandfathered` entries since the last signed-off
  commit fail without the paired annotation. test:
  `tests/plans/test_plan_coverage_ci.py::test_no_unauthorized_grandfathered_additions`.
- A9.9 — CI does not require a live task DB; the
  `.gobby/plans/index.yaml`, committed manifests, and
  `.gobby/plans/.grandfathered-task-state.yaml` snapshot are
  self-describing. Every assertion in A9.1–A9.8 and A9.10–A9.12
  resolves from committed data alone. behavior: "test runs
  successfully with `GOBBY_LIVE_DB=0`; no skipped or
  conditionally-relaxed checks under that flag" in
  `tests/plans/test_plan_coverage_ci.py`. test:
  `tests/plans/test_plan_coverage_ci.py::test_ci_runs_under_no_live_db_with_no_skipped_checks`.
- A9.10 — CI maps each index entry's `plan_kind` to the parser's
  `PlanKind` enum before calling
  `parse_plan(path, plan_kind=...)`: index `implementation` →
  `PlanKind.implementation` (strict), index `strategy` →
  `PlanKind.strategy` (permissive), index `legacy` →
  `PlanKind.strategy` (permissive — same parse mode as strategy
  since both share the "permissive on missing-`kind:` headings"
  contract; the `PlanKind` enum has only two values, the third
  index value `legacy` is a routing classifier, not a parser
  mode). The mapping lives in CI helper
  `tests/plans/test_plan_coverage_ci.py::_resolve_parser_kind` (or
  equivalent module-level helper). test:
  `tests/plans/test_plan_coverage_ci.py::test_parse_plan_dispatch_by_plan_kind`
  asserts all three index values dispatch to the correct
  `PlanKind` constant and that an index value outside
  `{implementation, strategy, legacy}` raises a clear schema
  error.
- A9.11 — every `.gobby/plans/task-*.md` file has a matching
  `entries[*].plan_id` in `.gobby/plans/index.yaml`; an
  unindexed plan file fails CI citing the missing entry. This
  closes the silent-skip hole where legacy or newly added plans
  could escape A9 by not appearing in the index. test:
  `tests/plans/test_plan_coverage_ci.py::test_every_plan_file_has_index_entry`.
- A9.12 — every `entries[*].plan_id` in `.gobby/plans/index.yaml`
  has a corresponding `.gobby/plans/<plan_id>.md` file on disk;
  a stale index row pointing at a missing or deleted plan file
  fails CI citing the row's `plan_id` and the expected path. This
  is the reciprocal of A9.11 — together they enforce a strict
  bijection between the on-disk plan-file set and the indexed
  entry set, with no silent additions on either side. test:
  `tests/plans/test_plan_coverage_ci.py::test_every_index_entry_has_plan_file`.
- A9.13 — every `plan_kind: legacy` entry in
  `.gobby/plans/index.yaml` has a matching row in
  `.gobby/plans/.legacy-classification.yaml`; the row carries
  `root_open` (snapshot), non-empty `legacy_reason` and
  `root_title`, and — when `root_open: true` — exactly one of
  `retrofit_target` or `non_retrofit_acknowledgment` plus its
  paired `*_exists: true`, `*_open: true`, and `*_title`
  snapshot fields. CI verifies open-task status from those
  snapshot fields under `GOBBY_LIVE_DB=0` (A9.9) without needing
  the live DB. CI fails on: missing row, missing
  `legacy_reason` or `root_title`, open root without retrofit/
  non-retrofit field, both fields present, `*_exists: false`,
  `*_open: false`, or live-DB drift on any snapshot field. When
  the live DB is available, A9 asserts every snapshot field
  (`root_open`, `root_title`, `*_exists`, `*_open`, `*_title`)
  matches live state. file:
  `.gobby/plans/.legacy-classification.yaml`. test:
  `tests/plans/test_plan_coverage_ci.py::test_every_legacy_entry_has_classification_row`,
  `tests/plans/test_plan_coverage_ci.py::test_open_root_legacy_requires_retrofit_or_acknowledgment_with_open_snapshot`,
  `tests/plans/test_plan_coverage_ci.py::test_legacy_classification_snapshot_matches_live_db_when_available`.
  This closes the Round 5 hole where active plan files could be
  silently exempted from the manifest gate by classification
  alone, and the Round 6 hole where retrofit-target open status
  was unverifiable under `GOBBY_LIVE_DB=0`.
- A9.14 — `src/gobby/cli/plan_snapshots.py` implements the
  `gobby plan grandfathered-refresh` and `gobby plan
  legacy-classification-refresh` Click subcommands. Each reads
  the live task DB, regenerates the corresponding snapshot file
  (`.gobby/plans/.grandfathered-task-state.yaml` or
  `.gobby/plans/.legacy-classification.yaml`) from authoritative
  state, preserves all required fields per the schemas in A9.7
  and A9.13, and writes ISO-8601 UTC `generated_at`. Both
  subcommands must be deterministic given the same DB state
  (sorted entries, stable formatting). Subcommands also wire
  into the main `gobby plan` CLI group registered by A4 so
  `gobby plan --help` lists them. file:
  `src/gobby/cli/plan_snapshots.py`. test:
  `tests/plans/test_plan_snapshots_cli.py::test_grandfathered_refresh_generates_snapshot`,
  `tests/plans/test_plan_snapshots_cli.py::test_legacy_classification_refresh_generates_snapshot`,
  `tests/plans/test_plan_snapshots_cli.py::test_refresh_is_deterministic_for_fixed_db_state`,
  `tests/plans/test_plan_snapshots_cli.py::test_refresh_subcommands_registered_in_plan_cli`.
- A9.15 — pre-commit hook entry in `.pre-commit-config.yaml`
  named `gobby-plan-snapshots-refresh` runs both refresh
  subcommands when `.gobby/plans/.grandfathered`,
  `.gobby/plans/index.yaml`, or either snapshot file is
  modified. The hook fails the commit if regeneration produces
  diffs not already staged (i.e., the user edited the input
  files but did not regenerate the snapshots), citing the stale
  fields and the exact subcommand to run. file:
  `.pre-commit-config.yaml`. test:
  `tests/plans/test_plan_snapshots_hook.py::test_hook_rejects_stale_grandfathered_snapshot`,
  `tests/plans/test_plan_snapshots_hook.py::test_hook_rejects_stale_legacy_classification_snapshot`,
  `tests/plans/test_plan_snapshots_hook.py::test_hook_passes_when_snapshots_fresh`.
- A9.16 — A9 CI also runs both refresh subcommands in `--check`
  mode (no-write, exit non-zero on diff) as part of
  `tests/plans/test_plan_coverage_ci.py::test_snapshots_match_live_db_when_available`.
  Under `GOBBY_LIVE_DB=0` the check is skipped (the snapshot
  fields are themselves the source of truth per A9.9; the
  drift check requires live DB by definition). When live DB is
  available, drift fails CI. test:
  `tests/plans/test_plan_coverage_ci.py::test_snapshots_match_live_db_when_available`.

## A10 Contract documentation

`kind: deliverable`

Document the plan-coverage contract in `CLAUDE.md` and in the plan,
expansion, and expansion-qa skill READMEs (per strategy A10 — note
this is a tighter scope than "every skill"; the contract surface is
discoverable via these four places).

**Files to modify:**

- `CLAUDE.md` — add a "Plan-Coverage Contract" section.
- `src/gobby/install/shared/skills/plan/SKILL.md` — link to
  plan-draft for the grammar; documents the contract from the
  authoring side. (Already touched in A1; A10 confirms the
  documentation surface.)
- `src/gobby/install/shared/skills/plan-draft/SKILL.md` — already
  modified in A1 (grammar, kind enum, acceptance shape, deferral,
  covers).
- `src/gobby/install/shared/skills/expand/SKILL.md` — documents
  the expansion-side obligations under the Plan-Coverage
  Contract: a `.coverage-ledger.yaml` companion file MUST exist
  before expansion proceeds, leaves MUST emit structured
  `covers:<plan-id>:<section-id>:<item-id>` labels, expansion-qa
  is the gate (A5), and free-form `plan-ref:` labels are not
  honored. Strategy A10 explicitly names the expansion skill
  README as a contract surface; this is the canonical authoring
  surface for `/gobby expand` consumers.
- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` —
  already modified in A5; A10 confirms the contract reference is
  present in the agent's `instructions:` block. (No expansion-qa
  SKILL.md exists; the agent's inline instructions are the
  documentation surface.)

**Files to create:**

- `docs/contracts/plan-coverage.md` — single-page contract
  reference linking back to the four skill files and CLAUDE.md.
  Acts as the canonical reading order for new contributors.

**Behavior contract:**

`CLAUDE.md` Plan-Coverage Contract section MUST include, verbatim
or by reference:

- The canonical regex (verbatim, in a code block).
- The `kind` enum (`deliverable | framing | verification | deferred`)
  and the per-kind acceptance/no-acceptance rule.
- The acceptance-item shape (`A<section>.<n>` IDs, four artifact
  kinds: file | symbol | test | behavior).
- The typed deferral object spec (task_ref, reason, owner,
  original_acceptance_items; provenance label
  `deferred-from:<plan-id>:<section-id>`; closed task fails the
  gate).
- The structured covers record format
  (`covers:<plan-id>:<section-id>:<item-id>`); a one-line
  statement that free-form `plan-ref:` labels are not honored.
- The `gobby plan coverage` CLI synopsis with all ten flags
  (four required: `--plan`, `--plan-id`, `--plan-hash`,
  `--task-tree`; six optional: `--root-task`, `--project-id`,
  `--matrix-file`, `--evidence`, `--manifest`, `--regenerate`)
  and exit codes (0, 2, 3, 4, 5, 6, 7, 8).
- The five evidence kinds (`commits | task-diff | worktree-diff |
  coverage-matrix | none`).
- The bootstrap-ledger requirement: every new epic plan must
  ship a `.coverage-ledger.yaml` companion file, adversary-reviewed
  before expansion, until the contract tooling is mature.
- The `.grandfathered` mechanism: reserved for already-merged
  epics; additions require a paired `# remove-by: <task-ref>`
  annotation and an open task.
- The **table-row decomposition rule**: any `deliverable` section
  whose body uses a markdown table to enumerate work items MUST
  emit one acceptance item per data row with stable IDs.
  Plan-adversary qualitatively rejects deliverables that
  enumerate work in tables without per-row acceptance items.

**Acceptance:**

- A10.1 — `CLAUDE.md` includes a "Plan-Coverage Contract" section
  containing all ten bullets above (canonical regex, kind enum,
  acceptance-item shape, deferral object, covers record, CLI
  synopsis with all ten flags + exit codes, evidence kinds,
  bootstrap ledger, .grandfathered, table-row decomposition —
  verbatim or by reference to plan-draft / docs/contracts).
  file: `CLAUDE.md`.
- A10.2 — `src/gobby/install/shared/skills/plan/SKILL.md`,
  `plan-draft/SKILL.md`, `plan-review/SKILL.md`,
  `expand/SKILL.md`, and the `expansion-qa` agent YAML each
  contain or link to the contract surface relevant to their
  authoring/review/expansion role. file:
  `src/gobby/install/shared/skills/plan-draft/SKILL.md` (canonical
  authoring surface; the other four files link to it). The
  remaining four contract surfaces are covered by A1.5 (plan-review),
  A1.6 (planner), A1.7 (plan-adversary), A5.1 (expansion-qa), and
  A10.9 (expand).
- A10.3 — `docs/contracts/plan-coverage.md` exists as a
  contract-reference page linking back to the four skill files
  (plan, plan-draft, plan-review, expand), the expansion-qa
  agent YAML, `CLAUDE.md`, and the canonical regex / library /
  CLI / evidence surfaces. file: `docs/contracts/plan-coverage.md`.
- A10.4 — documentation explicitly states that free-form
  `plan-ref:` labels are not honored; only structured
  `covers:<plan-id>:<section-id>:<item-id>` labels are valid
  coverage signal. behavior: "the string 'plan-ref: labels are
  not honored' (or substring 'plan-ref' alongside 'not honored')
  appears in CLAUDE.md and docs/contracts/plan-coverage.md" in
  `CLAUDE.md` and `docs/contracts/plan-coverage.md`.
- A10.5 — documentation explicitly states the bootstrap-ledger
  requirement: every new epic plan ships a
  `.coverage-ledger.yaml` companion until the tooling is mature.
  behavior: "the bootstrap ledger requirement is documented in
  CLAUDE.md and docs/contracts/plan-coverage.md" in `CLAUDE.md`
  and `docs/contracts/plan-coverage.md`.
- A10.6 — documentation states the `.grandfathered` mechanism
  rules: reserved for already-merged epics; additions require
  paired `# remove-by:` annotation and an open task. behavior:
  "the .grandfathered rules are documented in CLAUDE.md and
  docs/contracts/plan-coverage.md" in `CLAUDE.md` and
  `docs/contracts/plan-coverage.md`.
- A10.7 — `tests/docs/test_claude_md_contract_section.py::test_plan_coverage_section_present`
  asserts all ten required documentation bullets are present in
  `CLAUDE.md` (canonical regex, kind enum, acceptance-item shape,
  deferral object, covers record, CLI synopsis with ten flags +
  exit codes, evidence kinds, bootstrap ledger, .grandfathered,
  table-row decomposition). test:
  `tests/docs/test_claude_md_contract_section.py::test_plan_coverage_section_present`.
- A10.8 — test:
  `tests/docs/test_claude_md_contract_section.py::test_canonical_regex_pinned_in_claude_md`
  asserts the canonical regex literal in `CLAUDE.md` matches the
  `gobby.plans.parser.PLAN_HEADING_REGEX.pattern` string. test:
  `tests/docs/test_claude_md_contract_section.py::test_canonical_regex_pinned_in_claude_md`.
- A10.9 — `src/gobby/install/shared/skills/expand/SKILL.md`
  documents the expansion-side contract obligations: a
  `.coverage-ledger.yaml` companion MUST exist before expansion;
  leaves MUST emit structured
  `covers:<plan-id>:<section-id>:<item-id>` labels;
  expansion-qa is the mechanical gate (A5); free-form
  `plan-ref:` labels are not honored. test:
  `tests/docs/test_expand_skill_contract_section.py::test_expand_skill_documents_coverage_contract`
  asserts the four bullets above are present (verbatim or by
  link to `docs/contracts/plan-coverage.md`). file:
  `src/gobby/install/shared/skills/expand/SKILL.md`.
- A10.10 — `CLAUDE.md` and `docs/contracts/plan-coverage.md`
  document the table-row decomposition rule: any `deliverable`
  section whose body uses a markdown table to enumerate work
  items MUST emit one acceptance item per data row with stable
  IDs. Plan-adversary qualitatively rejects deliverables that
  enumerate work in tables without per-row acceptance items.
  This is the rule that closes the #12725 missing-section
  failure mode for all future plans. test:
  `tests/docs/test_claude_md_contract_section.py::test_table_row_decomposition_rule_documented`
  asserts the rule appears in `CLAUDE.md` and
  `docs/contracts/plan-coverage.md`.

## A11 Out of scope

`kind: framing`

Items explicitly outside Epic 1's scope. These are not deferred
sections (no typed deferral objects); they are notes about what is
intentionally absent and where the work lives instead.

- **D0 storage foundation audit** — Epic 2, Phase D0. Audits
  `run_id` semantics, startup sweep, FK cascade, candidate scanner,
  artifact plan-path/hash semantics, isolation-base column. Epic 1
  ships only the `base_commit_sha` and `plan_file_hash` migrations
  required by A6; the broader audit is Epic 2.
- **D0.8 dispatcher slot reservation primitive (F11)** — Epic 2,
  Phase D0. The atomic `try_reserve_slot` primitive is dispatcher
  concurrency, not plan-coverage.
- **F1 conductor deletion** — Epic 2, Phase F. Delete
  `src/gobby/conductor/`. Out of Epic 1's scope.
- **F4 retired-row migration** — Epic 2, Phase F. Disable retired
  workflow_definitions rows. Out of Epic 1's scope.
- **Phases B–G implementation** — Epic 2 entirely. Build
  entry-point contract, lifecycle transition tools, dispatcher
  package, agents and skills (including holistic-reviewer agent
  that consumes A6's library), retirement, documentation.
- **Holistic-reviewer agent (Phase E §2.1, §2.2)** — Epic 2.
  Epic 1 ships the A6 evidence library; the qualitative
  4-point-method review and verdict mapping are Epic 2.
- **#12728 PR/merge automation** — separate intake. Real PR
  creation, AI conflict resolution, merge-commit-sha capture.
- **Epic 2 detail plan authoring** — separate task; runs after
  Epic 1's tooling lands so Epic 2's plan can be validated under
  the new contract.
- **Test-architecture detail-plan changes** — Epic 2 §2.3.

## A12 Verification

`kind: verification`

End-to-end acceptance for Epic 1 close. Each item below references
the deliverable section and the test or behavior that proves it.

- The plan parser library (A2) parses both
  `.gobby/plans/task-12725-lifecycle-dispatch.md` (post-A7
  retrofit) and `.gobby/plans/task-13173-lifecycle-dispatch-recovery.md`
  without raising. Verified by
  `tests/plans/test_parser.py::test_parses_task_12725_lifecycle_dispatch`
  and
  `tests/plans/test_parser.py::test_parses_task_13173_recovery`.
- The plan parser library parses this very plan
  (`task-13175-plan-coverage-contract.md`). Verified by
  `tests/plans/test_parser.py::test_parses_self`.
- The pinned-strings grammar test passes for every documented
  shape. Verified by
  `tests/plans/test_parser_grammar.py::test_regex_pinned_strings`.
- The coverage library rejects `task_tree=db|jsonl` calls without
  scope (`plan_id`, `root_task_ref`, `project_id`) at type-check
  time and at runtime. Verified by
  `tests/plans/test_coverage_signature.py` (all four tests).
- `coverage_manifest_path(...)` sanitizes every component portably
  and disambiguates Windows reserved names, case collisions,
  truncate-with-hash, and slash/path traversal. Verified by
  `tests/plans/test_coverage_manifest_path.py` (all sanitization
  edge-case tests).
- Identity-collision rejection blocks accidental overwrite of a
  manifest with a different `plan_hash` unless `--regenerate` is
  passed. Verified by
  `tests/plans/test_coverage_identity.py::test_identity_collision_blocks_overwrite`.
- Expansion-QA rejection on `missing|invalid` rows cites
  `(section_id, item_id)` for each failing row. Verified by
  `tests/workflows/test_expansion_qa_rejection.py::test_missing_row_triggers_rejection`.
- Worktree-diff evidence resolution uses the immutable
  `base_commit_sha` (never `target_branch`) and yields `invalid`
  rows on missing/unresolvable bases instead of silently
  no-evidence. Verified by
  `tests/plans/test_evidence_worktree_diff.py::test_resolves_with_base_sha`,
  `test_invalid_when_base_sha_null`,
  `test_invalid_when_base_sha_unresolvable`.
- Migration preserves legacy artifact rows; new isolation writes
  enforce `base_commit_sha` at app level. Verified by
  `tests/storage/tasks/test_artifacts_isolation_base.py::test_migration_preserves_legacy_rows`,
  `tests/storage/tasks/test_artifacts_isolation_base_app_enforcement.py::test_new_isolation_write_without_base_raises`.
- #12725 retrofit (A7 Step 1) leaves
  `task-12725-lifecycle-dispatch.md` parseable by A2's parser
  without raising. Verified by
  `tests/plans/test_parser.py::test_parses_task_12725_lifecycle_dispatch`.
- The bootstrap ledger
  (`task-13175-plan-coverage-contract.coverage-ledger.yaml`)
  enumerates every deliverable acceptance item in this plan; the
  re-validation gate after A4 lands matches manifest to ledger and
  blocks Epic 1 close on mismatch. Verified by
  `tests/plans/test_bootstrap_ledger_revalidation.py::test_close_blocked_on_ledger_mismatch`.
- Repo-wide CI (A9) walks every active plan, asserts
  manifest+hash+rows, rejects orphan manifests, enforces
  `.grandfathered` and `legacy-classification` snapshots, and
  rejects un-paired additions. Verified by
  `tests/plans/test_plan_coverage_ci.py` (covering A9.1–A9.16
  assertions: bijective plan-file/index, manifest gate,
  hash gate, zero-row gate, orphan-manifest rejection,
  grandfathered + legacy-classification snapshots,
  no-live-DB self-describing CI, plan_kind dispatch,
  reciprocal index→file).
- Documentation (A10) pins the canonical regex in `CLAUDE.md` to
  match the parser constant. Verified by
  `tests/docs/test_claude_md_contract_section.py::test_canonical_regex_pinned_in_claude_md`.
- Manual smoke: in this repo, after Epic 1 lands,
  `uv run python -c "from gobby.plans.parser import parse_plan,
  PlanKind; doc =
  parse_plan('.gobby/plans/task-13173-lifecycle-dispatch-recovery.md',
  plan_kind=PlanKind.strategy); print(len(doc.sections),
  len(doc.framing_headings))"` parses the strategy doc cleanly,
  yielding canonical strategy sections (A1–A10, D0.x, B1–B5,
  C1–C6, D1–D8, F1–F4) and recording the narrative headings
  (`## Context`, `## Phase A — ...`, `## Adversary Review Log`,
  `## Verification`, `## Out of Scope (filed as follow-ups)`,
  `### Round N — REJECTED`, etc.) in `framing_headings`. **No
  coverage manifest is generated for the strategy doc** — A9.6
  treats any manifest pointing at a `plan_kind: strategy` entry
  as orphan, and the strategy plan has no `--manifest` output by
  design. The smoke is parse-only.

# Compile manifest-driven (sub-plan A of §2.20 cleanup)

## Overview
`kind: framing`

Make the deterministic expansion compile path consume the §2.21 manifest as
single source of truth, and patch the agent-routing fallback so non-backend
work no longer silently defaults to `backend-developer`. Validate via a
compile-only dry-run against `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`.
**No apply** — plan-12725's task tree is built in sub-plan C, after sub-plan
B adds specialist agents.

## Constraints
`kind: framing`

- **No LLM fallback.** Deterministic compile only; never invoke the LLM-driven
  expansion path or plan-adversary review during sub-plan A's work.
- **No manual task repair.** Failed compile output gets fixed in expansion code,
  not by hand-editing tasks.
- **No backend-developer fallback for non-backend work.** Until specialist
  agents land in sub-plan B, non-backend categories route to the existing
  `default` agent (`src/gobby/install/shared/workflows/agents/default.yaml`).
- **Compile-only.** This sub-plan never invokes `apply_run` against
  plan-12725. Compile alone produces the dry-run artifact (`compiled_spec`
  stored on the `expansion_runs` table).
- **No edits to plan-12725 deliverable structure.** Step 1.4 below appends a
  `## M1 Task Manifest` section but does not alter `kind: deliverable`
  sections, acceptance items, or phase ordering.
- Each `### N.N` section in this plan must be self-contained — the implementing
  agent sees ONLY that section during expansion.

## P1 Phase 1: Mechanics + routing fallback
`kind: framing`

**Goal**: ship five surgical changes (two routing-map patches, the
manifest-driven compile refactor, a stale fixture fix, the plan-12725 manifest
emit + coverage-header bump) and lock them in with an end-to-end dry-run
assertion harness against plan-12725. Stop at "compiled_spec passes
assertions" — sub-plan B picks up from there.

### 1.1 Patch agent routing fallback + emitter depends_on extraction [category: code]
`kind: deliverable`

Target: `src/gobby/tasks/expansion_service.py` (lines 33, 68–74, 231, 312–324);
`src/gobby/plans/manifest_emitter.py` (lines ~40–46, `_synthesize_entry`);
`src/gobby/plans/parser.py` (new shared helper);
`tests/plans/test_manifest_emitter.py`.

Three coupled changes: remove the silent `backend-developer` fallback (route
non-backend categories to the existing `default` agent), align the routing
map with `AUTOMATED_LEAF_CATEGORIES` (drop `planning` — it is intentionally
not a synthesizable leaf category and `validate_compiled_spec` rejects
planning leaves at `expansion_service.py:1075`), and patch the §2.21a
emitter so synthesized manifest entries preserve the source plan's
`(depends: …)` annotations.

**Edit A — `src/gobby/tasks/expansion_service.py:68-74`** (`_DETERMINISTIC_AGENT_BY_CATEGORY`)

Replace the current map (every category → `"backend-developer"`) with the
seven supported leaf categories from `AUTOMATED_LEAF_CATEGORIES`. `planning`
is intentionally absent — it is not in `AUTOMATED_LEAF_CATEGORIES` (line 33,
`{"code", "config", "docs", "manual", "refactor", "research", "test"}`) and
the existing `tests/tasks/test_expansion.py::test_validate_compiled_spec_rejects_planning_leaves`
locks that in.

```python
_DETERMINISTIC_AGENT_BY_CATEGORY = {
    "code": "backend-developer",
    "refactor": "backend-developer",
    "test": "test-architect",
    # No backend-developer fallback for non-backend work — route to existing
    # `default` agent until specialist agents land (sub-plan B). All keys
    # match AUTOMATED_LEAF_CATEGORIES; `planning` is intentionally absent
    # because validate_compiled_spec rejects planning leaves.
    "config": "default",
    "docs": "default",
    "manual": "default",
    "research": "default",
}
```

**Edit B — `src/gobby/tasks/expansion_service.py:312-324`** (`_contract_agent_fields`)

Replace the silent backend-developer fallback at lines 320–324:

```python
def _contract_agent_fields(
    *, category: str, title: str, description: str
) -> tuple[str, list[str], str]:
    assigned_agent = _DETERMINISTIC_AGENT_BY_CATEGORY.get(category)
    if assigned_agent is not None:
        return assigned_agent, [], description
    raise ValueError(
        f"contract category {category!r} has no specialist agent and is not "
        f"eligible for automated leaf creation; valid categories: "
        f"{sorted(_DETERMINISTIC_AGENT_BY_CATEGORY)}"
    )
```

This eliminates the silent fallback to `_DEFAULT_AGENT = "backend-developer"`.
`validate_compiled_spec` at line 1075 remains as a defense-in-depth check
for any path that bypasses `_contract_agent_fields`.

**Edit C — `src/gobby/plans/parser.py`** (new shared helpers — depends_on extractor + plan-id sentinel)

Two coupled additions to the parser module so emitter and compile path share
both definitions.

First — lift the `_DEPENDS_RE` regex from `expansion_service.py:231`:

```python
import re

_SECTION_DEPENDS_RE = re.compile(
    r"\(depends:\s*(?P<depends>[^)]+)\)", flags=re.IGNORECASE
)


def extract_section_dependencies(title: str) -> tuple[str, ...]:
    """Extract dependency section IDs from a `(depends: X, Y)` annotation.

    Returns an empty tuple if no annotation is present. Trims whitespace from
    each dependency reference.
    """
    match = _SECTION_DEPENDS_RE.search(title)
    if match is None:
        return ()
    return tuple(
        part.strip() for part in match.group("depends").split(",") if part.strip()
    )
```

Update `expansion_service.py:231` to import + use this helper instead of the
local `_DEPENDS_RE`. Update `_contract_section_depends` (line 248ff) to call
`parser.extract_section_dependencies(section.title)`.

Second — define a shared plan-id sentinel so parser and emitter agree on the
fallback used in `covers:<plan-id>:...` labels when a plan has no
`**Plan ID:**` header. Today `manifest_emitter.py:138` falls back to
`"unknown"` while parser `_compile_expected_labels` at `parser.py:801`
substitutes whatever `plan_id` is (literal `None` when missing). The
mismatch breaks the emit→reparse round-trip on no-Plan-ID plans:
`emit_stub_manifest` returns `fallback_force_approve`, appends a
`## Yolo Fallbacks` audit, and `parse_plan(parse_mode="expansion")` fails
with `covers:unknown` vs expected `covers:None` errors. Add:

```python
# Shared sentinel used when a plan omits its `**Plan ID:**` header. Parser
# and emitter MUST resolve the missing-id case the same way or covers-label
# round-trips fail.
MISSING_PLAN_ID_SENTINEL = "unknown"


def resolve_plan_id(plan_id: str | None) -> str:
    """Return the plan_id to use when generating or validating `covers:` labels.

    Affirmative fix is to add `**Plan ID:**` to the plan; this fallback is
    defense-in-depth so emit_stub_manifest + parse_plan round-trips cleanly
    even when an author forgets the header.
    """
    return plan_id or MISSING_PLAN_ID_SENTINEL
```

Update `parser.py:801` (inside `_compile_expected_labels`) to use
`resolve_plan_id(plan_id)` when building expected `covers:` labels. Update
`manifest_emitter.py:138` to use the same helper instead of the inline
`document.plan_id or "unknown"`.

**Edit D — `src/gobby/plans/manifest_emitter.py:~40-46`, line 138, and `_synthesize_entry`**

Three coupled changes:

```python
from gobby.plans.parser import extract_section_dependencies, resolve_plan_id

_AGENT_BY_CATEGORY: dict[str, str] = {
    "code": "backend-developer",
    "refactor": "backend-developer",
    "test": "test-architect",
}
_DEFAULT_AGENT_FALLBACK = "default"


def _agent_for(category: str) -> str:
    return _AGENT_BY_CATEGORY.get(category, _DEFAULT_AGENT_FALLBACK)
```

Inside `_synthesize_entry`:
- Replace `"depends_on": []` with
  `"depends_on": list(extract_section_dependencies(section.title))`.
- Replace the hardcoded `_DEFAULT_AGENT` reference with `_agent_for(category)`.

At line 138, replace `plan_id = document.plan_id or "unknown"` with
`plan_id = resolve_plan_id(document.plan_id)` so emitter and parser agree on
the fallback when `**Plan ID:**` is absent.

**Edit E — `tests/plans/test_manifest_emitter.py`**

Update existing tests + add new ones:

- `test_default_assignment_and_tdd_by_category`: assert the seven-category
  map (code/refactor → backend-developer; test → test-architect;
  config/docs/manual/research → default). Drop any assertion about
  `planning` (it's no longer routed; falls through to default-fallback).
- `test_synthesized_entry_preserves_section_depends_on` (new): synthetic plan
  with deliverables `### 1.1 A [category: code]` and `### 1.2 B [category:
  code] (depends: 1.1)` — assert the second entry's `depends_on == ["1.1"]`.
- `test_synthesized_entry_preserves_multi_dep` (new): `(depends: 1.1, 2.3,
  Phase 1)` → `depends_on == ["1.1", "2.3", "Phase 1"]`.
- `test_emit_and_reparse_round_trips_with_no_plan_id` (new): write a synthetic
  plan with `kind: deliverable` sections but **no** `**Plan ID:**` header to
  a tmp path; call `emit_stub_manifest(path)` and assert the outcome is
  `"fresh"` (not `"fallback_force_approve"`); then call
  `parse_plan(path, parse_mode="expansion")` and assert it returns without
  raising. Locks in that the parser/emitter sentinels stay aligned.

**Preflight — monolith refactor task (compliance, not refactor work)**

`src/gobby/tasks/expansion_service.py` is 1,495 lines (over the 1,000-line
cap from CLAUDE.md guiding principle #2: *NEVER create or leave monoliths*).
Before any of 1.1's edits land, search gobby-tasks for an existing open
refactor task targeting that file under epic `#12730`; if none exists, create
one as `category: refactor`, `status: open`, `allow_automation: false` so the
edits in 1.1 are recorded against an explicit deferral. The refactor itself
is **not** sub-plan A's work — only the task filing is in scope here.

**Acceptance:**

- 1.1.1 — `_DETERMINISTIC_AGENT_BY_CATEGORY` in `expansion_service.py`
  contains exactly the seven `AUTOMATED_LEAF_CATEGORIES`: `code`/`refactor`
  → `backend-developer`; `test` → `test-architect`; `config`/`docs`/`manual`/
  `research` → `default`; `planning` is intentionally absent. file:
  `src/gobby/tasks/expansion_service.py`.
- 1.1.2 — `_contract_agent_fields` raises `ValueError` for any category not
  in the map (instead of silently defaulting to `backend-developer`). symbol:
  `gobby.tasks.expansion_service._contract_agent_fields`.
- 1.1.3 — `gobby.plans.parser.extract_section_dependencies(title)` returns
  the dependency list parsed from a `(depends: …)` annotation; the
  expansion-service compile path imports + uses this helper instead of the
  local `_DEPENDS_RE`. symbol:
  `gobby.plans.parser.extract_section_dependencies`.
- 1.1.4 — `manifest_emitter._agent_for(category)` returns
  `backend-developer` for `code`/`refactor`, `test-architect` for `test`,
  and `default` otherwise; `_synthesize_entry` calls it. symbol:
  `gobby.plans.manifest_emitter._agent_for`.
- 1.1.5 — `manifest_emitter._synthesize_entry` populates `depends_on` from
  `extract_section_dependencies(section.title)` instead of hardcoding `[]`.
  symbol: `gobby.plans.manifest_emitter._synthesize_entry`.
- 1.1.6 — `tests/plans/test_manifest_emitter.py::test_default_assignment_and_tdd_by_category`
  asserts the full seven-category map. test:
  `tests/plans/test_manifest_emitter.py::test_default_assignment_and_tdd_by_category`.
- 1.1.7 — `tests/plans/test_manifest_emitter.py::test_synthesized_entry_preserves_section_depends_on`
  asserts a single-dep section is preserved into `manifest_entry.depends_on`.
  test: `tests/plans/test_manifest_emitter.py::test_synthesized_entry_preserves_section_depends_on`.
- 1.1.8 — `tests/plans/test_manifest_emitter.py::test_synthesized_entry_preserves_multi_dep`
  asserts a multi-dep `(depends: 1.1, 2.3, Phase 1)` annotation produces
  `depends_on == ["1.1", "2.3", "Phase 1"]`. test:
  `tests/plans/test_manifest_emitter.py::test_synthesized_entry_preserves_multi_dep`.
- 1.1.9 — `gobby.plans.parser.resolve_plan_id(None) == "unknown"` and
  `resolve_plan_id("foo") == "foo"`; `MISSING_PLAN_ID_SENTINEL == "unknown"`
  is exported from the parser module. symbol:
  `gobby.plans.parser.resolve_plan_id`.
- 1.1.10 — `parser._compile_expected_labels` (line 801) and
  `manifest_emitter._synthesize_entry` (line 138) both call
  `resolve_plan_id(plan_id)` instead of inlining their own fallback. symbol:
  `gobby.plans.parser._compile_expected_labels`.
- 1.1.11 — `tests/plans/test_manifest_emitter.py::test_emit_and_reparse_round_trips_with_no_plan_id`
  asserts that a synthetic plan with no `**Plan ID:**` round-trips cleanly
  through `emit_stub_manifest` (returns `"fresh"`) and
  `parse_plan(parse_mode="expansion")` (no raise). test:
  `tests/plans/test_manifest_emitter.py::test_emit_and_reparse_round_trips_with_no_plan_id`.
- 1.1.12 — Before any code edit in 1.1 lands, an open refactor task targeting
  `src/gobby/tasks/expansion_service.py` exists in gobby-tasks under epic
  `#12730` (search first; create if missing). The task is unclaimed and
  `allow_automation: false`. behavior: "monolith preflight task filed for
  expansion_service.py before sub-plan A edits land".

### 1.2 Refactor `compile_plan_to_spec` to be manifest-driven [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/tasks/expansion_service.py` (lines 447–651):
`compile_plan_to_spec`, `_contract_section_tasks`, `_contract_phase_index`,
`_contract_task_ids`, `_parse_contract_plan`.

Today `compile_plan_to_spec` iterates `kind: deliverable` sections directly
and infers task metadata from section-title regex. The §2.21 contract makes
the manifest the single source of truth. This deliverable rewires compile to
iterate `plan_doc.manifest_entries` and consume the typed fields verbatim.

**`ManifestEntry` fields** (already exposed by `gobby.plans.parser`):

```python
@dataclass(frozen=True)
class ManifestEntry:
    title: str
    category: str
    task_type: str
    depends_on: tuple[str, ...]
    validation_criteria: str
    labels: tuple[str, ...]      # covers:<plan-id>:<section-id>:<item-id>
    assigned_agent: str | None
    tdd: bool
    source_section: str
    source_line: int
```

**New shape of `compile_plan_to_spec`** (pseudocode):

```python
def compile_plan_to_spec(self, plan_doc: PlanDocument, task: Task) -> dict[str, Any]:
    plan_id = _contract_plan_id(plan_doc)
    section_by_id = {s.section_id: s for s in plan_doc.sections}
    phase_by_section_id = self._contract_phase_index(plan_doc)

    phases: list[dict[str, Any]] = []
    phase_by_id: dict[str, dict[str, Any]] = {}
    tasks: list[dict[str, Any]] = []
    dependencies: list[dict[str, str]] = []

    for entry in plan_doc.manifest_entries:
        section = section_by_id.get(entry.source_section)
        if section is None or section.kind is not Kind.deliverable:
            raise ValueError(
                f"manifest entry source_section={entry.source_section!r} "
                f"does not resolve to a kind: deliverable section"
            )
        # … phase nesting via phase_by_section_id …
        emitted = self._contract_entry_tasks(
            plan_doc=plan_doc,
            entry=entry,
            section=section,
            phase_id=phase_id,
            plan_id=plan_id,
        )
        # emitted is (test, impl, ref) when entry.tdd, else (single,)
        tasks.extend(emitted)
        # dependencies wired per Cross-tdd-mode rules below
    return {
        "version": 1, "parent_task_id": task.id,
        "plan_file": str(plan_doc.source_path),
        "phases": phases, "tasks": tasks,
        "dependencies": self._dedupe_dependencies(dependencies),
        "execution_groups": [], "deferrals": [],
        "contract_plan": True, "plan_id": plan_id,
        "deliverable_count": len(plan_doc.manifest_entries),
    }
```

**`_contract_entry_tasks`** (replaces `_contract_section_tasks`):

When `entry.tdd is True`: emit three tasks (TEST/IMPL/REF) using `entry.title`,
`entry.category` (IMPL), `"test"`/`"refactor"` (TEST/REF). Description prose
comes from the source section body for context; titles get `[TEST] ` /
`[IMPL] ` / `[REF] ` prefixes; labels = `entry.labels` verbatim;
`assigned_agent` = `entry.assigned_agent` verbatim; `validation_criteria` =
`entry.validation_criteria` verbatim; `task_type` = `entry.task_type`.

When `entry.tdd is False`: emit one single task with no `[TEST]`/`[REF]`
siblings and no `[IMPL]` prefix on the title (just `entry.title` as-is).

**Stable IDs** — extend `_contract_task_ids(section_id)` to include a fourth
form for tdd-false entries. Today returns `(test_id, impl_id, ref_id)` with
suffixes `::test`, `::impl`, `::ref`. Add a new helper or extend signature:

```python
def _contract_task_ids(section_id: str) -> tuple[str, str, str]:
    return (f"{section_id}::test", f"{section_id}::impl", f"{section_id}::ref")

def _contract_single_task_id(section_id: str) -> str:
    return f"{section_id}::single"
```

**Cross-tdd-mode dependency wiring** — when an entry depends on another, the
dependency target depends on the blocker's tdd-mode:

| Caller tdd | Blocker tdd | Edge wired |
|---|---|---|
| true (TEST→IMPL→REF chain) | true | `caller_TEST` depends on `blocker_REF` |
| true | false | `caller_TEST` depends on `blocker_single` |
| false (single only) | true | `caller_single` depends on `blocker_REF` |
| false | false | `caller_single` depends on `blocker_single` |

The internal IMPL→TEST and REF→IMPL edges within a TDD triplet are unchanged.

**Phase nesting** — `_contract_phase_index` walks `parent_id` up to a section
matching `_CONTRACT_PHASE_ID_RE = ^P(?P<number>\d+)$`. Already works for plan
A's `P1` and for plan-12725's `P1`/`P2`/`P3`. No change needed in this
deliverable.

**Failure modes that must raise loudly** (no silent skip):

- Manifest entry's `source_section` references a missing or non-deliverable
  section.
- A plan deliverable exists with no manifest entry pointing at it (orphan
  deliverable). Surface via `_validate_compiled_spec` post-hoc; the parser's
  expansion-mode check should already catch this upstream.
- An `entry.depends_on` reference resolves to a section that has no manifest
  entry of its own.

**Promote the contract-plan parse to expansion mode** —
`ExpansionService._parse_contract_plan` (line 632) currently calls
`parse_plan(parse_mode="draft")`, which tolerates a missing manifest. Once
1.2's compile uses `manifest_entries` as SSOT, a missing manifest would
silently produce an empty spec. Change line 641 from
`parse_mode="draft"` to `parse_mode="expansion"` so a deliverable-only
plan with no `## M1 Task Manifest` section raises
`PlanParseError("missing manifest")` upstream of `compile_plan_to_spec`.
The existing `except (OSError, PlanParseError) as exc:` handler at lines
642–651 already wraps this as a `ValueError("Plan file must conform to
the Plan-Coverage Contract: …")` for the run log, which is the right
surface message.

Reuse existing helpers where possible: `_contract_covers_labels`,
`_contract_acceptance_lines`, `_contract_artifact_summary`,
`_contract_agent_fields`, `_clean_contract_section_title` — but called with
manifest entry as primary input, source section as fallback for prose only.

**Acceptance:**

- 1.2.1 — `compile_plan_to_spec` iterates `plan_doc.manifest_entries` and
  raises `ValueError` if any entry's `source_section` does not resolve to a
  `kind: deliverable` section. symbol:
  `gobby.tasks.expansion_service.ExpansionService.compile_plan_to_spec`.
- 1.2.2 — TDD triplet emission fires only when `entry.tdd is True`; tdd-false
  entries emit exactly one task with no prefix on the title. symbol:
  `gobby.tasks.expansion_service.ExpansionService._contract_entry_tasks`.
- 1.2.3 — `_contract_single_task_id(section_id)` returns
  `f"{section_id}::single"` and is used for all tdd-false leaves. symbol:
  `gobby.tasks.expansion_service._contract_single_task_id`.
- 1.2.4 — Cross-tdd-mode dependency wiring: caller-TEST depends on
  blocker-REF when both tdd; caller-TEST depends on blocker-single when
  blocker is tdd-false; caller-single depends on the corresponding blocker
  terminal task. test:
  `tests/tasks/test_expansion_service_compile_manifest_driven.py::test_cross_tdd_mode_dependencies`.
- 1.2.5 — Compiled spec preserves manifest entry fields verbatim onto
  resulting tasks: `title`, `category`, `task_type`, `validation_criteria`,
  `labels`, `assigned_agent`. test:
  `tests/tasks/test_expansion_service_compile_manifest_driven.py::test_entry_fields_preserved`.
- 1.2.6 — Phase nesting honors `P\d+` framing parents via
  `_contract_phase_index`. test:
  `tests/tasks/test_expansion_service_compile_manifest_driven.py::test_phase_nesting_p1_p2_p3`.
- 1.2.7 — A new test fixture file at
  `tests/tasks/test_expansion_service_compile_manifest_driven.py` covers
  acceptance 1.2.1 through 1.2.6 with synthetic plans + manifests. file:
  `tests/tasks/test_expansion_service_compile_manifest_driven.py`.
- 1.2.8 — `ExpansionService._parse_contract_plan` calls
  `parse_plan(parse_mode="expansion")`; a deliverable-only plan with no
  `## M1 Task Manifest` raises `ValueError("Plan file must conform to the
  Plan-Coverage Contract: …")` (wrapping `PlanParseError("missing
  manifest")`). symbol:
  `gobby.tasks.expansion_service.ExpansionService._parse_contract_plan`.
- 1.2.9 — `tests/tasks/test_expansion_service_compile_manifest_driven.py::test_missing_manifest_raises`
  passes a synthetic plan with `kind: deliverable` sections but no
  manifest section to `_parse_contract_plan` and asserts the wrapping
  `ValueError` fires. test:
  `tests/tasks/test_expansion_service_compile_manifest_driven.py::test_missing_manifest_raises`.

### 1.3 Fix stale fixture path in compile-service tests [category: refactor] (depends: 1.2)
`kind: deliverable`

Target: `tests/tasks/test_expansion_service_compile.py` (line 33);
`tests/tasks/test_expansion_service_compile_minimal.py` (if same path).

Both files reference `task-12725-lifecycle-dispatch.md` (no `-rev1` suffix).
That file does not exist; only `task-12725-lifecycle-dispatch-rev1.md` does.
All compile-service tests currently fail with `FileNotFoundError`.

**Edits**:

1. Update `_canonical_plan_path()` (line 33 of `test_expansion_service_compile.py`):

   ```python
   def _canonical_plan_path() -> Path:
       return (
           Path(__file__).resolve().parents[2]
           / ".gobby/plans/task-12725-lifecycle-dispatch-rev1.md"
       )
   ```

2. Update expected counts to match the manifest-driven semantics shipped in
   1.2. Plan-12725 has 32 deliverables; 21 are tdd-true (`code`/`refactor`)
   and 11 are tdd-false (`config`/`docs`/`manual`). Expected:

   - `spec["deliverable_count"] == 32`
   - `len(spec["tasks"]) == 21 * 3 + 11 * 1 == 74`

3. Apply the same fixture-path update to
   `tests/tasks/test_expansion_service_compile_minimal.py` if it imports the
   same helper or hardcodes the path.

4. Walk every assertion in both files; replace any that assumes
   "always 3 tasks per deliverable" with the manifest-aware shape (3 if tdd
   else 1).

**Acceptance:**

- 1.3.1 — `_canonical_plan_path()` returns
  `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`. symbol:
  `tests.tasks.test_expansion_service_compile._canonical_plan_path`.
- 1.3.2 — `tests/tasks/test_expansion_service_compile.py::test_compile_contract_plan_emits_tdd_leaves_by_phase`
  asserts `deliverable_count == 32` and `len(tasks) == 74`. test:
  `tests/tasks/test_expansion_service_compile.py::test_compile_contract_plan_emits_tdd_leaves_by_phase`.
- 1.3.3 — `tests/tasks/test_expansion_service_compile_minimal.py` (if it
  exists and uses the stale path) is updated parallel to 1.3.1/1.3.2 and
  passes. file: `tests/tasks/test_expansion_service_compile_minimal.py`.
- 1.3.4 — `uv run pytest tests/tasks/test_expansion_service_compile.py
  tests/tasks/test_expansion_service_compile_minimal.py -v` passes. behavior:
  "compile-service test suite is green against plan-12725 rev1" in this plan.

### 1.4 Emit plan-12725 manifest + bump coverage manifest header [category: config] (depends: 1.2)
`kind: deliverable`

Target: `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`;
`.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch-rev1.coverage.yaml`.

Plan-12725 currently has no `## M1 Task Manifest` section — line 2386's
manifest block is inside §2.21's *spec example*, not a real section.
`parse_plan(parse_mode="expansion")` raises
`PlanParseError("missing manifest")` at line 2871. This deliverable closes
that gap by emitting a manifest via the §2.21a stub emitter and refreshing
the coverage manifest header to match the new plan_hash.

**Sequence**:

0. **Plan-ID preflight** — Add an explicit `**Plan ID: task-12725-lifecycle-dispatch-rev1**`
   line to the plan header (after `# Lifecycle-state-driven agent dispatch`,
   before the first framing section). Without this, `parse_plan(...).plan_id`
   is `None` and the emit→reparse round-trip leans on the §1.1 sentinel
   fallback (`"unknown"`) — affirmative IDs are preferred. The plan_hash
   changes after this edit (handled by Step 3 below).

1. Run `gobby.plans.manifest_emitter.emit_stub_manifest` against the plan:

   ```python
   from pathlib import Path
   from gobby.plans.manifest_emitter import emit_stub_manifest
   outcome = emit_stub_manifest(
       Path(".gobby/plans/task-12725-lifecycle-dispatch-rev1.md")
   )
   assert outcome == "fresh", f"unexpected emit outcome: {outcome}"
   ```

   With 1.1's routing patch already merged: 21 entries (code+refactor) →
   `backend-developer`; 0 entries → `test-architect` (plan-12725 has no
   `[category: test]` deliverables); 11 entries (config+docs+manual) →
   `default`. Truthful per the no-backend-dev-fallback rule.

2. Verify the post-emit plan parses cleanly in expansion mode:

   ```python
   from pathlib import Path
   from gobby.plans.parser import parse_plan
   doc = parse_plan(
       Path(".gobby/plans/task-12725-lifecycle-dispatch-rev1.md"),
       parse_mode="expansion",
   )
   assert len(doc.manifest_entries) == 32
   ```

3. The plan_hash changed when the manifest was appended. Regenerate the
   coverage manifest header at
   `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch-rev1.coverage.yaml`
   so its `plan_hash:` field matches the new hash. Either run
   `gobby plan coverage --regenerate` (if the CLI flag covers header refresh)
   or update the header field in place to `parse_plan(...).source_hash`.

4. If `emit_stub_manifest` returns `"fallback_force_approve"` instead of
   `"fresh"`, the `## Yolo Fallbacks` audit appended to the plan names the
   cause. Fix the offending deliverable section in the plan (plan-text edits
   are allowed; only task-tree edits are off-limits) and re-emit. `emit_stub_manifest`
   is idempotent — re-emit replaces the bad manifest with a fresh one.

**Acceptance:**

- 1.4.1 — `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md` contains a
  `## M1 Task Manifest` section with `kind: manifest` front-matter. file:
  `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`.
- 1.4.2 — `parse_plan(path, parse_mode="expansion")` against plan-12725
  succeeds and `len(doc.manifest_entries) == 32`. behavior:
  "plan-12725 parses clean in expansion mode" in this plan.
- 1.4.3 — Coverage manifest header at
  `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch-rev1.coverage.yaml`
  carries a `plan_hash:` matching `parse_plan(plan_path,
  parse_mode="draft").source_hash`. file:
  `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/12725/task-12725-lifecycle-dispatch-rev1.coverage.yaml`.
- 1.4.4 — `tests/plans/test_plan_coverage_ci.py::test_manifest_plan_hash_matches_on_disk`
  passes after the header bump. test:
  `tests/plans/test_plan_coverage_ci.py::test_manifest_plan_hash_matches_on_disk`.
- 1.4.5 — `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md` carries an
  explicit `**Plan ID: task-12725-lifecycle-dispatch-rev1**` line in the
  header so `parse_plan(path, parse_mode="expansion").plan_id ==
  "task-12725-lifecycle-dispatch-rev1"` (no longer `None`). file:
  `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`.

### 1.5 Dry-run assertion harness against plan-12725 [category: test] (depends: 1.2, 1.3, 1.4)
`kind: deliverable`

Target: `tests/tasks/test_expansion_service_compile_plan_12725.py` (new file).

Slow-marked integration test that runs `compile_plan_to_spec` against the
real plan-12725 file (post-1.4 emit) and asserts the compiled spec matches
the manifest 1:1. This is the gate that proves sub-plan A's mechanics work
end-to-end before sub-plan B starts.

**Test outline**:

```python
import pytest
from pathlib import Path
from gobby.plans.parser import parse_plan
from gobby.tasks.expansion_service import ExpansionService

pytestmark = [pytest.mark.integration, pytest.mark.slow]

PLAN_PATH = Path(".gobby/plans/task-12725-lifecycle-dispatch-rev1.md")


def test_plan_12725_compiles_clean(service: ExpansionService, parent_task) -> None:
    doc = parse_plan(PLAN_PATH, parse_mode="expansion")
    spec = service.compile_plan_to_spec(doc, parent_task)

    assert spec["contract_plan"] is True
    assert spec["plan_id"] == "task-12725-lifecycle-dispatch-rev1"
    assert spec["deliverable_count"] == 32
    # 21 tdd-true × 3 + 11 tdd-false × 1
    assert len(spec["tasks"]) == 74

    # Phases: P1, P2, P3
    phase_ids = {p["id"] for p in spec["phases"]}
    assert phase_ids == {"phase-p1", "phase-p2", "phase-p3"}

    # Per-entry shape: title/category/task_type/validation_criteria/assigned_agent
    # match the manifest entry verbatim
    by_source = {entry.source_section: entry for entry in doc.manifest_entries}
    impl_or_single_tasks = [
        t for t in spec["tasks"]
        if not t["title"].startswith("[TEST]") and not t["title"].startswith("[REF]")
    ]
    assert len(impl_or_single_tasks) == 32  # one IMPL/single per entry
    for task in impl_or_single_tasks:
        section_id = task["source_section_id"]
        entry = by_source[section_id]
        assert entry.title in task["title"]
        assert task["category"] == entry.category
        assert task["assigned_agent"] == entry.assigned_agent
        assert sorted(task["labels"]) == sorted(entry.labels)

    # No deferrals (plan-12725 has zero `kind: deferred`)
    assert spec["deferrals"] == []

    # Dependency graph: plan-12725 has EXACTLY 24 deliverables with
    # `(depends: …)` annotations. After 1.1's emitter patch every annotation
    # lands in the manifest entry's `depends_on`. After 1.2's compile every
    # dep produces a wired edge in `spec["dependencies"]` per the
    # cross-tdd-mode rules. Every annotated blocker MUST resolve to another
    # manifest entry — unresolved or cross-phase targets fail the harness
    # loudly so a regression in §1.1 (drop a dep) or §1.2 (drop a wiring
    # edge) cannot pass silently.
    edges_by_caller: dict[str, set[str]] = {}
    for edge in spec["dependencies"]:
        edges_by_caller.setdefault(edge["task_id"], set()).add(edge["depends_on"])

    annotated_entries = [e for e in doc.manifest_entries if e.depends_on]
    assert len(annotated_entries) == 24, (
        f"plan-12725 must have exactly 24 (depends: …)-annotated "
        f"deliverables; got {len(annotated_entries)}. A drift here means "
        f"§1.1's emitter patch dropped section-title annotations."
    )

    by_section = {e.source_section: e for e in doc.manifest_entries}
    for entry in annotated_entries:
        caller_lead = (
            f"{entry.source_section}::test"
            if entry.tdd
            else f"{entry.source_section}::single"
        )
        for blocker_section in entry.depends_on:
            blocker_entry = by_section.get(blocker_section)
            assert blocker_entry is not None, (
                f"unresolved depends_on target: "
                f"{entry.source_section} depends on {blocker_section}, "
                f"which is not a manifest entry. Either the source plan's "
                f"annotation references a non-deliverable section or the "
                f"emitter dropped that entry."
            )
            blocker_terminal = (
                f"{blocker_section}::ref"
                if blocker_entry.tdd
                else f"{blocker_section}::single"
            )
            assert blocker_terminal in edges_by_caller.get(caller_lead, set()), (
                f"missing dependency edge: "
                f"{caller_lead} → {blocker_terminal} "
                f"(from {entry.source_section} depends_on {blocker_section})"
            )
```

**Note**: this test does NOT call `apply_run`. The compiled spec sits on
the in-memory return value; nothing is written to the tasks DB.

When an assertion fails, the harness names which entry/task mismatched.
Iterate sub-plan A's compile code (1.2) until the harness passes.

**Acceptance:**

- 1.5.1 — `tests/tasks/test_expansion_service_compile_plan_12725.py` exists,
  is marked `@pytest.mark.slow`, and asserts `deliverable_count == 32` +
  `len(tasks) == 74`. file:
  `tests/tasks/test_expansion_service_compile_plan_12725.py`.
- 1.5.2 — Harness asserts each manifest entry's
  title/category/task_type/validation_criteria/assigned_agent/labels are
  preserved verbatim onto the IMPL/single task. test:
  `tests/tasks/test_expansion_service_compile_plan_12725.py::test_plan_12725_compiles_clean`.
- 1.5.3 — Harness asserts phase nesting yields `phase-p1`, `phase-p2`,
  `phase-p3` exactly. test:
  `tests/tasks/test_expansion_service_compile_plan_12725.py::test_plan_12725_compiles_clean`.
- 1.5.4 — Harness asserts `spec["deferrals"] == []`. test:
  `tests/tasks/test_expansion_service_compile_plan_12725.py::test_plan_12725_compiles_clean`.
- 1.5.5 — Harness asserts the dependency graph end-to-end: exactly 24
  manifest entries have `depends_on`, every annotated blocker resolves to
  another manifest entry (no `None` skip), and every caller→blocker terminal
  edge is present in `spec["dependencies"]` per the cross-tdd-mode rules
  from 1.2. The harness fails loudly on count drift, unresolved targets, or
  missing edges. test:
  `tests/tasks/test_expansion_service_compile_plan_12725.py::test_plan_12725_compiles_clean`.
- 1.5.6 — `uv run pytest tests/tasks/test_expansion_service_compile_plan_12725.py
  -v --override-ini="addopts="` runs and the harness passes. behavior:
  "compile-only dry-run against plan-12725 produces a manifest-1:1 spec" in
  this plan.

## V1 Verification
`kind: verification`

End-to-end checks for sub-plan A. Sub-plan A is **complete** when all of
the following are green:

1. `uv run pytest tests/plans/test_manifest_emitter.py
   tests/plans/test_plan_coverage_ci.py
   tests/tasks/test_expansion_service_compile.py
   tests/tasks/test_expansion_service_compile_minimal.py
   tests/tasks/test_expansion_service_compile_manifest_driven.py
   tests/tasks/test_expansion_service_compile_plan_12725.py -v` — all pass.
2. `uv run python -c "from pathlib import Path; from gobby.plans.parser
   import parse_plan; doc = parse_plan(Path('.gobby/plans/task-12725-lifecycle-dispatch-rev1.md'),
   parse_mode='expansion'); assert len(doc.manifest_entries) == 32"` — manifest is real and parser-clean.
3. `uv run gobby expand compile #12725 --plan-file
   .gobby/plans/task-12725-lifecycle-dispatch-rev1.md --json-output` produces
   a `compiled_spec` whose shape matches the harness assertions in 1.5.
4. **No tasks created** under #12725 by sub-plan A's work (verify via
   `list_tasks(parent_task_id=12725)`); no `apply_run` was called for
   plan-12725.
5. `uv run ruff check src/ && uv run ruff format --check src/ && uv run
   mypy src/gobby/tasks/expansion_service.py src/gobby/plans/manifest_emitter.py` — clean.

## Out of scope (handed off to sub-plans B and C)
`kind: framing`

Sub-plan A intentionally stops at "compile mechanics work; plan-12725
compiled_spec passes assertions." The following are sub-plan B / C concerns:

- **Specialist agents** (workflow-author, tech-writer, architect, analyst,
  pm, ux-designer, etc.) — translated/authored from BMAD references in
  `.claude/bmad-skills/`. Sub-plan B owns this.
- **Category enum expansion** (`architecture`, `requirements`, `devops`,
  `ux`, `e2e`). Sub-plan B owns this; the existing 8-category enum suffices
  for sub-plan A's mechanics work.
- **Plan-12725 apply** + holistic-qa verification + `gobby plan coverage`
  matrix run + e2e `gobby build` smoke. Sub-plan C owns this.
- **Apply idempotency** (refuse-if-already-applied guard). Sub-plan B
  delivers it before sub-plan C applies.
- **Reconciliation with manual epic #13412** ("Implement remaining lifecycle
  dispatch plan sections 2.15-2.20 and 2.23-2.24"). Sub-plan C decides
  whether #13412 is closed/merged/repurposed alongside the contract-driven
  expansion of plan-12725.
- **Closure of #13391** (manifest contract foundation sub-epic). Sub-plan C
  closes it as `already_implemented` immediately before applying plan-12725.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

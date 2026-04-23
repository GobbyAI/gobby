# Depth-capped task expansion (default 3, max 5)

## Overview

Give task expansion an explicit, configurable depth ceiling. Default `max_depth = 3` (epic → sub-epic → tasks), overridable per run up to `5` (parallel to the existing agent-spawn `max_depth = 5`). Non-expansion creation paths (API `create_task`, internal `TaskTreeBuilder`) enforce an absolute ceiling of `5` so manual tooling can't produce deeper trees than expansion. Sync restore from `.gobby/tasks.jsonl` warns on historical data beyond the cap but does not reject.

Current expansion has no explicit depth model. Multi-phase plans (`## Phase N:` headings ≥ 2) produce 3-level trees; single-phase plans produce 2-level trees. This plan preserves that heuristic — we don't force a synthetic sub-epic layer when a single-phase expansion is fine — but adds a hard ceiling and a per-call override. When a multi-phase plan would exceed the active `max_depth`, expansion collapses to single-phase (with a log line) rather than rejecting.

The concrete pain point motivating this: #12286 auto-expanded directly into leaf tasks (depth 2) and had to be manually normalized into sub-epics #12594-#12596 with leaves reparented. With this change the default max_depth of 3 still permits the sub-epic layer when the plan has phases, and the explicit cap prevents unbounded recursion from any creation path.

## Constraints

- **Default behavior preserved.** Single-phase plans still produce depth-2 trees. No synthetic sub-epic layer is inserted.
- **Per-run override.** `max_depth` is accepted on `start_expansion_run` MCP call and `gobby tasks expand compile/apply` CLI. Value must be in `[2, 5]`. Unspecified falls back to daemon config default (3).
- **Absolute ceiling = 5.** Non-expansion creation paths reject when a parent is already at depth 5. Not user-tunable.
- **No destructive migration.** Existing depth-5+ task trees in the DB are left intact. Enforcement is creation-time only.
- **Sync warns, doesn't reject.** `.gobby/tasks.jsonl` restore accepts depth > 5 historical data with a logged warning.
- **Ship default-on.** No feature flag. Default max_depth = 3 keeps existing per-plan behavior because every plan we've seen fits within depth 3.
- **Reuse existing utilities.** Depth is computed from `Task.path_cache` when populated (count `.` + 1), falling back to `compute_path_cache` in `src/gobby/storage/tasks/_path_cache.py`.

## Phase 1: Config and storage foundation

**Goal**: Add the `max_depth` config field and persist the resolved value per expansion run so compile→apply can read it consistently.

### 1.1 Add max_depth to TaskExpansionConfig [category: config]

Target: `src/gobby/config/tasks.py`

Add a new field to `TaskExpansionConfig` (next to `max_subtasks`, around line 140) with the same Pydantic validator idiom already used elsewhere in that module:

```python
max_depth: int = Field(
    default=3,
    ge=2,
    le=5,
    description=(
        "Maximum tree depth produced by expansion (root task = depth 1). "
        "Default 3 = epic → sub-epic → tasks. Max 5, parallel to the agent-spawn "
        "max_depth. Per-run override via start_expansion_run(max_depth=...)."
    ),
)
```

No validator override is needed — `ge`/`le` cover the bounds. Import of `Field` is already present.

Confirm:
- Existing `TaskExpansionConfig(max_subtasks=15, max_depth=3)` continues to validate.
- `TaskExpansionConfig(max_depth=1)` raises `ValidationError`.
- `TaskExpansionConfig(max_depth=6)` raises `ValidationError`.
- `TaskExpansionConfig()` defaults `max_depth` to `3`.

### 1.2 Add max_depth column to expansion_runs [category: code]

Target: `src/gobby/storage/migrations.py` and `src/gobby/storage/expansion_runs.py`

Add a new migration to the ordered migrations list in `migrations.py`. The migration adds a nullable `max_depth INTEGER` column to `expansion_runs`:

```sql
ALTER TABLE expansion_runs ADD COLUMN max_depth INTEGER;
```

Nullable because historical runs didn't have this field; they should keep compiling/applying without a forced backfill. Read sites fall back to daemon config default when null.

Follow the existing pattern in `migrations.py`: append a new numbered migration (increment from the most recent), guard idempotently per the existing harness idiom. Register the migration in the ordered list.

Confirm:
- `PRAGMA table_info(expansion_runs)` after `gobby start` on a fresh DB shows `max_depth` column.
- Upgrading an existing DB with rows in `expansion_runs` runs cleanly and leaves pre-existing rows with `max_depth IS NULL`.

### 1.3 Thread max_depth through ExpansionRun and its manager [category: code]

Target: `src/gobby/storage/expansion_runs.py`

1. Add `max_depth: int | None = None` to the `ExpansionRun` dataclass (alongside `plan_file`, `provider`, etc.).
2. Update `LocalExpansionRunManager.create(...)` signature to accept an optional `max_depth: int | None`, write it to the new column, and echo it back on the returned `ExpansionRun`.
3. Update the row-reader (`_row_to_run` or equivalent) to hydrate `max_depth` from the row; `None` when column is NULL.
4. No changes to `start`, `append_log`, `save_compiled_spec`, `save_apply_result`, `mark_applying` — they don't touch `max_depth`.

Confirm:
- Creating a run with `max_depth=5` and re-fetching via `get(run_id)` returns `run.max_depth == 5`.
- Creating a run with `max_depth=None` stores `NULL` and reads back as `None`.

## Phase 2: Expansion service enforcement (depends: Phase 1)

**Goal**: Use the stored/resolved `max_depth` to reject too-deep expansions, collapse multi-phase to single-phase when the sub-epic layer would exceed the cap, and validate compiled specs against the cap.

### 2.1 Add depth-resolution helpers to ExpansionService [category: code]

Target: `src/gobby/tasks/expansion_service.py`

Add two private helpers to the `ExpansionService` class (near `_get_expansion_config` around line 1300):

```python
def _resolve_effective_max_depth(self, run: ExpansionRun) -> int:
    """Return the run's max_depth, falling back to daemon config default."""
    if run.max_depth is not None:
        return run.max_depth
    expansion_config = self._get_expansion_config()
    return expansion_config.max_depth if expansion_config else 3


def _resolve_task_depth(self, task: Task) -> int:
    """Depth of a task from the root (root = 1). Uses path_cache when populated."""
    from gobby.storage.tasks._path_cache import compute_path_cache

    if task.path_cache:
        return task.path_cache.count(".") + 1
    path = compute_path_cache(self.db, task.id)
    if path:
        return path.count(".") + 1
    # Orphan / unreachable — treat as root.
    return 1
```

Import `compute_path_cache` inside the function to avoid a top-level import cycle. `task.path_cache` is populated automatically by the task manager (`src/gobby/storage/tasks/_path_cache.py:update_path_cache`).

Confirm:
- A task with `parent_task_id=None` resolves to depth 1.
- A task two levels under root resolves to depth 3.

### 2.2 Reject over-depth expansion at compile entry [category: code]

Target: `src/gobby/tasks/expansion_service.py` (method `compile_run`, line 359)

At the top of `compile_run`, after fetching the task and run but before starting any LLM work, add:

```python
max_depth = self._resolve_effective_max_depth(run)
parent_depth = self._resolve_task_depth(task)
if parent_depth >= max_depth:
    self.run_manager.append_log(
        run_id,
        level="error",
        message=(
            f"Cannot expand task at depth {parent_depth}; "
            f"max_depth={max_depth} already reached"
        ),
    )
    raise ValueError(
        f"Task {task.id} is at depth {parent_depth}; "
        f"max_depth={max_depth} does not allow further expansion"
    )
```

This runs on every compile (including resume), so a run created before a cap change still fails cleanly when re-compiled.

Confirm:
- `compile_run` on a depth-3 task with `max_depth=3` raises `ValueError` with the expected message.
- `compile_run` on a depth-2 task with `max_depth=3` proceeds normally.
- The rejection log line is visible via `get_expansion_run`.

### 2.3 Collapse multi-phase to single-phase when the sub-epic layer would exceed max_depth [category: code]

Target: `src/gobby/tasks/expansion_service.py` (method `compile_run`, lines 370-382)

Replace the `if len(phase_sections) >= 2:` branch with:

```python
max_depth = self._resolve_effective_max_depth(run)
parent_depth = self._resolve_task_depth(task)

# Multi-phase expansion adds a sub-epic layer at parent_depth+1 with leaves at
# parent_depth+2. Only use it when both layers fit inside max_depth.
allow_subepic_layer = (parent_depth + 2) <= max_depth

if len(phase_sections) >= 2 and allow_subepic_layer:
    self.run_manager.append_log(
        run_id,
        level="info",
        message=f"Detected {len(phase_sections)} phases; compiling per-phase",
    )
    compiled_spec = await self._compile_multi_phase(run, task, phase_sections)
else:
    if len(phase_sections) >= 2 and not allow_subepic_layer:
        self.run_manager.append_log(
            run_id,
            level="warning",
            message=(
                f"Plan has {len(phase_sections)} phases but parent_depth="
                f"{parent_depth} + 2 > max_depth={max_depth}; "
                f"collapsing to single-phase (leaves at depth {parent_depth + 1})"
            ),
        )
    raw_spec = await self._generate_raw_spec(run, task)
    compiled_spec = self.normalize_compiled_spec(
        raw_spec, task=task, plan_file=run.plan_file
    )
```

In `apply_run`, the `multi_phase = len(phase_list) > 1` flag at line 445 governs whether sub-epic intermediates are created. Since compile already collapsed offending multi-phase cases, `apply_run` needs no change — a collapsed spec has `len(phases) == 1` and `multi_phase` stays `False`. Verify by reading lines 480-495 (the `if multi_phase:` block) to confirm the predicate is purely `multi_phase`.

When collapsing, the per-phase LLM call structure is lost (the single LLM call now sees the whole plan file, not a scoped section). This is the expected tradeoff — the user's constraint is "single-phase depth-2 is fine"; forcing a sub-epic layer solely to preserve per-phase LLM calls would defeat the point.

Confirm:
- Expanding a depth-2 task with max_depth=3 and a 3-phase plan logs the "collapsing" warning and produces a depth-3 tree (leaves only under the depth-2 task).
- Expanding a depth-1 task with max_depth=3 and a 3-phase plan keeps current behavior (sub-epics + leaves).
- Expanding a depth-1 task with max_depth=5 and a 3-phase plan keeps current behavior.
- Expanding a depth-3 task with max_depth=5 and a 3-phase plan keeps current behavior (sub-epics at depth-4 + leaves at depth-5).

### 2.4 Add depth validation to validate_compiled_spec [category: code]

Target: `src/gobby/tasks/expansion_service.py` (method `validate_compiled_spec`, line 710)

`validate_compiled_spec` currently takes only the spec. Extend its signature with optional `run` and `task` keywords (defaulting to None to preserve backward compatibility):

```python
def validate_compiled_spec(
    self,
    compiled_spec: dict[str, Any],
    *,
    run: ExpansionRun | None = None,
    task: Task | None = None,
) -> dict[str, Any]:
```

When both `run` and `task` are provided, additionally check:

1. Resolved `max_depth` is in `[2, 5]`.
2. With `parent_depth = self._resolve_task_depth(task)`:
   - If `len(phases) > 1` (multi-phase spec): ensure `parent_depth + 2 <= max_depth`.
   - Always: ensure `parent_depth + 1 <= max_depth` (otherwise the spec shouldn't exist — compile should have rejected).

Append a string to the `errors` list on any violation.

Update both call sites in the same file (`compile_run` line 383 and `apply_run` line 437) to pass `run=run, task=task`.

Confirm:
- A multi-phase spec with parent_depth=2, max_depth=3 produces a validation error (`parent_depth + 2 > max_depth`).
- A single-phase spec with parent_depth=2, max_depth=3 validates cleanly.
- Callers without `run`/`task` still work against the old signature.

## Phase 3: Public surface (depends: Phase 1, Phase 2)

**Goal**: Expose `max_depth` as a parameter on the MCP tool and CLI.

### 3.1 Add max_depth to start_expansion_run MCP tool [category: code]

Target: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

In `start_expansion_run` (line 183), add an optional `max_depth: int | None = None` keyword parameter. Validate inline:

```python
if max_depth is not None and not (2 <= max_depth <= 5):
    return {"success": False, "error": "max_depth must be in [2, 5]"}
```

Pass `max_depth` through to `LocalExpansionRunManager.create(...)`. The resolved value (fallback to config default when None) should be echoed in the response body so callers see what will actually be used:

```python
resolved_max_depth = max_depth if max_depth is not None else (
    expansion_config.max_depth if expansion_config else 3
)
# ... existing creation logic ...
return {
    "success": True,
    "run_id": run.id,
    "max_depth": resolved_max_depth,
    # ... existing fields ...
}
```

Also include `max_depth` in the response payloads of `get_expansion_run` and `get_latest_expansion_run` — pass `run.max_depth` through (may be `None`).

Confirm:
- `start_expansion_run(task_id=..., max_depth=5)` returns `max_depth: 5` and stores `5` in the row.
- `start_expansion_run(task_id=..., max_depth=1)` returns `{"success": false, "error": "max_depth must be in [2, 5]"}`.
- `start_expansion_run(task_id=...)` returns `max_depth: 3` (daemon default).
- `get_latest_expansion_run` on the run includes `max_depth`.

### 3.2 Add --max-depth flag to CLI expand commands [category: code]

Target: `src/gobby/cli/tasks/expand.py`

Add `--max-depth INT` as a Click option on `compile_cmd` (line 84) and `apply_cmd` (line 124). Use `click.IntRange(2, 5, clamp=False)`:

```python
@click.option(
    "--max-depth",
    type=click.IntRange(2, 5),
    default=None,
    help="Maximum tree depth produced by this expansion (2-5, default 3).",
)
```

`resume_cmd` (line 162) does NOT take `--max-depth` — it reuses the stored value from the run. Confirm by reading `resume_cmd` that it passes through the existing run without re-specifying depth.

Forward the parameter through the existing HTTP/service call sites. Where the CLI already wraps an HTTP POST, add `max_depth` to the JSON payload when not `None`.

Confirm:
- `uv run gobby tasks expand compile --task-id <id> --max-depth 5` creates a run with `max_depth=5`.
- `uv run gobby tasks expand compile --task-id <id> --max-depth 6` exits with Click's range error.
- `uv run gobby tasks expand compile --task-id <id>` omits `max_depth` from the payload; the daemon default (3) applies.

## Phase 4: Non-expansion creation guards

**Goal**: Prevent any non-expansion creation path from producing task trees deeper than 5 (the absolute hard ceiling).

### 4.1 Enforce absolute depth-5 cap in LocalTaskManager.create_task [category: code]

Target: `src/gobby/storage/tasks/_crud.py`

In `create_task`, when `parent_task_id` is provided, compute the parent's depth before inserting the new row. The existing parent-chain walk at line 183 (max_depth=100 safety loop) already traverses parents; piggyback on it or add a focused depth check:

```python
if parent_task_id is not None:
    parent_depth = self._compute_parent_depth(parent_task_id)
    if parent_depth >= 5:
        raise TaskDepthLimitError(
            f"Parent task {parent_task_id} is at depth {parent_depth}; "
            f"cannot create children beyond absolute depth 5"
        )
```

Define a new exception in the same module (or in `src/gobby/storage/tasks/_errors.py` if one exists):

```python
class TaskDepthLimitError(ValueError):
    """Raised when a task create would exceed the absolute depth-5 cap."""
```

Export it from the package `__init__.py` alongside the existing public names so route handlers can import and catch it.

Reuse `compute_path_cache` or the existing in-function walk — whichever the file already does. Do not add a second implementation.

Confirm:
- Creating a task with parent at depth 5 raises `TaskDepthLimitError`.
- Creating a task with parent at depth 4 succeeds (new task becomes depth 5).
- Creating a task with no parent (root) succeeds unconditionally.
- Creating via `create_task_with_decomposition` (used by expansion) goes through the same guard — expansion at `max_depth=5` with a depth-4 parent produces depth-5 children without error; expansion never produces depth-6 because Phase 2's compile-time rejection triggers first.

### 4.2 Guard TaskTreeBuilder recursion at absolute depth 5 [category: code]

Target: `src/gobby/tasks/tree_builder.py`

Extend `_create_node` (line 124) with a `depth` kwarg (root call gets `depth=1`). Reject and record an error when `depth > 5`:

```python
def _create_node(
    self,
    node: dict[str, Any],
    parent_task_id: str | None,
    sibling_index: int = 0,
    depth: int = 1,
) -> str | None:
    if depth > 5:
        title = node.get("title", "<no-title>")
        self._errors.append(
            f"Node '{title}' at depth {depth} exceeds absolute max of 5; skipping subtree"
        )
        return None
    # ... existing body ...
    for i, child in enumerate(children):
        self._create_node(child, parent_task_id=task.id, sibling_index=i, depth=depth + 1)
```

Update the single call site at line 90 in `build(...)` to pass `depth=1` explicitly (it is the default but making it explicit signals intent).

Confirm:
- A tree nested 6 deep reports a depth error in `result.errors` and does NOT create the depth-6 task.
- A tree nested 5 deep creates all 5 levels cleanly.
- The existing test in `tests/tasks/test_tree_builder_coverage.py` still passes (shallow trees).

### 4.3 Sync restore warns instead of rejecting [category: code]

Target: `src/gobby/sync/tasks.py`

Sync-in re-creates tasks from `.gobby/tasks.jsonl`. If any historical row describes a task whose parent chain resolves to depth > 5, the restore must still succeed — this is a restore path, not a creation path, and must round-trip whatever is in git.

Around line 460 (the existing parent-chain walk), when a parent depth ≥ 5 is detected during sync:

```python
logger.warning(
    "Sync restoring task %s with parent at depth %d — exceeds the "
    "current absolute max_depth=5 for new creations; restoring as-is "
    "because sync is a restore path, not a creation path.",
    task_id,
    parent_depth,
)
```

And bypass the `TaskDepthLimitError` check. Concretely: the call to `create_task` from sync should go through a sync-specific code path or flag (e.g. `allow_over_depth=True` kwarg on the CRUD helper) that disables the depth guard. Keep the flag private to `sync/tasks.py` — do not expose it on the MCP tool or route layer.

Confirm:
- Sync import of `.gobby/tasks.jsonl` containing a depth-6 historical task produces a warning log line and a task row in the DB preserving the original parent.
- The new MCP `create_task` tool still rejects depth-6 creates (the private flag is never set from that path).

## Phase 5: Prompt hierarchy context (depends: Phase 2)

**Goal**: Expose the hierarchy context (parent depth, max_depth, leaf-layer flag) to expansion prompts so the LLM reasons about depth explicitly. Prompts keep the existing output shape — no schema changes, just context.

### 5.1 Extend _build_prompt_context with depth keys [category: code]

Target: `src/gobby/tasks/expansion_service.py` (method `_build_prompt_context`, line 923)

Add three new keys to the returned context dict, threading `is_leaf_layer` in from the caller so `_build_prompt_context` itself doesn't recompute phase sections:

```python
def _build_prompt_context(
    self,
    run: ExpansionRun,
    task: Task,
    *,
    is_leaf_layer: bool,
    plan_content_override: str | None = None,
    single_phase_mode: bool = False,
    phase_title: str = "",
    phase_number: int = 0,
) -> dict[str, Any]:
    # ...existing body...
    return {
        # ... existing keys ...
        "parent_depth": self._resolve_task_depth(task),
        "max_depth": self._resolve_effective_max_depth(run),
        "is_leaf_layer": is_leaf_layer,
    }
```

Update call sites to thread the flag:

- `_generate_raw_spec` (line 792): passes `is_leaf_layer=True` (single-phase path always produces leaves under the parent).
- `_generate_raw_spec_for_phase` (line 797): passes `is_leaf_layer=True` (per-phase LLM calls produce leaves under the synthesized sub-epic).

In today's codebase every LLM expansion call produces leaves, so `is_leaf_layer` is effectively always `True`. Keeping it as a parameter (rather than hard-coding `True`) makes sub-epic-layer-only expansion (a likely follow-up) a one-line change later.

Confirm:
- Rendering any expansion prompt produces a context dict containing `parent_depth`, `max_depth`, `is_leaf_layer` keys.
- Jinja2 `StrictUndefined` does not raise on any existing prompt (new keys are additive).

### 5.2 Update expand-task.md with a hierarchy preamble [category: docs]

Target: `src/gobby/tasks/prompts/expand-task.md`

Add a new section immediately after the opening paragraph (after line 4), before `## Output Format`:

```markdown
## Hierarchy Context

You are generating tasks at depth **{{ parent_depth + 1 }}** of a tree whose maximum
allowed depth is **{{ max_depth }}** (absolute ceiling is 5 across the system).

- The parent task you are expanding is at depth **{{ parent_depth }}**.
- These subtasks are LEAF tasks (implementation-level). Do NOT output nested
  child hierarchies, sub-epics, or `children` arrays — your output schema is
  flat (see Output Format below).
- Sub-epic structure, if needed, is handled by the expansion pipeline based on
  the plan file's `## Phase N:` headings — not by you.
```

The Jinja2 environment in `_render_prompt` already uses `StrictUndefined`, so any missing context key is a loud failure. Confirm the new keys are in `_build_prompt_context` before updating the prompt (Phase 5.1 must land first in the same commit series).

Confirm:
- Rendering `expand-task.md` with `parent_depth=1, max_depth=3, is_leaf_layer=True` produces the preamble with numeric substitutions.
- Rendering with a missing key raises `UndefinedError`.
- Existing expansions produce the same flat `{subtasks: [...]}` or `{phases, tasks}` output (the preamble is context only, not a schema change).

### 5.3 Update expand-task-tdd.md to note hierarchy [category: docs]

Target: `src/gobby/tasks/prompts/expand-task-tdd.md`

Add a one-sentence clarification at the top of the `## How It Works` section (after line 5):

```markdown
The TDD sandwich applies at the leaf layer — the tasks you output become the
implementation rung of the tree (depth {{ parent_depth + 1 }} of {{ max_depth }}).
```

Same caveats as 5.2 — keys must be in the prompt context when TDD mode renders this template.

Confirm:
- `expand-task-tdd.md` renders with the new depth sentence when `tdd_mode=True`.

## Verification

Run targeted tests after each phase; run the full targeted set at the end:

```bash
uv run pytest tests/config/test_tasks_config.py -v
uv run pytest tests/storage/test_migrations.py -v
uv run pytest tests/storage/expansion_runs/ -v
uv run pytest tests/tasks/test_expansion_service.py -v
uv run pytest tests/tasks/test_tree_builder_coverage.py -v
uv run pytest tests/storage/tasks/ -v
uv run pytest tests/mcp_proxy/tools/tasks/test_expansion.py -v
uv run pytest tests/cli/test_expand_cli.py -v
uv run pytest tests/sync/ -v
uv run ruff check src/gobby/tasks/ src/gobby/config/tasks.py src/gobby/mcp_proxy/tools/tasks/_expansion.py src/gobby/cli/tasks/expand.py src/gobby/storage/tasks/ src/gobby/storage/expansion_runs.py src/gobby/tasks/tree_builder.py src/gobby/sync/tasks.py
uv run mypy src/gobby/tasks/ src/gobby/config/tasks.py src/gobby/storage/tasks/ src/gobby/storage/expansion_runs.py
```

End-to-end smoke against a running daemon:

```bash
uv run gobby start --verbose
# Expand with default (max_depth=3) on a depth-1 epic with 3-phase plan
uv run gobby tasks expand compile --task-id <root-epic-id> --plan-file docs/plans/<plan>.md
uv run gobby tasks expand apply --run-id <run-id>
# → expect 3-level tree (epic → sub-epics → leaves)

# Expand a depth-2 sub-epic with same 3-phase plan and max_depth=3
uv run gobby tasks expand compile --task-id <sub-epic-id>
# → expect "collapsing to single-phase" warning in run log; depth-3 leaves under sub-epic

# Expand a depth-3 leaf
uv run gobby tasks expand compile --task-id <leaf-id>
# → expect rejection: "max_depth=3 does not allow further expansion"

# Per-call override to depth 5
uv run gobby tasks expand compile --task-id <new-root> --max-depth 5
# → expect run row stores max_depth=5; tree can extend to depth 5

# Out-of-range
uv run gobby tasks expand compile --task-id <any> --max-depth 6
# → expect Click range error
```

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

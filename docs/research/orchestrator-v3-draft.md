# Orchestrator v3: Implementation Plan (DRAFT — SUPERSEDED)

> **Superseded by `docs/plans/orchestrator-v3-final.md`.** This was the original 5-stage plan. It has been integrated with the p2 evolution into the final plan.
>
> Original context: Continues from `docs/plans/orchestrator-v2-discussion.md`. Resolves all open questions and stages implementation across 5 phases.

## Context

The current orchestrator creates one worktree per task, producing N branches and N merge operations for an epic with N subtasks. This compounds merge conflict risk, wastes disk/git overhead, and provides no intelligence about which tasks can safely run in parallel. The industry standard is worktree-per-agent with no shared-worktree parallel dispatch — we're building something novel with file-based dependency analysis.

### Resolved Design Decisions (from v2 discussion)

| Question | Resolution |
|----------|-----------|
| Q1: Agent interaction model | Agents call MCP tools directly (`add_dependency`, `set_affected_files`) |
| Q2: Planning agent vs expansion | Replace expansion — planning agent uses expansion prompt internally + explores codebase |
| Q3: Formatter serialization | Orchestrator runs format step between parallel batches; dependency agent identifies serialization points |
| Q4: Unexpected file touches | Accept imperfection. Post-hoc update via `annotation_source='observed'` from git diff. No mid-execution re-analysis |
| Q5: Clones (Gemini) | Orchestrator is isolation-agnostic. `spawn_agent` already supports both `worktree_id` and `clone_id` |
| Q6: Staging | 5 stages (4 orchestrator + 1 TDD enforcement) as described below |

---

## Stage 1: Single Worktree Per Epic + Bug Fixes

**Goal**: Stop creating N branches/worktrees. One worktree per epic, sequential dispatch. Fix real bugs found during exploration.

### 1a. Fix orchestrator pipeline — explicit `use_local` and `provider`

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Modify `create_worktree` step to pass `use_local: true` explicitly (the auto-detection exists in the MCP tool but the pipeline should be explicit)
- Pass `provider` from pipeline input so CLI hooks get installed in the worktree
- Bug #9538 note: the MCP tool itself is fine — it auto-detects and passes `use_local`. The issue is pipeline-level, not tool-level

### 1b. Restructure pipeline: one worktree per epic

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

Current flow: `suggest_next_task` → `create_worktree` (per task) → `spawn_developer`
New flow:
- **First iteration only**: `create_worktree` with epic-level branch name, store `_worktree_id` in pipeline state
- **Subsequent iterations**: Reuse `_worktree_id` (already supported by `spawn_agent`)
- **Merge phase**: One merge at epic completion (epic branch → target), not per-task
- Remove per-task worktree creation step
- QA agent gets `worktree_id` so it can see the actual changes

### 1c. Pass worktree context to QA agent

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Current `spawn_qa` passes no `worktree_id` — QA can't see worktree code
- Pass `_worktree_id` to QA spawn step

### 1d. Add `use_local` support to clones

**File**: `src/gobby/clones/git.py` (method `create_clone`, ~line 509)

Clones always clone from the remote URL (`get_remote_url()`), missing unpushed local commits. Fix: when `use_local=True`, clone from the local repo path instead of the remote URL.

```python
def create_clone(
    self,
    clone_path: str | Path,
    branch_name: str,
    base_branch: str = "main",
    shallow: bool = True,
    use_local: bool = False,      # NEW parameter
) -> GitOperationResult:
```

When `use_local=True`:
- Use `str(self.repo_path)` as the clone source instead of `get_remote_url()`
- Full clone (not shallow) — shallow clones from local paths can be unreliable
- This gives the clone all local commits including unpushed work

Wire `use_local` through:
- `src/gobby/mcp_proxy/tools/clones.py` — add `use_local` param to `create_clone` MCP tool (mirror worktree tool pattern)
- `src/gobby/agents/isolation.py` — `CloneIsolationHandler.prepare_environment` should auto-detect unpushed commits (same as `WorktreeIsolationHandler` already does)

### Verification

- Run orchestrator pipeline on a test epic with 3+ subtasks
- Confirm only 1 worktree/branch is created
- Confirm all agents work in the same worktree
- Confirm QA agent can see code changes
- Confirm merge produces a single merge operation
- Create a clone with `use_local=True` from a branch with unpushed commits — confirm clone has them
- Create a clone with `use_local=False` — confirm existing behavior unchanged

---

## Stage 2: `task_affected_files` Infrastructure

**Goal**: Build the data layer for file-based dependency analysis.

### 2a. Database migration — `task_affected_files` table

**File**: `src/gobby/storage/migrations.py`

```sql
CREATE TABLE task_affected_files (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    annotation_source TEXT NOT NULL DEFAULT 'expansion',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, file_path)
);
CREATE INDEX idx_taf_task_id ON task_affected_files(task_id);
CREATE INDEX idx_taf_file_path ON task_affected_files(file_path);
```

- Add as next migration number (check current `BASELINE_VERSION`, currently 133)
- Update `BASELINE_SCHEMA` with the new table
- `annotation_source`: `'expansion'` | `'manual'` | `'observed'`

### 2b. `TaskAffectedFileManager`

**New file**: `src/gobby/storage/task_affected_files.py`

Follow `TaskDependencyManager` pattern (`src/gobby/storage/task_dependencies.py`):

```python
class TaskAffectedFileManager:
    def __init__(self, db: DatabaseProtocol)
    def set_files(self, task_id: str, files: list[str], source: str = "expansion") -> None
    def get_files(self, task_id: str) -> list[TaskAffectedFile]
    def add_file(self, task_id: str, file_path: str, source: str) -> TaskAffectedFile
    def remove_file(self, task_id: str, file_path: str) -> bool
    def find_overlapping_tasks(self, task_ids: list[str]) -> dict[tuple[str, str], list[str]]
    def get_tasks_for_file(self, file_path: str) -> list[TaskAffectedFile]
```

Key method: `find_overlapping_tasks` — given a set of task IDs, returns `{(task_a, task_b): [shared_file_paths]}` for all pairs with file overlap.

### 2c. Expansion prompt — add `affected_files` and `parallel_group`

**File**: `src/gobby/tasks/prompts/expand-task.md`

Add to the subtask schema table:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `affected_files` | `array[string]` | No | Relative file paths this subtask will read or modify |
| `parallel_group` | `string` | No | Group label for tasks safe to run concurrently |

Add guidance in the prompt about predicting file paths and grouping parallel work.

### 2d. Tree builder — wire `affected_files`

**File**: `src/gobby/tasks/tree_builder.py`

In `_create_node()`, after `task_manager.create_task(...)`:
- Read `node.get("affected_files", [])`
- Call `TaskAffectedFileManager.set_files(task.id, affected_files)`
- ~5-10 lines of new code

Store `parallel_group` as a task label with `parallel:` prefix (e.g., `parallel:group-a`). Tasks have `labels: list[str]` as a JSON column — no schema change needed. Query with `json_each(labels)` in SQLite.

### 2e. MCP tools for affected files

**New file**: `src/gobby/mcp_proxy/tools/tasks/_affected_files.py` (follows `_crud.py` pattern in tasks tools directory)

- `set_affected_files(task_id, files, source)` — for agents to call directly
- `get_affected_files(task_id)` — query
- `find_file_overlaps(task_ids)` — contention detection

Register in `src/gobby/mcp_proxy/tools/tasks/__init__.py` alongside existing task tool registries.

### Verification

- Unit tests for `TaskAffectedFileManager` (CRUD, overlap detection)
- Expand a test task and verify `affected_files` are stored
- Query overlaps between tasks and verify correct pairs returned
- Migration applies cleanly on existing databases

---

## Stage 3: Planning + Dependency Analysis Agents

**Goal**: Replace skill-based expansion with a planning agent that explores the codebase, and add a dependency analysis agent that validates file annotations.

### 3a. Planning agent definition

**New file**: `src/gobby/install/shared/agents/orchestrator-planner.yaml`

- Agent that receives an epic/plan and breaks it into subtasks
- Uses expansion prompt internally as template
- Can explore codebase (`Glob`, `Grep`, `Read` tools) to predict `affected_files`
- Creates task tree via MCP tools (not just LLM output)
- Replaces the current skill-based `expand` flow in the orchestrator

### 3b. Dependency analysis agent definition

**New file**: `src/gobby/install/shared/agents/orchestrator-dependency-analyzer.yaml`

- Receives task tree + file annotations from planning agent
- Explores codebase to validate/refine file predictions
- Creates blocking dependencies via `add_dependency` MCP tool
- Identifies serialization points (formatters, test suites, shared config)
- Detects nuanced cross-file dependencies (imports, shared state)

### 3c. Wire agents into orchestrator pipeline

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

Add two new steps before dispatch loop:
1. `spawn_planner` — planning agent breaks epic into subtasks with file annotations
2. `spawn_analyzer` — dependency agent validates and creates blocking deps

Both are blocking steps (pipeline waits for completion before dispatch).

### Verification

- Spawn planning agent on a test epic — verify subtasks have `affected_files`
- Spawn dependency agent — verify blocking deps are created based on file overlap
- Verify agents call MCP tools directly (not producing JSON for pipeline to apply)

---

## Stage 4: Parallel Dispatch

**Goal**: `suggest_next_tasks` (plural) returns batches of non-conflicting tasks. Orchestrator dispatches multiple agents into the shared worktree simultaneously.

### 4a. `suggest_next_tasks` MCP tool

**File**: `src/gobby/mcp_proxy/tools/task_readiness.py`

- Extract shared scoring logic from existing `suggest_next_task` into helper
- New tool returns batch of non-conflicting ready tasks
- Uses `TaskAffectedFileManager.find_overlapping_tasks()` to filter
- Considers in-progress tasks' file annotations (don't dispatch overlapping work)
- Greedy selection: iterate by score, add task if no file conflict with already-selected set
- Return shape: `{"suggestions": [...], "total_ready": int}`

### 4b. Orchestrator parallel dispatch

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Replace `suggest_next_task` with `suggest_next_tasks`
- Dispatch multiple `spawn_developer` calls per iteration (fire-and-forget)
- Add format step between batches (orchestrator-owned serialization point)
- `max_concurrent` input caps parallel agent count

### 4c. Post-hoc file annotation update

After each task completes, update `task_affected_files` with `annotation_source='observed'` from `git diff`. This improves future predictions and catches unexpected file touches.

### Verification

- Orchestrator dispatches 2+ agents in parallel for non-conflicting tasks
- Conflicting tasks are serialized (not dispatched together)
- Format step runs between batches
- Post-hoc annotations update after task completion

---

## Stage 5: Deterministic TDD Enforcement

**Goal**: Replace prompt-based TDD expansion with deterministic rule enforcement. Agents are blocked from writing implementation code until tests exist, and validation criteria are injected automatically per-file to enforce the outcome.

### Design

Two-layer enforcement:
- **Process layer** (PreToolUse rule): One-shot nudge — blocks the first Write to a new code file, tells the agent to write a test first
- **Outcome layer** (validation criteria): Per-file criteria injected via `mcp_call` to `update_task` — task can't close without tests for each file

### 5a. Add Jinja2 rendering to `mcp_call` arguments

**File**: `src/gobby/workflows/rule_engine.py` (method `_apply_non_block_effect`, ~line 420)

Currently `mcp_call` arguments are passed through raw. Block `reason` and `inject_context` templates are Jinja2-rendered. This is inconsistent. Add rendering for string values in `effect.arguments`:

```python
elif effect.type == "mcp_call":
    # Render string argument values through Jinja2 (consistent with block/inject)
    rendered_args = {}
    for k, v in (effect.arguments or {}).items():
        if isinstance(v, str) and "{{" in v:
            rendered_args[k] = self._render_template(v, ctx, allowed_funcs)
        else:
            rendered_args[k] = v
    mcp_calls.append({
        "server": effect.server,
        "tool": effect.tool,
        "arguments": rendered_args,
        "background": effect.background,
    })
```

~10 lines changed. Enables dynamic argument values in all `mcp_call` effects, not just TDD.

### 5b. `enforce_tdd` variable definition

**File**: `src/gobby/install/shared/variables/gobby-default-variables.yaml`

Add alongside existing variables:

```yaml
  enforce_tdd:
    value: false
    description: "Enable TDD enforcement — block implementation writes until tests exist"
  tdd_nudged_files:
    value: []
    description: "Tracks files that have been TDD-nudged (internal, do not set manually)"
  tdd_tests_written:
    value: []
    description: "Tracks test files written during TDD (internal, do not set manually)"
```

### 5c. Rule: `enforce-tdd-block` (before_tool)

**File**: `src/gobby/install/shared/rules/enforce-tdd-block.yaml`

```yaml
tags: [tdd, enforcement]

rules:
  enforce-tdd-block:
    description: "Block writes to new code files until a test is written first"
    event: before_tool
    enabled: false
    priority: 35
    when: >
      variables.get('enforce_tdd')
      and tool_input.get('file_path', '') not in variables.get('tdd_nudged_files', [])
      and not tool_input.get('file_path', '').endswith(('.yaml', '.yml', '.json', '.toml', '.md', '.txt', '.cfg', '.ini', '.lock'))
      and not tool_input.get('file_path', '').endswith('__init__.py')
      and not tool_input.get('file_path', '').startswith('tests/')
      and 'test_' not in tool_input.get('file_path', '').split('/')[-1]
      and '_test.' not in tool_input.get('file_path', '')
      and '.test.' not in tool_input.get('file_path', '')
      and '_spec.' not in tool_input.get('file_path', '')
      and '.spec.' not in tool_input.get('file_path', '')
    effects:
      - type: set_variable
        variable: tdd_nudged_files
        value: "variables.get('tdd_nudged_files', []) + [tool_input.get('file_path', '')]"
      - type: mcp_call
        server: gobby-tasks
        tool: update_task
        arguments:
          task_id: "{{ claimed_task_id }}"
          validation_criteria: "{{ 'Tests required for:\\n' + (variables.get('tdd_nudged_files', []) | join('\\n')) }}"
      - type: block
        tools: [Write]
        reason: >
          TDD enforcement: write a test for `{{ tool_input.get('file_path', '') }}` before
          writing the implementation. Create a failing test first, then implement to make it pass.
```

**How it works:**
1. Agent tries to Write `src/gobby/foo/bar.py`
2. Rule checks: `enforce_tdd` is true, file is a code file, not a test, not yet nudged
3. Effects fire in order (non-block effects first, block deferred):
   - `set_variable`: adds `src/gobby/foo/bar.py` to `tdd_nudged_files` list (one-shot)
   - `mcp_call`: calls `update_task` with `validation_criteria` rendered from the full `tdd_nudged_files` list (now includes current file). Jinja2 rendering enabled by 5a.
   - `block`: prevents the Write, tells agent to write test first
4. Agent writes `tests/foo/test_bar.py` — rule doesn't fire (test file, excluded by conditions)
5. Agent retries `src/gobby/foo/bar.py` — rule doesn't fire (file already in `tdd_nudged_files`)
6. At close time, `TaskValidator` checks `validation_criteria` which now lists every implementation file that needs tests

**Note on set_variable + mcp_call ordering**: `set_variable` fires before `mcp_call` (both are non-block effects, processed in declaration order). So when the `mcp_call` renders `tdd_nudged_files`, it already includes the current file path.

### 5d. Rule: `enforce-tdd-track-tests` (after_tool)

**File**: `src/gobby/install/shared/rules/enforce-tdd-track-tests.yaml`

```yaml
tags: [tdd, observability]

rules:
  enforce-tdd-track-tests:
    description: "Track test files written for TDD observability"
    event: after_tool
    enabled: false
    priority: 35
    when: >
      variables.get('enforce_tdd')
      and not event.data.get('error')
      and (tool_input.get('file_path', '').startswith('tests/')
           or 'test_' in tool_input.get('file_path', '').split('/')[-1]
           or '_test.' in tool_input.get('file_path', '')
           or '.test.' in tool_input.get('file_path', '')
           or '_spec.' in tool_input.get('file_path', '')
           or '.spec.' in tool_input.get('file_path', ''))
    effect:
      type: set_variable
      variable: tdd_tests_written
      value: "variables.get('tdd_tests_written', []) + [tool_input.get('file_path', '')]"
```

Tracks which test files were written. Useful for observability/metrics — e.g., "agent wrote 3 tests before 5 implementation files."

### Key Design Choices

| Choice | Rationale |
|--------|-----------|
| Write only, not Edit | Write creates new files. Edit modifies existing. TDD targets new code, not modifications |
| One-shot nudge via `tdd_nudged_files` | No complex state machine. Block once, let through after. Real enforcement at validation |
| `mcp_call` with Jinja2 rendering | Deterministic per-file criteria on the task, not prompt-based. Requires 5a (small engine change) |
| Per-file validation_criteria | Each blocked file is listed. Validator has exact file list to check against git diff |
| Config/init files excluded | `__init__.py`, `.yaml`, `.json`, `.md` etc. aren't TDD targets |
| Test detection via filename patterns | `tests/`, `test_*`, `*_test.*`, `*.test.*`, `*_spec.*`, `*.spec.*` — covers Python, JS/TS, Go |
| Default `false` | Opt-in. Not everyone wants TDD enforcement |
| `parallel_group` stored as label | Tasks have `labels: list[str]` (JSON column). Use `parallel:group-name` prefix. No schema change |

### Verification

- Enable `enforce_tdd = true` on a session
- Attempt to Write a new `.py` file — confirm block fires with TDD message
- Confirm `tdd_nudged_files` variable contains the file path
- Confirm `update_task` was called with per-file validation_criteria
- Write a test file — confirm no block, confirm `tdd_tests_written` updated
- Retry the `.py` file — confirm it goes through (already nudged)
- Close task — confirm validation checks the per-file test requirements
- Confirm config/init/`.md` files are not blocked
- Confirm Edit tool is not blocked (existing file modifications)
- Test mcp_call Jinja2 rendering works for other use cases too

---

## Critical Files

| File | Stage | Action |
|------|-------|--------|
| `src/gobby/install/shared/workflows/orchestrator.yaml` | 1, 3, 4 | Restructure pipeline |
| `src/gobby/clones/git.py` | 1 | Add `use_local` param to `create_clone` |
| `src/gobby/mcp_proxy/tools/clones.py` | 1 | Wire `use_local` to MCP tool |
| `src/gobby/agents/isolation.py` | 1 | Auto-detect unpushed commits in `CloneIsolationHandler` |
| `src/gobby/storage/migrations.py` | 2 | Add migration 134 |
| `src/gobby/storage/task_affected_files.py` | 2 | New manager (follows `task_dependencies.py` pattern) |
| `src/gobby/tasks/prompts/expand-task.md` | 2 | Add `affected_files` + `parallel_group` fields |
| `src/gobby/tasks/tree_builder.py` | 2 | Wire `affected_files` into `_create_node` |
| `src/gobby/mcp_proxy/tools/tasks/_affected_files.py` | 2 | New MCP tools for affected files |
| `src/gobby/mcp_proxy/tools/task_readiness.py` | 4 | `suggest_next_tasks` (plural) |
| `src/gobby/install/shared/agents/orchestrator-planner.yaml` | 3 | New agent definition |
| `src/gobby/install/shared/agents/orchestrator-dependency-analyzer.yaml` | 3 | New agent definition |
| `src/gobby/workflows/rule_engine.py` | 5 | Add Jinja2 rendering to `mcp_call` arguments |
| `src/gobby/install/shared/variables/gobby-default-variables.yaml` | 5 | Add `enforce_tdd` + tracking variables |
| `src/gobby/install/shared/rules/enforce-tdd-block.yaml` | 5 | New rule template |
| `src/gobby/install/shared/rules/enforce-tdd-track-tests.yaml` | 5 | New rule template |

## Existing Code to Reuse

| What | Where | How |
|------|-------|-----|
| `TaskDependencyManager` pattern | `src/gobby/storage/task_dependencies.py` | Mirror for `TaskAffectedFileManager` |
| `suggest_next_task` scoring | `src/gobby/mcp_proxy/tools/task_readiness.py` | Extract into shared helper |
| `TaskTreeBuilder._create_node` | `src/gobby/tasks/tree_builder.py` | Add `affected_files` read after task creation |
| Worktree reuse in `spawn_agent` | `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` | Already works — just pass `worktree_id` |
| `WorktreeGitManager.create_worktree` | `src/gobby/worktrees/git.py` | `use_local` fully implemented |
| Migration pattern | `src/gobby/storage/migrations.py` | Add numbered migration + update baseline |
| Rule template patterns | `src/gobby/install/shared/rules/require-task-before-edit.yaml` | PreToolUse block + `tool_input.get('file_path')` pattern |
| Expression-based set_variable | `src/gobby/install/shared/rules/track-listed-servers.yaml` | List append via `variables.get(..., []) + [...]` |

## Implementation Order

Stage 1 first — immediate value, fixes real bugs, achieves industry parity. Stage 5 (TDD enforcement) is independent of Stages 2-4 and can be built in parallel. Stages 2-4 are sequential (each builds on the previous). All stages are independently shippable.

# Canonical Pipeline Runs Rename with Pagination

## Overview

Pipelines is the only template-with-instance surface in gobby with split-brain naming — Python, SQL, HTTP, MCP, CLI, web, YAML, and installed skills all use *execution* while every other instance noun (agents *runs*, etc.) externalizes as *runs*. This plan canonicalizes the pipeline noun to `run` end-to-end, including storage tables and columns, and adds filtered pagination (`limit`/`offset`/`total_count`) on the list/search surfaces. Single atomic PR; no alias/dual-write — intermediate states would break consumers, so a four-PR split is unsafe.

Implementation is parented to planning epic **#12902**. After approval, `/gobby expand` produces the implementation tree.

## Constraints

- **Single PR, atomic.** All phases land together. Per-task `validation_criteria` must scope narrowly to named file/diff invariants — not *full test suite green* — because intermediate commits will not pass cross-surface tests until later phases land. The PR as a whole must pass CI.
- **No aliases.** Old route paths, MCP tool names, response keys, CLI commands, table/column names, and Python identifiers are removed, not aliased.
- **New IDs use `pr-` prefix** via the existing `generate_prefixed_id('pr')` mechanism (`src/gobby/utils/id.py:7`). The `pr-` prefix is unused today (verified). Existing rows in `pipeline_runs` after migration retain their original `pe-…` IDs as opaque legacy values; lookups by ID accept both prefixes (string equality, no parsing required).
- **Storage rename overrides prior memory.** Memory `e02f85fb` previously said *do not rename storage tables such as `pipeline_executions`*; that rule has been updated to apply only to `agent_runs` (already coherent) and explicitly carves out pipelines for full top-to-bottom rename.
- **Migration 221** is the next slot. Current `BASELINE_VERSION = 220` (`src/gobby/storage/_migration_registry.py:59`).
- **No file >1,000 lines.** Pre-existing over-threshold files this PR will touch (all deferred under #12730, out of scope; touch only rename-required lines):
  - `tests/cli/test_pipelines.py` — 1,239 lines (#12896 covers split)
  - `tests/mcp_proxy/tools/test_pipelines.py` — 1,299 lines (#12766 covers split)
  - `web/src/components/workflows/ReportsPage.tsx` — 1,434 lines (#12920 covers split)
  - `tests/workflows/test_stop_gates_rules.py` — 1,384 lines (#12921 covers split)
  - `docs/guides/cli-commands.md` — 1,423 lines (#12947 covers split) — touched by §6.3
  Post-PR, `src/gobby/workflows/pipeline_executor.py` (~963) and `src/gobby/storage/pipelines.py` (~780) must stay under 1,000 — extract a focused module if pagination/rename churn pushes either over.
- **MCP test surface**: 3 files. `tests/mcp_proxy/tools/test_pipelines.py` (1,299) — runtime/list/search/get suite. `tests/mcp_proxy/tools/workflows/test_pipelines.py` (433) — workflow-tooling-specific. `tests/mcp_proxy/tools/test_pipeline_query.py` (196) — `_pipeline_query.py` helpers. Rename touches all three.
- **Drift sweep** is a real test: assert no live user-facing pipeline-execution terms remain in routes, MCP names, response keys, CLI/web labels, installed YAML, skills, active guides, **runtime instruction strings (`src/gobby/conductor/manager.py`, `src/gobby/mcp_proxy/stdio.py`)**, **WebSocket broadcast emission**, and **cron storage**. Allowlist: migration code, archived docs under `docs/plans/completed/`, `PipelineExecutor.execute` implementation verbs, and unrelated non-pipeline `execution` contexts.
- **Per-file enumeration is non-authoritative.** Where the plan lists files (e.g., §1.3 caller sweep, §2.7 service-container wiring), treat the list as a hint of likely sites, not the contract. The implementing agent's authoritative target list comes from `gcode search "<canonical-symbol>"` (FTS+semantic) cross-checked with `rg -l "<canonical-symbol>" src/gobby/ tests/ web/src/` — every result that is not allowlisted must be renamed. Validation gates assert `gcode search` (or `rg`) returns zero hits for the old symbol set after the rename. Specifically: `LocalPipelineExecutionManager`, `PipelineExecutionManager`, `pipeline_execution_manager`, `execution_manager`, `_get_execution_manager`, `execution_manager_getter`, `PipelineExecution`, `StepExecution`, `ExecutionStatus`, `parent_execution_id`, `pipeline_execution_id`, `step_execution_id`, and `fail_stale_running_executions`.

---

## Phase 1: Storage Foundation

**Goal**: Rename storage tables/columns/indexes via migration 221, baseline schema, Python models, manager, ID prefix; add offset + filtered total_count pagination plumbing.

### 1.1 Add migration 221: pipeline runs schema + definition_json rewrites [category: code]

Target: `src/gobby/storage/_migration_registry.py`, new migration callable (mirror existing migration registration patterns in that file)

Implement migration v221 that performs schema rename, index recreation, and data rewrite of stored workflow definitions.

**Schema operations** (idempotent — guard each ALTER with a *table/column exists* check by querying `sqlite_master` first; SQLite raises if names already match):

```sql
-- Tables
ALTER TABLE pipeline_executions RENAME TO pipeline_runs;
ALTER TABLE step_executions      RENAME TO pipeline_step_runs;

-- Columns
ALTER TABLE pipeline_runs       RENAME COLUMN parent_execution_id TO parent_run_id;
ALTER TABLE pipeline_step_runs  RENAME COLUMN execution_id        TO run_id;
ALTER TABLE cron_runs           RENAME COLUMN pipeline_execution_id TO pipeline_run_id;
```

**Indexes**: enumerate the explicit drop set against the actual baseline (lines 683-708). Sweeping by `tbl_name` over-drops unrelated `cron_runs` indexes; sweeping by autoindex hits constraint-backed `sqlite_autoindex_*` rows that can't be dropped (`DROP INDEX sqlite_autoindex_*` raises). Current state:

- **`cron_runs`**: `idx_cron_runs_job`, `idx_cron_runs_triggered`, `idx_cron_runs_status` — **preserve verbatim**, none reference `pipeline_execution_id`. There is **no** `idx_cron_runs_pipeline_execution_id` in baseline; do not introduce `idx_cron_runs_pipeline_run_id` either (the column rename alone does not justify a new index).
- **`step_executions`**: explicit `idx_step_executions_*` set drops; the `sqlite_autoindex_*` backing `UNIQUE(execution_id, step_id)` survives — SQLite's `RENAME COLUMN` updates it in place.
- **`pipeline_executions`**: drop and recreate `idx_pipeline_executions_project`, `idx_pipeline_executions_status`, `idx_pipeline_executions_resume_token`, `idx_pe_status_updated`, `idx_pe_status_project_updated`.

**Drop-and-recreate pattern**: use `SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND name IN (<explicit list>)` for the drop step (the `sql IS NOT NULL` filter is defense-in-depth). Then recreate with run-named identifiers, same `(table, columns)` shape:

- `idx_pipeline_runs_project` on `pipeline_runs(project_id)` *(replaces `idx_pipeline_executions_project`)*
- `idx_pipeline_runs_status` on `pipeline_runs(status)` *(replaces `idx_pipeline_executions_status`)*
- `idx_pipeline_runs_resume_token` on `pipeline_runs(resume_token)` *(replaces `idx_pipeline_executions_resume_token`)*
- `idx_pipeline_runs_status_updated` on `pipeline_runs(status, updated_at)` *(replaces `idx_pe_status_updated`)*
- `idx_pipeline_runs_status_project_updated` on `pipeline_runs(status, project_id, updated_at)` *(replaces `idx_pe_status_project_updated`)*
- `idx_pipeline_step_runs_*` for every `idx_step_executions_*` that exists in baseline (audit `baseline_schema.sql` for the exact set; the rename preserves shape)
- *(no new index on `cron_runs(pipeline_run_id)` — see above)*

Drop and recreate set must match v220 baseline modulo the rename — verify by diffing `sqlite_master` rows pre/post-migration; names must match the new baseline (§1.3).

**Data rewrite for `workflow_definitions.definition_json`**: this column stores serialized YAML-derived workflow definitions (`src/gobby/storage/workflow_definitions.py:38`). Live rows reference old MCP tool names. Walk each row, parse JSON, recursively rewrite:

| Old | New |
|---|---|
| `get_pipeline_status` | `get_pipeline_run` |
| `list_pipeline_executions` | `list_pipeline_runs` |
| `search_pipeline_executions` | `search_pipeline_runs` |
| any string equal to `executions` (case-sensitive) used as a path segment in `output.<key>` references | `runs` |
| any output key literally `executions` | `runs` |
| `execution_id` (when used as a step input/output key referring to pipeline runs) | `run_id` |
| `parent_execution_id` (when used as a step input/output key) | `parent_run_id` |
| **argument key `pipeline_name`** (only when the surrounding step has `tool` ∈ {`list_pipeline_executions`, `list_pipeline_runs`, `search_pipeline_executions`, `search_pipeline_runs`}) | **`name`** |

Use a recursive walker that descends dicts and lists. Be conservative:

- Rewrite string values only when the surrounding key is `tool` (MCP tool name) or the value matches the exact literals above; do **not** mass-substitute the substring `execution` in arbitrary text content (e.g., user-authored descriptions).
- For path-style strings like `${steps.foo.output.executions}`, rewrite the `.executions` segment specifically.
- For the **argument key `pipeline_name` → `name` rule**, rewrite only when the walker is currently inside an `arguments` dict whose sibling-key `tool` resolves (after this same walker pass) to one of the renamed list/search tool names. Do **not** rename `pipeline_name` keys in unrelated contexts — e.g., stored run records (`pipeline_runs.pipeline_name` column reads via the dataclass), output records, or user-authored description strings.

**Data rewrite for `inter_session_messages.metadata_json`**: this column stores cross-session pending-message metadata that is keyed for completion-notification dedupe lookups (`src/gobby/storage/inter_session_messages.py:225-227` joins on `$.completion_id | $.run_id | $.execution_id`). Live in-flight rows may carry `metadata_json = {"execution_id": "pe-...", ...}` for completion notifications generated before the upgrade. Walk each row, parse the JSON, and rename any top-level key `execution_id` → `run_id` (preserve the value verbatim — `pe-…` IDs remain opaque legacy values per the constraints). This is the prerequisite for §2.6's SQL OR-branch removal: after the migration, no live row can have only `execution_id` in its metadata, so the lookup can drop the fallback safely.

**Data rewrite for installed `workflow_definitions` prose** (install-time-sync gap fix): bundled-content sync is install-time only (`src/gobby/cli/installers/shared.py`); YAML version bumps in §6.1 don't propagate to existing rows on user restart. Migration 221 updates rows in place. **Bundled-row predicate** (verified against `workflow_definitions.py` + `sync_pipelines.py:138` + `sync_rules.py:124`): `source = 'installed' AND project_id IS NULL AND tags LIKE '%"gobby"%'` — the `source` enum is `installed | agent | project | custom` (no `bundled`); bundled-sync creates rows with `source='installed'`, no project, `gobby` tag in JSON-encoded `tags`. For every matching row, apply §6.1's prose substitutions to JSON string values: `pipeline execution worker` → `pipeline run worker`; `Pipeline Execution Mode` → `Pipeline Run Mode`; `pipeline execution agent` → `pipeline run agent`; `Review completed pipeline executions` → `Review completed pipeline runs`; `Pipeline Execution` heading → `Pipeline Run`; `<name> execution is already running` → `<name> run is already in progress`; `(?i)\bpipeline executions?\b` → `pipeline run(s)`. Walker scoped to predicate so `source='custom'` or non-`gobby`-tagged rows untouched. Validation: seed v220 with a predicate-matching row carrying old prose, migrate, assert `definition_json` matches none of the old patterns above and contains the new noun.

**Data rewrite for installed `skills` rows** (same install-time-sync gap for gobby-owned bundled skills — `sync_bundled_skills` runs at install time only). Migration 221 also updates `skills`. **Predicate** (verified against `src/gobby/skills/sync.py:77-79,121` — `is_gobby_owned()` checks `bool(skill.metadata and "gobby" in skill.metadata)`, sync writes `source="installed"`): `source = 'installed' AND project_id IS NULL AND metadata LIKE '%"gobby"%'`. For every matching row, rewrite `content` applying: tool-name renames per the table above; CLI examples (`gobby pipelines history` → `gobby pipelines runs list --name <NAME>`; `status <ID>` → `runs show <ID>`; `list-runs` → `runs list`; `search` → `runs search`); URL examples (`/api/pipelines/executions` → `/api/pipelines/runs`); `execution_id` → `run_id` only inside backticks/code fences (not in prose); `(?i)\bpipeline executions?\b` → `pipeline run(s)`. Validation: seed v220 with a `metadata={"gobby":{"audience":"all"}}` skill row whose `content` contains the literal old strings; migrate; assert the row no longer contains any old literal and contains the new ones; assert a non-gobby skill row (no `gobby` metadata key) is bit-for-bit unchanged.

**Registration**: append to the migration list in `_migration_registry.py` after the current head; bump the registry's notion of head to 221.

**Idempotency**: re-running the migration on an already-migrated DB must be a no-op. Guard each ALTER and INDEX op.

`validation_criteria`: Migration 221 is registered in `_migration_registry.py`. Running on a fresh v220 DB transitions all five renames (2 tables, 3 columns, indexes) and rewrites `workflow_definitions.definition_json` for the seven literal substitutions above. The `sqlite_autoindex_*` row backing the `UNIQUE(execution_id, step_id)` constraint on `step_executions` survives the migration (becomes `(run_id, step_id)` autoindex on `pipeline_step_runs`); migration test asserts no `DROP INDEX sqlite_autoindex_*` is attempted. Re-running on a v221 DB is a no-op. Verified by `tests/storage/test_migrations.py` cases for v220→v221, including a case that seeds the UNIQUE-constraint autoindex and asserts the migration completes without error.

### 1.2 Update `baseline_schema.sql` to v221 + bump `BASELINE_VERSION` [category: refactor]

Target: `src/gobby/storage/baseline_schema.sql`, `src/gobby/storage/_migration_registry.py:59`

Rewrite `baseline_schema.sql` so a fresh DB built from the baseline matches the post-migration state of an upgraded DB:

- Rename `CREATE TABLE pipeline_executions` → `pipeline_runs`; rename `parent_execution_id` column → `parent_run_id`.
- Rename `CREATE TABLE step_executions` → `pipeline_step_runs`; rename `execution_id` column → `run_id`.
- In `CREATE TABLE cron_runs`, rename `pipeline_execution_id` column → `pipeline_run_id`.
- Replace every `CREATE INDEX` referencing the old names with the new identifiers (matching the list in 1.1 exactly). Specifically: rename the existing `idx_pe_status_updated` (line 707) → `idx_pipeline_runs_status_updated` and `idx_pe_status_project_updated` (line 708) → `idx_pipeline_runs_status_project_updated`. Audit the full index block for any other `idx_pe_*` / `idx_pipeline_executions_*` / `idx_step_executions_*` lines and rename consistently.
- Update foreign-key clauses: `REFERENCES pipeline_executions(id)` → `REFERENCES pipeline_runs(id)`.
- Update any trigger or check constraint referencing the old names.

Bump `BASELINE_VERSION = 220` to `BASELINE_VERSION = 221` in `_migration_registry.py:59`.

`validation_criteria`: `baseline_schema.sql` contains no occurrences of `pipeline_executions`, `step_executions`, `parent_execution_id`, `pipeline_execution_id`, or any `idx_pe_` index name. `BASELINE_VERSION = 221`. A fresh DB built from baseline_schema.sql matches the post-migration state of a v220 DB run through migration 221, verified by a test that initializes both paths and diffs `sqlite_master` (the diff must include the `idx_pe_*` → `idx_pipeline_runs_*` index renames, not just table/column renames).

### 1.3 Rename storage models, manager, ID prefix [category: refactor]

Target: `src/gobby/workflows/pipeline_state.py`, `src/gobby/storage/pipelines.py`, `src/gobby/storage/cron_models.py`, all callers (tracked by mypy/pyright failures)

**`src/gobby/workflows/pipeline_state.py`**:

- `class PipelineExecution` (lines 53–117) → `class PipelineRun`. Field `parent_execution_id` (line ~68) → `parent_run_id`. Update `from_row()` to read the new `parent_run_id` column; update `to_dict()` to emit `parent_run_id`.
- `class StepExecution` (lines 121–179) → `class PipelineStepRun`. Field `execution_id` (line ~125) → `run_id`. Update `from_row()` and `to_dict()`.
- `class ExecutionStatus` (lines 15–37) enum → `class PipelineRunStatus`. Enum members unchanged (PENDING, RUNNING, WAITING_APPROVAL, COMPLETED, FAILED, CANCELLED, INTERRUPTED).
- `class StepStatus` (lines 40–51) — leave unchanged; it describes step-level state, not pipeline-level. (Spec lists `PipelineRun`, `PipelineStepRun`, `PipelineRunStatus` — three types, not four.)
- **`class ApprovalRequired(Exception)` (lines 182–202)**: rename the constructor parameter `execution_id: str` (line 192) → `run_id: str`. Rename the stored attribute `self.execution_id = execution_id` (line 197) → `self.run_id = run_id`. Update the f-string at line 202 (`f"Approval required for step '{step_id}' in execution '{execution_id}': {message}"`) to `f"Approval required for step '{step_id}' in run '{run_id}': {message}"`. This exception is raised by the gatekeeper and read by HTTP/CLI/MCP approval-required handlers; the raise-site rename is owned by 2.1b and the handler-reads are owned by 3.1, 3.2, and 4.1 (each handler that accesses `e.execution_id` becomes `e.run_id` as part of those phases' caller sweeps).

**`src/gobby/storage/pipelines.py`**:

- `class LocalPipelineExecutionManager` (lines 21–780) → `class LocalPipelineRunManager`.
- All SQL: `pipeline_executions` → `pipeline_runs`; `step_executions` → `pipeline_step_runs`; column `execution_id` → `run_id` (in step queries); `parent_execution_id` → `parent_run_id`.
- Method renames:
  - `create_execution` → `create_run`
  - `get_execution` → `get_run`
  - `update_execution_status` → `update_run_status`
  - `list_executions` → `list_runs`
  - `search_executions` → `search_runs`
  - `count_by_status` — unchanged name (semantic is generic) but body queries `pipeline_runs`
  - `get_steps_for_execution` → `get_steps_for_run`
  - `get_steps_for_executions` → `get_steps_for_runs`
  - `create_step_execution` → `create_step_run`
  - `update_step_execution` → `update_step_run`
  - `get_step_by_approval_token` — unchanged name; body queries `pipeline_step_runs`
  - `get_execution_by_resume_token` → `get_run_by_resume_token`; `get_stalled_executions` → `get_stalled_runs`. Update all callers (storage tests in `tests/storage/test_pipeline_storage.py`, HTTP/MCP resume-token lookup, and `pipeline_heartbeat.py` per §2.4).
  - `interrupt_stale_running_executions` (line ~507) → `interrupt_stale_running_runs`
  - **`fail_stale_running_executions` (line ~578) — DELETE**. This method is currently a backwards-compatible alias that just calls `interrupt_stale_running_executions(exclude_ids=...)` (line 580 body). The no-alias rule applies; remove the wrapper entirely. All callers (`runner_lifecycle.py:592` and any docstring references in `mcp_proxy/tools/workflows/_pipeline_execution.py:541`) must call `interrupt_stale_running_runs` directly. Update §2.5's `runner_lifecycle.py` bullet to call `interrupt_stale_running_runs`, not `fail_stale_running_runs` — there is no `fail_*` canonical method in the renamed surface.
  - **`resolve_execution_reference` (line ~333) → `resolve_run_reference`**. Update all callers; this is a public method on the manager that resolves opaque references to canonical run IDs.
  - `reset_steps_from` — unchanged name; body queries `pipeline_step_runs`
  - `get_unreviewed_completions` — unchanged name; body queries `pipeline_runs`
- Parameter renames: `execution_id` → `run_id` everywhere; `parent_execution_id` → `parent_run_id`.
- ID generation: change `generate_prefixed_id('pe')` (line ~48) → `generate_prefixed_id('pr')`. Comment the line: existing rows keep `pe-…` IDs, only newly-created rows get `pr-…`.
- Manager attribute on `LocalDatabase` (likely `self.pipelines: LocalPipelineExecutionManager`) → `self.pipelines: LocalPipelineRunManager` (verify via grep on `LocalPipelineExecutionManager`). Update all callers.

**`src/gobby/storage/cron_models.py`**:

- `class CronRun` field `pipeline_execution_id` (line ~123) → `pipeline_run_id`. Update `from_row`/`to_dict`.

**Caller sweep**: this rename will produce ~40+ caller errors across `workflows/`, `cli/`, `mcp_proxy/tools/`, `servers/routes/`, `conductor/`, `scheduler/`. Resolve mechanically — every `execution_id` parameter on pipeline-run-related callsites becomes `run_id`; every `parent_execution_id` becomes `parent_run_id`; class names update accordingly. Phase 2 and Phase 3 own the runtime/API caller code in detail; this task only renames the storage symbols and updates **direct** storage callers (i.e., anything that imports from `src/gobby/storage/pipelines.py` or `src/gobby/workflows/pipeline_state.py`). Leave caller renames in `mcp_proxy/`, `servers/routes/`, `cli/` to their respective phases.

`validation_criteria`: Within the **direct-target files** owned by this task — `src/gobby/workflows/pipeline_state.py`, `src/gobby/storage/pipelines.py`, `src/gobby/storage/cron_models.py`, and any `LocalDatabase`-attribute mounting site identified by the `LocalPipelineExecutionManager` grep — the symbols `PipelineExecution`, `StepExecution`, `ExecutionStatus`, `LocalPipelineExecutionManager`, `parent_execution_id`, and storage-level `execution_id` fields are renamed. The ID prefix `pe-` is removed from `generate_prefixed_id` calls in `storage/pipelines.py`. Storage tests in `tests/storage/test_pipeline_storage.py` pass against the renamed manager and models. **Repo-wide absence of those symbols is intentionally NOT a 1.3 validation gate** — caller renames in `mcp_proxy/`, `servers/routes/`, `servers/websocket/`, `cli/`, `conductor/`, `workflows/pipeline/`, and the runner-lifecycle modules are owned by Phases 2–4 and 6, and the cross-tree absence is enforced by Phase 7's drift sweep after all owning tasks have run. The `ApprovalRequired` exception rename is covered below in this same task.

### 1.4 Add `offset` + filtered `total_count` to storage list/search [category: code]

Target: `src/gobby/storage/pipelines.py` (`list_runs`, `search_runs`)

Add pagination parameters and filtered count methods so the list/search responses can echo `total_count` matching the **same filter set** as the page (not a project-wide total).

**`list_runs` signature change**:

```python
def list_runs(
    self,
    *,
    status: PipelineRunStatus | None = None,
    pipeline_name: str | None = None,
    project_id: str | None = None,
    parent_run_id: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PipelineRun]:
```

Add `OFFSET ?` to the underlying SQL after `LIMIT ?`. Validate `offset >= 0` and `limit > 0` (raise ValueError on bad input).

**New `count_runs` method** that applies the same filter predicates as `list_runs` but returns an integer count (no LIMIT/OFFSET):

```python
def count_runs(
    self,
    *,
    status: PipelineRunStatus | None = None,
    pipeline_name: str | None = None,
    project_id: str | None = None,
    parent_run_id: str | None = None,
    session_id: str | None = None,
) -> int:
```

Implementation: build the same WHERE clause as `list_runs`, run `SELECT COUNT(*) FROM pipeline_runs WHERE ...`. Extract the WHERE-clause builder into a private helper `_build_runs_filter(...)` returning `(sql_fragment, params)` so both methods share it — prevents drift.

**`search_runs` signature change**:

```python
def search_runs(
    self,
    *,
    query: str,
    search_errors: bool = True,
    search_outputs: bool = False,
    status: PipelineRunStatus | None = None,
    project_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[PipelineRun]:
```

Add `OFFSET ?` to the SQL.

**New `count_search_runs` method**:

```python
def count_search_runs(
    self,
    *,
    query: str,
    search_errors: bool = True,
    search_outputs: bool = False,
    status: PipelineRunStatus | None = None,
    project_id: str | None = None,
) -> int:
```

Same approach: extract `_build_search_runs_filter(...)` helper, share between `search_runs` and `count_search_runs`.

**Status summary** (for HTTP/MCP response shape in Phase 3): add a method `status_summary_for_runs(filter_kwargs)` that returns a `dict[PipelineRunStatus, int]` of run counts grouped by status under the same filter set. Implementation: `SELECT status, COUNT(*) FROM pipeline_runs WHERE <same filter without status> GROUP BY status`. The status filter is dropped when computing the summary so the UI can show all status buckets at once.

**No-op for Phase 1**: `list_runs(limit=50, offset=0)` and `search_runs(...)` return identical results to today's `list_executions` / `search_executions` when offset is 0 — backward-compatible call shape (modulo the `_executions` → `_runs` rename in 1.3).

`validation_criteria`: `list_runs` and `search_runs` accept `offset: int = 0`. `count_runs` and `count_search_runs` exist and return filter-scoped totals. `status_summary_for_runs` returns a dict keyed by `PipelineRunStatus`. Helper `_build_runs_filter` is shared between list and count to prevent drift. Tests in `tests/storage/test_pipeline_storage.py` cover offset progression (offset=0, 10, 20 → distinct contiguous pages) and filtered count matching list length when `limit` exceeds the filtered set.

---

## Phase 2: Pipeline Runtime (depends: Phase 1)

**Goal**: Rename runtime internals — executor, approvals, resume/cancel, nested-pipeline parent linkage, webhooks, WebSocket payloads, completion notifications, wake metadata, conductor review, pipeline heartbeat, telemetry, scheduler.

### 2.1 Rename `PipelineExecutor` internals + nested parent linkage [category: refactor] (depends: 1.3)

Target: `src/gobby/workflows/pipeline_executor.py`

- `class PipelineExecutor` — keep the **class name** unchanged. The verb `execute` is implementation terminology, not the public noun. Spec explicitly carves out `PipelineExecutor.execute`.
- **`PipelineExecutor.__init__`** (line 82): constructor parameter `execution_manager: LocalPipelineExecutionManager` → `run_manager: LocalPipelineRunManager`. Update the docstring at line 96 (`execution_manager: Manager for pipeline execution records` → `run_manager: Manager for pipeline run records`). Update the attribute assignment at line 108 (`self.execution_manager = execution_manager` → `self.run_manager = run_manager`). Update the `ApprovalManager(...)` constructor call at line 118 — pass `run_manager=run_manager` (the `ApprovalManager` constructor parameter is renamed to `run_manager` in §2.1b). Audit the rest of `__init__` for any other `execution_manager` references and rename. Update every `self.execution_manager` reference throughout `pipeline_executor.py` to `self.run_manager`.
- `def execute(...)` (line ~211): keep the method name. Parameter `execution_id: str | None = None` (used for resume) → `run_id: str | None = None`. Local variable `execution` (the `PipelineRun` instance, throughout the function body) → `run`. Returns `PipelineRun`.
- `def approve(self, *, token, approved_by) -> ...` (line ~787): only the inner SQL/state references rename; `token` parameter unchanged.
- `def reject(...)` (line ~830): same.
- `def _execute_nested_pipeline(...)` (line ~838): all `parent_execution_id` references → `parent_run_id`. Local `nested_execution: PipelineRun` → `nested_run`.
- `def _emit_event(self, event, execution_id, ...)` (line ~124): rename the parameter to `run_id`. Update the local dict key passed downstream: `execution_id` → `run_id`. **This is necessary but NOT sufficient** — see 2.1a, the broadcast/runner-broadcasting/app-context surfaces also serialize the field independently.
- `def _notify_completion(self, execution, ...)` (line ~142): parameter rename `execution` → `run`. Webhook metadata dict key `execution_id` → `run_id` (full webhook payload rename happens in 2.2).
- `def resume_interrupted_pipelines(...)` (line ~530): renames `execution`/`execution_id` locals to `run`/`run_id`.

`validation_criteria`: Inside `pipeline_executor.py`, no parameter, attribute, or local variable is named `execution_id`, `parent_execution_id`, `execution_manager`, or `LocalPipelineExecutionManager`. Class name `PipelineExecutor` is preserved. Method name `execute` is preserved. The constructor takes `run_manager: LocalPipelineRunManager` and stores `self.run_manager`. The downstream `ApprovalManager(...)` call passes `run_manager=run_manager`. The local emit-event call passes `run_id` to the broadcaster. `tests/workflows/test_pipeline_executor_core.py` and `test_pipeline_executor_errors.py` pass after their fixtures and assertions are updated to the new names (assert against `run_id` and the renamed constructor kwarg, fixtures named accordingly).

### 2.1a WebSocket broadcast + runner-broadcasting + app-context payload rename [category: refactor] (depends: 1.3)

Target: `src/gobby/app_context.py`, `src/gobby/runner_broadcasting.py`, `src/gobby/servers/websocket/broadcast.py`

The pipeline-run event chain is wired through three additional surfaces that each independently shape the wire payload. Renaming `_emit_event` in `pipeline_executor.py` alone leaves the field as `execution_id` on the wire because these surfaces re-serialize. All three must rename together.

For each file:

- Identify the broadcast-callback signature (the param that today is named `execution_id`) and rename to `run_id`.
- Identify any dict-literal or model.dict() output that emits `execution_id` as a JSON key on the WebSocket payload and rename to `run_id`.
- Update internal type names if any local `PipelineExecution`-typed parameter or variable references remain (likely; verify via gcode `outline`).
- Bump the inline payload-shape comment if one exists so future contributors see the contract.

`tests/servers/websocket/test_broadcast.py` (or the closest existing module — confirm via gcode `search "broadcast"` under `tests/servers/`) must add a regression test that asserts the WebSocket frame received by a subscriber contains `run_id` (not `execution_id`) when a pipeline-run event is broadcast end-to-end.

`validation_criteria`: ripgrep `\bexecution_id\b` across `src/gobby/app_context.py`, `src/gobby/runner_broadcasting.py`, and `src/gobby/servers/websocket/broadcast.py` returns zero matches. The end-to-end WebSocket test asserts `run_id` reaches subscribers. No `PipelineExecution` type reference remains in those three files.

### 2.1b Rename `ApprovalManager` approval-state internals [category: refactor] (depends: 1.3)

Target: `src/gobby/workflows/pipeline/gatekeeper.py`

This file defines `class ApprovalManager` (imported by `pipeline_executor.py:13` as `from gobby.workflows.pipeline.gatekeeper import ApprovalManager`). The class owns approval-state transitions and emits `pipeline_approval_*` events through a callback-based pattern parallel to `PipelineExecutor._emit_event`. It is a peer of 2.1, not subordinate to it: it has its own `_emit_event` (line 38), its own log-extra dict, and its own SQL/manager calls. Today it references `execution_id` in 18 distinct places. **The class name `ApprovalManager` is preserved** — the verb `approve` is implementation terminology, not the public noun, just like `PipelineExecutor.execute`.

- `def _emit_event(self, event: str, execution_id: str, **kwargs) -> None` (line 38) — rename param `execution_id` → `run_id`. Update the inner `event_callback(event, execution_id, **kwargs)` call (line 42) to pass `run_id`. Update the structured-log `extra={"event": event, "execution_id": execution_id}` (line 46) → `extra={"event": event, "run_id": run_id}`.
- **`ApprovalManager.__init__`**: constructor parameter `execution_manager: LocalPipelineExecutionManager` → `run_manager: LocalPipelineRunManager`. The attribute assignment (likely `self.execution_manager = execution_manager`) → `self.run_manager = run_manager`. This is the parameter that `PipelineExecutor.__init__` (§2.1, line 118) passes; both ends rename together.
- `self.execution_manager.update_step_execution(...)` calls (line 87 and downstream) become `self.run_manager.update_step_run(...)`. Update every internal call site.
- Parameters named `step_execution_id` (e.g., line 88) — rename to `step_run_id`.
- All remaining `execution_id` references in this file (parameter names, dict keys in approval state objects, SQL parameter names, log fields, comments) — rename to `run_id`.
- Rename any local variable typed `StepExecution` to a `PipelineStepRun` per the type rename in 1.3.
- Audit the rest of the file for any `PipelineExecution`, `StepExecution`, `ExecutionStatus`, `LocalPipelineExecutionManager` import or annotation and rename to the new names.
- **`ApprovalRequired` raise site**: every `raise ApprovalRequired(execution_id=..., ...)` in this file becomes `raise ApprovalRequired(run_id=..., ...)`. The exception class itself is renamed in 1.3.

**Tests**: `tests/workflows/pipeline/test_gatekeeper.py` (or the closest gatekeeper test module — confirm via gcode `search "test_gatekeeper"`) — update assertions on `_emit_event` payload (assert `run_id` in event dict), update fixtures that construct approval state, update `step_execution_id` keyword args, update fixtures that pass `execution_manager=` to `ApprovalManager(...)` to use `run_manager=`.

`validation_criteria`: ripgrep `\bexecution_id\b|\bstep_execution_id\b|\bexecution_manager\b|\bLocalPipelineExecutionManager\b` across `src/gobby/workflows/pipeline/gatekeeper.py` returns zero matches. `ApprovalManager.__init__` accepts `run_manager: LocalPipelineRunManager`. The gatekeeper's emit-event passes `run_id` to its callback. Existing gatekeeper tests pass against the renamed surface.

### 2.2 Rename webhook payloads [category: refactor] (depends: 2.1)

Target: `src/gobby/workflows/pipeline_webhooks.py`

`class WebhookNotifier`:

- `notify_approval_pending(...)` (line ~41): payload dict key `execution_id` → `run_id`. Keep `step_id`, `token`, `approve_url`, `reject_url`. Emit `run_id` ONLY (no dual key).
- `notify_complete(...)` (line ~76): payload `execution_id` → `run_id`. Keep `outputs`, `completed_at`.
- `notify_failure(...)` (line ~111): payload `execution_id` → `run_id`. Keep `error`.

Caller updates: any place that constructs a webhook payload manually (search for the literal `execution_id:` in the codebase outside this file).

Update tests in `tests/workflows/test_pipeline_webhooks.py` (324 lines) and `tests/workflows/test_webhook_executor.py` (467 lines): assertions on payload keys must read `run_id`. Also update the `mock_execution()` fixture in `test_pipeline_webhooks.py:21` to `mock_run()` returning a `PipelineRun`.

`validation_criteria`: All webhook payloads emit `run_id` (no `execution_id`). `tests/workflows/test_pipeline_webhooks.py` and `tests/workflows/test_webhook_executor.py` pass with renamed assertions. No remaining `execution_id` string literal in `pipeline_webhooks.py`.

### 2.3 Rename cron + scheduler linkage [category: refactor] (depends: 1.3)

Target: `src/gobby/storage/cron_models.py`, `src/gobby/storage/cron.py`, `src/gobby/scheduler/executor.py`, anywhere `CronRun.pipeline_execution_id` is referenced

- `cron_models.py` model field rename done in 1.3 (`pipeline_execution_id` → `pipeline_run_id`).
- **`src/gobby/storage/cron.py`** — this is the cron storage manager that owns the SQL for inserting and updating cron-run rows. Lines 320, 334, and 351 each reference `pipeline_execution_id` (in INSERT column list, parameter binding from a `CronRun`, and an UPDATE field-name allowlist respectively). Rename all three sites to `pipeline_run_id`. Rename any local helper that names this field. Update any tests in `tests/storage/test_cron_storage.py` (or the closest cron-storage test module — confirm via gcode `search "test_cron"` under `tests/storage/`) that assert the old column name.
- `src/gobby/scheduler/executor.py`: update the line that calls `self.storage.update_run(cron_run.id, pipeline_execution_id=execution.id)` (or equivalent) to `pipeline_run_id=run.id`. Local naming: rename the cron-run variable to avoid name collision with the new pipeline `run` (suggest `cron_run` for the cron-run side and `run` for the pipeline-run side).
- Any `CronRunManager.update_run(...)` signature in `src/gobby/storage/cron.py` taking `pipeline_execution_id=...` keyword: rename to `pipeline_run_id=...`.

`validation_criteria`: `pipeline_execution_id` no longer appears as a parameter name, dict key, or column reference under `src/gobby/` **excluding migration code** (specifically `src/gobby/storage/_migration_registry.py` and any module under `src/gobby/storage/migrations/` — these legitimately contain the old column name to perform the v220→v221 rename and to rewrite persisted `metadata_json` per §1.1). Same allowlist as Phase 7's drift sweep. The scheduler's cron-trigger path passes `pipeline_run_id=run.id` to the cron-runs update. `src/gobby/storage/cron.py` lines that previously held `pipeline_execution_id` (320, 334, 351 at draft-time) all carry `pipeline_run_id`. Existing scheduler and cron-storage tests pass with the renamed parameter.

### 2.4 Rename heartbeat, conductor review, conductor manager, health, telemetry [category: refactor] (depends: 2.1)

Target: `src/gobby/workflows/pipeline_heartbeat.py`, `src/gobby/conductor/pipeline_review.py`, `src/gobby/conductor/manager.py`, `src/gobby/servers/routes/admin/_health.py`, telemetry emit sites

- `pipeline_heartbeat.py` (`class PipelineHeartbeat`, lines 32–238): all internal `execution`/`execution_id` references rename per 2.1 pattern. Method `check_stalled_executions` → `check_stalled_runs` (line ~62). Storage call `manager.get_stalled_executions(...)` → `manager.get_stalled_runs(...)` per §1.3.
- `conductor/pipeline_review.py`: `gather_review_data(execution: PipelineExecution, steps: list[StepExecution])` → `gather_review_data(run: PipelineRun, steps: list[PipelineStepRun])` (line ~64). `class ExecutionReviewData` → `class PipelineRunReviewData` (or similar — check current name and rename consistently). `build_review_json` (line ~153) field rename in the produced JSON: `execution_id` → `run_id`.
- **`conductor/manager.py`** — line 32 contains a hardcoded instruction string sent to the conductor agent: `"2. Check pipeline state: use \`get_pipeline_status\` to find stalled or waiting pipelines"`. Rewrite to `"...use \`get_pipeline_run\` to find stalled or waiting pipelines"`. Audit the rest of the file for any other `execution_id`, `get_pipeline_status`, `list_pipeline_executions`, or `search_pipeline_executions` literals in instruction text; rewrite each to the new tool/field name. This is user-facing text consumed by the conductor agent's prompt — leaving the old name yields a "tool not found" error on the first conductor tick after Phase 3.2 lands.
- **`conductor/manager.py` `class ConductorManager`**: line 59 constructor param `execution_manager: LocalPipelineExecutionManager | None = None` → `run_manager: LocalPipelineRunManager | None = None`; line 69 attribute `self._execution_manager = execution_manager` → `self._run_manager = run_manager` (update every `self._execution_manager` reference); line 123 docstring (`"Reviewed 3 executions"` → `"Reviewed 3 runs"`); line 125 `if not self._execution_manager:` → `if not self._run_manager:`; line 129 `self._run_manager.get_unreviewed_completions(limit=5)` (method name unchanged per §1.3, attribute renames). Audit `get_steps_for_execution(...)` → `get_steps_for_run(...)` calls. Loop variable `execution` → `run` (iterates `PipelineRun` instances); update every dot-access, log message, and `f"Reviewed {n} execution(s)"` → `f"Reviewed {n} run(s)"`.

**Tests**: `tests/conductor/test_conductor_review.py` (or closest equivalent — gcode `search "test_conductor"`) update `ConductorManager(execution_manager=...)` fixtures to `run_manager=...` and assert run-noun review-summary strings.
- `servers/routes/admin/_health.py`: any `execution`-named pipeline metrics rename. Confirm via gcode `search "execution"` first; if no pipeline-execution refs in health, skip this file.
- Telemetry/log attributes: search for `execution_id=` in `logger.info`/`logger.error`/`tracer.start_as_current_span`/OpenTelemetry attribute calls under `src/gobby/workflows/` and `src/gobby/conductor/`. Rename to `run_id=`.

`validation_criteria`: Heartbeat method `check_stalled_runs` exists. `gather_review_data` accepts `run` and `steps` of types `PipelineRun` and `PipelineStepRun`. `build_review_json` output JSON uses `run_id`. Logged structured fields under `workflows/` and `conductor/` use `run_id`. `conductor/manager.py` contains no `get_pipeline_status` / `list_pipeline_executions` / `search_pipeline_executions` / `execution_id` / `_execution_manager` / `LocalPipelineExecutionManager` literals (verified by ripgrep). `ConductorManager.__init__` accepts `run_manager` (verified by introspection or test). The review-summary text reads `Reviewed N run(s)` (verified by `tests/conductor/test_conductor_review.py`). `tests/workflows/test_pipeline_heartbeat.py` (if present) and `tests/conductor/test_pipeline_review.py` (if present) pass with renamed assertions.

### 2.5 Rename runner restart lifecycle + stale-approval expiration [category: refactor] (depends: 1.3)

Target: `src/gobby/runner_lifecycle.py`, `src/gobby/runner_maintenance.py`

These two top-level runner modules call into the renamed pipeline manager from background loops (restart-recovery and maintenance-tick). They use the old method names and field names directly, so they break the moment Phase 1.3 lands the manager rename.

**`src/gobby/runner_lifecycle.py`**:

- Line 592: `runner.pipeline_execution_manager.fail_stale_running_executions(...)` → `runner.pipeline_run_manager.interrupt_stale_running_runs(...)`. The `fail_*` alias is deleted in 1.3; the canonical method is `interrupt_stale_running_runs`. Update both the attribute access and the method name.
- Line 603: `runner.pipeline_execution_manager.list_executions(...)` → `runner.pipeline_run_manager.list_runs(...)`. Update keyword args if the surrounding call passes `parent_execution_id=` or `execution_id=`.
- Audit the rest of this file for any other `pipeline_execution_manager` attribute access, `execution_id`/`parent_execution_id` locals or log fields, or `PipelineExecution`/`StepExecution` type annotations — rename per 1.3.

**`src/gobby/runner_maintenance.py`**:

- Lines 240–246: `pipeline_execution_manager.update_step_execution(step_execution_id=step.id, ...)` → `pipeline_run_manager.update_step_run(step_run_id=step.id, ...)`. The variable `pipeline_execution_manager` is renamed where it's bound (search upward for the assignment).
- Lines 245–246: `pipeline_execution_manager.update_execution_status(execution_id=step.execution_id, ...)` → `pipeline_run_manager.update_run_status(run_id=step.run_id, ...)`. Note `step.execution_id` is the `StepExecution.execution_id` field renamed in 1.3 to `PipelineStepRun.run_id`, so the attribute access changes too.
- Line 251: f-string `f"in execution {step.execution_id}"` → `f"in run {step.run_id}"`. This is a log message and reads as a sentence, so rename the noun consistently.
- Audit the rest of this file for any other manager-attribute access, `execution_id`/`parent_execution_id` locals or log fields — rename per 1.3.

**Manager attribute name**: the attribute on `runner` (and on `app_context`/`GobbyRunner`/wherever the manager is mounted) renames from `pipeline_execution_manager` → `pipeline_run_manager`. Phase 1.3's "Manager attribute on `LocalDatabase`" bullet covered the storage-side `self.pipelines` rename; the runner-side attribute is its own surface and needs explicit calling-out here. Update wherever `pipeline_execution_manager` appears as a *runner* attribute (likely in `src/gobby/runner.py`, `src/gobby/runner_init.py`, or a similar bootstrap module — confirm via `rg -n "pipeline_execution_manager" src/gobby/`).

**Tests**: `tests/test_runner_lifecycle.py` (if present), `tests/test_runner_maintenance.py` (if present), and `tests/runner/test_resume_interrupted.py` (or the closest restart-recovery suite — confirm via gcode `search "test_resume" "test_restart"` under `tests/`). Update assertions on stale-approval expiration paths and on the restart-recovery sweep so they reference `pipeline_run_manager` and `run_id`.

`validation_criteria`: ripgrep `\bpipeline_execution_manager\b|\bexecution_id\b|\bfail_stale_running_executions\b|\blist_executions\b|\bupdate_step_execution\b|\bupdate_execution_status\b` across `src/gobby/runner_lifecycle.py` and `src/gobby/runner_maintenance.py` returns zero matches. The runner attribute is `pipeline_run_manager` everywhere (verified by ripgrep across `src/gobby/`). Existing restart-recovery and maintenance-tick tests pass against the renamed surface.

### 2.6 Rename durable wake metadata + completion-notification dedupe [category: refactor] (depends: 1.1, 1.3)

Target: `src/gobby/events/wake.py`, `src/gobby/events/completion_registry.py`, `src/gobby/storage/inter_session_messages.py`

The durable cross-session wake/completion-notification stack carries a separate metadata contract that dedupes completions across daemon restarts. It currently accepts `execution_id` as a fallback key on three independent surfaces. The no-alias rule requires removing the fallback after migration 221 has rewritten any persisted `execution_id` keys in `inter_session_messages.metadata_json` (covered by §1.1's data rewrite extension).

- **`src/gobby/events/wake.py:246-247`** — the `_resolve_completion_id` static method currently reads `metadata.get("completion_id") or metadata.get("run_id") or metadata.get("execution_id")`. Drop the trailing `or metadata.get("execution_id")` fallback. The method becomes `metadata.get("completion_id") or metadata.get("run_id")`. Audit the rest of `wake.py` for any other `execution_id` literal in metadata-shaped contexts and rename or remove.
- **`src/gobby/storage/inter_session_messages.py:225-227`** — the duplicate-detection query has three OR branches: `$.completion_id`, `$.run_id`, `$.execution_id`. Drop the `OR json_extract(metadata_json, '$.execution_id') = ?` branch (line 227). Update the bind-param tuple at line 231 from `(to_session, message_type, completion_id, completion_id, completion_id)` to `(to_session, message_type, completion_id, completion_id)`. The remaining branches still cover `completion_id` and `run_id`, which is sufficient post-migration since §1.1 has rewritten any `execution_id` metadata to `run_id`.
- **`src/gobby/events/completion_registry.py:48`** — the docstring `completion_id: Unique ID (execution_id or run_id)` → `completion_id: Unique ID (run_id)`. Audit the rest of this file for any other `execution_id` mentions in docstrings, type hints, or stored attributes; rename per the no-alias rule.

**Order requirement**: depends on §1.1's `inter_session_messages.metadata_json` rewrite landing first in the same PR. Without it, dropping the SQL OR branch loses dedupe coverage for pre-upgrade in-flight notifications. PR-level test seeds `metadata_json = {"execution_id": "pe-..."}`, runs migration 221, asserts rewrite to `{"run_id": "pe-..."}` and post-migration lookup hits the `$.run_id` branch.

**Tests**: `tests/events/test_wake.py` and `tests/storage/test_inter_session_messages.py` (or closest equivalents — verify via gcode `search "test_wake"` / `"test_inter_session"`). Each adds a parametrized case for pre-migration `{"execution_id": ...}` metadata.

`validation_criteria`: ripgrep `\bexecution_id\b` across `src/gobby/events/wake.py`, `src/gobby/events/completion_registry.py`, and `src/gobby/storage/inter_session_messages.py` returns zero matches. The duplicate-detection query in `inter_session_messages.py` has exactly two OR branches (`completion_id`, `run_id`). The migration-test from §1.1 validates that pre-migration `metadata_json = {"execution_id": ...}` rows are rewritten to `{"run_id": ...}` and remain findable post-rename.

### 2.7 Rename service-container + runner-attribute wiring [category: refactor] (depends: 1.3)

**Canonical enumeration**: per the constraint above, the authoritative target list is `gcode search "LocalPipelineExecutionManager"` ∪ `gcode search "execution_manager"` ∪ `gcode search "pipeline_execution_manager"` ∪ `gcode search "_get_execution_manager"` ∪ `gcode search "execution_manager_getter"` ∪ `gcode search "PipelineExecutionManager"`, intersected with non-allowlisted source paths (`src/gobby/`, `tests/`, `web/src/`; exclude `src/gobby/storage/_migration_registry.py` and `src/gobby/storage/migrations/` and `docs/plans/completed/`). Cross-check with `rg -l` on the same patterns to catch any FTS gaps. Every non-allowlisted hit is in scope. The file list below is a starting hint of likely sites; **the implementing agent runs the gcode/rg sweep and renames every result**, not just these.

Likely sites (non-exhaustive, hint only): `src/gobby/runner.py`, `src/gobby/runner_init.py`, `src/gobby/app_context.py`, `src/gobby/servers/http.py`, `src/gobby/mcp_proxy/registries.py`, `src/gobby/mcp_proxy/tools/workflows/__init__.py`, `src/gobby/mcp_proxy/tools/workflows/_pipelines.py`, `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`, `src/gobby/hooks/factory.py`, `src/gobby/communications/reactions.py`.

Rename mapping (apply to every gcode/rg hit):

| Old | New |
|---|---|
| `LocalPipelineExecutionManager` | `LocalPipelineRunManager` |
| `PipelineExecutionManager` (Protocol) | `PipelineRunManager` |
| `pipeline_execution_manager` (attribute/kwarg/local) | `pipeline_run_manager` |
| `execution_manager` (parameter/kwarg/attribute/local) | `run_manager` |
| `_get_execution_manager` (closure helper) | `_get_run_manager` |
| `execution_manager_getter` (parameter) | `run_manager_getter` |

**Special-case notes** (non-mechanical context — implementing agent runs the gcode sweep first, then attends to these):

- **`src/gobby/runner_init.py:709-713`**: heartbeat-construction guard `RuntimeError("pipeline_execution_manager required for heartbeat")` rewrites to `RuntimeError("pipeline_run_manager required for heartbeat")` — message text matters for `tests/test_runner_init.py` assertions.
- **`src/gobby/hooks/factory.py:484-490`**: hook-side pipeline executor is constructed inside a broad `except Exception` block — silent failure mode on rename mismatch. Verify the construction succeeds in `tests/hooks/test_factory.py` rather than relying on the absence of an exception.
- **`src/gobby/app_context.py:67`**: attribute carries a comment annotation (`# LocalPipelineExecutionManager`) — update the comment alongside the rename so a future reader doesn't mis-resolve the type.
- **`src/gobby/mcp_proxy/registries.py:190`**: `execution_manager_getter` is a closure-captured lambda; the renamed `run_manager_getter` must remain a closure over the renamed outer variable. Verify the closure binding.
- **`src/gobby/communications/reactions.py:117`**: `ReactionHandler` reads `self._services.pipeline_execution_manager` → `pipeline_run_manager`. The renamed `_services` container attribute is owned by the runner-side wiring; this caller is downstream.

**Tests**: gcode-driven enumeration extends to `tests/`. Run `gcode search` for each old symbol filtered to `tests/` and update every match. Concrete fixture-bearing modules likely affected: `tests/test_app_context.py`, `tests/mcp_proxy/test_registries.py`, `tests/communications/test_reactions.py`, `tests/test_runner_init.py`, `tests/hooks/test_factory.py`, `tests/mcp_proxy/tools/spawn_agent/test_factory.py`. The reaction-handler approval flow continues to function end-to-end (verify by an integration-shaped reaction → approval → run-state-change test).

`validation_criteria`: **canonical gate** — `gcode search "<symbol>" | jq '.total'` returns 0 for every symbol in the rename mapping table above (`LocalPipelineExecutionManager`, `PipelineExecutionManager`, `pipeline_execution_manager`, `execution_manager`, `_get_execution_manager`, `execution_manager_getter`), and `rg -l "<symbol>" src/gobby/ tests/ web/src/` returns no non-allowlisted matches. Allowlist: `src/gobby/storage/_migration_registry.py`, `src/gobby/storage/migrations/`, `tests/test_pipeline_runs_drift_sweep.py`, `docs/plans/completed/`, plan/task files. Behavioral verifications: reaction-handler approval flow functions (`tests/communications/test_reactions.py`); heartbeat guard raises `pipeline_run_manager required for heartbeat` (runner-init tests); MCP workflow registry forwards `run_manager_getter` end-to-end (`tests/mcp_proxy/test_registries.py` + `tests/mcp_proxy/tools/test_pipelines.py`); hook-side executor constructs with `run_manager=` (`tests/hooks/test_factory.py`); spawn-agent subscriber persistence instantiates `LocalPipelineRunManager` (`tests/mcp_proxy/tools/spawn_agent/test_factory.py`).

---

## Phase 3: Public APIs — HTTP & MCP (depends: Phase 2)

**Goal**: Replace `executions`-named HTTP routes and MCP tools with `runs`-named equivalents; remove old surfaces (no aliases); thread the new pagination response shape through both.

### 3.1 HTTP routes: rename + remove old + add pagination response [category: refactor]

Target: `src/gobby/servers/routes/pipelines.py`, route registration in `src/gobby/servers/http.py`

**Route surface changes** (one HTTP module file, ~441 lines today):

| Old | New | Notes |
|---|---|---|
| `POST /api/pipelines/run` | `POST /api/pipelines/run` (unchanged path) | Response body returns `{run_id: pr-…, status: ...}` instead of `{execution_id: pe-…, status: ...}`. 200/202 behavior unchanged. |
| `GET /api/pipelines/executions` | `GET /api/pipelines/runs` | New query params: `status`, `name` (renamed from `pipeline_name`), `project_id`, `parent_run_id` (renamed), `session_id`, `limit`, `offset`. Response shape per below. |
| `GET /api/pipelines/executions/search` | `GET /api/pipelines/runs/search` | Query params: `q`, `status`, `search_errors`, `search_outputs`, `project_id`, `limit`, `offset`. Same pagination shape; response also includes `query: <q>`. |
| `GET /api/pipelines/executions/{execution_id}` | `GET /api/pipelines/runs/{run_id}` | Path param renamed. Response: `{status: ok, run: {...}}`. |
| `GET /api/pipelines/{execution_id}` (root detail per memory `423cb4dc`) | **REMOVE** | Spec: *old root /{execution_id} detail routes are removed*. Delete the handler. |
| `POST /api/pipelines/approve/{token}` | `POST /api/pipelines/approve/{token}` (unchanged path) | Response body emits `run_id` instead of `execution_id` if currently included. |
| `POST /api/pipelines/reject/{token}` | `POST /api/pipelines/reject/{token}` (unchanged path) | Same. |

**Response shape for list/search** (canonical, expressed as a Python dict literal — keys are JSON strings on the wire):

```python
{
    'status': 'ok',
    'runs': [run.to_dict() for run in runs],
    'count': len(runs),                    # page size
    'total_count': filtered_total,         # filter-scoped total from count_runs
    'limit': limit,
    'offset': offset,
    'status_summary': {                    # filter-scoped (status filter dropped)
        'running': 12, 'completed': 80, 'failed': 3, 'waiting_approval': 1,
    },
}
```

`/runs/search` response also includes `query: q`.

**Remove old routes**. Do not 308/redirect. Old routes must return 404. Update the handler module so the FastAPI router has no `@router.get(/executions)`, `@router.get(/executions/search)`, `@router.get(/executions/{...})`, `@router.get(/{execution_id})` registrations. Reflect this in any OpenAPI assertions in tests.

**Cron-info join**: the existing helper `_batch_load_cron_info` (currently joins `cron_runs` on `pipeline_execution_id`) — update its query to use `pipeline_run_id`.

**`PipelineRunRequest`** body model (rename from `PipelineRunRequest` if it exists, else keep — verify): fields `name`, `inputs`, `project_id` unchanged.

**Header propagation**: any code that reads/sets an `X-Gobby-Execution-Id` header — rename to `X-Gobby-Run-Id` (search; if none, skip).

**Pagination input validation**: `list_runs`/`search_runs` raise `ValueError` for `limit <= 0` or `offset < 0` (§1.4). HTTP must reject at the boundary, not propagate to 500. Use FastAPI `Query(..., ge=0)` for `offset` and `Query(..., gt=0, le=200)` for `limit`; map any escaping `ValueError` to 400. Tests assert `?limit=0`, `?limit=-1`, `?offset=-1` each return 4xx naming the offending parameter.

**User-facing prose**: rewrite docstrings, `detail=` strings, and OpenAPI summaries in this file from "pipeline execution(s)" to "pipeline run(s)". Concrete sites at draft-time: lines 29 (`Response body for successful pipeline execution.`), 48 (`Load cron trigger info for a batch of pipeline execution IDs.`), 98 (`List pipeline executions with optional filters.`), 176 (`Search pipeline executions by text across pipeline names and step errors.`), 245 (`detail="project_id required for pipeline execution"`), plus three more matches under the same `rg -ni "pipeline execution" src/gobby/servers/routes/pipelines.py` query. Also rewrite "pipeline execution" mentions in `src/gobby/config/app.py:488` (`description="Pipeline execution configuration"` → `"Pipeline run configuration"`) and `src/gobby/config/pipelines.py:9` (`"""Configuration for pipeline execution."""` → `"""Configuration for pipeline runs."""`).

`validation_criteria`: `servers/routes/pipelines.py` registers no routes containing the substring `executions` or `{execution_id}`. New routes `GET /runs`, `GET /runs/search`, `GET /runs/{run_id}` exist and return the canonical paginated shape with `runs`, `count`, `total_count`, `limit`, `offset`, `status_summary`. `_batch_load_cron_info` joins on `pipeline_run_id`. `tests/servers/routes/test_pipelines.py` (or equivalent) covers (a) old routes return 404, (b) new routes return 200 with pagination metadata, (c) `total_count` reflects filter scope (e.g., `?status=running` returns total_count of running rows only).

### 3.1a Rename session-resumability SQL [category: refactor]

Target: `src/gobby/servers/routes/sessions/core.py`, `src/gobby/servers/websocket/handlers/session_observe.py`

Two non-pipeline-routes surfaces query the renamed pipeline-runs table directly via raw SQL when computing whether a session is resumable / attachable. After migration 221 these will throw `no such table: pipeline_executions`.

- **`src/gobby/servers/routes/sessions/core.py:143`** — currently `"SELECT DISTINCT session_id FROM pipeline_executions "`. Rename to `"SELECT DISTINCT session_id FROM pipeline_runs "`. Audit any surrounding column references in the same query (e.g., `WHERE status IN (...)`) and verify column names didn't change in a way that breaks the predicate. Audit the rest of this module for any other `pipeline_executions` / `step_executions` / `execution_id` SQL or Python references and rename.
- **`src/gobby/servers/websocket/handlers/session_observe.py:486`** — currently `"SELECT id FROM pipeline_executions "`. Rename to `"SELECT id FROM pipeline_runs "`. Audit the rest of this handler for execution-named SQL or Python references and rename.

**Tests**: `tests/servers/routes/sessions/test_core.py` (or the closest session-routes test module — confirm via gcode `search "test_core" "sessions"` under `tests/servers/`) and `tests/servers/websocket/test_session_observe.py` (or the closest session-observe test). Each must seed a `pipeline_runs` row in a way that triggers the resumability/attach predicate and assert the resulting session-state response. If the existing tests reference `pipeline_executions` directly, rename the fixtures.

`validation_criteria`: ripgrep `\bpipeline_executions\b|\bstep_executions\b|\bexecution_id\b` across `src/gobby/servers/routes/sessions/core.py` and `src/gobby/servers/websocket/handlers/session_observe.py` returns zero matches. Both files compile against the post-migration schema (no `no such table` errors in the session-route or session-observe tests). Resumability and attach behavior is preserved end-to-end (verified by the existing session-state tests after rename).

### 3.2 MCP tools: rename + add pagination [category: refactor]

Target: three implementation files (the MCP pipeline surface is split across them) plus the stdio instructions and the registration site:

- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py` — wrapper / dispatch entry points (~23KB)
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_query.py` — list/search/get-status helpers (~5KB)
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py` — run/resume/cancel/approve/reject orchestration (~23KB; this is where the heavy logic for nested-pipeline parent linkage, status assembly, and approval state lives)
- `src/gobby/mcp_proxy/stdio.py` — server-side instruction text shown to MCP clients (lines 85–87 today reference `execution_id` and `get_pipeline_status` in advisory prose)
- MCP tool registration site (likely `src/gobby/mcp_proxy/tools/workflows/__init__.py` or a `register_tools` function — confirm via gcode `search "register" "_pipelines"` under `src/gobby/mcp_proxy/`).

| Old | New | Notes |
|---|---|---|
| `_run_pipeline(name, inputs, continuation_prompt)` | unchanged name | Returns `{run_id: pr-…, ...}` instead of `{execution_id: ...}`. Wrapper in `_pipelines.py`; orchestration in `_pipeline_execution.py`. |
| `_resume_pipeline(execution_id, from_step)` | `_resume_pipeline(run_id, from_step)` | Param rename. Returns `run_id`. Bodies in `_pipelines.py` + `_pipeline_execution.py`. |
| `_cancel_pipeline(execution_id)` | `_cancel_pipeline(run_id)` | Param rename. Same two-file split. |
| `_approve_pipeline(token, approved_by)` | unchanged | Token-based; returns include `run_id` not `execution_id`. Body in `_pipeline_execution.py`. |
| `_reject_pipeline(token, rejected_by)` | unchanged | Same. |
| `_get_pipeline_status(execution_id)` | **REMOVED, replaced by `_get_pipeline_run(run_id)`** | New tool name. Same response semantics (full run + steps), but the **top-level result key changes from `execution` to `run`** — current callers/tests assert `result["execution"]`; the renamed tool must return `result["run"]`. Wrapper in `_pipelines.py`; helper body in `_pipeline_query.py`; nested-status assembly delegated to `_pipeline_execution.py`. |
| `_list_pipeline_executions(status, pipeline_name, session_id, parent_execution_id, limit, brief, include_steps)` | `_list_pipeline_runs(status, name, session_id, parent_run_id, limit, offset, brief, include_steps)` | Rename + add `offset`. Wrapper in `_pipelines.py`; body in `_pipeline_query.py`. Response per below. |
| `_search_pipeline_executions(query, search_errors, search_outputs, status, limit, include_steps)` | `_search_pipeline_runs(query, search_errors, search_outputs, status, limit, offset, include_steps)` | Rename + add `offset`. Same wrapper/body split. Response shape plus `query`. |

**`src/gobby/mcp_proxy/stdio.py` instruction-text rewrite** (lines 85–87 at draft-time):

- Drop the dual-mention of `run_id or execution_id`; persist only `run_id`.
- Replace `get_pipeline_status` with `get_pipeline_run`.

**`src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py:541` docstring rewrite**: the docstring currently references `fail_stale_running_executions(exclude_ids=...)`. Update to `interrupt_stale_running_runs(exclude_ids=...)` per the §1.3 alias deletion. Audit the rest of `_pipeline_execution.py` for any other docstring or comment references to `fail_stale_running_executions` and rewrite.

**Pagination input validation**: storage raises `ValueError` for invalid `limit`/`offset` (§1.4). The MCP wrappers catch at the boundary and return `{success: False, error: "Invalid pagination: <msg>"}` (match sibling-tool error shape; verify via gcode). Tests assert `_list_pipeline_runs(limit=0)`, `(offset=-1)`, same for `_search_pipeline_runs`, each return `success: False` naming the offending parameter.

**User-facing prose**: rewrite registered MCP tool `description=` strings and error strings in `_pipelines.py` from "pipeline execution(s)" to "pipeline run(s)". Concrete sites at draft-time (11 matches via `rg -ni "pipeline execution" src/gobby/mcp_proxy/tools/workflows/_pipelines.py`): line 259 (`Resume a failed pipeline execution. Resets steps from the failure point`), 301 (`Approve a pipeline execution that is waiting for approval.`), 318 (`Reject a pipeline execution that is waiting for approval.`), 335 (`Cancel a running pipeline execution and kill associated agents.`), 342 (`Pipeline execution manager not available`), plus six more under the same query. Audit and rewrite all 11. Also rewrite any prose in `_pipeline_query.py` and `_pipeline_execution.py` (run `rg -ni "pipeline execution" src/gobby/mcp_proxy/tools/workflows/`).

**Response key invariants** for list/search MCP tools — match HTTP shape exactly:

```python
{
    'status': 'ok',
    'runs': [...],
    'count': page_size,
    'total_count': filtered_total,
    'limit': limit,
    'offset': offset,
    'status_summary': {...},
    # search only:
    'query': q,
}
```

**Tool registration — Python wrapper names vs registered MCP tool names**: the table above uses `_get_pipeline_run` / `_list_pipeline_runs` / `_search_pipeline_runs` (leading underscore) as the **internal Python wrapper function names** that live in `_pipelines.py`. The **registered MCP tool names** (visible to MCP clients via `list_tools` / `call_tool`) are the same identifiers **without** the leading underscore: `get_pipeline_run`, `list_pipeline_runs`, `search_pipeline_runs`, `run_pipeline`, `resume_pipeline`, `cancel_pipeline`, `approve_pipeline`, `reject_pipeline`. The parent spec and §1.1's `definition_json` rewrite table both use the no-underscore form because that's the public contract. Verify the registration mechanism in the workflows MCP server module (likely `src/gobby/mcp_proxy/tools/workflows/__init__.py` or a `register_tools` function — confirm via gcode `search "register" "_pipelines"` under `src/gobby/mcp_proxy/`); the registration may strip the leading underscore automatically, or it may require explicit naming. Whichever is current, the renamed tools must end up registered as `get_pipeline_run` / `list_pipeline_runs` / `search_pipeline_runs` (no underscore). The old names (`get_pipeline_status`, `list_pipeline_executions`, `search_pipeline_executions`) must produce a *tool not found* error post-rename — explicit removal, not deprecation.

`validation_criteria`: 
- All three implementation files (`_pipelines.py`, `_pipeline_query.py`, `_pipeline_execution.py`) contain no occurrences of `execution_id`, `parent_execution_id`, `get_pipeline_status`, `list_pipeline_executions`, or `search_pipeline_executions` (ripgrep verified).
- The MCP tool registration exposes the **no-underscore** public names `get_pipeline_run`, `list_pipeline_runs`, `search_pipeline_runs`, `run_pipeline`, `resume_pipeline`, `cancel_pipeline`, `approve_pipeline`, `reject_pipeline` (verified by introspecting `list_tools` at runtime against the workflows MCP server). The Python wrapper functions in `_pipelines.py` are named with a leading underscore (`_get_pipeline_run` etc.) per existing convention, but the registered names must be without.
- The old names (`get_pipeline_status`, `list_pipeline_executions`, `search_pipeline_executions`) are absent from the registered tool list. Calling any of them via `call_tool` returns a *tool not found* error.
- `_get_pipeline_run` returns `{run: {...}, ...}` at the top level (key is `run`, not `execution`); existing tests asserting `result["execution"]` are updated to `result["run"]`.
- All renamed tools accept `run_id` where applicable; `_list_pipeline_runs` and `_search_pipeline_runs` accept `offset` and return `total_count` matching filter scope.
- `src/gobby/mcp_proxy/stdio.py` contains no `execution_id` or `get_pipeline_status` references (ripgrep verified).
- All three test files cover the renamed surface:
  - `tests/mcp_proxy/tools/test_pipelines.py` (1,299 lines today — do not expand; touch only the lines required for the rename) covers run/resume/cancel/approve/reject + the renamed list/search/detail tool wrappers and asserts pagination metadata in responses.
  - `tests/mcp_proxy/tools/test_pipeline_query.py` (196 lines today) covers `_pipeline_query.py` helper-level pagination (offset progression, filtered total_count, status_summary).
  - `tests/mcp_proxy/tools/workflows/test_pipelines.py` (433 lines today) covers the workflow-tooling-specific subset.
- A test-execution probe asserts that calling `_get_pipeline_status`, `_list_pipeline_executions`, or `_search_pipeline_executions` on the MCP proxy returns a `tool not found` error.

---

## Phase 4: CLI Surface (depends: Phase 3)

**Goal**: Restructure CLI to `gobby pipelines runs <list|show|search>`; remove redundant runtime list/search at the top level; add pagination flags.

### 4.1 CLI: `gobby pipelines runs` subcommand + remove old [category: refactor]

Target: `src/gobby/cli/pipelines.py`

Reshape command surface using Click groups. Keep top-level **definition** verbs (`list` for pipeline definitions, `show NAME`, `run NAME`) and move all **instance** access under a `runs` subgroup:

| Old | New |
|---|---|
| `gobby pipelines list` | `gobby pipelines list` (unchanged — lists pipeline **definitions**) |
| `gobby pipelines show NAME` | `gobby pipelines show NAME` (unchanged — shows one definition) |
| `gobby pipelines run NAME [k=v ...]` | `gobby pipelines run NAME [k=v ...]` (unchanged — verb to start a run; outputs `run_id`) |
| `gobby pipelines status EXECUTION_ID` (`show_pipeline_run` at line 374, takes an execution_id) | `gobby pipelines runs show RUN_ID` |
| `gobby pipelines list-runs` (`list_pipeline_runs` at line 586) | `gobby pipelines runs list` |
| `gobby pipelines search QUERY` (`search_executions` at line 672) | `gobby pipelines runs search QUERY` |
| `gobby pipelines history NAME` (`history_pipeline` at line 527) | **REMOVE** — redundant with `runs list --name NAME`. Delete the handler. |
| `gobby pipelines approve TOKEN` | `gobby pipelines approve TOKEN` (unchanged) |
| `gobby pipelines reject TOKEN` | `gobby pipelines reject TOKEN` (unchanged) |

**New flags on `runs list`**:
- `--status <status>`
- `--name <pipeline-name>` (was `--pipeline`)
- `--limit <int>` (default 50; `click.IntRange(min=1, max=200)` — reject `--limit 0` and negative values at the CLI boundary so the storage `ValueError` never propagates to a Click traceback)
- `--offset <int>` (default 0; `click.IntRange(min=0)` — reject negative values at the CLI boundary)
- `--json`

**New flags on `runs search`**:
- `--limit <int>` (default 20; `click.IntRange(min=1, max=200)`)
- `--offset <int>` (default 0; `click.IntRange(min=0)`)
- existing search filters

**Pagination input validation**: `click.IntRange` on `--limit`/`--offset` per the bullets above renders `Invalid value for '--limit': 0 is not in the range 1<=x<=200.` and exits non-zero. CLI tests (scoped to avoid breaching the 1,239-line ceiling on `tests/cli/test_pipelines.py`) assert `--limit 0`, `--limit -1`, `--offset -1` each exit non-zero naming the offending flag.

**User-facing prose**: rewrite Click command docstrings (which become CLI help text) and `click.echo()` error strings from "pipeline execution(s)" to "pipeline run(s)". Concrete sites at draft-time (6 matches via `rg -ni "pipeline execution" src/gobby/cli/pipelines.py`): line 349 (`click.echo(f"Pipeline execution failed: {e}", err=True)`), 354 (`"""Get pipeline execution manager instance."""`), 375 (`"""Show status of a pipeline execution.`), 451 (`"""Approve a pipeline execution waiting for approval.`), 489 (`"""Reject a pipeline execution waiting for approval.`), plus one more. Rewrite each.

**Python identifiers in `src/gobby/cli/pipelines.py`** (per the gcode-driven enumeration constraint above): `def get_execution_manager()` (line 353) → `def get_run_manager()`; every `execution_manager` local/parameter → `run_manager`; every `click.argument('execution_id')` → `click.argument('run_id')`; every `click.argument('parent_execution_id')` → `click.argument('parent_run_id')`. JSON response keys emitted by `--json` mode (`{"execution_id": ..., "execution": {...}}`) → `{"run_id": ..., "run": {...}}` to match the HTTP/MCP response shape from §3.1/§3.2. Manager method calls update per §1.3 renames: `get_execution(...)` → `get_run(...)`, `list_executions(...)` → `list_runs(...)`, `search_executions(...)` → `search_runs(...)`, `get_steps_for_execution(...)` → `get_steps_for_run(...)`. Validation: `gcode search "execution_manager"` and `gcode search "execution_id"` filtered to `src/gobby/cli/pipelines.py` return zero results; `tests/cli/test_pipelines.py` (touch only required lines per the 1,239-line ceiling) and `tests/cli/test_pipelines_coverage.py` updated assertions reference `run_id`/`run` JSON keys.

**Output**: human-readable list output should print a footer like `Showing 1–50 of 234 (use --offset 50 for next page)` when `total_count > limit`. JSON mode emits the full pagination dict.

**Backwards compat**: NONE. Old commands (`status`, `list-runs`, `search`, `history`) are removed.

**Click structure**: introduce a nested group:

```python
@pipelines.group('runs')
def runs_group():
    '''Pipeline run instances.'''

@runs_group.command('list')
def list_runs_cmd(...):
    ...

@runs_group.command('show')
@click.argument('run_id')
def show_run_cmd(run_id, ...):
    ...

@runs_group.command('search')
@click.argument('query')
def search_runs_cmd(query, ...):
    ...
```

`validation_criteria`: `gobby pipelines runs list/show/search` exist. `gobby pipelines status`, `list-runs`, `search`, `history` no longer exist. `runs list --offset 50` skips the first 50 rows. `--json` output includes `total_count`, `limit`, `offset`. Tests in `tests/cli/test_pipelines.py` cover the new surface; do not expand the file past 1239 lines (file an out-of-scope split task under #12730 if needed).

---

## Phase 5: Web Frontend (depends: Phase 3)

**Goal**: Rename TypeScript types/hooks/components to run terminology; consume new HTTP shape; replace any existing pagination UI with Next/Prev controls reading `total_count`/`offset`.

### 5.1 Web frontend rename + Next/Prev pagination [category: refactor]

Target: full set of web files that import the hook/types or render execution-named UI:

- `web/src/components/activity/PipelinesTab.tsx`
- `web/src/hooks/usePipelineExecutions.ts`
- `web/src/components/workflows/execution-utils.tsx`
- `web/src/components/workflows/PipelineExecutionsView.tsx`
- **`web/src/components/workflows/ReportsPage.tsx`** (also imports `usePipelineExecutions` and consumes `PipelineExecutionRecord`; renders `parent_execution_id` in the UI)
- **`web/src/components/workflows/ReportingTab.tsx`** (same imports + rendering pattern)
- `web/src/components/activity/__tests__/PipelinesTab.test.tsx`
- **`web/src/hooks/useCronJobs.ts`** — `interface CronRun` (line 29) has a `pipeline_execution_id: string | null` field (line 39) that mirrors the renamed cron-storage column. Rename to `pipeline_run_id`. Update any local variable typed `CronRun` (e.g., line 89 `useState<CronRun[]>`, line 220 `runNow` callback's return type, line 228 `data.run as CronRun`) — the type rename propagates automatically, but verify the cron-API mock fixtures used by the hook respond with `pipeline_run_id` per Phase 3.1's HTTP-route shape change.
- **`web/src/components/workflows/CronJobsPage.tsx`** (and any sibling cron-job UI component that consumes `useCronJobs` or renders `pipeline_execution_id`) — replace any `cron.pipeline_execution_id` field reads with `cron.pipeline_run_id`. Audit the file via `rg 'pipeline_execution_id' web/src/components/workflows/CronJobs*.tsx` to surface every render site.
- **`web/src/components/workflows/PipelinesPage.css`** — 7 CSS classes use `pipeline-execution*` selectors (lines 214, 218, 222, 231, 235, 252, 302 at draft-time). Rename to `pipeline-run*`:
  - `.pipeline-execution` → `.pipeline-run`
  - `.pipeline-execution:last-child` → `.pipeline-run:last-child`
  - `.pipeline-execution-header` → `.pipeline-run-header`
  - `.pipeline-execution-header:hover` → `.pipeline-run-header:hover`
  - `.pipeline-execution-info` → `.pipeline-run-info`
  - `.pipeline-execution-meta` → `.pipeline-run-meta`
  - `.pipeline-execution-details` → `.pipeline-run-details`
  Every TSX consumer of these classes (`execution-utils.tsx:68`, `PipelineExecutionsView.tsx:102,105,111,116,128`) must be updated in lockstep — class-name strings in JSX must match the CSS file. CSS BEM modifier `pipeline-execution--${execution.status}` (line 102 in `PipelineExecutionsView.tsx`) → `pipeline-run--${run.status}`.
- Any other file that imports `usePipelineExecutions`, `PipelineExecution`, `PipelineStepExecution`, or references `pipeline_execution_id` — verify completeness via `rg -l 'usePipelineExecutions|PipelineExecution|PipelineStepExecution|parent_execution_id|pipeline_execution_id' web/src/` before declaring the task done; the list above is not authoritative if new web code lands between drafting and implementation.

**File renames** (filesystem level — preserve git history with `git mv`):

| Old path | New path |
|---|---|
| `web/src/hooks/usePipelineExecutions.ts` | `web/src/hooks/usePipelineRuns.ts` |
| `web/src/components/workflows/execution-utils.tsx` | `web/src/components/workflows/run-utils.tsx` |
| `web/src/components/workflows/PipelineExecutionsView.tsx` | `web/src/components/workflows/PipelineRunsView.tsx` |
| `web/src/components/workflows/ReportsPage.tsx` | unchanged path (file stays; internals rename) |
| `web/src/components/workflows/ReportingTab.tsx` | unchanged path (file stays; internals rename) |
| `web/src/components/activity/__tests__/PipelinesTab.test.tsx` | unchanged path |

**Type renames** (in their respective files):

- `interface PipelineExecution` → `interface PipelineRun` (`usePipelineRuns.ts:21–44`). Field `parent_execution_id` → `parent_run_id`.
- `interface PipelineStepExecution` → `interface PipelineStepRun` (`usePipelineRuns.ts:4–19`). Field `execution_id` → `run_id`.
- `interface Filters` (`usePipelineRuns.ts:46–49`) — extend with `offset?: number`. The `pipeline_name` filter → `name` to match the new HTTP query param.
- `interface StepData` (`run-utils.tsx:30–39`): rename internal references.

**Hook changes** (`usePipelineRuns.ts`):

- `usePipelineExecutions` → `usePipelineRuns`. URL changes:
  - List: `${API}/api/pipelines/executions?...` → `${API}/api/pipelines/runs?...`
  - Search: `${API}/api/pipelines/executions/search?...` → `${API}/api/pipelines/runs/search?...`
  - Detail: `${API}/api/pipelines/executions/{id}` (or `/api/pipelines/{id}`) → `${API}/api/pipelines/runs/{id}`
- Response parsing reads `data.runs`, `data.total_count`, `data.limit`, `data.offset`, `data.status_summary` (was `data.executions`).
- Hook returns `{runs, totalCount, limit, offset, statusSummary, isLoading, error}` from the previous `{executions, ...}` shape.

**User-facing prose**: in addition to type/hook/CSS-class renames, the visible labels and toast/error messages must match the new public surface. Concrete sites identified by ripgrep:

- `web/src/components/workflows/ReportsPage.tsx:545` heading text `Pipeline Executions` → `Pipeline Runs`.
- `web/src/components/workflows/ReportsPage.tsx:640` empty-state text `No {subTab === "pipelines" ? "pipeline executions" : "agent runs"}{" "}` → `No {subTab === "pipelines" ? "pipeline runs" : "agent runs"}{" "}`.
- `web/src/components/workflows/PipelineExecutionsView.tsx:93` empty-state text `No pipeline executions{filters.status ? ` with status "${filters.status}"` : ''}` → `No pipeline runs{filters.status ? ` with status "${filters.status}"` : ''}`.
- `web/src/hooks/usePipelineExecutions.ts:71,78` (file is renamed to `usePipelineRuns.ts` per the file-rename table above) error log strings `"Failed to fetch pipeline executions:"` → `"Failed to fetch pipeline runs:"`.

**Component changes**:

- `PipelinesTab.tsx`: `interface PipelinesTabProps` (lines 7–9) — rename any `execution`-typed props. Filter UI: rename `pipeline_name` filter input to `name`. Replace existing pagination UI with a Next/Prev control:

```tsx
<div className='flex items-center gap-2'>
  <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Prev</Button>
  <span>{offset + 1}–{Math.min(offset + limit, totalCount)} of {totalCount}</span>
  <Button disabled={offset + limit >= totalCount} onClick={() => setOffset(offset + limit)}>Next</Button>
</div>
```

- `PipelineRunsView.tsx`: every `execution` reference renamed; URL param reading swaps `executionId` → `runId`.

- `ReportsPage.tsx` and `ReportingTab.tsx`: replace the `usePipelineExecutions` import with `usePipelineRuns`. Replace `PipelineExecutionRecord` (or whatever the imported type is named) with `PipelineRun`. Rename any local `parent_execution_id` field reads to `parent_run_id`. If either page renders an `executionId` URL param or detail link, swap to `runId` / `runs/{id}`. Add or extend Vitest coverage so these two pages are exercised against the new API shape — at minimum, a render test that asserts the component fetches `/api/pipelines/runs`.

**Vitest** (`PipelinesTab.test.tsx`, 133 lines): update mocks for the new `runs` response shape; assert Prev disables at offset 0; assert Next disables when `offset + limit >= total_count`. Update fixture data to use `pr-` IDs and `run_id` keys. Add a test that the URL `${API}/api/pipelines/runs` is what the hook hits (via msw or fetch spy), proving the old `/executions` URL is gone from the client.

**Type-check**: `npm run type-check` (or the web type-check command — verify in `web/package.json`) must pass.

`validation_criteria`: Web client hits `/api/pipelines/runs*` URLs only (verified by network-mock test). Type names `PipelineExecution`, `PipelineStepExecution`, and `PipelineExecutionRecord` no longer exist in `web/src/`. The string `usePipelineExecutions` no longer exists in `web/src/` (verified by ripgrep). Files `usePipelineExecutions.ts`, `execution-utils.tsx`, and `PipelineExecutionsView.tsx` are removed (renamed via git mv). `ReportsPage.tsx` and `ReportingTab.tsx` import `usePipelineRuns` and contain no `parent_execution_id` references. Next/Prev pagination renders the correct page indicator. `npm run type-check` (or the web type-check script declared in `web/package.json`) and `npx vitest run web/` pass.

---

## Phase 6: Templates, Skills, Active Docs (depends: Phase 3)

**Goal**: Update YAML pipelines/agents/rules, gobby skills referencing the renamed tools, and active guides — so a fresh installation, on first sync, produces no execution-named references.

### 6.1 Update installed YAML templates [category: refactor]

Target: `src/gobby/install/shared/workflows/pipelines/*.yaml`, `src/gobby/install/shared/workflows/agents/*.yaml`, `src/gobby/install/shared/workflows/rules/**/*.yaml`

Files identified by the explore (11 direct refs):

- `src/gobby/install/shared/workflows/pipelines/orchestrator.yaml` — `reentry_check.output.executions` → `reentry_check.output.runs` (line ~45). Audit other `${steps.*.output.*}` references in this file for `executions` segments.
- `src/gobby/install/shared/workflows/pipelines/dev-orchestrator.yaml` — same audit.
- `src/gobby/install/shared/workflows/pipelines/delivery-orchestrator.yaml` — same.
- `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml` — same.
- `src/gobby/install/shared/workflows/pipelines/nightly-fixes.yaml` — same.
- `src/gobby/install/shared/workflows/pipelines/nightly-memory-cleanup.yaml` — same.
- `src/gobby/install/shared/workflows/agents/pipeline-worker.yaml` — agent prompt + tool list updates: `get_pipeline_status` → `get_pipeline_run`, `list_pipeline_executions` → `list_pipeline_runs`, `search_pipeline_executions` → `search_pipeline_runs`.
- `src/gobby/install/shared/workflows/agents/conductor.yaml` — same audit.
- `src/gobby/install/shared/workflows/rules/pipeline-enforcement/auto-run-pipeline.yaml` — match-on conditions and injected context strings updated.

**Substitution rules**:

- Tool-name strings (`tool: get_pipeline_status` etc.): rename per the table in 1.1 (the `definition_json` migration applies the same substitutions to live DB rows).
- Step output paths (`${steps.foo.output.executions}` and `${steps.foo.output.executions[0].id}`): rename `executions` segment to `runs`.
- Output keys at the pipeline/step level that the YAML emits as `executions`: rename to `runs`.
- Variable names referencing `execution_id` → `run_id` in YAML expressions.
- **Argument key `pipeline_name` → `name`** when the surrounding step's `tool` is one of `list_pipeline_executions`, `list_pipeline_runs`, `search_pipeline_executions`, or `search_pipeline_runs`. The 6 bundled pipeline YAMLs (`orchestrator.yaml:41`, `dev-orchestrator.yaml:35`, `delivery-orchestrator.yaml:51`, `front-half-orchestrator.yaml:44`, `nightly-fixes.yaml:16`, `nightly-memory-cleanup.yaml:28`) each have `arguments.pipeline_name: "..."` in a reentry_check step that lists prior runs of the same pipeline; rename the **key** to `name`, preserve the value verbatim. Do **not** rewrite `pipeline_name` keys in unrelated contexts (top-level pipeline definition, output records, stored run-record fields, user-authored description text).
- **User-facing prose**: rewrite execution-noun mentions in agent prompts and step messages to use run terminology consistently with the public surface:
  - `pipeline-worker.yaml`: `"pipeline execution worker"` → `"pipeline run worker"`; `"Follow the pipeline execution steps exactly"` → `"Follow the pipeline run steps exactly"`; the `## Pipeline Execution` heading → `## Pipeline Run`; the file's top-line `description: Minimal agent restricted to pipeline execution via inline step enforcement` → `... pipeline runs via inline step enforcement`.
  - `conductor.yaml`: `"Review completed pipeline executions when useful"` → `"Review completed pipeline runs when useful"`.
  - `auto-run-pipeline.yaml`: `## Pipeline Execution Mode` → `## Pipeline Run Mode`; `"You are a pipeline execution agent"` → `"You are a pipeline run agent"`.
  - The 6 pipelines that emit `"Skipped: another <name> execution is already running"` (e.g., `orchestrator.yaml:50`): rewrite each to `"Skipped: another <name> run is already in progress"` — the noun changes from `execution` to `run`, and the verb phrase from `is already running` to `is already in progress` to avoid a noun/verb collision (`run is already running` reads awkwardly).

**Bump `version` field on each updated YAML** so the install-time sync (`gobby install`) detects drift via hash comparison and re-installs the new content. Note: bundled-content sync is install-time only — the daemon does not auto-sync on startup outside dev mode. For existing user upgrades that don't run `gobby install`, the migration 221 prose rewrite in §1.1 covers installed `workflow_definitions` rows directly. Two paths: install-time sync for fresh installs and re-installs; migration 221 for in-place upgrades.

`validation_criteria`: ripgrep across `src/gobby/install/shared/workflows/` for the literals `pipeline_executions`, `step_executions`, `parent_execution_id`, `execution_id`, `output.executions`, `get_pipeline_status`, `list_pipeline_executions`, `search_pipeline_executions` returns zero matches. Each updated YAML has its `version` field bumped. The bundled-content sync test (or manual re-init of `~/.gobby/gobby-hub.db`) confirms updated definitions install cleanly.

### 6.2 Update gobby skills [category: refactor]

Target: skills under `src/gobby/install/shared/skills/` that reference pipeline execution tools

Audit (ripgrep `pipeline_execution|get_pipeline_status|list_pipeline_executions|search_pipeline_executions|execution_id|gobby pipelines (history|status|list-runs|search)` across `src/gobby/install/shared/skills/`):

- `plan/SKILL.md` — likely references the `expand-task` pipeline; if it documents pipeline execution outputs, update.
- `expand/SKILL.md` (or whichever skill drives `/gobby expand`) — references the `expand-task` pipeline; rename any execution-noun mentions.
- `pipeline-debug/SKILL.md` (if it exists) — heaviest user; rename tool names and example responses.
- **`orchestrate/SKILL.md`** — line 599 has the comment `# Pipeline execution history` → rewrite to `# Pipeline run history`.
- **`test-battery/SKILL.md`** — line 384 has the same comment `# Pipeline execution history` → rewrite to `# Pipeline run history`.
- Any skill with a pipeline tool example.

**User-facing prose rule**: in addition to tool-name and structured-key rewrites, replace human-readable mentions of "pipeline execution(s)" with "pipeline run(s)" wherever they appear in skill prose (markdown body, code comments shown to users, example outputs). Be conservative around code blocks that demonstrate the *prior* CLI surface (those should be updated to the new commands per §6.3); do not mass-substitute the substring `execution` in non-pipeline contexts.

For each skill: bump the `version:` field in the SKILL.md frontmatter so `sync_bundled_content_to_db` re-installs.

`validation_criteria`: ripgrep across `src/gobby/install/shared/skills/` for the same literal set as 6.1 returns zero matches. Each touched skill has its `version` bumped.

### 6.3 Update active docs [category: docs]

Targets (all files have live execution-named refs, verified by `rg`): `docs/guides/pipelines.md` (290), `docs/guides/workflows-overview.md` (156), `docs/guides/mcp-tools.md` (841), `docs/guides/cli-commands.md` (1,423 — pre-existing monolith, **#12947 covers split**, scope here is rename-only edits). **Active agent docs**: `CLAUDE.md` (root, line ~83 has `gobby pipelines status`), `AGENTS.md` (root sibling, audit), `GEMINI.md` (if present, audit), `src/gobby/workflows/CLAUDE.md` (lines 72-73 list old `pipeline_executions`/`step_executions` tables in the DB-tables section — rewrite to `pipeline_runs`/`pipeline_step_runs`). Audit nested `src/gobby/**/CLAUDE.md` for any other pipeline-execution refs via `rg -n 'pipeline_executions|step_executions|gobby pipelines (history|status|list-runs|search)|(?i)\bpipeline executions?\b' CLAUDE.md AGENTS.md GEMINI.md src/gobby/`.

**Substitution rules** (apply across all four files): tool names per §1.1 table (`get_pipeline_status` → `get_pipeline_run`, etc.); URL examples (`/api/pipelines/executions` → `/api/pipelines/runs`); CLI examples (`gobby pipelines history`/`status <ID>`/`list-runs`/`search` → `gobby pipelines runs list --name <NAME>`/`runs show <ID>`/`runs list`/`runs search`); response-key `execution_id` → `run_id`; prose `(?i)\bpipeline executions?\b` → `pipeline run(s)`; pagination shape (`total_count`, `limit`, `offset`, `status_summary`) added to example responses in `pipelines.md` and `mcp-tools.md`. **In `cli-commands.md`**: delete the obsolete `gobby pipelines history` section entirely; rewrite the `search` section to `runs search QUERY`; do not restructure beyond rename edits. **Allowlist**: `PipelineExecutor.execute` implementation-verb mentions; archived plans under `docs/plans/completed/`.

`validation_criteria`: `rg -n 'get_pipeline_status|list_pipeline_executions|search_pipeline_executions|/api/pipelines/executions|gobby pipelines (history|status|list-runs|search)\b|\bexecution_id\b|(?i)\bpipeline executions?\b' docs/guides/` returns zero matches outside `PipelineExecutor.execute` allowlist contexts. Example responses in `pipelines.md` and `mcp-tools.md` include the new pagination shape.

---

## Phase 7: Drift Sweep (depends: Phase 4, Phase 5, Phase 6)

**Goal**: A real test that fails if any execution-named user-facing surface re-enters the codebase.

### 7.1 Add drift-sweep regression guard [category: code]

Target: new file `tests/test_pipeline_runs_drift_sweep.py`

Static-analysis regression guard, not application-code coverage — the file ripgreps the source tree for forbidden execution literals on user-facing surfaces and fails CI if any reappear. TDD wrapper expansion produces a meta-test of the allowlist (seed a forbidden literal in a non-allowlisted file → guard catches; seed in an allowlisted path → guard exempts).

**Implementation** (single quotes throughout to keep the file self-consistent; equivalent to double-quoted Python):

```python
'''Drift sweep: verify pipeline-execution terminology has been fully removed
from user-facing surfaces. New occurrences of the old terms in tracked
locations indicate a regression.'''

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# (label, paths, forbidden patterns)
FORBIDDEN_SURFACES = [
    (
        'http_routes',
        ['src/gobby/servers/routes/'],
        [r'\bpipeline_executions\b', r'\bstep_executions\b',
         r'\bexecution_id\b', r'\bparent_execution_id\b',
         r'/executions\b',
         r'(?i)\bpipeline executions?\b'],
    ),
    (
        'mcp_tools',
        ['src/gobby/mcp_proxy/tools/', 'src/gobby/mcp_proxy/stdio.py'],
        [r'\bget_pipeline_status\b',
         r'\blist_pipeline_executions\b',
         r'\bsearch_pipeline_executions\b',
         r'\bexecution_id\b',
         r'(?i)\bpipeline executions?\b'],
    ),
    (
        'cli',
        ['src/gobby/cli/pipelines.py'],
        [r'\bpipeline_executions\b', r'\bstep_executions\b',
         r'\blist-runs\b', r'\bhistory_pipeline\b',
         r'(?i)\bpipeline executions?\b',
         r'\bexecution_id\b', r'\bparent_execution_id\b',
         r'\bget_execution_manager\b', r'\bexecution_manager\b',
         r'\bLocalPipelineExecutionManager\b', r'\bPipelineExecution\b'],
    ),
    (
        'config_prose',
        ['src/gobby/config/app.py', 'src/gobby/config/pipelines.py'],
        [r'(?i)\bpipeline executions?\b'],
    ),
    (
        'conductor_instructions',
        ['src/gobby/conductor/'],
        [r'\bget_pipeline_status\b',
         r'\blist_pipeline_executions\b',
         r'\bsearch_pipeline_executions\b',
         r'\bexecution_id\b', r'\bparent_execution_id\b'],
    ),
    (
        'runner_lifecycle',
        ['src/gobby/runner_lifecycle.py',
         'src/gobby/runner_maintenance.py',
         'src/gobby/workflows/pipeline/gatekeeper.py'],
        [r'\bpipeline_execution_manager\b',
         r'\bexecution_id\b', r'\bparent_execution_id\b',
         r'\bfail_stale_running_executions\b',
         r'\blist_executions\b',
         r'\bupdate_step_execution\b',
         r'\bupdate_execution_status\b'],
    ),
    (
        'session_resumability',
        ['src/gobby/servers/routes/sessions/core.py',
         'src/gobby/servers/websocket/handlers/session_observe.py'],
        [r'\bpipeline_executions\b', r'\bstep_executions\b',
         r'\bexecution_id\b'],
    ),
    (
        'wake_metadata',
        ['src/gobby/events/wake.py',
         'src/gobby/events/completion_registry.py',
         'src/gobby/storage/inter_session_messages.py'],
        [r'\bexecution_id\b'],
    ),
    (
        'broadcast_payload',
        ['src/gobby/app_context.py',
         'src/gobby/runner_broadcasting.py',
         'src/gobby/servers/websocket/broadcast.py'],
        [r'\bexecution_id\b', r'\bparent_execution_id\b',
         r'\bPipelineExecution\b', r'\bStepExecution\b'],
    ),
    (
        'cron_storage',
        ['src/gobby/storage/cron.py', 'src/gobby/storage/cron_models.py'],
        [r'\bpipeline_execution_id\b'],
    ),
    (
        'web',
        ['web/src/'],
        [r'PipelineExecution\b', r'PipelineStepExecution\b',
         r'usePipelineExecutions\b', r'/api/pipelines/executions',
         r'\bpipeline_execution_id\b', r'\bparent_execution_id\b',
         r'\bpipeline-execution\b',
         r'(?i)\bpipeline executions?\b'],
    ),
    (
        'installed_yaml',
        ['src/gobby/install/shared/workflows/'],
        [r'\bpipeline_executions\b', r'\bstep_executions\b',
         r'\bget_pipeline_status\b',
         r'\blist_pipeline_executions\b',
         r'\bsearch_pipeline_executions\b',
         r'\.executions\b',
         r'(?i)\bpipeline executions?\b',
         r'\bexecution is already running\b',
         r'(?i)\bpipeline execution worker\b',
         r'(?i)\bpipeline execution agent\b',
         r'(?i)\bpipeline execution mode\b'],
    ),
    (
        'skills',
        ['src/gobby/install/shared/skills/'],
        [r'\bget_pipeline_status\b',
         r'\blist_pipeline_executions\b',
         r'\bsearch_pipeline_executions\b',
         r'(?i)\bpipeline executions?\b',
         r'\bgobby pipelines (history|status|list-runs|search)\b', r'\bexecution_id\b', r'/api/pipelines/executions'],
    ),
    (
        'active_docs',
        ['docs/guides/', 'CLAUDE.md', 'AGENTS.md', 'GEMINI.md', 'src/gobby/workflows/CLAUDE.md'],
        [r'\bget_pipeline_status\b',
         r'\blist_pipeline_executions\b',
         r'\bsearch_pipeline_executions\b',
         r'/api/pipelines/executions',
         r'(?i)\bpipeline executions?\b',
         r'\bgobby pipelines (history|status|list-runs|search)\b',
         r'\bexecution_id\b',
         r'\bpipeline_executions\b', r'\bstep_executions\b'],
    ),
]

# Allowlist: migrations, archived plans, the drift test itself, plan files.
ALLOWLIST_PATHS = [
    re.compile(r'src/gobby/storage/_migration_registry\.py$'),
    re.compile(r'src/gobby/storage/migrations/.*\.py$'),
    re.compile(r'tests/test_pipeline_runs_drift_sweep\.py$'),
    re.compile(r'docs/plans/completed/.*'),
    re.compile(r'docs/plans/.*'),
    re.compile(r'\.gobby/plans/.*'),
]


def _ripgrep(pattern: str, paths: list[str]) -> list[tuple[str, int, str]]:
    cmd = ['rg', '--no-heading', '--line-number', pattern, *paths]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode == 1:  # no match
        return []
    if result.returncode != 0:
        raise RuntimeError(f'ripgrep failed: {result.stderr}')
    matches = []
    for line in result.stdout.splitlines():
        path, line_no, content = line.split(':', 2)
        matches.append((path, int(line_no), content))
    return matches


def _is_allowlisted(path: str) -> bool:
    return any(p.search(path) for p in ALLOWLIST_PATHS)


@pytest.mark.parametrize(
    'label,paths,patterns',
    FORBIDDEN_SURFACES,
    ids=[s[0] for s in FORBIDDEN_SURFACES],
)
def test_no_execution_drift(label, paths, patterns):
    '''Surface must contain none of the forbidden execution-naming patterns.'''
    violations = []
    for pattern in patterns:
        for match_path, line_no, content in _ripgrep(pattern, paths):
            if _is_allowlisted(match_path):
                continue
            violations.append(f'{match_path}:{line_no}: {content.strip()}')
    assert not violations, (
        f'Drift detected on surface {label!r}:\n' + '\n'.join(violations)
    )
```

`validation_criteria`: `tests/test_pipeline_runs_drift_sweep.py` exists and passes (zero violations across all parametrized surfaces). The test reports `file:line:content` on failure. Allowlist exempts migration code and archived docs (verified by a meta-test that seeds a forbidden literal in a migration file and asserts the guard still passes). Test depends on `rg` on `$PATH` (already a dev dependency).

---

## Task Mapping

<!-- Updated after task creation by /gobby expand -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| Phase 1.1 Migration 221 | TBD | Pending |
| Phase 1.2 Baseline schema v221 | TBD | Pending |
| Phase 1.3 Storage models/manager rename | TBD | Pending |
| Phase 1.4 Pagination plumbing | TBD | Pending |
| Phase 2.1 Executor internals | TBD | Pending |
| Phase 2.1a WebSocket/broadcast/app-context payload | TBD | Pending |
| Phase 2.1b Gatekeeper approval-state internals | TBD | Pending |
| Phase 2.2 Webhook payloads | TBD | Pending |
| Phase 2.3 Cron + scheduler linkage (incl. storage/cron.py) | TBD | Pending |
| Phase 2.4 Heartbeat/conductor (incl. manager.py)/health/telemetry | TBD | Pending |
| Phase 2.5 Runner restart lifecycle + stale-approval expiration | TBD | Pending |
| Phase 2.6 Wake metadata + completion-notification dedupe | TBD | Pending |
| Phase 2.7 Service-container + runner-attribute wiring | TBD | Pending |
| Phase 3.1 HTTP routes | TBD | Pending |
| Phase 3.1a Session-resumability SQL | TBD | Pending |
| Phase 3.2 MCP tools | TBD | Pending |
| Phase 4.1 CLI | TBD | Pending |
| Phase 5.1 Web | TBD | Pending |
| Phase 6.1 Installed YAML | TBD | Pending |
| Phase 6.2 Skills | TBD | Pending |
| Phase 6.3 Active docs | TBD | Pending |
| Phase 7.1 Drift sweep | TBD | Pending |

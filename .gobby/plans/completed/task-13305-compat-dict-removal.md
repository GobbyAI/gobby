# Remove vestigial compat sub-dict from Task serialization

## Overview

`Task.to_dict()`, `Task.to_brief()`, and the JSONL exporter all emit a redundant `compat: {status, assignee}` block. Both keys are *also* emitted at the top level of the same dict, so `compat` is pure duplication — a holdover from the migration that demoted top-level `status`/`assignee` in favor of the richer `state` block (`lifecycle_stage`, `owner_session_id`, `is_claimed`) and then re-added them as legacy duplicates while leaving `compat` behind as the bridge. Drop the sub-dict from every emit site, drop the unreachable `compat.get("status")` fallback in the JSONL importer, and update the tests that pin its presence.

## Constraints

- **DB is source of truth; JSONL is export/import-only.** Verified: `TaskSyncManager.import_from_jsonl()` is the sole reader of `.gobby/tasks.jsonl` in the entire codebase. There is no auto-import on daemon startup, no file watcher, no scheduler that hydrates from JSONL. Export runs manually via `gobby tasks sync --export` and in the pre-push hook (`src/gobby/hooks/git/pre-push`); import runs only via manual `gobby tasks sync --import`. The design intent is documented at `src/gobby/runner_init.py:423`: *"JSONL files are backup/export artifacts. Reads are explicit only via CLI/MCP."* Therefore the `compat.get("status")` fallback in `import_from_jsonl` is provably dead for any JSONL produced by current code — every modern export emits top-level `status` alongside the soon-to-be-removed `compat` block.
- **Accepted edge case.** Importing a `.gobby/tasks.jsonl` produced by a Gobby version old enough to have emitted `compat.status` *without* top-level `status` (a brief migration window) would no longer preserve the original status — affected rows would fall through to the `"open"` default. User has accepted this risk.
- **Wait for epic #13175 to merge before execution.** That epic touches the same files (`storage/tasks/_models.py`, `tests/servers/routes/test_tasks_routes.py`, `tests/mcp_proxy/tools/test_tasks_crud_coverage.py`) — landing this on top minimizes conflicts and keeps each diff cleanly scoped.
- **Top-level `status` and `assignee` stay.** They are the legacy fields callers actually use; demoting those is out of scope.
- **No new tests.** This is pure deletion; the existing assertions just need to stop pinning `compat`'s presence.

## Phase 1: Cleanup

**Goal**: Delete every `compat` emit and read site, then trim the test assertions that guarded its presence — leaving the wire format unchanged except for the removed duplicate sub-dict.

### 1.1 Remove compat sub-dict from all serialization sites and tests [category: refactor]

Targets:
- `src/gobby/storage/tasks/_models.py`
- `src/gobby/sync/tasks.py`
- `tests/tasks/test_sync_tasks.py`
- `tests/cli/test_tasks_cli.py`
- `tests/mcp_proxy/tools/test_tasks_crud_coverage.py`
- `tests/servers/routes/test_tasks_routes.py`

#### Production changes (4 sites, ~12 lines deleted)

**`src/gobby/storage/tasks/_models.py`** — `Task.to_dict()` around line 309 and `Task.to_brief()` around line 373. Both emit:

```python
"compat": {
    "status": self.status,
    "assignee": self.assignee,
},
```

Delete both blocks. The top-level `"status": self.status,` and `"assignee": self.assignee,` keys (already present in both dicts) cover every consumer.

**`src/gobby/sync/tasks.py`** — `export_to_jsonl()` around line 152 emits the same block in `task_dict`. Delete it; top-level `status` is already in the same dict.

**`src/gobby/sync/tasks.py`** — `import_from_jsonl()` around line 338 has:

```python
compat = data.get("compat") or {}
# ... a few lines down ...
legacy_status = data.get("status") or compat.get("status") or "open"
```

Delete the `compat = data.get("compat") or {}` line. Shorten the chain to:

```python
legacy_status = data.get("status") or "open"
```

The `compat.get("assignee")` reference does not exist in `import_from_jsonl` (verified by grep) — only `compat.get("status")`. No further import-side cleanup.

#### Test changes (5 files, 9 references)

**`tests/tasks/test_sync_tasks.py`** — three sites:
- Line ~56: `assert "compat" in task1_data` → delete.
- Line ~63: `assert task2_data["compat"]["status"] == "open"` → rewrite as `assert task2_data["status"] == "open"`.
- Line ~395: a fixture dict literal with a `"compat": {...}` key → delete the key and its sub-dict. The surrounding test should still pass because top-level `status`/`assignee` are populated alongside it; if the fixture's purpose was *specifically* to test the compat fallback path in `import_from_jsonl`, delete the entire test (since that path no longer exists). Inspect the test name and assertion to decide.

**`tests/cli/test_tasks_cli.py`** — line ~87: a fixture dict has `"compat": {"status": "open", ...}`. Delete the key.

**`tests/mcp_proxy/tools/test_tasks_crud_coverage.py`** — lines ~33 and ~131: `assert "compat" in result`. Delete both.

**`tests/servers/routes/test_tasks_routes.py`** — lines ~125, ~243, ~297: `assert "compat" in task` / `assert "compat" in data`. Delete all three.

#### Verification (run after edits, before commit)

1. **Grep proof** — must return zero hits in `src/` and `tests/`:
   ```bash
   grep -rn -F '"compat"' src/ tests/ | grep -v '.pyc' | grep -v 'install/shared/setup/setup.mjs'
   ```
   The `setup.mjs` hit is a YAML library internal — unrelated to this work.

2. **Targeted tests** — these five files cover every changed surface:
   ```bash
   uv run pytest tests/tasks/test_sync_tasks.py tests/cli/test_tasks_cli.py \
                 tests/mcp_proxy/tools/test_tasks_crud_coverage.py \
                 tests/servers/routes/test_tasks_routes.py -v
   ```

3. **Lint and type** on the two production files:
   ```bash
   uv run ruff check src/gobby/storage/tasks/_models.py src/gobby/sync/tasks.py
   uv run mypy src/gobby/storage/tasks/_models.py src/gobby/sync/tasks.py
   ```

4. **Smoke** — round-trip a task through the daemon to confirm `compat` is absent from real responses:
   ```bash
   uv run gobby restart
   # Via MCP, call gobby-tasks/get_task with brief=true and brief=false on any existing task.
   # Inspect the result dict — confirm no `compat` key at any level.
   uv run gobby tasks export
   # Inspect .gobby/tasks.jsonl — confirm no compat field on any line.
   uv run gobby tasks import
   # Round-trip should succeed against the freshly exported JSONL.
   ```

## Task Mapping

<!-- Updated after task creation by /gobby expand -->

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| 1.1 Remove compat sub-dict from all serialization sites and tests | — | pending |

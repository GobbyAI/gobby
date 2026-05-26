# Plan: Personal Workspace for Project-Optional Tasks

**Status:** Completed
**Created:** 2025-01-29

## Summary

Enable tasks and sessions to work without project context by using a well-known `_personal` project UUID, following the existing pattern for `_orphaned` and `_migrated` projects.

## Problem

Tasks and sessions require `project_id`. When Claude Code runs outside a project directory, Gobby auto-creates `.gobby/project.json` (polluting the directory). We need tasks to work without project context while remaining cloud-sync ready.

## Solution: Well-Known Personal Project

Use the existing special project pattern:

```sql
-- Already exist in migrations.py:71-75
'00000000-0000-0000-0000-000000000000' → '_orphaned'
'00000000-0000-0000-0000-000000000001' → '_migrated'

-- Add this
'00000000-0000-0000-0000-000000060887' → '_personal'
```

**Behavior:**

- When no project context → tasks/sessions use `_personal` project
- `#N` references work as-is (scoped to personal project)
- Sessions work naturally (has project_id)
- No sync file needed for personal tasks (they're in the hub database)

## Cloud Sync Model

```text
Machine A                    Cloud                      Machine B
──────────                   ─────                      ──────────
_personal (00..02)           User's Personal            _personal (00..02)
├── Task uuid-aaa (#1) ──►   Workspace        ◄──────── Task uuid-bbb (#1)
└── Task uuid-ccc (#2)       ├── uuid-aaa              └── Task uuid-ddd (#2)
                             ├── uuid-bbb
                             ├── uuid-ccc
                             └── uuid-ddd
```

- **UUIDs are the sync key**, not seq_num
- `seq_num` (`#N`) is local convenience only
- Well-known project UUID maps to cloud user's personal workspace

## Implementation Steps

### 1. Add Personal Project Row

Add `_personal` project in migration.

- File: `src/gobby/storage/migrations.py`
- Add: `INSERT INTO projects VALUES ('00000000-0000-0000-0000-000000000002', '_personal', ...)`

### 2. Add Helper Constant

Define `PERSONAL_PROJECT_ID` constant for use across codebase.

- File: `src/gobby/storage/projects.py`
- Add: `PERSONAL_PROJECT_ID = "00000000-0000-0000-0000-000000000002"`

### 3. Update Task Creation

Fall back to personal project instead of auto-initializing a project.

- File: `src/gobby/mcp_proxy/tools/tasks/_crud.py:71-77`
- Change: When `get_project_context()` returns None, use `PERSONAL_PROJECT_ID` instead of calling `initialize_project()`

### 4. Update Session Registration

Use personal project when no project context.

- File: `src/gobby/mcp_proxy/tools/sessions.py` (session tools)
- Ensure sessions can register against personal project

### 5. Update List Tasks

Add `personal` filter option.

- File: `src/gobby/mcp_proxy/tools/tasks/_crud.py`
- Add parameter: `personal: bool = False` to filter for personal project tasks

### 6. CLI Updates (Optional)

Add `--personal` flag to task commands.

- Files: `src/gobby/cli/tasks/`

## Files to Modify

| File | Change |
| :--- | :--- |
| `src/gobby/storage/migrations.py` | Add `_personal` project row |
| `src/gobby/storage/projects.py` | Add `PERSONAL_PROJECT_ID` constant |
| `src/gobby/mcp_proxy/tools/tasks/_crud.py` | Fall back to personal project, add `personal` filter |
| `src/gobby/mcp_proxy/tools/sessions.py` | Handle personal project context |

## Verification

1. **Restart daemon** to run migration: `gobby restart`
2. **Test outside project context:**

   ```bash
   cd /tmp
   # Create task via MCP tool
   create_task(title="Personal task test")
   ```

3. **Verify task in personal project:**

   ```bash
   list_tasks(personal=True)
   # Should show task with project_id = 00000000-...-000000000002
   ```

4. **Verify existing project tasks unaffected:**

   ```bash
   cd ~/Projects/gobby
   list_tasks()  # Should show project tasks, not personal
   ```
5. **Run tests:** `uv run pytest tests/storage/ -v -k task`

## Future: Cloud Sync Prep

When building cloud sync, add:

```sql
ALTER TABLE tasks ADD COLUMN cloud_id TEXT;
ALTER TABLE tasks ADD COLUMN sync_status TEXT DEFAULT 'local';
ALTER TABLE tasks ADD COLUMN last_synced_at TEXT;
```

Not needed now - UUIDs already provide sync identity.

## Design Decisions

1. **Well-known UUID vs machine-specific IDs**: Chose well-known UUID for simplicity. Each machine has its own PostgreSQL database, so personal workspace is inherently machine-scoped. Cloud sync maps the well-known UUID to the authenticated user's personal workspace.

2. **No schema changes**: The `_personal` project is just a row in the existing `projects` table, following the `_orphaned`/`_migrated` pattern.

3. **Future evolution to Workspaces**: When team collaboration is needed, can introduce a Workspace layer above projects. The personal workspace concept maps cleanly to this future model.

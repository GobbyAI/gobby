# Native Task Tracking System

## Overview

A beads-inspired task tracking system native to gobby, providing agents with persistent task management across sessions. Unlike beads, this integrates directly with gobby's session/project model and supports multi-CLI workflows (Claude Code, Gemini, Codex).

**Inspiration:** <https://github.com/steveyegge/beads>

## Build Order

```text
Phases 1-10: Core task system (self-contained, build first)
    ↓
Workflow Engine (Phases 1-10 in WORKFLOWS.md) - Can be built parallel to Tasks
    ↓
Phases 11-13: Integration + LLM expansion (Requires both Core Tasks and Workflow Engine)
```

The core task system provides immediate value without workflows. Agents can use task MCP tools directly with guidance from CLAUDE.md instructions.

## Core Design Principles

1. **Agent-first** - Tasks are created and managed by agents, not humans
2. **Session-aware** - Tasks link to sessions where they were discovered/worked
3. **Git-distributed** - JSONL export enables sharing via git
4. **Dependency-driven** - Ready work detection surfaces unblocked tasks
5. **Collision-resistant** - Hash-based IDs for multi-agent scenarios

## Data Model

### Tasks Table

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,              -- Human-friendly: gt-xxxxxx (6 chars, collision-resistant)
    project_id TEXT NOT NULL,         -- FK to projects
    platform_id TEXT,                 -- UUID for future platform sync (nullable, populated on sync)
    parent_task_id TEXT,              -- For hierarchical breakdown (gt-a1b2c3.1)
    discovered_in_session_id TEXT,    -- Session where task was discovered
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'open',       -- open, in_progress, closed
    priority INTEGER DEFAULT 2,       -- 1=High, 2=Medium, 3=Low
    task_type TEXT DEFAULT 'task',    -- bug, feature, task, epic, chore
    assignee TEXT,                    -- Agent or human identifier
    labels TEXT,                      -- JSON array
    closed_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id),
    FOREIGN KEY (discovered_in_session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_tasks_project ON tasks(project_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);
CREATE UNIQUE INDEX idx_tasks_platform_id ON tasks(platform_id) WHERE platform_id IS NOT NULL;
```

### Dependencies Table

```sql
CREATE TABLE task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,            -- The task that is blocked/related
    depends_on TEXT NOT NULL,         -- The task it depends on
    dep_type TEXT NOT NULL,           -- blocks, related, discovered-from
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, depends_on, dep_type)
);

CREATE INDEX idx_deps_task ON task_dependencies(task_id);
CREATE INDEX idx_deps_depends_on ON task_dependencies(depends_on);
```

### Session-Task Link Table

```sql
CREATE TABLE session_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,             -- worked_on, discovered, mentioned, closed
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE(session_id, task_id, action)
);

CREATE INDEX idx_session_tasks_session ON session_tasks(session_id);
CREATE INDEX idx_session_tasks_task ON session_tasks(task_id);
```

## Dependency Types

| Type | Behavior | Example |
|------|----------|---------|
| `blocks` | Hard dependency - prevents task from being "ready" | "Fix auth" blocks "Add user profile" |
| `related` | Soft link - informational only | Two tasks touch same code |
| `discovered-from` | Task was found while working on another | Bug found during feature work |

## ID Generation

Tasks use a hash-based ID with optional platform UUID for fleet sync:

- **`id`**: Human-friendly `gt-{6 chars}` (collision-resistant within central DB)
- **`platform_id`**: UUID for future platform sync (nullable, generated on first sync)

### ID Format

IDs use the format `gt-{hash}` where hash is 6 hex characters derived from:

- Timestamp (nanoseconds)
- Random bytes
- Project ID

Hierarchical children use dot notation: `gt-a1b2c3.1`, `gt-a1b2c3.2`

```python
import hashlib
import os
import time

def generate_task_id(project_id: str) -> str:
    """Generate collision-resistant task ID."""
    data = f"{time.time_ns()}{os.urandom(8).hex()}{project_id}"
    hash_hex = hashlib.sha256(data.encode()).hexdigest()[:6]
    return f"gt-{hash_hex}"

def generate_child_id(parent_id: str, child_num: int) -> str:
    """Generate child ID from parent."""
    return f"{parent_id}.{child_num}"

def generate_platform_id() -> str:
    """Generate UUID for platform sync (called lazily on first sync)."""
    import uuid
    return uuid.uuid4().hex
```

### Platform Sync (Future)

When gobby connects to the remote platform for fleet management:

1. Tasks without `platform_id` get a UUID generated on first sync
2. Platform uses `platform_id` for cross-machine references
3. Local tools continue using human-friendly `gt-xxxxxx` IDs
4. JSONL exports include both `id` and `platform_id` for portability

## Ready Work Query

The core insight from beads: surface tasks that have no unresolved `blocks` dependencies.

```sql
SELECT t.* FROM tasks t
WHERE t.project_id = ?
  AND t.status = 'open'
  AND NOT EXISTS (
    SELECT 1 FROM task_dependencies d
    JOIN tasks blocker ON d.depends_on = blocker.id
    WHERE d.task_id = t.id
      AND d.dep_type = 'blocks'
      AND blocker.status != 'closed'
  )
ORDER BY t.priority ASC, t.created_at ASC
LIMIT ?;
```

## Git Sync Architecture

### File Structure

```text
.gobby/
├── tasks.jsonl           # Canonical task data
├── tasks_meta.json       # Sync metadata (last_export, hash)
└── gobby.db              # SQLite cache (not committed)
```

### JSONL Format

Each line is a complete task record with embedded dependencies:

```json
{"id":"gt-a1b2c3","platform_id":null,"project_id":"proj-123","title":"Fix auth bug","status":"open","priority":1,"task_type":"bug","dependencies":[{"depends_on":"gt-x9y8z7","dep_type":"blocks"}],"created_at":"2025-01-15T10:00:00Z","updated_at":"2025-01-15T10:00:00Z"}
```

Note: `platform_id` is null until first platform sync, then contains UUID.

### Sync Behavior

**Export (SQLite → JSONL):**

- Triggered after task mutations (create/update/delete)
- 5-second debounce to batch rapid changes
- Writes to `.gobby/tasks.jsonl`
- Updates `.gobby/tasks_meta.json` with export timestamp and content hash

**Import (JSONL → SQLite):**

- Triggered on daemon start
- Triggered after `git pull` (via hook or manual)
- Merges JSONL records into SQLite
- Conflict resolution: last-write-wins based on `updated_at`

### Git Hooks (Optional Enhancement)

```bash
# .git/hooks/post-merge
#!/bin/bash
gobby tasks sync --import

# .git/hooks/pre-commit
#!/bin/bash
gobby tasks sync --export
```

## MCP Tools

### Task CRUD

```python
@mcp.tool()
def create_task(
    title: str,
    description: str | None = None,
    priority: int = 2,
    task_type: str = "task",
    parent_task_id: str | None = None,
    blocks: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Create a new task in the current project."""

@mcp.tool()
def get_task(task_id: str) -> dict:
    """Get task details including dependencies."""

@mcp.tool()
def update_task(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Update task fields."""

@mcp.tool()
def close_task(task_id: str, reason: str) -> dict:
    """Close a task with a reason."""

@mcp.tool()
def reopen_task(task_id: str, reason: str | None = None) -> dict:
    """Reopen a closed task with optional reason."""

@mcp.tool()
def delete_task(task_id: str, cascade: bool = False) -> dict:
    """Delete a task. Use cascade=True to delete children."""

@mcp.tool()
def list_tasks(
    status: str | None = None,
    priority: int | None = None,
    task_type: str | None = None,
    assignee: str | None = None,
    label: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 50,
) -> dict:
    """List tasks with optional filters."""
```

### Label Management

```python
@mcp.tool()
def add_label(task_id: str, label: str) -> dict:
    """Add a label to a task (appends to existing labels)."""

@mcp.tool()
def remove_label(task_id: str, label: str) -> dict:
    """Remove a label from a task."""
```

### Dependency Management

```python
@mcp.tool()
def add_dependency(
    task_id: str,
    depends_on: str,
    dep_type: str = "blocks",
) -> dict:
    """Add a dependency between tasks."""

@mcp.tool()
def remove_dependency(task_id: str, depends_on: str) -> dict:
    """Remove a dependency."""

@mcp.tool()
def get_dependency_tree(task_id: str, direction: str = "both") -> dict:
    """Get dependency tree. Direction: blockers, blocking, or both."""

@mcp.tool()
def check_dependency_cycles() -> dict:
    """Detect circular dependencies in the project."""
```

### Ready Work

```python
@mcp.tool()
def list_ready_tasks(
    priority: int | None = None,
    task_type: str | None = None,
    assignee: str | None = None,
    limit: int = 10,
) -> dict:
    """List tasks with no unresolved blocking dependencies."""

@mcp.tool()
def list_blocked_tasks(limit: int = 20) -> dict:
    """List tasks that are blocked and what blocks them."""
```

### Session Integration

```python
@mcp.tool()
def link_task_to_session(
    task_id: str,
    session_id: str | None = None,  # Current session if None
    action: str = "worked_on",
) -> dict:
    """Link a task to a session (worked_on, discovered, mentioned, closed)."""

@mcp.tool()
def get_session_tasks(session_id: str | None = None) -> dict:
    """Get all tasks associated with a session."""

@mcp.tool()
def get_task_sessions(task_id: str) -> dict:
    """Get all sessions that touched a task."""
```

### Git Sync

```python
@mcp.tool()
def sync_tasks(direction: str = "both") -> dict:
    """Sync tasks between SQLite and JSONL. Direction: import, export, or both."""

@mcp.tool()
def get_sync_status() -> dict:
    """Get sync status: last export time, pending changes, conflicts."""
```

### Compaction

```python
@mcp.tool()
def analyze_compaction(days: int = 30) -> dict:
    """Find tasks eligible for compaction (closed longer than threshold)."""

@mcp.tool()
def compact_task(task_id: str, summary: str) -> dict:
    """Apply compaction summary to a task. Preserves title, replaces description."""

@mcp.tool()
def get_compaction_stats() -> dict:
    """Get compaction statistics: candidates, compacted count, space saved."""
```

### Maintenance

```python
@mcp.tool()
def validate_tasks() -> dict:
    """Validate task data integrity (foreign keys, orphans, short_ref uniqueness)."""

@mcp.tool()
def clean_tasks() -> dict:
    """Clean up orphaned dependencies and compact database."""

@mcp.tool()
def import_tasks(file_path: str, format: str = "jsonl") -> dict:
    """Import tasks from file (jsonl or markdown format)."""
```

## CLI Commands

```bash
# Task management
gobby tasks list [--status STATUS] [--priority N] [--ready]
gobby tasks show TASK_ID [--verbose]     # --verbose shows full UUID
gobby tasks create "Title" [-d DESC] [-p PRIORITY] [-t TYPE]
gobby tasks update TASK_ID [--status S] [--priority P]
gobby tasks close TASK_ID --reason "Done"
gobby tasks reopen TASK_ID [--reason "Reason"]
gobby tasks delete TASK_ID [--cascade]

# Dependencies
gobby tasks dep add TASK BLOCKER [--dep-type TYPE]
gobby tasks dep remove TASK BLOCKER
gobby tasks dep tree TASK
gobby tasks dep cycles

# Labels
gobby tasks label add TASK LABEL
gobby tasks label remove TASK LABEL
gobby tasks label list                   # List all labels in project

# Ready work
gobby tasks ready [--limit N]
gobby tasks blocked

# Sync
gobby tasks sync [--import] [--export]
gobby tasks sync --status

# Git hooks
gobby tasks hooks install                # Install pre-commit, post-merge hooks
gobby tasks hooks uninstall

# Compaction (memory decay)
gobby tasks compact --analyze            # List candidates (closed 30+ days)
gobby tasks compact --apply --id TASK --summary FILE
gobby tasks compact --stats

# Maintenance
gobby tasks doctor                       # Validate setup and data integrity
gobby tasks validate                     # Validate JSONL integrity
gobby tasks clean                        # Remove orphaned data, compact DB

# Import
gobby tasks import FILE [--format md|jsonl]
gobby tasks import --from-beads          # Migrate from beads

# Configuration
gobby tasks config --stealth [on|off]    # Toggle stealth mode

# Stats
gobby tasks stats
```

## Implementation Checklist

### Phase 1: Storage Layer (Completed)

- [x] Create database migration for tasks table
- [x] Create database migration for task_dependencies table
- [x] Create database migration for session_tasks table
- [x] Implement ID generation utility
- [x] Create `src/storage/tasks.py` with `LocalTaskManager` class
- [x] Implement `create()` method
- [x] Implement `get()` method
- [x] Implement `update()` method
- [x] Implement `delete()` method with cascade option
- [x] Implement `list()` method with filters
- [x] Implement `close()` method
- [x] Add unit tests for LocalTaskManager

### Phase 2: Dependency Management

- [x] Create `src/storage/task_dependencies.py` with `TaskDependencyManager` class
- [x] Implement `add_dependency()` method
- [x] Implement `remove_dependency()` method
- [x] Implement `get_blockers()` method (what blocks this task)
- [x] Implement `get_blocking()` method (what this task blocks)
- [x] Implement `get_dependency_tree()` method with recursive traversal
- [x] Implement `check_cycles()` using DFS cycle detection
- [x] Add validation to prevent self-dependencies
- [x] Add unit tests for TaskDependencyManager

### Phase 3: Ready Work Detection

- [x] Implement `list_ready_tasks()` query in LocalTaskManager
- [x] Implement `list_blocked_tasks()` query with blocker details
- [x] Add priority-based sorting to ready tasks
- [x] Add assignee filtering to ready tasks
- [x] Add unit tests for ready work queries

### Phase 4: Session Integration

- [x] Create `src/storage/session_tasks.py` with `SessionTaskManager` class
- [x] Implement `link_task()` method
- [x] Implement `unlink_task()` method
- [x] Implement `get_session_tasks()` method
- [x] Implement `get_task_sessions()` method
- [x] Add action type validation (worked_on, discovered, mentioned, closed)
- [x] Update session summary to include task activity
- [x] Add unit tests for SessionTaskManager

### Phase 5: Git Sync - Export

- [x] Create `src/sync/tasks.py` with `TaskSyncManager` class
- [x] Implement JSONL serialization for tasks with embedded dependencies
- [x] Implement `export_to_jsonl()` method
- [x] Implement debounced export (5-second delay)
- [x] Create `.gobby/tasks_meta.json` schema
- [x] Implement content hash calculation for change detection
- [x] Add export trigger after task mutations
- [x] Add unit tests for export functionality

### Phase 6: Git Sync - Import

- [x] Implement JSONL deserialization
- [x] Implement `import_from_jsonl()` method
- [x] Implement last-write-wins conflict resolution
- [x] Handle deleted tasks (tombstone or removal)
- [x] Implement `sync_status()` method
- [x] Add import trigger on daemon start
- [x] Add unit tests for import functionality
- [x] Add integration test for round-trip sync

### Phase 7: MCP Tools (Completed)

- [x] Add `create_task` tool to MCP server
- [x] Add `get_task` tool to MCP server
- [x] Add `update_task` tool to MCP server
- [x] Add `close_task` tool to MCP server
- [x] Add `delete_task` tool to MCP server
- [x] Add `list_tasks` tool to MCP server
- [x] Add `add_dependency` tool to MCP server
- [x] Add `remove_dependency` tool to MCP server
- [x] Add `get_dependency_tree` tool to MCP server
- [x] Add `check_dependency_cycles` tool to MCP server
- [x] Add `list_ready_tasks` tool to MCP server
- [x] Add `list_blocked_tasks` tool to MCP server
- [x] Add `link_task_to_session` tool to MCP server
- [x] Add `get_session_tasks` tool to MCP server
- [x] Add `get_task_sessions` tool to MCP server
- [x] Add `sync_tasks` tool to MCP server
- [x] Add `get_sync_status` tool to MCP server
- [x] Update MCP tool documentation

### Phase 8: CLI Commands (Completed)

- [x] Add `gobby tasks` command group to CLI
- [x] Implement `gobby tasks list` command
- [x] Implement `gobby tasks show` command
- [x] Implement `gobby tasks create` command
- [x] Implement `gobby tasks update` command
- [x] Implement `gobby tasks close` command
- [x] Implement `gobby tasks delete` command
- [x] Implement `gobby tasks dep add` command
- [x] Implement `gobby tasks dep remove` command
- [x] Implement `gobby tasks dep tree` command
- [x] Implement `gobby tasks dep cycles` command
- [x] Implement `gobby tasks ready` command
- [x] Implement `gobby tasks blocked` command
- [x] Implement `gobby tasks sync` command
- [x] Implement `gobby tasks stats` command
- [x] Add CLI help text and examples

### Phase 9: Hook & Git Integration

- [ ] Add task context to session hooks
- [ ] Implement `gobby tasks hooks install` command
- [ ] Create git pre-commit hook (export before commit)
- [ ] Create git post-merge hook (import after pull)
- [ ] Create git post-checkout hook (import on branch switch)
- [ ] Add `gobby install --git-hooks` option for git hook installation
- [ ] Document git hook setup

### Phase 9.5: Compaction (Memory Decay)

> Reduces old closed tasks to summaries, preventing unbounded growth.

- [x] Add `compacted_at` and `summary` columns to tasks table (migration)
- [x] Implement `TaskCompactor` class in `src/tasks/compaction.py`
- [x] Implement `analyze_compaction_candidates()` - find closed tasks older than threshold
- [x] Implement `compact_task()` - replace description with LLM summary, mark compacted
- [x] Implement `get_compaction_stats()` - count candidates, compacted, space saved
- [x] Add `compact_tasks` MCP tool with `--analyze`, `--apply`, `--days` options
- [x] Add `gobby tasks compact` CLI command
  - `gobby tasks compact --analyze` - list candidates
  - `gobby tasks compact --apply --id TASK --summary FILE` - apply summary
  - `gobby tasks compact --stats` - show compaction statistics
- [x] Add configurable threshold (default: 30 days closed)
- [x] Add unit tests for compaction

### Phase 9.6: Label Management

- [x] Add `add_label` MCP tool (append label to existing)
- [x] Add `remove_label` MCP tool (remove specific label)
- [x] Add `gobby tasks label add TASK LABEL` CLI command
- [x] Add `gobby tasks label remove TASK LABEL` CLI command
- [x] Add `gobby tasks label list` CLI command (list all labels in project)

### Phase 9.7: Maintenance Tools

- [x] Implement `TaskValidator` class for data integrity checks
- [x] Add `gobby tasks doctor` CLI command
  - Check database schema version
  - Validate foreign key integrity
  - Check for orphaned dependencies
  - Verify short_ref uniqueness per project
- [x] Add `gobby tasks validate` CLI command (validate JSONL integrity)
- [x] Add `gobby tasks clean` CLI command
  - Remove orphaned dependencies
  - Compact SQLite database
  - Clear stale sync metadata
- [x] Add `validate_tasks` and `clean_tasks` MCP tools

### Phase 9.8: Import Tools

- [x] Implement `TaskImporter` class in `src/tasks/import.py`
- [x] Add markdown import (parse `- [ ] task` format)
- [x] Add bulk JSONL import from file
- [x] Add `import_tasks` MCP tool
- [x] Add `gobby tasks import FILE [--format md|jsonl]` CLI command
- [x] Add `gobby tasks import --from-beads` for beads migration

### Phase 9.9: Stealth Mode

- [x] Add `tasks_stealth` boolean to project config schema
- [x] Update `TaskSyncManager` to check stealth setting
- [x] If stealth: export to `~/.gobby/tasks/{project_id}.jsonl` instead of `.gobby/tasks.jsonl`
- [x] Add `gobby tasks config --stealth [on|off]` CLI command
- [x] Document stealth mode in README

### Phase 10: Documentation & Polish

- [ ] Add tasks section to README
- [ ] Create `docs/tasks.md` with usage guide
- [ ] Add example workflows for agents
- [ ] Add task-related configuration options to `config.yaml`
- [ ] Performance testing with 1000+ tasks
- [ ] Add `gobby tasks` to CLI help output
- [ ] Document fleet-ready architecture (UUID for future platform sync)

## Workflow Engine Integration (Future)

> **Dependency:** Phases 11-13 require the Workflow Engine (see `docs/plans/WORKFLOWS.md`) to be built first. The core task system (Phases 1-10) is self-contained and should be implemented first.

Once workflows exist, the task system integrates with the Workflow Engine to provide persistent task tracking across sessions while workflows handle ephemeral execution state.

### Relationship Model

```text
┌─────────────────────────────────────────────────────────────┐
│                 Workflow Engine (Ephemeral)                 │
│  - Phase state (plan → execute → verify)                    │
│  - Current task index                                       │
│  - Tool restrictions per phase                              │
│  - Transition triggers                                      │
└─────────────────────────┬───────────────────────────────────┘
                          │ persists to / reads from
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Task System (Persistent)                    │
│  - Task records with dependencies                           │
│  - Status tracking (open → in_progress → closed)            │
│  - Session linkage (discovered_in, worked_on)               │
│  - Git sync for cross-machine sharing                       │
└─────────────────────────────────────────────────────────────┘
```

### Integration Points

| Workflow Action | Task System Behavior |
|-----------------|---------------------|
| `call_llm` (decompose) | Creates tasks with `blocks` dependencies based on order |
| `write_todos` | Also persists to tasks table with session linkage |
| `mark_todo_complete` | Updates task status to `closed` |
| `enter_phase(execute)` | Marks current task as `in_progress` |
| `enter_phase(verify)` | Prepares task for status update |
| Workflow handoff | Includes pending task IDs for next session |

### Workflow-Aware Task Creation

When the `plan-to-tasks.yaml` workflow decomposes a plan:

1. LLM generates ordered task list with verification criteria
2. Tasks are persisted to `tasks` table with:
   - `discovered_in_session_id` set to current session
   - Sequential `blocks` dependencies (task 2 blocks on task 1)
   - `verification` stored in description or labels
3. WorkflowState references task IDs (not ephemeral list)
4. On session end, incomplete tasks remain in database
5. Next session can query `list_ready_tasks()` to continue

### Schema Addition for Workflow Link

```sql
-- Add to tasks table
ALTER TABLE tasks ADD COLUMN workflow_name TEXT;        -- Which workflow created this
ALTER TABLE tasks ADD COLUMN verification TEXT;         -- Verification criteria from decomposition
ALTER TABLE tasks ADD COLUMN sequence_order INTEGER;    -- Order within parent epic
```

---

## LLM-Powered Task Expansion

Unlike beads (which is purely a tracking system), gobby provides LLM-powered task decomposition as a first-class feature.

### Expansion Strategies

| Strategy | Description | Use Case |
|----------|-------------|----------|
| `checklist` | Break into sequential subtasks | Simple features |
| `parallel` | Independent subtasks (no dependencies) | Refactoring multiple files |
| `epic` | Create epic with child tasks | Large features |
| `tdd` | Test task → implementation task pairs | Test-driven development |

### MCP Tools for Expansion

```python
@mcp.tool()
def expand_task(
    task_id: str,
    strategy: str = "checklist",  # checklist, parallel, epic, tdd
    max_subtasks: int = 10,
) -> dict:
    """
    Use LLM to decompose a task into subtasks.

    Reads task title/description, generates child tasks with:
    - Appropriate dependencies based on strategy
    - Verification criteria for each subtask
    - Priority inherited from parent

    Returns created subtask IDs.
    """

@mcp.tool()
def expand_from_spec(
    spec_content: str,
    spec_type: str = "prd",  # prd, user_story, bug_report, rfc
    parent_task_id: str | None = None,
    strategy: str = "epic",
) -> dict:
    """
    Parse a specification and generate a task tree.

    For PRDs: Creates epic with feature tasks
    For user stories: Creates acceptance criteria as subtasks
    For bug reports: Creates investigation → fix → verify tasks
    For RFCs: Creates research → design → implement tasks

    Returns root task ID and full task tree.
    """

@mcp.tool()
def suggest_next_task(
    context: str | None = None,
) -> dict:
    """
    LLM analyzes current session context and suggests which ready task to work on.

    Considers:
    - Files already read/modified in session
    - Recent task activity
    - Priority and dependencies

    Returns recommended task with reasoning.
    """
```

### Expansion Prompt Template

```python
EXPAND_TASK_PROMPT = """
Break down this task into atomic, actionable subtasks.

Task: {title}
Description: {description}
Strategy: {strategy}

Requirements:
- Each subtask should be completable in a single focused session
- Each subtask should have clear verification criteria
- Order subtasks by dependency (what must be done first)
- For '{strategy}' strategy: {strategy_guidance}

Output as JSON:
{{
  "subtasks": [
    {{
      "title": "...",
      "description": "...",
      "verification": "How to verify this is complete",
      "depends_on_index": null | 0 | 1 | ...  // Index of blocking subtask
    }}
  ]
}}
"""
```

### Agent Instructions for Task Management

Add to project's `CLAUDE.md` or inject via workflow:

```markdown
## Task Management

This project uses gobby's task system for persistent work tracking.

### When to Create Tasks
- When you discover work items during implementation
- When you identify blockers or prerequisites
- When scope expands beyond the current task
- When you find bugs while working on features

### When to Query Tasks
- At session start: `list_ready_tasks()` to see unblocked work
- Before starting work: `get_task(id)` for context
- When blocked: `list_blocked_tasks()` to understand dependencies

### Task Lifecycle
1. `list_ready_tasks()` → find work
2. `update_task(id, status="in_progress")` → claim it
3. Work on the task
4. `close_task(id, reason="...")` → mark complete
5. File discovered work with `create_task(..., discovered_from=current_task_id)`

### Decomposition
For large tasks, use `expand_task(id)` to break them down before starting.
```

---

## Implementation Checklist Updates

### Phase 11: Workflow Integration

- [ ] Add `workflow_name` column to tasks table (migration)
- [ ] Add `verification` column to tasks table (migration)
- [ ] Add `sequence_order` column to tasks table (migration)
- [ ] Create `src/workflows/task_actions.py` for workflow-task bridge
- [ ] Implement `persist_decomposed_tasks()` action
- [ ] Implement `update_task_from_workflow()` action
- [ ] Implement `get_workflow_tasks()` to retrieve tasks for workflow state
- [ ] Update `plan-to-tasks.yaml` to use persistent tasks
- [ ] Add task IDs to workflow handoff data
- [ ] Update workflow `on_session_start` to load pending tasks
- [ ] Implement ID mapping in `persist_decomposed_tasks` (map temp indices 1,2.. to gt-{hash})
- [ ] Add unit tests for workflow-task integration

### Phase 12: LLM-Powered Expansion

- [ ] Create `src/tasks/expansion.py` with `TaskExpander` class
- [ ] Implement expansion prompt templates per strategy
- [ ] Implement `expand_task()` method
- [ ] Implement `expand_from_spec()` method
- [ ] Implement `suggest_next_task()` method
- [ ] Add `expand_task` MCP tool
- [ ] Add `expand_from_spec` MCP tool
- [ ] Add `suggest_next_task` MCP tool
- [ ] Add `gobby tasks expand TASK_ID [--strategy S]` CLI command
- [ ] Add `gobby tasks import-spec FILE [--type T]` CLI command
- [ ] Add unit tests for TaskExpander
- [ ] Add integration tests with mock LLM

### Phase 13: Agent Instructions

- [ ] Create `templates/task-instructions.md` for CLAUDE.md injection
- [ ] Add `gobby tasks instructions` command to output template
- [ ] Document task management patterns for agents
- [ ] Add examples of discovery-during-work pattern
- [ ] Add examples of decomposition pattern

---

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | **Task ID format** | `gt-{6 chars}` + optional `platform_id` UUID | Human-friendly IDs for local use. UUID generated lazily on platform sync. Minimal refactor now, fleet-ready later. |
| 2 | **Task scope** | Single machine now, fleet-ready | Central DB supports multiple projects. `platform_id` column ready for gobby_platform fleet management. |
| 3 | **Stealth mode** | Store JSONL in `~/.gobby/tasks/{project_id}.jsonl` | Per-project setting to avoid committing tasks to git. |
| 4 | **ID collision handling** | Retry with random salt on collision | SHA-256 with 6 hex chars gives ~16M unique IDs. Collision unlikely, but handle gracefully with retry. |

---

## Future Enhancements

### GitHub Integration (P3)

Sync tasks with GitHub Issues for visibility to non-agent collaborators:

- [ ] Add `github_repo` to project config
- [ ] Implement `GitHubSyncManager` class
- [ ] Two-way sync: gobby tasks ↔ GitHub Issues
- [ ] Map task fields: title, description, status (open/closed), labels, assignee
- [ ] Add `gobby tasks github sync` CLI command
- [ ] Add `gobby tasks github link TASK ISSUE_NUM` for manual linking
- [ ] Handle conflicts (last-write-wins or prompt user)
- [ ] Add `sync_github` MCP tool

### Other Future Enhancements

- **Auto-discovery from transcripts**: LLM extracts tasks from session transcripts
- **Task templates**: Pre-defined task structures for common patterns
- **Task notifications**: WebSocket events when tasks change
- **Multi-project dependencies**: Cross-project task relationships
- **Task search**: Full-text search across title and description
- **Visualization**: Web dashboard for task/dependency visualization
- **JIRA/GitLab integration**: External tracker sync (lower priority than GitHub)
- **gobby_platform**: Remote fleet management for multiple machines

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
    status TEXT DEFAULT 'open',       -- open, in_progress, closed, failed
    priority INTEGER DEFAULT 2,       -- 1=High, 2=Medium, 3=Low
    task_type TEXT DEFAULT 'task',    -- bug, feature, task, epic, chore
    assignee TEXT,                    -- Agent or human identifier
    labels TEXT,                      -- JSON array
    closed_reason TEXT,
    -- Enhanced expansion fields (Phase 12)
    details TEXT,                     -- Implementation notes/guidance
    test_strategy TEXT,               -- How to verify completion
    original_instruction TEXT,        -- Original user request (for validation)
    complexity_score INTEGER,         -- 1-10 complexity rating
    estimated_subtasks INTEGER,       -- Recommended subtask count
    expansion_context TEXT,           -- JSON: gathered context during expansion
    -- Validation fields (Phase 12.5)
    validation_criteria TEXT,         -- Natural language prompt for validating completion
    use_external_validator BOOLEAN DEFAULT FALSE,  -- Use separate validation agent vs completing agent
    validation_fail_count INTEGER DEFAULT 0,       -- Track consecutive validation failures
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

### LLM Expansion (gobby-tasks internal server)

```python
@mcp.tool()
def expand_task(
    task_id: str,
    strategy: str = "checklist",  # checklist, parallel, epic, tdd
    max_subtasks: int = 10,
    analyze_codebase: bool = True,
    infer_validation: bool = True,
) -> dict:
    """
    Use LLM to decompose a task into subtasks with codebase analysis.

    When analyze_codebase=True:
    - Reads relevant files for context
    - Infers which files/modules each subtask will touch
    - Auto-creates dependency relationships based on code structure

    When infer_validation=True:
    - Generates validation_criteria for each subtask

    Returns created subtask IDs with dependencies.
    """

@mcp.tool()
def expand_from_spec(
    spec_content: str,
    spec_type: str = "prd",  # prd, user_story, bug_report, rfc
    parent_task_id: str | None = None,
    strategy: str = "epic",
    analyze_codebase: bool = True,
) -> dict:
    """
    Parse a specification and generate a task tree with dependencies.

    For PRDs: Creates epic with feature tasks
    For user stories: Creates acceptance criteria as subtasks
    For bug reports: Creates investigation → fix → verify tasks
    For RFCs: Creates research → design → implement tasks

    Returns root task ID and full task tree with validation criteria.
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

### Validation (gobby-tasks internal server)

```python
@mcp.tool()
def validate_task(task_id: str) -> dict:
    """
    Validate task completion against its validation_criteria.

    - Gathers context (files changed, test results, etc.)
    - Runs validation prompt against current state
    - If use_external_validator=True, spawns separate validation agent

    On failure:
    - Increments validation_fail_count
    - Creates fix subtask if create_fix_subtask config is true
    - Sets status to 'failed' if max_validation_fails exceeded

    Returns validation result with pass/fail and reasoning.
    """

@mcp.tool()
def get_validation_status(task_id: str) -> dict:
    """
    Get validation status for a task.

    Returns:
    - validation_criteria (if set)
    - use_external_validator setting
    - validation_fail_count
    - Last validation result (if any)
    """

@mcp.tool()
def reset_validation_count(task_id: str) -> dict:
    """Reset validation_fail_count to 0 for manual retry."""
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

# LLM Expansion
gobby tasks expand TASK_ID [--strategy S] [--no-codebase] [--no-validation]
gobby tasks import-spec FILE [--type prd|user_story|bug_report|rfc]
gobby tasks suggest                      # Suggest next task based on context

# Validation
gobby tasks validate TASK_ID             # Run validation against criteria
gobby tasks list --status failed         # List tasks that failed validation
gobby tasks reset-validation TASK_ID     # Reset validation_fail_count for retry

# Configuration
gobby tasks config --stealth [on|off]    # Toggle stealth mode

# Stats
gobby tasks stats
```

## Configuration (config.yaml)

Task expansion and validation use the same LLM provider infrastructure as other gobby features (session summaries, tool recommendations, etc.). Providers can be subscription-based SDKs (Claude Agent SDK, Codex SDK, Gemini SDK) or API-key based (LiteLLM).

```yaml
# Task Expansion Configuration
task_expansion:
  enabled: true
  provider: "claude"                    # claude, codex, gemini, litellm
  model: "claude-sonnet-4-5"            # Higher reasoning model for codebase analysis

  # Context gathering
  analyze_codebase: true                # Read relevant files during expansion
  max_context_files: 20                 # Max files to include for context
  max_file_lines: 200                   # Lines to include per file

  # Expansion behavior
  max_subtasks: 15                      # Max subtasks per expansion
  default_strategy: "phased"            # phased, flat, tdd
  create_dependencies: true             # Auto-create blocks relationships
  infer_validation: true                # Auto-generate validation_criteria for subtasks

  # Research mode (web search for best practices before expansion)
  research_enabled: true                # Enabled by default for quality expansions
  research_provider: "claude"           # Can differ from main provider
  max_search_results: 5

  # Prompts
  system_prompt: |
    You are a senior software architect breaking down development tasks.

    Your goal is to decompose a task into actionable, atomic subtasks that:
    1. Can each be completed in a single focused session
    2. Have clear verification criteria
    3. Are ordered by dependency (what must be done first)
    4. Include implementation guidance

    You have access to:
    - The task description and any context
    - Relevant codebase files
    - Related tasks for reference
    - Web research on best practices

  user_prompt: |
    ## Task to Expand

    **Title:** {title}
    **Description:** {description}
    **Original Instruction:** {original_instruction}

    ## Project Context

    **Relevant Files:**
    {file_context}

    **Related Tasks:**
    {related_tasks}

    **Project Patterns:**
    - Test Framework: {test_framework}
    - Build Tool: {build_tool}
    - Key Directories: {directories}

    {research_section}

    ## Instructions

    Break this task into phases and subtasks.

    For each subtask provide:
    - `title`: Clear, actionable title
    - `description`: What needs to be done
    - `details`: Implementation notes, code snippets, patterns to follow
    - `test_strategy`: How to verify this subtask is complete
    - `depends_on_indices`: Array of subtask indices (0-based) that must complete first
    - `files_touched`: Files this subtask will create or modify

    Output as JSON matching this schema:
    ```json
    {
      "complexity_analysis": {
        "score": <1-10>,
        "reasoning": "<why this complexity>",
        "recommended_subtasks": <count>
      },
      "phases": [
        {
          "name": "<phase name>",
          "description": "<what this phase accomplishes>",
          "subtasks": [
            {
              "title": "...",
              "description": "...",
              "details": "...",
              "test_strategy": "...",
              "depends_on_indices": [],
              "files_touched": []
            }
          ]
        }
      ]
    }
    ```

# Task Validation Configuration
task_validation:
  enabled: true
  provider: "claude"                    # Provider for validation agent
  model: "claude-haiku-4-5"             # Fast model for validation checks
  max_validation_fails: 3               # Mark task 'failed' after this many failures
  create_fix_subtask: true              # Create "fix validation" subtask on failure
  prompt: |
    You are validating that a task has been completed correctly.

    ## Task
    **Title:** {title}
    **Description:** {description}
    **Validation Criteria:** {validation_criteria}

    ## Context
    **Files Changed:**
    {files_changed}

    **Test Results:**
    {test_results}

    ## Instructions
    Evaluate whether the task has been completed according to the validation criteria.
    Be thorough but fair - minor style differences are acceptable if functionality is correct.

    Output JSON:
    ```json
    {
      "passed": true|false,
      "reasoning": "Detailed explanation of pass/fail decision",
      "issues": ["List of specific issues if failed"],
      "suggestions": ["Optional improvement suggestions"]
    }
    ```
```

### Provider Selection

- **claude**: Claude Agent SDK (requires Claude subscription)
- **codex**: OpenAI Codex SDK (requires OpenAI subscription)
- **gemini**: Gemini SDK (requires Google AI subscription)
- **litellm**: Any LLM via API key (configured in `llm_providers.litellm`)

### Validation Behavior

1. When a task has `validation_criteria` set, validation can be triggered:
   - By the completing agent calling `validate_task(task_id)`
   - By workflow automation after task marked closed
   - By external validation agent if `use_external_validator: true`

2. On validation failure:
   - `validation_fail_count` is incremented
   - If `create_fix_subtask: true`, creates a subtask describing what failed
   - If `validation_fail_count >= max_validation_fails`, task status → `failed`

3. `failed` status alerts humans that automated resolution failed

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

Unlike beads (which is purely a tracking system), gobby provides LLM-powered task decomposition as a first-class feature. This is enhanced with context-aware expansion inspired by [claude-task-master](https://github.com/eyaltoledano/claude-task-master).

### Expansion Strategies

| Strategy | Description | Use Case |
|----------|-------------|----------|
| `phased` | Group subtasks into logical phases with dependencies | Complex features (default) |
| `flat` | Simple sequential subtasks | Quick tasks |
| `tdd` | Test task → implementation task pairs | Test-driven development |

### Expansion Architecture

Before expansion, gather context:

1. **Related tasks** - Fuzzy search for similar/related tasks
2. **Codebase context** - Relevant files based on task description
3. **Project structure** - Key directories, patterns, conventions
4. **Research** - Web search for best practices (enabled by default)

```python
@dataclass
class ExpansionContext:
    related_tasks: list[Task]         # Similar tasks for reference
    relevant_files: list[str]         # File paths to analyze
    file_contents: dict[str, str]     # Partial file contents
    project_patterns: dict[str, Any]  # Detected patterns (test framework, etc.)
    search_results: list[dict] | None # Web search results
```

### Expansion Output Schema

```json
{
  "complexity_analysis": {
    "score": 7,
    "reasoning": "Multiple components, API integration, requires testing",
    "recommended_subtasks": 5
  },
  "phases": [
    {
      "name": "Setup & Foundation",
      "description": "Initial setup and core structure",
      "subtasks": [
        {
          "title": "Create game board HTML structure",
          "description": "Set up 4x4 grid with CSS Grid",
          "details": "Use semantic HTML, add ARIA labels for accessibility",
          "test_strategy": "Visual inspection, grid renders correctly",
          "depends_on_indices": [],
          "files_touched": ["index.html", "styles.css"]
        }
      ]
    }
  ]
}
```

### Dependency Mapping

Dependencies are created based on:

1. **Explicit `depends_on_indices`** - LLM specifies which subtasks must complete first
2. **Phase ordering** - Later phases block on earlier phases completing
3. **Parent blocking** - Parent task blocked until all children complete

```python
def create_expansion_dependencies(
    parent_task: Task,
    subtasks: list[Task],
    expansion_result: dict,
) -> list[tuple[str, str]]:
    """
    Returns list of (task_id, blocks_task_id) tuples.
    """
    dependencies = []

    # Map indices to created task IDs
    index_to_id = {i: st.id for i, st in enumerate(subtasks)}

    for phase in expansion_result["phases"]:
        for subtask_data in phase["subtasks"]:
            subtask_idx = subtask_data["_index"]  # Added during processing
            for dep_idx in subtask_data.get("depends_on_indices", []):
                if dep_idx in index_to_id:
                    # dep_idx blocks subtask_idx
                    dependencies.append((
                        index_to_id[subtask_idx],
                        index_to_id[dep_idx]
                    ))

    # All subtasks block the parent task
    for subtask in subtasks:
        dependencies.append((parent_task.id, subtask.id))

    return dependencies
```

### MCP Tools for Expansion

```python
@mcp.tool()
async def expand_task(
    task_id: str,
    num_subtasks: int | None = None,     # Override recommended count
    research: bool = True,                # Enable web research (default: True)
    force: bool = False,                  # Clear existing subtasks
    prompt: str | None = None,            # Additional context/guidance
    analyze_codebase: bool = True,        # Gather file context
) -> dict:
    """
    Expand a task into phases and subtasks using AI analysis.

    Process:
    1. Gather context (related tasks, codebase files)
    2. Research best practices (unless research=False)
    3. Generate phased breakdown with dependencies
    4. Create subtasks with blocks relationships
    5. Update parent task with expansion metadata

    Returns:
        {
            "success": True,
            "task_id": "gt-abc123",
            "complexity": {"score": 7, "reasoning": "..."},
            "phases": [...],
            "subtasks_created": ["gt-def456", "gt-ghi789", ...],
            "dependencies_created": 5
        }
    """

@mcp.tool()
async def analyze_complexity(
    task_id: str | None = None,
    all_pending: bool = False,
) -> dict:
    """
    Analyze task complexity without expanding.

    Returns complexity score, recommended subtask count,
    and expansion guidance for one or all pending tasks.
    """

@mcp.tool()
async def expand_all(
    research: bool = True,
    max_tasks: int = 10,
    min_complexity: int = 5,  # Only expand tasks >= this complexity
) -> dict:
    """
    Expand all pending tasks that haven't been expanded.

    Uses complexity analysis to determine subtask counts.
    Respects dependencies - expands in topological order.
    """

@mcp.tool()
def expand_from_spec(
    spec_content: str,
    spec_type: str = "prd",  # prd, user_story, bug_report, rfc
    parent_task_id: str | None = None,
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

### Example Expansion

**Input Task:**

```text
Title: Create a basic 2048 game
Description: Create a basic 2048 game using HTML and JavaScript
```

**Created Tasks:**

```text
gt-abc123 (parent): Create a basic 2048 game [status: open]
├── gt-def001: Create project directory and base HTML [status: open]
├── gt-def002: Style game board with CSS Grid [blocked by: gt-def001]
├── gt-def003: Implement game state management [blocked by: gt-def001]
├── gt-def004: Implement tile movement logic [blocked by: gt-def003]
├── gt-def005: Add keyboard controls and render loop [blocked by: gt-def002, gt-def004]
└── gt-def006: Implement win/lose detection [blocked by: gt-def005]

Parent gt-abc123 is blocked by all subtasks (can only close when all children complete)
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

### Phase 12: Enhanced Task Expansion System

> Upgrade gobby's task expansion to match [claude-task-master](https://github.com/eyaltoledano/claude-task-master) capabilities.
> Uses configured `task_expansion` provider (Claude Agent SDK, Codex SDK, Gemini SDK, or LiteLLM).
> Tools are registered in `gobby-tasks` internal MCP server.

#### Phase 12.1: Schema Updates

- [ ] Create migration for `details` column
- [ ] Create migration for `test_strategy` column
- [ ] Create migration for `original_instruction` column
- [ ] Create migration for `complexity_score` column
- [ ] Create migration for `estimated_subtasks` column
- [ ] Create migration for `expansion_context` column (JSON)
- [ ] Update `Task` dataclass with new fields
- [ ] Update `to_dict()` and `from_dict()` methods
- [ ] Update JSONL serialization for new fields
- [ ] Add unit tests for schema changes

#### Phase 12.2: Context Gathering

- [ ] Create `src/tasks/context.py` with `ExpansionContextGatherer` class
- [ ] Implement `gather_related_tasks()` - fuzzy search by title/description
- [ ] Implement `gather_relevant_files()` - Glob/Grep based on task keywords
- [ ] Implement `read_file_context()` - partial file contents with line limits
- [ ] Implement `detect_project_patterns()` - test framework, build tool, etc.
- [ ] Create `ExpansionContext` dataclass
- [ ] Add configuration for `max_context_files`, `max_file_lines`
- [ ] Add unit tests for context gathering

#### Phase 12.3: Enhanced Expansion Prompt

- [ ] Create `src/tasks/prompts/expand.py` with prompt templates
- [ ] Load system_prompt and user_prompt from config.yaml
- [ ] Implement context injection (files, related tasks, patterns)
- [ ] Implement research section injection (when enabled)
- [ ] Create JSON schema for expansion output
- [ ] Implement response parsing with validation
- [ ] Handle markdown code block extraction
- [ ] Add fallback for malformed responses
- [ ] Add unit tests for prompt generation and parsing

#### Phase 12.4: Dependency Wiring

- [ ] Update `expand_task()` to parse `depends_on_indices`
- [ ] Implement `create_expansion_dependencies()` helper
- [ ] Create subtasks with proper `parent_task_id`
- [ ] Create `blocks` dependencies between subtasks based on indices
- [ ] Create dependency from parent to all subtasks (parent blocked until children done)
- [ ] Handle phase-level implicit dependencies
- [ ] Run `check_dependency_cycles()` after creation
- [ ] Add transaction rollback on cycle detection
- [ ] Add unit tests for dependency wiring

#### Phase 12.5: Research Mode

- [ ] Implement `research_task()` helper using WebSearch
- [ ] Format search results for prompt injection
- [ ] Cache research results in `expansion_context`
- [ ] Add `--no-research` CLI flag to disable (enabled by default)
- [ ] Add unit tests for research mode

#### Phase 12.6: MCP Tool Updates

- [ ] Update `expand_task` tool with new parameters:
  - `num_subtasks: int | None` - Override recommended count
  - `research: bool = True` - Enable/disable web research
  - `force: bool = False` - Clear existing subtasks
  - `prompt: str | None` - Additional context/guidance
  - `analyze_codebase: bool = True` - Gather file context
- [ ] Add `analyze_complexity` tool - analyze without expanding
- [ ] Add `expand_all` tool - expand all pending tasks
- [ ] Add `expand_from_spec` tool - parse PRD/user story/bug report
- [ ] Add `suggest_next_task` tool - LLM recommends next ready task
- [ ] Update tool schemas for progressive disclosure
- [ ] Add detailed tool documentation

#### Phase 12.7: CLI Updates

- [ ] Update `gobby tasks expand TASK_ID` with new flags:
  - `--num N` - Override subtask count
  - `--no-research` - Disable research mode
  - `--force` - Clear existing subtasks
  - `--prompt "context"` - Additional guidance
- [ ] Add `gobby tasks complexity TASK_ID` command
- [ ] Add `gobby tasks complexity --all --pending` command
- [ ] Add `gobby tasks expand --all` command
- [ ] Update `gobby tasks show TASK_ID --expansion` to show phases/dependencies
- [ ] Add progress indicators for long operations
- [ ] Add unit tests for CLI commands

#### Phase 12.8: Testing & Documentation

- [ ] Add integration test: expand → subtasks created with dependencies
- [ ] Add integration test: expand with research mode
- [ ] Add integration test: expand_all with complexity filtering
- [ ] Add integration test: dependency cycle prevention
- [ ] Test with real project (2048 game example from plan)
- [ ] Update CLAUDE.md task management section
- [ ] Update docs/tasks.md with expansion guide
- [ ] Add example prompts and outputs to documentation

### Phase 12.5: Task Validation

> Uses configured `task_validation` provider. Validates task completion against `validation_criteria`.
> Tools are registered in `gobby-tasks` internal MCP server.

**Schema Migration:**

- [ ] Add `validation_criteria` column to tasks table
- [ ] Add `use_external_validator` column to tasks table
- [ ] Add `validation_fail_count` column to tasks table
- [ ] Add `failed` as valid status value

**Core Implementation:**

- [ ] Create `TaskValidationConfig` in `src/config/app.py`
- [ ] Create `src/tasks/validation.py` with `TaskValidator` class
- [ ] Implement `validate_task()` method - runs validation prompt against current state
- [ ] Implement `gather_validation_context()` - reads relevant files, test results, etc.

**Failure Handling:**

- [ ] Increment `validation_fail_count` on failure
- [ ] If `create_fix_subtask: true`, create subtask with failure details
- [ ] If `validation_fail_count >= max_validation_fails`, set status → `failed`
- [ ] `failed` tasks surface in `list_tasks(status="failed")` for human review

**External Validator Support:**

- [ ] When `use_external_validator: true` on task, spawn separate validation agent
- [ ] Validation agent uses configured provider/model from `task_validation`
- [ ] Pass task context, files changed, test results to validation agent

**MCP Tools (gobby-tasks internal server):**

- [ ] Add `validate_task` tool to `src/mcp_proxy/tools/tasks.py`
- [ ] Add `get_validation_status` tool to `src/mcp_proxy/tools/tasks.py`
- [ ] Add `reset_validation_count` tool (for manual retry)

**CLI Commands:**

- [ ] Add `gobby tasks validate TASK_ID` command
- [ ] Add `gobby tasks list --status failed` filter
- [ ] Add `gobby tasks reset-validation TASK_ID` command

**Testing:**

- [ ] Add unit tests for TaskValidator
- [ ] Add integration tests with mock LLM
- [ ] Test failure → subtask creation flow
- [ ] Test max_validation_fails → failed status flow

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
| 5 | **LLM provider for expansion/validation** | Use existing `llm_providers` infrastructure | Subscription SDKs (Claude/Codex/Gemini) or LiteLLM for API keys. Same pattern as session summaries, tool recommendations. |
| 6 | **Validation failure handling** | Create subtask + increment counter + fail after max | Gives agent multiple attempts to fix. `failed` status alerts humans. Subtask documents what went wrong. |
| 7 | **Codebase analysis during expansion** | Enabled by default, configurable | LLM reads relevant files to understand context. Infers dependencies from code structure. Can disable with `--no-codebase`. |
| 8 | **Validation criteria format** | Natural language prompt | Simple, flexible. LLM evaluates against current state. No rigid schema needed. |
| 9 | **Research mode** | Enabled by default | Quality over speed - web search for best practices improves expansion quality. Use `--no-research` to disable. |
| 10 | **Prompts location** | Full text in config.yaml | Users can see and customize prompts without reading source code. Config is the source of truth. |
| 11 | **Phase structure** | Required in output, optional to display | Always generate phases for logical grouping, but CLI can flatten if user prefers. |
| 12 | **Dependency format** | Index-based in prompt, converted to task IDs | Simpler for LLM to reference by index, we map to real IDs after creation. |
| 13 | **Parent blocking** | Parent blocked by all children | Parent task can't close until subtasks complete - enforces completion. |
| 14 | **Research caching** | Store in `expansion_context` | Avoid re-searching if task re-expanded. |
| 15 | **Force behavior** | Delete existing subtasks | Clean slate, not merge - too complex to merge intelligently. |

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

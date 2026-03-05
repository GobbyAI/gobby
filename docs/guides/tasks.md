# Task Management Guide

Gobby includes a native task tracking system designed for AI-assisted development. Tasks are persistent across sessions, support dependencies, and sync with git.

## Core Concepts

- **Task**: A unit of work with title, description, priority, and status
- **Epic**: A parent task that groups subtasks
- **Dependencies**: Tasks can block other tasks (A must complete before B starts)
- **Ready Work**: Tasks with no unresolved blocking dependencies
- **Sync**: Tasks export to `.gobby/tasks.jsonl` for git versioning

## Quick Start

### MCP Tools (for AI Agents)

```python
# Check what's ready to work on
call_tool(server_name="gobby-tasks", tool_name="list_ready_tasks", arguments={})

# Create a task (status='open', no assignee by default)
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Fix authentication bug",
    "priority": 1,
    "task_type": "bug",
    "session_id": "<your_session_id>"  # Required - tracks who created the task
})
# Task is created with status='open' and no assignee

# Create AND claim a task in one call (set claim=True)
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Fix authentication bug",
    "priority": 1,
    "task_type": "bug",
    "session_id": "<your_session_id>",
    "claim": True  # Auto-claim: status='in_progress', assignee=session_id
})
# Task is now in_progress and assigned to your session

# To claim an EXISTING unclaimed task, use claim_task:
call_tool(server_name="gobby-tasks", tool_name="claim_task", arguments={
    "task_id": "gt-abc123",
    "session_id": "<your_session_id>"
})

# Complete it
call_tool(server_name="gobby-tasks", tool_name="close_task", arguments={
    "task_id": "gt-abc123",
    "reason": "completed"
})
```

### CLI Commands

```bash
# List ready work
gobby tasks list --ready

# Create a task
gobby tasks create "Fix login bug" -p 1 -t bug

# Update status
gobby tasks update gt-abc123 --status in_progress

# Close task
gobby tasks close gt-abc123 --reason "Fixed"

# Sync with git
gobby tasks sync
```

## Task Lifecycle

```text
open → in_progress → needs_review → approved → closed
                  ↘                            ↗
                   escalated (human intervention needed)
```

- **open**: Ready or blocked, not started
- **in_progress**: Currently being worked on
- **needs_review**: Agent-complete, awaiting human sign-off (HITL)
- **approved**: Review gate passed, workflow handles the rest (merge, deploy, etc.)
- **closed**: Completed with reason
- **escalated**: Needs human intervention (validation failures, blocked, etc.)

### Review Status (HITL)

Tasks enter `needs_review` status instead of `closed` when:
- Task has `requires_user_review=true` (explicitly flagged for human approval)
- Agent uses `override_justification` to bypass validation failures

**Fields:**
- `requires_user_review`: Boolean flag for mandatory human approval
- `accepted_by_user`: Audit trail - set to `true` when user closes from review

**Dependency behavior:**
- Tasks in `review` with `requires_user_review=false` unblock dependents (treated as complete)
- Tasks in `review` with `requires_user_review=true` keep dependents blocked until user closes

**Workflow condition:**
- `task_needs_user_review()` - Returns true when session_task is in review AND requires user approval

## Task Types

| Type | Use For |
|------|---------|
| `task` | General work items (default) |
| `bug` | Something broken |
| `feature` | New functionality |
| `epic` | Large feature with subtasks |
| `chore` | Maintenance, dependencies, tooling |

## Priority Levels

| Priority | Meaning |
|----------|---------|
| 1 | High (critical bugs, major features) |
| 2 | Medium (default) |
| 3 | Low (polish, optimization) |

## Dependencies

Tasks can block other tasks. A blocked task won't appear in `list_ready_tasks` until its blockers are complete.

**Complete for dependency purposes:**
- Status is `closed`, OR
- Status is `review` AND `requires_user_review=false`

```python
# Task A blocks Task B (B depends on A completing first)
call_tool(server_name="gobby-tasks", tool_name="add_dependency", arguments={
    "task_id": "gt-taskB",      # The dependent task
    "depends_on": "gt-taskA",   # The blocker
    "dep_type": "blocks"
})

# Create task that BLOCKS other tasks (this task must complete first)
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Implement feature",
    "blocks": ["gt-parent-epic"],  # This task blocks the parent
    "session_id": "<your_session_id>"  # Required
})

# Create task that DEPENDS ON other tasks (those tasks must complete first)
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Integration tests",
    "depends_on": ["#1", "#2"],  # This task is blocked by #1 and #2
    "session_id": "<your_session_id>"  # Required
})
```

### Dependency Types

| Type | Behavior |
|------|----------|
| `blocks` | Hard dependency - prevents task from being "ready" |
| `related` | Soft link - informational only |
| `discovered-from` | Task found while working on another |

## Task Expansion Workflow

Gobby provides a two-phase approach to breaking down complex work:

```text
save_expansion_spec → execute_expansion
        ↓                    ↓
  (plan subtasks)    (create atomically)
```

### Phase 1: Save Expansion Spec

First, plan your subtasks by saving an expansion specification:

```python
# MCP: Save expansion spec for review/later execution
call_tool(server_name="gobby-tasks", tool_name="save_expansion_spec", arguments={
    "task_id": "gt-abc123",
    "spec": {
        "subtasks": [
            {"title": "Design auth API", "description": "..."},
            {"title": "Implement login endpoint", "depends_on": [0]},
            {"title": "Add session management", "depends_on": [1]}
        ]
    }
})
```

### Phase 2: Execute Expansion

Then atomically create all subtasks with dependencies:

```python
# MCP: Execute the saved expansion spec
call_tool(server_name="gobby-tasks", tool_name="execute_expansion", arguments={
    "task_id": "gt-abc123"
})
```

### Check for Pending Expansion

After session compaction, check if expansion was interrupted:

```python
# MCP: Check for pending expansion spec
call_tool(server_name="gobby-tasks", tool_name="get_expansion_spec", arguments={
    "task_id": "gt-abc123"
})
```

For structured planning, use the `/gobby-expand` skill which guides you through this process.

## AI-Powered Task Features

### Suggest Next Task

Get AI-powered suggestion for the best task to work on:

```python
# Get AI suggestion for next task
call_tool(server_name="gobby-tasks", tool_name="suggest_next_task", arguments={
    "session_id": "<your_session_id>"
})
```

### Complexity Analysis (CLI)

Analyze task complexity from the CLI:

```bash
# Analyze single task
gobby tasks complexity #42

# Analyze all open tasks
gobby tasks complexity --all
```

## Task Validation

Validate task completion with AI assistance. Validation uses actual git diffs to verify real code changes.

### Automatic Validation on Close

When closing a task with `validation_criteria`, the system automatically:
1. Fetches uncommitted git changes (staged + unstaged)
2. Passes the actual diff to the validation LLM
3. Blocks the close if validation fails

```python
# Close task - validation happens automatically if task has validation_criteria
call_tool(server_name="gobby-tasks", tool_name="close_task", arguments={
    "task_id": "gt-abc123",
    "reason": "completed"
})
# If validation fails, returns: {"error": "validation_failed", "message": "...", "validation_status": "invalid"}

# Skip validation if needed
call_tool(server_name="gobby-tasks", tool_name="close_task", arguments={
    "task_id": "gt-abc123",
    "reason": "completed",
    "skip_validation": True
})
```

### Generate Validation Criteria

Tasks need `validation_criteria` for validation to run:

```python
# Generate criteria for a single task
call_tool(server_name="gobby-tasks", tool_name="generate_validation_criteria", arguments={
    "task_id": "gt-abc123"
})

# Or set criteria manually when creating/updating
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Add logout button",
    "validation_criteria": "- Logout button visible in header\n- Clicking logs user out\n- Redirects to login page",
    "session_id": "<your_session_id>"  # Required
})
```

### Manual Validation

```python
# Validate a task explicitly (with optional changes_summary)
call_tool(server_name="gobby-tasks", tool_name="validate_task", arguments={
    "task_id": "gt-abc123",
    "changes_summary": "Added login form with validation"  # Optional - uses git diff if not provided
})

# Check validation status
call_tool(server_name="gobby-tasks", tool_name="get_validation_status", arguments={
    "task_id": "gt-abc123"
})

# Reset validation count for retry
call_tool(server_name="gobby-tasks", tool_name="reset_validation_count", arguments={
    "task_id": "gt-abc123"
})
```

### CLI Validation Commands

```bash
# Generate validation criteria
gobby tasks generate-criteria gt-abc123

# Generate criteria for all open tasks missing it
gobby tasks generate-criteria --all

# Validate a task
gobby tasks validate gt-abc123

# Reset validation failure count
gobby tasks reset-validation gt-abc123
```

## Git Sync

Tasks automatically sync to `.gobby/tasks.jsonl`:

- **Export**: After task changes (5s debounce)
- **Import**: On daemon start
- **Manual**: `gobby tasks sync`

### Git Hook Auto-Sync

Install git hooks for automatic task sync on commits and branch changes:

```bash
gobby install
```

**Hooks installed:**
| Hook | Trigger | Action |
|------|---------|--------|
| `pre-commit` | Before each commit | Export tasks to JSONL |
| `post-merge` | After pull/merge | Import tasks from JSONL |
| `post-checkout` | On branch switch | Import tasks from JSONL |

The installer chains with existing hooks (preserving pre-commit framework if present) and creates backups before modification.

This ensures tasks stay in sync with your git branches without manual intervention.

### Stealth Mode

Keep tasks out of git (store in `~/.gobby/` instead):

```bash
gobby tasks config --stealth on
```

## Search

Find tasks by content using TF-IDF full-text search. Complements `list_tasks` (filters by metadata) with content-based discovery.

### MCP Tools

```python
# Search tasks by content
call_tool(server_name="gobby-tasks", tool_name="search_tasks", arguments={
    "query": "authentication login",
    "status": "open",           # Optional: filter by status
    "task_type": "bug",         # Optional: filter by type
    "priority": 1,              # Optional: filter by priority
    "limit": 10,                # Optional: max results (default 10)
    "min_score": 0.1,           # Optional: minimum relevance score
    "all_projects": False       # Optional: search across all projects
})

# Rebuild search index (usually automatic)
call_tool(server_name="gobby-tasks", tool_name="reindex_tasks", arguments={
    "all_projects": False       # Optional: reindex all projects
})
```

### CLI Commands

```bash
# Search tasks
gobby tasks search "authentication bug"
gobby tasks search "login" --status open --type bug --limit 5
gobby tasks search "OAuth" --all-projects --json

# Rebuild search index
gobby tasks reindex
gobby tasks reindex --all-projects
```

### How It Works

- Uses TF-IDF (Term Frequency-Inverse Document Frequency) for relevance scoring
- Searches task titles and descriptions
- Results sorted by relevance score (higher = better match)
- Index automatically updates when tasks change
- Same TF-IDF backend as `gobby-memory` search

## Complete MCP Tool Reference

### Task CRUD

| Tool | Description |
|------|-------------|
| `create_task` | Create a new task (supports `depends_on` for inline dependencies) |
| `get_task` | Get task details with dependencies |
| `update_task` | Update task fields |
| `close_task` | Close a task with reason |
| `delete_task` | Delete a task (`cascade` or `unlink` for dependents) |
| `list_tasks` | List tasks with filters |
| `add_label` | Add a label to a task |
| `remove_label` | Remove a label from a task |

### Dependencies

| Tool | Description |
|------|-------------|
| `add_dependency` | Create dependency between tasks |
| `remove_dependency` | Remove a dependency |
| `get_dependency_tree` | Get blockers/blocking tree |
| `check_dependency_cycles` | Detect circular dependencies |

### Ready Work

| Tool | Description |
|------|-------------|
| `list_ready_tasks` | Tasks with no unresolved blockers |
| `list_blocked_tasks` | Tasks waiting on others |

### Progressive Discovery

List operations return **brief format** (8 fields) to minimize token usage:

```json
{"id", "title", "status", "priority", "type", "parent_task_id", "created_at", "updated_at"}
```

Use `get_task` to retrieve full details (description, validation criteria, commits, etc.):

```python
# Step 1: Discover tasks
tasks = call_tool(server_name="gobby-tasks", tool_name="list_ready_tasks", arguments={})

# Step 2: Get full details for specific task
task = call_tool(server_name="gobby-tasks", tool_name="get_task", arguments={"task_id": "gt-abc"})
```

### Session Integration

| Tool | Description |
|------|-------------|
| `link_task_to_session` | Associate task with session |
| `get_session_tasks` | Tasks linked to a session |
| `get_task_sessions` | Sessions that touched a task |

### Git Sync

| Tool | Description |
|------|-------------|
| `sync_tasks` | Trigger import/export |
| `get_sync_status` | Get sync status |

### Task Expansion

| Tool | Description |
|------|-------------|
| `save_expansion_spec` | Save expansion spec for later execution |
| `execute_expansion` | Execute saved expansion atomically |
| `get_expansion_spec` | Check for pending expansion (resume after compaction) |
| `suggest_next_task` | AI suggests next task to work on |

### Validation

| Tool | Description |
|------|-------------|
| `validate_task` | Validate task completion (auto-gathers git context) |
| `get_validation_status` | Get validation details |
| `reset_validation_count` | Reset failure count for retry |
| `get_validation_history` | Full validation history with iterations |
| `get_recurring_issues` | Analyze recurring validation issues |
| `clear_validation_history` | Clear all validation history |
| `de_escalate_task` | Return escalated task to open status |
| `generate_validation_criteria` | Generate validation criteria using LLM |
| `run_fix_attempt` | Spawn fix agent for validation issues |
| `validate_and_fix` | Run validation loop with auto-fix |

### Search

| Tool | Description |
|------|-------------|
| `search_tasks` | Full-text search tasks by content (TF-IDF) |
| `reindex_tasks` | Rebuild search index |

## CLI Command Reference

```bash
# Task management
gobby tasks list [--status S] [--priority N] [--ready] [--blocked] [--json]
gobby tasks show TASK_ID
gobby tasks create "Title" [-d DESC] [-p PRIORITY] [-t TYPE]
gobby tasks update TASK_ID [--status S] [--priority P]
gobby tasks close TASK_ID --reason "Done"
gobby tasks reopen TASK_ID
gobby tasks delete TASK_ID [--cascade]

# Dependencies
gobby tasks dep add TASK BLOCKER
gobby tasks dep remove TASK BLOCKER
gobby tasks dep tree TASK
gobby tasks dep cycles

# Labels
gobby tasks label add TASK LABEL
gobby tasks label remove TASK LABEL

# Commit linking
gobby tasks commit link TASK SHA
gobby tasks commit unlink TASK SHA
gobby tasks commit auto
gobby tasks diff TASK_ID

# Ready work
gobby tasks ready [--limit N]
gobby tasks blocked
gobby tasks suggest

# Sync
gobby tasks sync [--import] [--export]

# Search
gobby tasks search <QUERY> [--status S] [--type T] [--limit N] [--all-projects] [--json]
gobby tasks reindex [--all-projects]

# Complexity
gobby tasks complexity TASK_ID [--all]

# Validation
gobby tasks generate-criteria TASK_ID   # Generate criteria for one task
gobby tasks generate-criteria --all     # Generate for all open tasks
gobby tasks validate TASK_ID            # Run validation
gobby tasks validation-history TASK_ID  # View validation history
gobby tasks validation-history TASK_ID --clear  # Clear history
gobby tasks de-escalate TASK_ID         # Return escalated task to open

# Maintenance
gobby tasks stats
gobby tasks doctor                      # Validate data integrity
gobby tasks clean                       # Fix data issues
gobby tasks compact                     # Compaction commands
```

## Data Storage

- **Database**: `~/.gobby/gobby-hub.db` (SQLite)
- **Git sync**: `.gobby/tasks.jsonl` (or `~/.gobby/tasks/{project}.jsonl` in stealth mode)

## Task ID Format

- Generated: `gt-{6 hex chars}` (e.g., `gt-a1b2c3`)
- Hierarchical: `gt-a1b2c3.1`, `gt-a1b2c3.2` (subtasks)
- Prefix matching supported: `gt-a1b` matches `gt-a1b2c3`

## Claude Code Task Integration

Gobby transparently intercepts Claude Code's built-in task tools (`TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`) and syncs operations to Gobby's persistent task store.

### How It Works

When you use Claude Code's built-in task tools, Gobby:

1. Lets the CC tool execute normally
2. Syncs the result to Gobby's persistent storage via post-tool-use hooks
3. Enriches responses with Gobby-specific metadata

This gives you the best of both worlds: Claude Code's native task UI + Gobby's persistence and features.

### Using Gobby Features via CC

Pass Gobby options in the `metadata.gobby` field:

```python
TaskCreate(
    subject="Implement OAuth",
    metadata={
        "gobby": {
            "task_type": "feature",
            "priority": 1,
            "validation_criteria": "All tests pass"
        }
    }
)
```

### Status Mapping

| Claude Code | Gobby |
|-------------|-------|
| `pending` | `open` |
| `in_progress` | `in_progress` |
| `completed` | `closed` |

### Dual ID Format

Tasks are addressable by both:
- **Seq number**: `#47` (human-friendly, project-scoped)
- **UUID**: `abc123-...` (globally unique)

Both formats work when referencing tasks.

### Response Enrichment

Gobby enriches CC task responses with additional metadata in a `gobby` block:

- `validation_status`: Current validation state
- `is_expanded`: Whether task has been broken into subtasks
- `subtask_count`: Number of child tasks
- `commit_count`: Linked commits
- `path_cache`: Hierarchical position (e.g., "1.2.3")
- `task_type`: Gobby task type
- `priority`: Task priority

### Benefits Over Session-Scoped Tasks

Claude Code's built-in tasks are session-scoped and don't persist across sessions. With Gobby's integration:

- **Persistence**: Tasks survive session restarts and compactions
- **Commit Linking**: Include `[task-id]` in commit messages for auto-linking
- **Validation Gates**: Define criteria that must pass before closing
- **LLM Expansion**: Break complex tasks into subtasks with embedded TDD steps
- **Git Sync**: Tasks export to `.gobby/tasks.jsonl` for version control

---
name: gobby-tasks-guide
description: Use this skill when working with the gobby-tasks server to manage project tasks, dependencies, and ready work detection. This skill teaches the MCP tool discovery workflow and task management patterns specific to the gobby-tasks registry.
---

# Gobby Tasks Guide

## Overview

The `gobby-tasks` server provides task management tools for tracking work, dependencies, and session integration. It's an internal registry (not a downstream MCP server) accessed through the same three-step discovery workflow as external tools.

This skill covers:
1. How to access gobby-tasks tools via MCP
2. Task CRUD operations and lifecycle
3. Dependencies and ready work detection
4. Git synchronization

---

## Part 1: Accessing Internal Tools

### The `gobby-*` Server Pattern

Internal tool registries use the `gobby-*` prefix. They're handled locally by the daemon, not proxied to external servers.

| Server | Type | Purpose |
|--------|------|---------|
| `gobby-tasks` | Internal | Task management, dependencies, sync |
| `context7`, `supabase` | External | Proxied to downstream MCP servers |

### Three-Step Workflow

Internal tools use the same discovery pattern as external tools:

#### Step 1: List Available Tools

```python
# List all gobby-tasks tools
mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="list_tools",
    arguments={"server": "gobby-tasks"}
)
```

Returns tool names and brief descriptions (lightweight).

#### Step 2: Get Tool Schema

```python
# Get full schema before calling
mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="get_tool_schema",
    arguments={
        "server_name": "gobby-tasks",
        "tool_name": "create_task"
    }
)
```

Returns complete inputSchema with all parameters.

#### Step 3: Call the Tool

```python
# Execute the tool
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="create_task",
    arguments={"title": "Implement feature X"}
)
```

Note: `server_name` changes to `gobby-tasks` when calling (not `gobby`).

---

## Part 2: Task Management

### Available Tools

| Tool | Purpose |
|------|---------|
| `create_task` | Create a new task |
| `get_task` | Get task details with dependencies |
| `update_task` | Update task fields |
| `close_task` | Close with reason |
| `delete_task` | Delete (optional cascade) |
| `list_tasks` | List with filters |
| `add_label` | Add label to task |
| `remove_label` | Remove label from task |

### Task Lifecycle

```
open → in_progress → closed
```

- **open**: Ready or blocked, not started
- **in_progress**: Currently being worked on
- **closed**: Completed (with reason)

### Creating Tasks

```python
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="create_task",
    arguments={
        "title": "Fix login bug",
        "description": "Users getting 401 on valid credentials",
        "priority": 1,           # 1=High, 2=Medium, 3=Low
        "task_type": "bug",      # task, bug, feature, epic
        "labels": ["auth", "urgent"]
    }
)
```

### Listing Tasks

```python
# List open tasks
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="list_tasks",
    arguments={"status": "open"}
)

# Filter by priority and type
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="list_tasks",
    arguments={
        "priority": 1,
        "task_type": "bug"
    }
)
```

### Updating Tasks

```python
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="update_task",
    arguments={
        "task_id": "gt-abc123",
        "status": "in_progress",
        "assignee": "josh"
    }
)
```

### Closing Tasks

```python
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="close_task",
    arguments={
        "task_id": "gt-abc123",
        "reason": "completed"  # or "wont_fix", "duplicate"
    }
)
```

---

## Part 3: Dependencies & Ready Work

### Dependency Tools

| Tool | Purpose |
|------|---------|
| `add_dependency` | Create dependency between tasks |
| `remove_dependency` | Remove dependency |
| `get_dependency_tree` | Get blockers/blocking tree |
| `check_dependency_cycles` | Detect circular dependencies |
| `list_ready_tasks` | Tasks with no unresolved blockers |
| `list_blocked_tasks` | Tasks waiting on others |

### Understanding Dependencies

**"A blocks B"** means:
- Task A must be completed before B can start
- B depends on A
- A is a "blocker" of B
- B is "blocked by" A

```python
# Task A blocks Task B
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="add_dependency",
    arguments={
        "task_id": "gt-taskB",      # The dependent task
        "depends_on": "gt-taskA",   # The blocker
        "dep_type": "blocks"        # Default
    }
)
```

### Creating Tasks with Dependencies

Use `blocks` parameter as syntactic sugar:

```python
# Create task that blocks others
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="create_task",
    arguments={
        "title": "Set up database",
        "blocks": ["gt-api-task", "gt-auth-task"]  # This task blocks these
    }
)
```

### Finding Ready Work

Ready tasks are open tasks with no unresolved blocking dependencies:

```python
# What can I work on now?
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="list_ready_tasks",
    arguments={"limit": 10}
)
```

### Viewing Blocked Tasks

```python
# What's waiting on something?
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="list_blocked_tasks",
    arguments={}
)
```

Returns tasks with their blockers listed.

### Dependency Tree

```python
# Get full dependency context
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="get_dependency_tree",
    arguments={
        "task_id": "gt-abc123",
        "direction": "both"  # "blockers", "blocking", or "both"
    }
)
```

---

## Part 4: Git Synchronization

### Sync Tools

| Tool | Purpose |
|------|---------|
| `sync_tasks` | Import/export to JSONL |
| `get_sync_status` | Check sync state |

### How Sync Works

Tasks sync bidirectionally with `.gobby/tasks.jsonl`:
- **Export**: Task changes write to JSONL (debounced)
- **Import**: JSONL changes import to database
- **Conflict resolution**: Last-write-wins

### Manual Sync

```python
# Full sync (import + export)
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="sync_tasks",
    arguments={"direction": "both"}
)

# Import only
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="sync_tasks",
    arguments={"direction": "import"}
)
```

### Check Sync Status

```python
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="get_sync_status",
    arguments={}
)
```

---

## Part 5: Session Integration

### Session Tools

| Tool | Purpose |
|------|---------|
| `link_task_to_session` | Associate task with session |
| `get_session_tasks` | Tasks linked to a session |
| `get_task_sessions` | Sessions that touched a task |

### Linking Tasks to Sessions

```python
mcp__gobby__call_tool(
    server_name="gobby-tasks",
    tool_name="link_task_to_session",
    arguments={
        "task_id": "gt-abc123",
        "session_id": "session-xyz",
        "action": "worked_on"  # worked_on, discovered, mentioned, closed
    }
)
```

---

## Quick Reference

### Discovery Pattern
```python
# 1. List tools
list_tools(server="gobby-tasks")

# 2. Get schema
get_tool_schema(server_name="gobby-tasks", tool_name="...")

# 3. Call tool
call_tool(server_name="gobby-tasks", tool_name="...", arguments={...})
```

### Common Workflows

**Start working on next task:**
```python
list_ready_tasks() → update_task(status="in_progress")
```

**Complete a task:**
```python
close_task(task_id, reason="completed")
```

**Check what's blocked:**
```python
list_blocked_tasks() → get_dependency_tree(task_id)
```

### Task ID Format

- Generated IDs: `gt-{6 hex chars}` (e.g., `gt-a1b2c3`)
- GitHub imports: `gh-{issue_number}` (e.g., `gh-42`)
- Prefix matching supported: `gt-a1b` matches `gt-a1b2c3`

### Priority Levels

| Value | Meaning |
|-------|---------|
| 1 | High |
| 2 | Medium (default) |
| 3 | Low |

### Task Types

`task`, `bug`, `feature`, `epic`, `chore`, `issue`

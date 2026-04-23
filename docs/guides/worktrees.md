# Worktree Management Guide

Gobby uses git worktrees for parallel development, enabling multiple agents to work on different tasks simultaneously without conflicts.

## Quick Start

```bash
# Create a worktree for a feature branch
gobby worktrees create feature/auth --task #123

# List worktrees
gobby worktrees list

# Show worktree details
gobby worktrees show WORKTREE_ID

# Sync with base branch
gobby worktrees sync WORKTREE_ID

# Clean up after merge
gobby worktrees delete WORKTREE_ID
```

```python
# MCP: Create a worktree
call_tool(server_name="gobby-worktrees", tool_name="create_worktree", arguments={
    "branch_name": "feature/auth",
    "task_id": "#123",
    "base_branch": "main"
})

# Claim worktree for an agent session
call_tool(server_name="gobby-worktrees", tool_name="claim_worktree", arguments={
    "worktree_id": "<worktree_id>",
    "session_id": "<agent_session_id>"
})
```

## Concepts

### What is a Git Worktree?

A git worktree is a linked working directory that shares the same repository:

```text
main-repo/              # Main working directory (main branch)
├── .git/
└── src/

.gobby/worktrees/
├── feature-auth/       # Worktree (feature/auth branch)
│   └── src/
└── bugfix-login/       # Worktree (bugfix/login branch)
    └── src/
```

All worktrees share:
- Git history
- Remote configuration
- Hooks

Each worktree has its own:
- Working directory
- Index (staging area)
- HEAD (current branch)

### Worktree Lifecycle

```text
created → claimed → active → released → merged → deleted
                      ↓
                   stale → cleaned up
```

- **created**: Worktree exists, no owner
- **claimed**: Agent has claimed ownership
- **active**: Agent is working in worktree
- **released**: Agent released ownership
- **merged**: Branch merged to base
- **deleted**: Worktree removed
- **stale**: No activity for extended period

### Task Linking

Worktrees can be linked to tasks:

```text
Task #123: Implement auth
└── Worktree: feature/auth
    └── Commits linked to task
```

This enables:
- Automatic branch naming
- Commit tracking
- Diff aggregation via `get_task_diff`

## CLI Commands

### `gobby worktrees create`

Create a new worktree for parallel development.

```bash
gobby worktrees create BRANCH_NAME [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--base` | Base branch (default: current) |
| `--task` | Link to task ID |

### `gobby worktrees list`

List worktrees.

```bash
gobby worktrees list [--status STATUS]
```

| Option | Description |
|--------|-------------|
| `--status` | Filter: active, stale, merged |

### `gobby worktrees show`

Show details for a worktree.

```bash
gobby worktrees show WORKTREE_ID
```

### `gobby worktrees claim`

Claim a worktree for a session.

```bash
gobby worktrees claim WORKTREE_ID --session SESSION_ID
```

### `gobby worktrees release`

Release a worktree.

```bash
gobby worktrees release WORKTREE_ID
```

### `gobby worktrees sync`

Sync worktree with its base branch.

```bash
gobby worktrees sync WORKTREE_ID
```

### `gobby worktrees delete`

Delete a worktree.

```bash
gobby worktrees delete WORKTREE_ID
```

### `gobby worktrees stale`

Detect stale worktrees.

```bash
gobby worktrees stale [--days N]
```

| Option | Description |
|--------|-------------|
| `--days` | Inactivity threshold (default: 7) |

### `gobby worktrees cleanup`

Clean up stale worktrees.

```bash
gobby worktrees cleanup [--days N] [--dry-run]
```

| Option | Description |
|--------|-------------|
| `--days` | Inactivity threshold |
| `--dry-run` | Show what would be deleted |

### `gobby worktrees stats`

Show worktree statistics.

```bash
gobby worktrees stats
```

## MCP Tools

### create_worktree

Create a new git worktree for isolated development.

```python
call_tool(server_name="gobby-worktrees", tool_name="create_worktree", arguments={
    "branch_name": "feature/auth",
    "base_branch": "main",  # optional, defaults to current
    "task_id": "#123"  # optional, links to task
})
```

### get_worktree

Get details of a specific worktree.

```python
call_tool(server_name="gobby-worktrees", tool_name="get_worktree", arguments={
    "worktree_id": "<worktree_id>"
})
```

### list_worktrees

List worktrees with optional filters.

```python
call_tool(server_name="gobby-worktrees", tool_name="list_worktrees", arguments={
    "status": "active"  # optional
})
```

### claim_worktree

Claim ownership of a worktree for an agent session.

```python
call_tool(server_name="gobby-worktrees", tool_name="claim_worktree", arguments={
    "worktree_id": "<worktree_id>",
    "session_id": "<agent_session_id>"
})
```

### release_worktree

Release ownership of a worktree.

```python
call_tool(server_name="gobby-worktrees", tool_name="release_worktree", arguments={
    "worktree_id": "<worktree_id>"
})
```

### delete_worktree

Delete a worktree (both git and database record).

```python
call_tool(server_name="gobby-worktrees", tool_name="delete_worktree", arguments={
    "worktree_id": "<worktree_id>"
})
```

### sync_worktree

Sync a worktree with the main branch.

```python
call_tool(server_name="gobby-worktrees", tool_name="sync_worktree", arguments={
    "worktree_id": "<worktree_id>"
})
```

### mark_worktree_merged

Mark a worktree as merged (ready for cleanup).

```python
call_tool(server_name="gobby-worktrees", tool_name="mark_worktree_merged", arguments={
    "worktree_id": "<worktree_id>"
})
```

### abandon_worktree

Mark a worktree as abandoned.

```python
call_tool(server_name="gobby-worktrees", tool_name="abandon_worktree", arguments={
    "worktree_id": "<worktree_id>"
})
```

### reactivate_worktree

Reactivate a worktree and clear merge cleanup scheduling.

```python
call_tool(server_name="gobby-worktrees", tool_name="reactivate_worktree", arguments={
    "worktree_id": "<worktree_id>"
})
```

### detect_stale_worktrees

Find worktrees with no activity for a period.

```python
call_tool(server_name="gobby-worktrees", tool_name="detect_stale_worktrees", arguments={
    "days": 7  # inactivity threshold
})
```

### cleanup_stale_worktrees

Mark and optionally delete stale worktrees.

```python
call_tool(server_name="gobby-worktrees", tool_name="cleanup_stale_worktrees", arguments={
    "days": 7,
    "delete": True  # actually delete, not just mark
})
```

### get_worktree_stats

Get worktree statistics for the project.

```python
call_tool(server_name="gobby-worktrees", tool_name="get_worktree_stats", arguments={})
```

### get_worktree_by_task

Get worktree linked to a specific task.

```python
call_tool(server_name="gobby-worktrees", tool_name="get_worktree_by_task", arguments={
    "task_id": "#123"
})
```

### link_task_to_worktree

Link a task to an existing worktree.

```python
call_tool(server_name="gobby-worktrees", tool_name="link_task_to_worktree", arguments={
    "task_id": "#123",
    "worktree_id": "<worktree_id>"
})
```

## Merge Operations

Gobby provides AI-powered merge conflict resolution.

### CLI Commands

```bash
# Start a merge
gobby merge start feature/auth --target main

# Check merge status
gobby merge status

# Resolve conflict with AI
gobby merge resolve src/auth.py --ai

# Apply and complete
gobby merge apply

# Abort if needed
gobby merge abort
```

### MCP Tools

```python
# Start merge
call_tool(server_name="gobby-merge", tool_name="merge_start", arguments={
    "source_branch": "feature/auth",
    "target_branch": "main"
})

# Check status
call_tool(server_name="gobby-merge", tool_name="merge_status", arguments={})

# Resolve with AI
call_tool(server_name="gobby-merge", tool_name="merge_resolve", arguments={
    "file_path": "src/auth.py",
    "use_ai": True
})

# Apply
call_tool(server_name="gobby-merge", tool_name="merge_apply", arguments={})

# Abort
call_tool(server_name="gobby-merge", tool_name="merge_abort", arguments={})
```

## Clone Management

For complete isolation (separate remote tracking), use clones instead of worktrees.

### CLI Commands

```bash
# Create a clone
gobby clones create --branch feature/auth --task #123

# List clones
gobby clones list

# Spawn agent in clone
gobby clones spawn CLONE_ID "Implement feature"

# Sync with remote
gobby clones sync CLONE_ID

# Merge back
gobby clones merge CLONE_ID --target main

# Delete clone
gobby clones delete CLONE_ID
```

### MCP Tools

```python
# Create clone
call_tool(server_name="gobby-clones", tool_name="create_clone", arguments={
    "branch_name": "feature/auth",
    "task_id": "#123"
})

# List clones
call_tool(server_name="gobby-clones", tool_name="list_clones", arguments={
    "status": "active"
})

# Sync clone
call_tool(server_name="gobby-clones", tool_name="sync_clone", arguments={
    "clone_id": "<clone_id>"
})

# Merge to target
call_tool(server_name="gobby-clones", tool_name="merge_clone", arguments={
    "clone_id": "<clone_id>",
    "target_branch": "main"
})

# Delete clone
call_tool(server_name="gobby-clones", tool_name="delete_clone", arguments={
    "clone_id": "<clone_id>"
})
```

## Worktrees vs Clones

| Feature | Worktree | Clone |
|---------|----------|-------|
| Storage | Shared .git | Full copy |
| Speed | Fast create | Slower create |
| Isolation | Branch-level | Complete |
| Remote | Shared | Separate tracking |
| Use case | Parallel branches | Complete isolation |

**Use worktrees when:**
- Working on multiple branches in same repo
- Fast switching needed
- Shared git history is fine

**Use clones when:**
- Complete isolation required
- Separate remote tracking needed
- Testing against different repo states

## Agent Isolation Pattern

Spawn agents in worktrees for parallel development:

```python
# 1. Create worktree linked to task
worktree = call_tool("gobby-worktrees", "create_worktree", {
    "branch_name": "feature/task-123",
    "task_id": "#123"
})

# 2. Spawn agent with worktree isolation
call_tool("gobby-agents", "spawn_agent", {
    "prompt": "Implement the feature",
    "task_id": "#123",
    "session_id": "<parent_session>",
    "isolation": "worktree"
})

# Agent automatically works in the worktree
```

## Best Practices

### Do

- Link worktrees to tasks for traceability
- Sync regularly to avoid merge conflicts
- Clean up stale worktrees
- Use meaningful branch names

### Don't

- Create too many worktrees
- Leave worktrees unclaimed for long
- Delete worktrees with uncommitted changes
- Ignore stale worktree warnings

## Data Storage

| Path | Description |
|------|-------------|
| `.gobby/worktrees/` | Worktree directories |
| `~/.gobby/gobby-hub.db` | Worktree metadata |

## See Also

- [agents.md](agents.md) - Agent management
- [tasks.md](tasks.md) - Task system
- [mcp-tools.md](mcp-tools.md) - Full MCP tool reference

---
name: clones
description: This skill should be used when the user asks to "/gobby clones", "create clone", "spawn in clone". Manage git clones for isolated parallel development - create, spawn agents, sync, merge, and delete.
category: core
metadata:
  gobby:
    audience: all
    depth: [0, 1]
---

# /gobby clones - Clone Management Skill

This skill manages git clones via the gobby-clones MCP server. Clones are full repository copies that can be worked on independently without affecting the main repository.

## Clones vs Worktrees

| Feature | Clones | Worktrees |
|---------|--------|-----------|
| Isolation | Full copy - completely isolated | Shared .git - linked to main |
| Disk space | Full repo copy | Minimal (shared objects) |
| Remote operations | Independent - can push/pull | Shares remotes with main |
| Merge conflicts | Detected at push/merge time | Immediate when switching |
| Best for | Parallel agents, CI, risky experiments | Local feature branches |

**Use clones when:**
- Running multiple agents in parallel on independent tasks
- Working on features that might conflict
- Need complete isolation from main repo state
- Want to easily discard work without affecting main

**Use worktrees when:**
- Working on related features that share code
- Need quick switching between branches
- Disk space is a concern
- Working solo on sequential tasks

## Session Context

**IMPORTANT**: Use the `session_id` from your SessionStart hook context.

Look for `Gobby Session Ref:` or `Gobby Session ID:` in your system context:
```
Gobby Session Ref: #5
Gobby Session ID: <uuid>
```

**Note**: All `session_id` and `parent_session_id` parameters accept #N, N, UUID, or prefix formats.

## Tool Schema Reminder

**First time calling a tool this session?** Use `get_tool_schema(server_name, tool_name)` before `call_tool` to get correct parameters. Schemas are cached per session—no need to refetch.

## Subcommands

### `/gobby clones create <branch-name> <clone-path>` - Create a new clone
Call `create_clone` with:
- `branch_name`: (required) Branch to clone
- `clone_path`: (required) Path where clone will be created
- `remote_url`: Remote URL (defaults to origin of parent repo)
- `task_id`: Optional task ID to link
- `base_branch`: Base branch (default: main)
- `depth`: Clone depth (default: 1 for shallow)

Creates an isolated git clone for development.

Example: `/gobby clones create feature/auth /tmp/gobby-clones/auth`
-> `create_clone(branch_name="feature/auth", clone_path="/tmp/gobby-clones/auth")`

### `/gobby clones show <clone-id>` - Show clone details
Call `get_clone` with:
- `clone_id`: (required) Clone ID

Returns clone details including path, branch, status, and linked task.

Example: `/gobby clones show clone-abc123` -> `get_clone(clone_id="clone-abc123")`

### `/gobby clones list` - List all clones
Call `list_clones` with:
- `status`: Filter by status (active, syncing, stale, cleanup)
- `limit`: Max results (default: 50)

Returns clones with path, branch, status, and associated task.

Example: `/gobby clones list` -> `list_clones()`
Example: `/gobby clones list active` -> `list_clones(status="active")`

### `/gobby clones spawn <branch-name> <prompt>` - Spawn agent in new clone
Call `spawn_agent_in_clone` with:
- `prompt`: (required) Task description for the agent
- `branch_name`: (required) Name for the branch in the clone
- `parent_session_id`: (required) Parent session ID for context
- `task_id`: Optional task ID to link
- `base_branch`: Branch to clone from (default: main)
- `clone_path`: Optional custom path for the clone
- `mode`: Execution mode (terminal, autonomous) - default: terminal
- `terminal`: Terminal type (auto, ghostty, etc.) - default: auto
- `provider`: LLM provider (claude, gemini, codex, antigravity) - default: claude
- `model`: Optional model override
- `workflow`: Workflow to activate
- `timeout`: Max runtime in seconds (default: 120)
- `max_turns`: Max conversation turns (default: 10)

Creates clone + spawns agent in one call. The agent receives clone context including:
- Warning that they're in an isolated clone
- Clone path and branch name
- Rules to stay within the clone directory

Example: `/gobby clones spawn feature/auth Implement OAuth login`
-> `spawn_agent_in_clone(branch_name="feature/auth", prompt="Implement OAuth login", parent_session_id="<session_id>")`

Example: `/gobby clones spawn feature/auth --autonomous Fix all type errors`
-> `spawn_agent_in_clone(branch_name="feature/auth", prompt="Fix all type errors", mode="autonomous", parent_session_id="<session_id>")`

### `/gobby clones sync <clone-id>` - Sync with remote
Call `sync_clone` with:
- `clone_id`: (required) Clone ID to sync
- `direction`: Sync direction (pull, push, both) - required

Syncs the clone with its remote repository.

Example: `/gobby clones sync clone-abc123 pull` -> `sync_clone(clone_id="clone-abc123", direction="pull")`
Example: `/gobby clones sync clone-abc123 push` -> `sync_clone(clone_id="clone-abc123", direction="push")`

### `/gobby clones merge <clone-id>` - Merge clone to target branch
Call `merge_clone` with:
- `clone_id`: (required) Clone ID to merge
- `target_branch`: Target branch to merge into (default: main)

Performs:
1. Push clone changes to remote
2. Fetch branch in main repo
3. Merge to target branch

On success, sets cleanup_after to 7 days. If conflicts occur, use gobby-merge tools to resolve.

Example: `/gobby clones merge clone-abc123` -> `merge_clone(clone_id="clone-abc123")`
Example: `/gobby clones merge clone-abc123 develop` -> `merge_clone(clone_id="clone-abc123", target_branch="develop")`

### `/gobby clones delete <clone-id>` - Delete a clone
Call `delete_clone` with:
- `clone_id`: (required) Clone ID to delete
- `force`: Force deletion even with uncommitted changes (default: false)

Removes the clone directory and database record.

Example: `/gobby clones delete clone-abc123` -> `delete_clone(clone_id="clone-abc123")`
Example: `/gobby clones delete clone-abc123 --force` -> `delete_clone(clone_id="clone-abc123", force=true)`

## Response Format

After executing the appropriate MCP tool, present the results clearly:
- For create: Show clone path, branch name, and ID
- For show: Full clone details
- For list: Table with branch, path, status, task ID
- For spawn: Show clone + agent creation confirmation with run_id
- For sync: Show sync result (pull/push status)
- For merge: Show merge result, or conflicts if any
- For delete: Confirm deletion

## Clone Lifecycle

1. `active` - Currently in use
2. `syncing` - Currently syncing with remote
3. `stale` - No recent activity
4. `cleanup` - Marked for cleanup (after merge)

## Parallel Development Workflow

A typical parallel workflow using clones:

```text
1. Create subtasks for parallel work
   /gobby tasks expand #parent --parallel

2. Spawn agents in separate clones
   /gobby clones spawn feature/auth "Implement OAuth" --task #1
   /gobby clones spawn feature/api "Add API endpoints" --task #2
   /gobby clones spawn feature/tests "Write integration tests" --task #3

3. Each agent works in complete isolation

4. When agent completes, merge back to main
   /gobby clones merge clone-auth
   /gobby clones merge clone-api
   /gobby clones merge clone-tests

5. Handle any merge conflicts
   /gobby merge start wt-main feature/auth

6. Clean up merged clones
   /gobby clones delete clone-auth
```

## Error Handling

Common errors:
- "Clone not found" - Invalid clone_id
- "No remote URL provided" - Repository has no remote configured
- "Clone failed" - Git clone operation failed
- "Sync failed" - Push or pull failed
- "Merge conflicts detected" - Use gobby-merge to resolve

If the subcommand is not recognized, show available subcommands:
- create, show, list, spawn, sync, merge, delete

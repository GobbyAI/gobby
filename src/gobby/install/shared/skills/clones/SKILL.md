---
name: clones
description: Git clones for isolated parallel development. Use when full repo isolation is needed beyond what worktrees provide.
version: "1.0.0"
category: core
triggers: clones, git clone, clone workspace, full repo isolation
metadata:
  gobby:
    audience: all
    depth: [0, 1]
---

# Clones — Full Repo Isolation

Clones create a complete, independent copy of the repository on a separate branch. They're managed via the gobby-clones MCP server. Use progressive discovery for tool schemas.

## Clones vs Worktrees

**Default to worktrees.** They're lighter, faster, and work with all supported CLIs.

Use clones when you need something worktrees can't provide:

| Need | Use |
|------|-----|
| Lightweight branch isolation | **Worktree** |
| Quick switching between branches | **Worktree** |
| Disk space matters | **Worktree** (shared objects) |
| Complete isolation from main repo state | **Clone** |
| Independent remote operations (push/pull) | **Clone** |
| Multiple agents that might conflict with each other | **Clone** |
| Risky experiments you want to fully discard | **Clone** |

## Lifecycle

1. **Create** — shallow clone on a named branch
2. **Work** — agent or user develops in the clone
3. **Sync** — push/pull to keep in sync with remote
4. **Merge** — merge changes back to target branch
5. **Cleanup** — delete clone after merge (auto-scheduled 7 days post-merge)

Statuses: `active` → `syncing` → `cleanup` (after merge). `stale` if no recent activity.

## Parallel Development Pattern

```text
1. Create subtasks for parallel work
2. Spawn agents in separate clones (one branch per agent)
3. Each agent works in complete isolation
4. Merge completed clones back to main
5. Handle conflicts with gobby-merge if needed
6. Delete merged clones
```

## Merge Conflicts

Conflicts are detected at merge time, not during development. If a merge fails with conflicts, use gobby-merge tools to resolve them before retrying.

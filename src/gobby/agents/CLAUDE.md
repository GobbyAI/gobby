# Agents Source Reference

This directory implements agent spawning, process management, isolation, and lifecycle.

## Key Classes

| Class | File | Purpose |
|-------|------|---------|
| `AgentSpawner` | `spawn.py` | Prepares agent spawns: session creation, isolation, prompt building |
| `SpawnExecutor` | `spawn_executor.py` | Executes the prepared spawn (process creation) |
| `AgentRunner` | `runner.py` | Manages running agent processes, completion tracking |
| `IsolationHandler` | `isolation.py` | Compatibility facade for isolation models, handlers, repair, and factory helpers |

## File Index

### Spawning
- `spawn.py` — `AgentSpawner`: creates child session, builds prompt, prepares environment, activates step workflow
- `spawn_executor.py` — `SpawnExecutor`: launches CLI subprocess after spawn preparation
- `dry_run.py` — Dry-run spawn validation (checks definition, workflow, isolation without executing)

### Process Management
- `runner.py` — `AgentRunner`: process lifecycle, completion detection, result publishing
- `runner_models.py` — Data models for agent runs
- `runner_queries.py` — Database-backed agent run state queries
- `runner_tracking.py` — Token/turn tracking for running agents

### Isolation
- `isolation.py` — Compatibility facade preserving the historical `gobby.agents.isolation` import surface.
- `isolation_models.py` — Shared `IsolationContext`, `SpawnConfig`, `IsolationHandler`, and branch naming.
- `isolation_none.py` — `NoneIsolationHandler` for current-directory execution.
- `isolation_worktree.py` — `WorktreeIsolationHandler` for git worktree creation, reuse, base capture, and cleanup.
- `isolation_clone.py` — `CloneIsolationHandler` and clone base commit capture.
- `isolation_repair.py` — Hook copying, Droid hook merging, project metadata repair, MCP config patching, and provider preflight helpers.
- `isolation_code_index.py` — Isolated workspace code-index preflight wrapper.
- `isolation_factory.py` — `get_isolation_handler` factory.

### Sessions & Context
- `session.py` — Child session management (creation, variable initialization, ancestry)
- `context.py` — Prompt context building (preamble from agent definition + spawn prompt)
- `definitions.py` — Agent definition resolution from database

### Lifecycle
- `lifecycle_monitor.py` — Background monitor for agent health and timeouts
- `sync.py` — Agent state synchronization
- `kill.py` — DB-backed agent termination helpers

### Terminal Backend
- `tmux/` — Tmux spawner subdirectory:
  - `tmux/spawner.py` — `TmuxSpawner`: creates tmux sessions/panes for terminal-mode agents
- `spawners/` — CLI-specific spawners (Claude, Gemini, Codex)
- `pty_reader.py` — PTY output reader for process monitoring

### Other
- `constants.py` — Agent-related constants (depth limits, timeouts)
- `sandbox.py` — Sandbox configuration for agent processes

## Agent Spawn Flow

```
spawn_agent() called
  → AgentSpawner.prepare()
    → Create child session (session.py)
    → Resolve agent definition (definitions.py)
    → Setup isolation (isolation.py facade, implementation in isolation_*.py)
    → Build prompt (context.py)
    → Activate step workflow (if steps defined)
  → SpawnExecutor.execute()
    → Launch CLI subprocess (tmux/spawner.py)
  → AgentRunner.track()
    → Monitor process, detect completion
    → Publish completion event
```

## Guides

- [Agents](../../docs/guides/agents.md) — Agent definitions, step workflows, isolation, lifecycle
- [Orchestration](../../docs/guides/orchestration.md) — How agents are dispatched in pipeline-based and MCP tool-based orchestration

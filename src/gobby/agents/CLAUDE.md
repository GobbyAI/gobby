# Agents Source Reference

This directory implements agent spawning, process management, isolation, and lifecycle.

## Key Classes

| Class | File | Purpose |
|-------|------|---------|
| `AgentSpawner` | `spawn.py` | Prepares agent spawns: session creation, isolation, prompt building |
| `SpawnExecutor` | `spawn_executor.py` | Executes the prepared spawn (process creation) |
| `AgentRunner` | `runner.py` | Manages running agent processes, completion tracking |
| `AgentRegistry` | `registry.py` | In-memory registry of running agents |
| `IsolationHandler` | `isolation.py` | Worktree/clone/none isolation handlers |

## File Index

### Spawning
- `spawn.py` — `AgentSpawner`: creates child session, builds prompt, prepares environment, activates step workflow
- `spawn_executor.py` — `SpawnExecutor`: launches CLI subprocess after spawn preparation
- `dry_run.py` — Dry-run spawn validation (checks definition, workflow, isolation without executing)

### Process Management
- `runner.py` — `AgentRunner`: process lifecycle, completion detection, result publishing
- `runner_models.py` — Data models for agent runs
- `runner_queries.py` — Database queries for agent run records
- `runner_tracking.py` — Token/turn tracking for running agents

### Isolation
- `isolation.py` — Three handlers: `NoneIsolationHandler`, `WorktreeIsolationHandler`, `CloneIsolationHandler`. Each prepares the working directory and git state.

### Sessions & Context
- `session.py` — Child session management (creation, variable initialization, ancestry)
- `context.py` — Prompt context building (preamble from agent definition + spawn prompt)
- `definitions.py` — Agent definition resolution from database

### Registry & Lifecycle
- `registry.py` — In-memory agent registry (tracks running processes)
- `lifecycle_monitor.py` — Background monitor for agent health and timeouts
- `sync.py` — Agent state synchronization

### Terminal Backend
- `tmux/` — Tmux spawner subdirectory:
  - `tmux/spawner.py` — `TmuxSpawner`: creates tmux sessions/panes for terminal-mode agents
- `spawners/` — CLI-specific spawners (Claude, Gemini, Codex)
- `pty_reader.py` — PTY output reader for process monitoring

### Other
- `constants.py` — Agent-related constants (depth limits, timeouts)
- `sandbox.py` — Sandbox configuration for agent processes
- `codex_session.py` — Codex CLI session adapter
- `gemini_session.py` — Gemini CLI session adapter

## Agent Spawn Flow

```
spawn_agent() called
  → AgentSpawner.prepare()
    → Create child session (session.py)
    → Resolve agent definition (definitions.py)
    → Setup isolation (isolation.py)
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
- [Orchestrator](../../docs/guides/orchestrator.md) — How agents are dispatched by the orchestrator

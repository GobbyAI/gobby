# AGENTS.md

This file provides guidance to Codex (OpenAI) when working with code in this repository.

## Project Overview

Gobby is a local-first daemon that unifies AI coding assistants (Codex, Claude Code, Gemini CLI) under one persistent, extensible platform. It provides:

- **Session management** that survives restarts and context compactions
- **Task system** with dependency graphs, TDD expansion, and validation gates
- **MCP proxy** with progressive discovery (tools stay lightweight until needed)
- **Workflow engine** that enforces steps, tool restrictions, and transitions
- **Worktree orchestration** for parallel development
- **Memory system** for persistent facts across sessions

## Development Commands

```bash
# Environment setup
uv sync                          # Install dependencies (Python 3.13+)

# Daemon management
uv run gobby start --verbose     # Start daemon with verbose logging
uv run gobby stop                # Stop daemon
uv run gobby status              # Check daemon status

# Code quality
uv run ruff check src/           # Lint
uv run ruff format src/          # Auto-format
uv run mypy src/                 # Type check

# Testing (run specific tests, not full suite)
uv run pytest tests/test_file.py -v    # Run specific test file
uv run pytest tests/storage/ -v        # Run specific module
uv run pytest tests/path/ --cov=gobby --cov-report=term-missing  # Add coverage to any run
```

**Coverage threshold**: 80% (enforced in CI and pre-push)

**Test markers**: `unit`, `slow`, `integration`, `e2e`

## Architecture Overview

```text
src/gobby/
├── cli/                    # CLI commands (Click)
├── runner.py              # Main daemon entry point
├── servers/               # HTTP and WebSocket servers
├── mcp_proxy/            # MCP proxy layer (20+ tool modules)
├── hooks/                # Hook event system
├── adapters/             # CLI-specific hook adapters (codex.py)
├── sessions/             # Session lifecycle and parsers
├── tasks/                # Task system (expansion, validation)
├── workflows/            # Workflow engine (state machine)
├── agents/               # Agent spawning logic
├── worktrees/            # Git worktree management
├── memory/               # Memory system (TF-IDF, semantic)
├── storage/              # SQLite storage layer
├── llm/                  # Multi-provider LLM abstraction
├── config/               # Configuration (YAML/JSON)
└── utils/                # Git, logging, project utilities
```

### Key File Locations

| Path | Purpose |
| :--- | :--- |
| `~/.gobby/config.yaml` | Daemon configuration |
| `~/.gobby/gobby-hub.db` | SQLite database |
| `.gobby/project.json` | Project metadata |
| `.gobby/tasks.jsonl` | Task sync file |

## Task Management

Before using Edit, Write, or NotebookEdit tools, create or claim a task:

```python
# Create task and claim it (sets status to in_progress)
call_tool("gobby-tasks", "create_task", {
    "title": "Fix bug",
    "task_type": "bug",
    "session_id": "<your_session_id>",  # From SessionStart context
    "claim": true  # Required to auto-claim the task
})

# Or claim an existing task
call_tool("gobby-tasks", "claim_task", {
    "task_id": "#123",  # The task to claim
    "session_id": "<your_session_id>"
})

# After work: commit with [task-id] prefix, then close
call_tool("gobby-tasks", "close_task", {
    "task_id": "...",
    "commit_sha": "..."
})
```

**If blocked**: Create or claim a task before using Edit, Write, or NotebookEdit tools.

## Session Context

Your `session_id` is injected at session start. Look for `Gobby Session Ref:` or `Gobby Session ID:` in your system context:

```text
Gobby Session Ref: #5
Gobby Session ID: <uuid>
```

**Note**: All `session_id` parameters accept #N, N, UUID, or prefix formats.

If not present, use `get_current_session`:

```python
call_tool("gobby-sessions", "get_current_session", {
    "external_id": "<your-codex-session-id>",
    "source": "codex"
})
```

## Spawned Agent Protocol

When spawned as a subagent (via `spawn_agent`), use these tools to communicate and terminate:

### 1. Get your session info

```python
call_tool("gobby-sessions", "get_current_session", {
    "external_id": "<your-session-id>",
    "source": "codex"
})
# Returns: {"session_id": "...", "agent_run_id": "..."}
```

### 2. Send results to parent

```python
call_tool("gobby-agents", "send_to_parent", {
    "message": "Task completed: implemented authentication flow"
})
```

### 3. Mark work complete

```python
call_tool("gobby-sessions", "mark_loop_complete", {
    "session_id": "<session_id>"
})
```

### 4. Terminate yourself

```python
call_tool("gobby-agents", "kill_agent", {
    "run_id": "<agent_run_id>"  # From get_current_session response
})
```

**IMPORTANT**: Do NOT use `/quit` or similar CLI commands - always use `kill_agent` to properly terminate.

## Code Conventions

Type hints required. Use `async/await` for I/O. Run `ruff format` and `ruff check` before committing.

## Troubleshooting

| Issue | Solution |
| ------- | ---------- |
| "Edit/Write blocked" | Create or claim a task first |
| "Task has no commits" | Commit with `[task-id]` in message before closing |
| "Agent depth exceeded" | Max nesting is 3 - reduce agent spawning depth |
| Import errors | Run `uv sync` |

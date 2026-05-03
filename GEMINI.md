# Gobby — Project Context

This file provides guidance for developing the Gobby codebase.

## Guiding Principles

These are enforced by hooks, rules and workflows.

1. **ALWAYS use progressive tool discovery.** Do not try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).
2. **NEVER create or leave monoliths.** Keep non-test Python, TypeScript, and CSS source files under 1,000 lines. For non-test `.py`, `.ts`, `.tsx`, and `.css` files only, you *MUST* search for an existing refactor task or create it if one does not already exist in gobby-tasks. Place it under the parent #12730 for the gobby project if working in gobby. Create a new epic only if no suitable open or active epic exists. Leave these tasks for another agent to pick up.
3. **ALWAYS create or claim a task before editing a file.** This applies to file edits only — no task needed for plan mode, research, investigation, or answering questions unless the user explicitly requests one.
4. **Validation runs when closing with a commit. If a commit is done, validation must run.** `skip_validation` is silently stripped when commits are attached.
5. **NEVER close a task without a commit if there are diffs.** If you changed something, you have to commit it.
6. **NEVER stop while you have a claimed task in progress.** Your stop hook is blocked while you have a claimed task. Task must be closed before stopping. If you claim a task, you finish a task.
7. **NEVER mark a task as needs_review if you don't genuinely need the user to review your work.** Do not use it as a workaround to not committing/closing. Escalate to the user if you are genuinely stuck or need guidance.
8. **You found it, you own it.** Every error, test failure, lint warning, or type error you encounter is yours to fix — even if it's pre-existing, even if it's unrelated to your task. Fix it before closing your task. The only exception is something that genuinely requires multi-session architectural planning; even then, investigate thoroughly and attempt the fix before filing a task to defer it.
9. **ALWAYS use gobby-memory to record valuable memories.** You have access to a sophisticated memory system via gobby-memory through the MCP proxy. Use it to store and retrieve facts about the codebase, design decisions, and other relevant information.
10. **NEVER be a sycophant.** Do not agree with the user just for the sake of agreement. If you disagree with the user, you *MUST* voice your concerns and provide alternative solutions.
11. **NEVER leave options or unanswered questions in plans.** Plans are for execution, not exploration. If there are unanswered questions or ideas that need to be explored, explore them before finalizing the plan.
12. **ALWAYS choose/present the best approach to solve a problem. The best, most correct fix is *ALWAYS* in scope. NEVER choose or present the simplest approach if it is not the best or most complete/correct approach.**
13. **ALWAYS remember: Rule templates are not rules.** Templates must be installed in the rules engine to function. Templates are enabled by default and sync to the DB on first startup. The DB is the source of truth — before telling the user a rule is disabled, check the installed version in the DB.
14. **Agent depth limit of 5.** No recursive agent chains deeper than 5 levels.

## Progressive Tool Discovery Enforced by Hooks

Gobby uses an MCP proxy with progressive discovery. This means that you can't just call any tool you want.
Each step (list_mcp_servers, list_tools, get_tool_schema, call_tool) is a separate top-level tool (e.g., mcp__gobby__list_mcp_servers).
Load each via ToolSearch before first use.
Do NOT try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).

## DO NOT RUN THE FULL PYTEST SUITE

The repo has over 15,000 tests. Running the full suite takes over 30 minutes. Do not run the full suite unless explicitly asked to do so.

## Plan Mode

Task management MCP calls (gobby-tasks) are allowed during plan mode. Planning includes organizing work, not just designing it.

## Project Overview

Gobby is a local-first daemon that unifies AI coding assistants (Claude Code, Gemini CLI, Codex CLI) under one persistent, extensible platform. It provides:

- **Session management** that survives restarts and context compactions
- **Task system** with dependency graphs, TDD expansion, and validation gates
- **MCP proxy** with progressive discovery (tools stay lightweight until needed)
- **Rule engine** with declarative enforcement (block, set_variable, inject_context, mcp_call)
- **On-demand workflows** for structured multi-step processes (plan-execute, TDD, etc.)
- **Pipeline system** for deterministic automation with approval gates
- **Agent spawning** with P2P messaging, command coordination, and worktree isolation
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

## Web Frontend Development

When working in the `web/` directory (especially within isolated clones):

- **Setup:** Run `npm install` inside `web/`.
- **Dev Server:** Run `npm run dev` (Vite). If working in a clone and you need to test visually, the clone's dev server will run on a non-standard port. You can expose it using tailscale: `tailscale serve --bg localhost:<port>`.
- **Type Checking:** Run `npm run type-check` (`tsc --noEmit`) to verify TypeScript errors before committing.
- **Building:** Run `npm run build`.
- **Linting:** Run `npm run lint`.
- **Testing:** Run `npm run test` (Vitest).

## Architecture Overview

```text
src/gobby/
├── cli/                    # CLI commands (Click)
├── runner.py              # Main daemon entry point
├── servers/               # HTTP and WebSocket servers
├── mcp_proxy/            # MCP proxy layer (20+ tool modules)
├── hooks/                # Hook event system
├── adapters/             # CLI-specific hook adapters (gemini.py)
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

Before editing files, create or claim a task:

```python
# Create task (with claim=true to auto-claim and set status to in_progress)
call_tool("gobby-tasks", "create_task", {
    "title": "Fix bug",
    "task_type": "bug",
    "session_id": "<your_session_id>",  # From SessionStart context
    "claim": True  # Required to auto-claim the task
})

# Or claim an existing task
call_tool("gobby-tasks", "claim_task", {
    "task_id": "#123",  # The task to claim
    "session_id": "<your_session_id>"
})

# After work: commit with [gobby-#task-id] prefix, then close
# Example: git commit -m "[gobby-#6961] Fix authentication bug"
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
    "external_id": "<your-gemini-session-id>",
    "source": "gemini"
})
```

## Spawned Agent Protocol

When spawned as a subagent (via `spawn_agent`), follow the workflow instructions provided at session start. The workflow will guide you through the task lifecycle.

**Key points:**
- Your workflow instructions are injected at session start and step transitions
- Follow the workflow's termination instructions (typically `kill_agent`)
- Do NOT use `/quit` or similar CLI commands

### Send results to parent

```python
call_tool("gobby-agents", "send_to_parent", {
    "session_id": "<your_gobby_session_id>",
    "content": "Task completed: implemented authentication flow"
})
```

### Terminate (when workflow instructs)

```python
call_tool("gobby-agents", "kill_agent", {
    "session_id": "<your_gobby_session_id>"
})
```

## Code Conventions

Type hints required. Use `async/await` for I/O. Run `ruff format` and `ruff check` before committing.

## Troubleshooting

| Issue | Solution |
| ------- | ---------- |
| "Edit/Write blocked" | Create or claim a task first |
| "Task has no commits" | Commit with `[task-id]` in message before closing |
| "Agent depth exceeded" | Max nesting is 5 — reduce agent spawning depth (enforced maximum). |
| Import errors | Run `uv sync` |

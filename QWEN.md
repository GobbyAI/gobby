# Gobby — Project Context

This file provides guidance for developing the Gobby codebase.

## Guiding Principles

These are enforced by hooks, rules and workflows.

1. **ALWAYS use progressive tool discovery.** Do not try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).
2. **NEVER create or leave monoliths.** Before editing hand-maintained production `.py`, `.ts`, `.tsx`, `.css`, `.rs`, `.js`, `.mjs`, `.cjs`, or `.sh` files, check current and projected line counts. Exactly 1,000 lines violates the ceiling. If an edit touches or would produce a file at or above the ceiling, load `decompose-monolith` and complete the decomposition inside the current claimed task and session. Loading the skill permits structural edits; every touched applicable file must be below 1,000 lines before commit, task or review completion, and turn end. Tests, documentation, generated or vendored sources, baselines, and fixtures are excluded. Deferred refactor tasks are prohibited for threshold violations.
3. **ALWAYS create or claim a task before editing a file.** This applies to file edits only — no task needed for plan mode, research, investigation, or answering questions unless the user explicitly requests one.
4. **Closing a leaf task is a checklist:** linked commit, no uncommitted task edits, a clean validation run visible in your session transcript, and a bounded criteria review.
5. **NEVER close a task without a commit if there are diffs.** If you changed something, you have to commit it.
6. **NEVER stop while you have a claimed task in progress.** Your stop hook is blocked while you have a claimed task. Task must be closed before stopping. If you claim a task, you finish a task.
7. **Escalate only when the user explicitly needs to review your work, your agent skill/workflow/pipeline directs escalation, or you are genuinely stuck and need guidance.** Do not use escalation as a workaround for committing, closing, or completing required validation.
8. **You found it, you fix it — in this session.** Every error, test failure,
   lint warning, or type error you encounter is yours to fix before closing —
   including breakage already present in committed code, no matter which task
   or commit introduced it. Filing a task for a finding is deferral, not
   fixing, and deferral is never yours to self-grant. The single exclusion is
   another active session's or agent's uncommitted files in the shared
   worktree, and it exists for exactly one reason: never destroy in-flight
   work. It does not hand the finding off. Leave those paths untouched — do
   not modify, format, stage, commit, or roll them back — and send the owner
   (resolved from session/task file-attribution metadata) the exact failing
   command, diagnostics, and affected paths via `gobby-agents:send_message`;
   if no owner resolves, tell the user. Failures confined to those uncommitted
   foreign paths are the only ones that do not block your close gates. If a
   fix is genuinely too large to land in this session, say so and let the user
   decide — only the user can approve a deferral task.
9. **ALWAYS use gobby-memory to record valuable memories.** You have access to a sophisticated memory system via gobby-memory through the MCP proxy. Use it to store and retrieve facts about the codebase, design decisions, and other relevant information.
10. **NEVER be a sycophant.** Do not agree with the user just for the sake of agreement. If you disagree with the user, you *MUST* voice your concerns and provide alternative solutions.
11. **NEVER leave options or unanswered questions in plans.** Plans are for execution, not exploration. If there are unanswered questions or ideas that need to be explored, explore them before finalizing the plan.
12. **ALWAYS solve the whole problem with the least mechanism that solves it.** Correctness and completeness are non-negotiable — a shortcut that dodges the root cause, skips edge cases, or ships a partial fix is a cop-out, not simplicity. Among approaches that fully solve the problem, prefer the one with the least unjustified mechanism. Complexity must earn its place; so must every line of code.
13. **ALWAYS remember: Rule templates are not rules.** Templates must be installed in the rules engine to function. Templates are enabled by default and sync to the DB on first startup. The DB is the source of truth — before telling the user a rule is disabled, check the installed version in the DB.
14. **ALWAYS prefer gcode over grep/rg/sed/awk/nl.** gcode is an advanced code index/graph tool and is *FAR* superior to grep/rg/sed/awk/nl for code search and analysis.
15. **NEVER guess or assume unless explicitly asked.** Only state things you *KNOW* to be true, otherwise challenge your guess or assumption through exploration, research, and/or tool use.
16. **DO NOT CREATE BACKWARD COMPATIBILITY.** We haven't shipped 0.5.0 yet. There is no backward compatibility to maintain.
17. **Agent depth limit of 5.** No recursive agent chains deeper than 5 levels.
18. **ALWAYS use `gobby-agents:send_message` for direct cross-session agent communication.** Reserve `gobby-sessions:send_keys` for terminal control.

## Progressive Tool Discovery Enforced by Hooks

Gobby uses an MCP proxy with progressive discovery. This means that you can't just call any tool you want.
Each step (list_mcp_servers, list_tools, get_tool_schema, call_tool) is a separate top-level tool (e.g., mcp__gobby__list_mcp_servers).
Load each via ToolSearch before first use.
Do NOT try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).

## DO NOT RUN THE FULL PYTEST SUITE

The repo has over 15,000 tests. Running the full suite takes over 30 minutes. Do not run the full suite unless explicitly asked to do so.

When running pytest as an agent, always prefix pytest commands with `GOBBY_TEST_PROTECT=1`.

Pytest must be isolated from the user’s running Gobby daemon and real local daemon state. Tests that need daemon behavior must start/use an isolated test
daemon with temporary state and ports; they must not talk to the existing user daemon.

Daemon logs are in `~/.gobby/logs/`.

## Plan Mode

Task management MCP calls (gobby-tasks) are allowed during plan mode. Planning includes organizing work, not just designing it.

## Project Overview

A local-first daemon to unify your AI coding tools. Session tracking and handoffs across Claude Code, Codex, Droid, Gemini, and QwenCode. An MCP proxy that discovers tools without flooding context. Task management with dependencies, validation, and TDD expansion. Agent spawning and worktree orchestration. Persistent memory, extensible workflows, and hooks.

- **Session management** that survives restarts and context compactions
- **Task system** with dependency graphs, TDD expansion, and validation gates
- **MCP proxy** with progressive discovery (tools stay lightweight until needed)
- **Rule engine** with declarative enforcement (block, set_variable, inject_context, mcp_call)
- **On-demand workflows** for structured multi-step processes (plan-execute, TDD, etc.)
- **Pipeline system** for deterministic automation with approval gates
- **Agent spawning** with P2P messaging, command coordination, and worktree isolation
- **Memory system** for persistent facts across sessions

**Key characteristics:**

- Python 3.13+ package, distributed via PyPI
- Local daemon with HTTP, WebSocket, and MCP endpoints — no cloud dependency
- PostgreSQL-backed hub storage with a sophisticated rule engine, workflow/pipeline system, and memory system (keyword + vector search via Qdrant)
- Built with extensive type hints, async/await throughout, and full test coverage (80%+ enforced)
- "Built with Gobby" — most of the codebase was written by AI agents using Gobby's own task system

**Core subsystems:**

| Module | Purpose |
|---|---|
| `cli/` | Click-based CLI commands (~25 subcommands) |
| `servers/` | FastAPI HTTP server + WebSocket endpoints |
| `mcp_proxy/` | MCP proxy with progressive tool discovery |
| `hooks/` + `adapters/` | Event-driven hook system with CLI-specific adapters |
| `agents/` | Agent spawning with tmux, worktree/clone isolation |
| `sessions/` | Session lifecycle, transcript parsing, context compaction |
| `tasks/` | Task system with dependency graphs, TDD expansion, validation |
| `workflows/` | Rule engine, workflow engine, pipeline executor (~47 modules) |
| `memory/` | Persistent memory with semantic + keyword search |
| `storage/` | Hub storage layer with migrations and backend adapters (~20 modules) |
| `skills/` | Skill management (SKILL.md format, filesystem/GitHub/ZIP sources) |
| `config/` | YAML-based daemon configuration (~15 modules) |
| `llm/` | Multi-provider LLM abstraction (Claude, Gemini, OpenAI-compatible) |
| `conductor/` | Orchestration daemon with token budget tracking |
| `scheduler/` | Cron job scheduler |
| `code_index/` | AST-aware code indexing (gcode integration) |

## Building and Running

All development uses `uv`. Python 3.13+ is required.

### Setup

```bash
uv sync                          # Install runtime + dev dependencies
```

### Daemon Management

```bash
uv run gobby start --verbose     # Start daemon with verbose logging
uv run gobby stop                # Stop daemon
uv run gobby restart             # Restart daemon
uv run gobby status              # Check daemon health
```

### Project Initialization

```bash
uv run gobby init                # Initialize .gobby state for this repo
uv run gobby install             # Detect and install hooks for supported CLIs
```

### Code Quality

```bash
uv run ruff check src/           # Lint (line length: 100, target: py313)
uv run ruff format src/          # Auto-format
uv run mypy src/                 # Strict type checking
```

### Testing

```bash
# Run a specific test file (preferred during development)
uv run pytest tests/tasks/test_validation.py -v

# Run a specific module
uv run pytest tests/storage/ -v

# Run with coverage
uv run pytest tests/workflows/ --cov=gobby --cov-report=term-missing

# Exclude slow tests
uv run pytest -m "not slow"

# Run integration tests only
uv run pytest -m integration
```

**Important:** The repo has over 15,000 tests (30+ min full run). Do NOT run the full suite unless explicitly asked. Target specific files or modules.

**Coverage threshold:** 80% minimum (enforced in CI and pre-push).

**Test markers:** `unit`, `slow`, `integration`, `e2e`, `cli`, `no_config_protection`.

### Pipeline Management

```bash
uv run gobby pipelines list            # List available pipelines
uv run gobby pipelines run <name>      # Run a pipeline
uv run gobby pipelines status <id>     # Check execution status
uv run gobby pipelines approve <token> # Approve waiting pipeline
uv run gobby pipelines reject <token>  # Reject waiting pipeline
```

## Development Conventions

### Coding Style

- **Python 3.13+** with full type hints on all functions
- **4-space indentation**, lines within Ruff's 100-character limit
- **`snake_case`** for modules/functions, **`PascalCase`** for classes, **`test_*.py`** for test files
- **`async`/`await`** for I/O-heavy paths
- Keep applicable source files **under 1,000 lines** by decomposing them in the current task
- Prefer small, focused modules within existing package boundaries

### Error Handling

Use specific exceptions, never bare `except`. Use structured logging with context.

### Database Access

Always use connection context managers:

```python
with self.db.transaction() as conn:
    conn.execute("INSERT INTO tasks VALUES (?, ?)", (task_id, title))
```

### Commit Messages

Follow the task-linked pattern: `[gobby-#NNNNN] <type>: <summary>`
Types: `fix`, `feat`, `refactor`, `chore`.

### Agent Workflow (Critical)

Before editing files, **create or claim a Gobby task** and work under that task. Use the `gobby-tasks` MCP server for task lifecycle operations — **never** use the `gobby tasks` CLI or direct storage/SQL/REST mutations for agent task writes. The MCP path is the only path that correctly updates workflow/session state.

When working task state:

- Use lifecycle MCP tools: `create_task` (with `claim=true`), `claim_task`, `close_task`, `reopen_task`, `escalate_task`; use `gobby-tasks-ops` review tools such as `submit_for_review(stage_name="...")`
- Do NOT set generic `status`/`assignee` fields through `update_task`, CLI, or DB writes
- If `gobby-tasks` MCP is unavailable, stop and surface that as the blocker

### Templates vs Active Enforcement

Files in `src/gobby/install/shared/` (rules/, workflows/, agents/, pipelines/) are **templates**. They are synced to the `workflow_definitions` DB table on first startup. The **DB is the source of truth** for what's active, not the YAML template files.

## Key File Locations

| Path | Purpose |
|---|---|
| `~/.gobby/bootstrap.yaml` | Pre-DB bootstrap settings (ports, database URL, bind_host) |
| `bootstrap.yaml` `database_url` | Runtime hub database connection |
| `~/.gobby/logs/` | Log files |
| `.gobby/project.json` | Project metadata |
| `.gobby/tasks.jsonl` | Local task backup exported by pre-push; gitignored, imported only on demand |
| `src/gobby/runner.py` | Main daemon entry point (GobbyRunner) |
| `src/gobby/cli/__init__.py` | CLI entry point (Click) |

## Configuration

Configuration lives in `src/gobby/config/` with ~15 modules covering:

- `app.py` — DaemonConfig (YAML config model)
- `bootstrap.py` — Pre-DB bootstrap settings
- `features.py` — Feature flags
- `feature_base.py` — Feature LLM routing configuration
- `mcp.py` — MCP server configuration
- `tasks.py`, `sessions.py`, `skills.py` — Subsystem-specific config

## Troubleshooting

| Issue | Solution |
|---|---|
| Import errors | Run `uv sync` |
| Daemon won't start | Check logs in `~/.gobby/logs/` |
| MCP connection issues | Verify daemon is running: `gobby status` |
| Type errors | Run `uv run mypy src/` |
| Lint errors | Run `uv run ruff check src/ --fix` |

## Status

**Version:** 0.4.0 (pre-1.0, evolving rapidly)
**License:** Apache 2.0
**Roadmap:** Local AI integration testing, UI polish, onboarding improvements, bundled agent/workflow finalization (see `ROADMAP.md`)

# Gobby — Project Context

## Project Overview

Gobby is a **local-first daemon and workflow control plane for AI coding tools**. It unifies AI coding CLIs (Claude Code, Gemini CLI, Codex) under a single persistent platform with shared sessions, memory, tasks, workflows, and guardrails.

**Key characteristics:**
- Python 3.13+ package, distributed via PyPI
- Local daemon with HTTP, WebSocket, and MCP endpoints — no cloud dependency
- SQLite-backed storage with a sophisticated rule engine, workflow/pipeline system, and memory system (FTS5 + vector search via Qdrant)
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
| `storage/` | SQLite storage layer with migrations (~20 modules) |
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

**Important:** The repo has 14,000+ tests (30+ min full run). Do NOT run the full suite unless explicitly asked. Target specific files or modules.

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
- Keep files **under 1,000 lines** — create refactor tasks if needed
- Prefer small, focused modules within existing package boundaries

### Error Handling

Use specific exceptions, never bare `except`. Use structured logging with context.

### SQLite

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
- Use lifecycle MCP tools: `create_task` (with `claim=true`), `claim_task`, `mark_task_needs_review`, `close_task`, `reopen_task`, `escalate_task`
- Do NOT set generic `status`/`assignee` fields through `update_task`, CLI, or DB writes
- If `gobby-tasks` MCP is unavailable, stop and surface that as the blocker

### Templates vs Active Enforcement

Files in `src/gobby/install/shared/` (rules/, workflows/, agents/, pipelines/) are **templates**. They are synced to the `workflow_definitions` DB table on first startup. The **DB is the source of truth** for what's active, not the YAML template files.

## Key File Locations

| Path | Purpose |
|---|---|
| `~/.gobby/bootstrap.yaml` | Pre-DB bootstrap settings (ports, db_path, bind_host) |
| `~/.gobby/gobby-hub.db` | SQLite database |
| `~/.gobby/logs/` | Log files |
| `.gobby/project.json` | Project metadata |
| `.gobby/tasks.jsonl` | Task sync file (git-native) |
| `src/gobby/runner.py` | Main daemon entry point (GobbyRunner) |
| `src/gobby/cli/__init__.py` | CLI entry point (Click) |

## Configuration

Configuration lives in `src/gobby/config/` with ~15 modules covering:
- `app.py` — DaemonConfig (YAML config model)
- `bootstrap.py` — Pre-DB bootstrap settings
- `features.py` — Feature flags
- `llm_providers.py` — LLM provider configuration
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

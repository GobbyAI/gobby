# Contributing to Gobby
<!-- guardrail test -->

Thank you for your interest in contributing to Gobby! This document provides guidelines and information for contributors.

## Development Setup

### Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) package manager
- At least one supported AI CLI for testing:
  - [Claude Code](https://claude.ai/code)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli)
  - [Codex CLI](https://github.com/openai/codex)

### Getting Started

```bash
# Clone the repository
git clone https://github.com/GobbyAI/gobby.git
cd gobby

# Install dependencies
uv sync

# Initialize project (use -C to target a different directory)
uv run gobby init

# Install hooks for detected CLIs
uv run gobby install

# Run the daemon in development mode
uv run gobby start --verbose
```

## Development Workflow

### Running the Daemon

```bash
# Start with verbose logging
uv run gobby start --verbose

# Check status
uv run gobby status

# Restart the daemon
uv run gobby restart

# Stop the daemon
uv run gobby stop
```

### Code Quality

We use automated tools to maintain code quality. Run these before submitting a PR:

```bash
# Linting
uv run ruff check src/

# Auto-format code
uv run ruff format src/

# Type checking
uv run mypy src/
```

### Code Style

- We use [Ruff](https://github.com/astral-sh/ruff) for linting and formatting
- Line length: 100 characters
- Target Python version: 3.13
- Type hints are required for all functions
- Follow PEP 8 conventions
- Use async for I/O-bound operations
- Use specific exceptions, not bare `except`
- Always use connection context managers for SQLite
- Use structured logging with context

## Pull Request Process

1. **Fork the repository** and create a feature branch from `main`
2. **Make your changes** following the code style guidelines
3. **Write tests** for new functionality
4. **Run the full test suite** to ensure nothing is broken
5. **Update documentation** if you're adding or changing features
6. **Submit a pull request** with a clear description of changes

### PR Guidelines

- Keep PRs focused on a single change
- Write clear commit messages
- Reference any related issues
- Ensure CI passes before requesting review

## Testing

The full test suite runs pre-push. During development, run specific tests rather than the full suite:

```bash
# Run a specific test file
uv run pytest tests/test_example.py -v

# Run a specific test module
uv run pytest tests/storage/ -v

# Run tests excluding slow tests
uv run pytest -m "not slow"

# Run only integration tests
uv run pytest -m integration
```

`pre-push-test.sh` requires a PostgreSQL test DSN for hub runtime tests. It resolves
one in this order:

1. `DATABASE_URL`, when already exported.
2. The configured bootstrap `database_url`.
3. A Docker fallback from `docker-compose.test.yml`.

The Docker fallback accepts either `docker compose` or `docker-compose`, starts
the `postgres-test` service, and exports both `DATABASE_URL` and
`GOBBY_POSTGRES_TEST_DSN` to pytest. The container listens on
`${GOBBY_POSTGRES_TEST_PORT:-60892}` with default database, user, and password
from `GOBBY_POSTGRES_TEST_DB`, `GOBBY_POSTGRES_TEST_USER`, and
`GOBBY_POSTGRES_TEST_PASSWORD`.

### Test Coverage

We maintain a minimum of 80% test coverage (enforced in CI).

### Test Markers

Use markers to categorize tests: `unit`, `slow`, `integration`, `e2e`.

## Project Structure

```text
src/gobby/
├── cli/                    # CLI commands (Click, ~25 modules)
│   ├── __init__.py        # Main CLI group
│   ├── daemon.py          # start, stop, restart, status
│   ├── agents.py          # Agent management
│   ├── rules.py           # Rule management
│   └── ...                # sessions, worktrees, memory, pipelines, etc.
│
├── runner.py              # Main daemon entry point (GobbyRunner)
│
├── servers/               # HTTP and WebSocket servers
│   ├── http.py           # FastAPI HTTP server
│   ├── routes/           # HTTP API routes (tasks, sessions, agents, etc.)
│   └── websocket/        # WebSocket server (broadcast, chat, voice, tmux)
│
├── mcp_proxy/            # MCP proxy layer
│   ├── server.py         # FastMCP server implementation
│   ├── manager.py        # MCPClientManager (connection pooling)
│   ├── tools/            # 20+ internal tool modules
│   └── transports/       # HTTP, stdio, WebSocket transports
│
├── hooks/                # Hook event system
│   ├── hook_manager.py   # Central coordinator
│   ├── events.py         # HookEvent, HookResponse models
│   └── skill_manager.py  # Skill discovery for hooks
│
├── adapters/             # CLI-specific hook adapters
│   ├── claude_code.py    # Claude Code adapter
│   ├── gemini.py         # Gemini CLI adapter
│   └── codex_impl/       # Codex adapter implementation
│
├── agents/               # Agent spawning and lifecycle
│   ├── spawn.py          # Agent spawner
│   ├── runner.py         # AgentRunner process management
│   ├── definitions.py    # Agent definition models
│   └── registry.py       # Agent registry (DB-backed)
│
├── sessions/             # Session lifecycle
├── tasks/                # Task system (expansion, validation)
│
├── workflows/            # Rule engine and workflow system (~47 modules)
│   ├── rule_engine.py    # RuleEngine (declarative enforcement)
│   ├── definitions.py    # Rule/workflow/agent definition models
│   ├── safe_evaluator.py # Safe expression evaluator
│   ├── engine.py         # WorkflowEngine (on-demand state machines)
│   └── pipeline_executor.py  # PipelineExecutor
│
├── memory/               # Persistent memory system
├── conductor/            # Orchestration daemon
├── skills/               # Skill management
├── storage/              # SQLite storage layer (~20 modules)
├── llm/                  # Multi-provider LLM abstraction
├── config/               # Configuration (~15 modules)
└── utils/                # Utilities (git, daemon client, etc.)
```

## Reporting Issues

When reporting issues, please include:

- Python version (`python --version`)
- Operating system
- Gobby version
- Steps to reproduce
- Expected vs actual behavior
- Relevant log output (from `~/.gobby/logs/`)

## Questions?

If you have questions, feel free to:

- Open a [GitHub Discussion](https://github.com/GobbyAI/gobby/discussions)
- Check existing issues for similar questions

## License

By contributing to Gobby, you agree that your contributions will be licensed under the Apache License 2.0.

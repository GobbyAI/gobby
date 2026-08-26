# Gobby Development Guide

> Updated: 2026-07-31

## Prerequisites

- **Python 3.13+** - Required for type hints and async features
- **uv** - Recommended package manager (or pip)
- **Git** - Version control

## Quick Start

```bash
# Clone repository
git clone https://github.com/GobbyAI/gobby.git
cd gobby

# Install dependencies
uv sync

# Start daemon
uv run gobby start --verbose

# Verify running
uv run gobby status
```

## Development Commands

### Daemon Management

| Command | Description |
|---------|-------------|
| `uv run gobby start` | Start daemon (background) |
| `uv run gobby start --verbose` | Start with debug logging |
| `uv run gobby stop` | Stop daemon |
| `uv run gobby status` | Show daemon status |
| `uv run gobby restart` | Restart daemon |

### Hook Installation

| Command | Description |
|---------|-------------|
| `uv run gobby install` | Full install: managed stack, daemon config, hooks for all detected CLIs |
| `uv run gobby install claude` | Claude Code hooks only (existing install) |
| `uv run gobby install agy` | AGY CLI hooks only (existing install) |
| `uv run gobby install codex` | Codex CLI hooks only (existing install) |
| `uv run gobby uninstall` | Remove all hooks and managed tools; never touches Docker or data |

### Project Initialization

| Command | Description |
|---------|-------------|
| `uv run gobby init` | Initialize project in cwd |
| `uv run gobby init --name "MyProject"` | With custom name |

## Testing

### Run Tests

```bash
# All tests (no coverage by default; 15k+ tests, 30+ minutes)
uv run pytest

# Single file
uv run pytest tests/test_config.py -v

# Specific test
uv run pytest tests/test_config.py::test_load_config -v

# Skip slow tests
uv run pytest -m "not slow"

# Only integration tests
uv run pytest -m integration
```

### Coverage

```bash
# Terminal report
uv run pytest --cov=gobby --cov-report=term-missing

# HTML report
uv run pytest --cov=gobby --cov-report=html
open htmlcov/index.html

# XML report (for CI)
uv run pytest --cov=gobby --cov-report=xml
```

**Coverage Threshold:** 80% (enforced in CI and pre-push, not by bare `pytest`)

## Code Quality

### Linting

```bash
# Check for issues
uv run ruff check src/

# Auto-fix issues
uv run ruff check src/ --fix

# Check specific file
uv run ruff check src/gobby/cli/daemon.py
```

### Formatting

```bash
# Format code
uv run ruff format src/

# Check without changes
uv run ruff format --check src/
```

### Type Checking

```bash
# Full type check
uv run mypy src/

# Single file
uv run mypy src/gobby/runner.py
```

## Common Workflows

### 1. Making Code Changes

```bash
# 1. Make your changes
# 2. Run quality checks
uv run ruff check src/ --fix
uv run ruff format src/
uv run mypy src/

# 3. Run tests
uv run pytest

# 4. Commit
git add .
git commit -m "feat: description"
```

### 2. Adding Dependencies

```bash
# Production dependency
uv add <package>

# Development dependency
uv add --group dev <package>

# Sync environment from lockfile
uv sync
```

### 3. Debugging

```bash
# Start with verbose logging
uv run gobby start --verbose

# Watch logs
tail -f ~/.gobby/logs/gobby.log

# Watch error logs
tail -f ~/.gobby/logs/gobby-error.log
```

### 4. Testing MCP Tools

```bash
# Add gobby-daemon MCP server to Claude Code
claude mcp add --transport stdio gobby-daemon -- gobby mcp-server

# Test via Claude Code
# Ask Claude to call gobby tools like status(), list_tools(), etc.
```

## Project Structure

```
src/gobby/
├── cli/                # CLI commands (Click); entry point gobby.cli:cli
├── runner.py           # Daemon process - start here for server changes
├── adapters/           # Add new CLI support here
├── hooks/              # Hook event handling
├── mcp_proxy/          # MCP tools and proxy logic
├── servers/            # HTTP and WebSocket servers
├── sessions/           # Session management
├── storage/            # Database operations
└── utils/              # Shared utilities
```

## Key Files to Know

| File | When to Edit |
|------|--------------|
| `cli/` (package) | Adding new CLI commands |
| `adapters/*.py` | Supporting new CLI tools |
| `hooks/events.py` | Adding new hook event types |
| `mcp_proxy/server.py` | Adding new MCP tools |
| `config/app.py` | Adding new config options |
| `crates/gcore/assets/schema/` | Changing database schema |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `GOBBY_HOME` | Custom Gobby home directory (default `~/.gobby`); runtime config lives in the PostgreSQL hub, with pre-DB settings in `~/.gobby/bootstrap.yaml` |
| `ANTHROPIC_API_KEY` | Claude API (BYOK mode) |
| `OPENAI_API_KEY` | OpenAI API (BYOK mode) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google/Vertex ADC forwarded to supported CLIs |

## Troubleshooting

### Daemon won't start

```bash
# Check if already running
uv run gobby status

# Kill stale processes
pkill -f "gobby.runner"

# Check port availability
lsof -i :60887
lsof -i :60888

# Check logs
cat ~/.gobby/logs/gobby-error.log
```

### Tests failing

```bash
# Run with verbose output
uv run pytest -vvs

# Run specific failing test
uv run pytest tests/test_file.py::test_name -vvs

# Check for missing dependencies
uv sync --dev
```

### Type errors

```bash
# Show detailed errors
uv run mypy src/ --show-error-codes

# Check specific module
uv run mypy src/module.py
```

_Last verified: 2026-07-31_

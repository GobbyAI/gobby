# Contributing to Gobby

Thank you for your interest in contributing to Gobby! This document provides guidelines and information for contributors.

## Development Setup

### Prerequisites

- Python 3.11+
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

# Run the daemon in development mode
uv run gobby start --verbose

# Run tests to verify setup
uv run pytest
```

## Development Workflow

### Running the Daemon

```bash
# Start with verbose logging
uv run gobby start --verbose

# Check status
uv run gobby status

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

# Run all tests
uv run pytest

# Run tests with coverage report
uv run pytest --cov=src/gobby --cov-report=html
```

### Code Style

- We use [Ruff](https://github.com/astral-sh/ruff) for linting and formatting
- Line length: 100 characters
- Target Python version: 3.11
- Type hints are required for all functions
- Follow PEP 8 conventions

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

### Running Tests

```bash
# Run all tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_example.py -v

# Run tests excluding slow tests
uv run pytest -m "not slow"

# Run only integration tests
uv run pytest -m integration
```

### Test Coverage

We maintain a minimum of 80% test coverage. The CI will fail if coverage drops below this threshold.

## Project Structure

```text
gobby/
├── src/                    # Main source code
│   ├── cli.py             # CLI commands
│   ├── runner.py          # Daemon runner
│   ├── config/            # Configuration management
│   ├── hooks/             # Hook system
│   ├── install/           # Hook installation scripts
│   ├── llm/               # LLM provider integrations
│   ├── mcp_proxy/         # MCP client manager
│   ├── servers/           # HTTP and WebSocket servers
│   ├── sessions/          # Session management
│   ├── storage/           # SQLite storage layer
│   └── tools/             # Tool schema management
├── tests/                 # Test files
├── docs/                  # Documentation
└── .github/               # GitHub workflows and templates
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

By contributing to Gobby, you agree that your contributions will be licensed under the MIT License.

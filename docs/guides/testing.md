# Testing Guide

Token-efficient test/lint/typecheck infrastructure for agent workflows.

## Overview

Agents running test/lint/typecheck commands via Bash dump massive output (hundreds to thousands of lines) into their context window, wasting tokens. `gobby-tests` solves this by:

1. Running commands via async subprocess, capturing output to disk
2. On success: returning a brief summary (last few lines)
3. On failure: using Haiku LLM to extract only the errors from output
4. Storing results in the DB for later retrieval

This works for any language — pytest, vitest, jest, cargo test, go test, eslint, clippy, mypy, tsc, and more.

## Quick Start

### 1. Ensure verification commands are configured

Check your `.gobby/project.json`:

```json
{
  "verification": {
    "unit_tests": "uv run pytest tests/ -x -q",
    "lint": "uv run ruff check src/",
    "type_check": "uv run mypy src/",
    "format": "uv run ruff format --check src/"
  }
}
```

These are auto-detected by `gobby init` for Python, Node.js, Rust, and Go projects.

### 2. Run a check via MCP

```
# Discover the tools
list_tools("gobby-tests")
get_tool_schema("gobby-tests", "run_check")

# Run a check
call_tool("gobby-tests", "run_check", {"category": "lint"})
```

### 3. Understand the output

**Success** — brief summary:
```json
{
  "success": true,
  "run_id": "tr-a1b2c3d4e5f6",
  "category": "lint",
  "status": "completed",
  "exit_code": 0,
  "summary": "All checks passed.\nFound 0 errors."
}
```

**Failure** — LLM-extracted errors only:
```json
{
  "success": true,
  "run_id": "tr-a1b2c3d4e5f6",
  "category": "unit_tests",
  "status": "failed",
  "exit_code": 1,
  "summary": "tests/test_foo.py:42 - AssertionError: expected 3, got 5\ntests/test_bar.py:18 - TypeError: missing arg 'config'"
}
```

## Verification Commands

### The project.json verification section

Standard categories:

| Category | Description | Example |
|----------|-------------|---------|
| `unit_tests` | Unit test suite | `uv run pytest tests/ -x -q` |
| `lint` | Linter | `uv run ruff check src/` |
| `type_check` | Type checker | `uv run mypy src/` |
| `format` | Format checker | `uv run ruff format --check src/` |
| `integration` | Integration tests | `uv run pytest tests/integration/` |
| `security` | Security scanner | `bandit -r src/` |

### Custom commands

Add any custom categories to the `custom` dict:

```json
{
  "verification": {
    "unit_tests": "uv run pytest tests/",
    "custom": {
      "ts_check": "cd web && npx tsc --noEmit",
      "frontend_tests": "cd web && npm test",
      "cargo_test": "cargo test"
    }
  }
}
```

Note: `ts_check` is treated as a standard key in some projects (added at top level).

## MCP Tools Reference

### run_check

Run a verification command by category.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `category` | string | yes | Verification category name |
| `paths` | string | no | Override target paths (appended to command) |
| `extra_args` | string | no | Extra arguments to append |
| `timeout` | int | no | Timeout in seconds (default 300) |

### get_run_status

Check if a run is complete. Useful after timeout recovery.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `run_id` | string | yes | Test run ID (tr-xxxxx) |

### get_run_result

Get detailed results with optional raw output.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `run_id` | string | yes | Test run ID (tr-xxxxx) |
| `include_output` | bool | no | Include paginated raw output |
| `output_offset` | int | no | Line offset for pagination |
| `output_limit` | int | no | Lines per page (default 50) |

## Blocking Rule

The `require-test-tools` rule template prevents agents from running test/lint/typecheck commands directly via Bash.

### What's blocked

- `uv run pytest`, `uv run mypy`
- `npm test`, `npm run lint`, `npm run typecheck`
- `npx vitest`, `npx jest`, `npx tsc`, `npx eslint`
- `cargo test`, `cargo clippy`
- `go test`, `go vet`
- `uv run ruff check` (without `--fix`)

### What's allowed

- `uv run ruff format`, `uv run ruff check --fix`
- `npm run format`, `npm run fix`
- `cargo fmt`, `go fmt`

### Enabling the rule

The rule template is disabled by default. Enable it via the rules engine or daemon config.

## Configuration

### TestSummarizerConfig

In `config.yaml` or via the config store:

```yaml
test_summarizer:
  enabled: true          # Enable LLM summarization on failure
  provider: claude       # LLM provider
  model: haiku           # Model (fast/cheap recommended)
  max_output_lines: 200  # Max lines sent to LLM
```

When LLM summarization is disabled or unavailable, the last 50 lines of output are returned as fallback.

## How It Works

1. **Command resolution**: `run_check` reads `.gobby/project.json` → `verification[category]`
2. **Execution**: `asyncio.create_subprocess_shell` with stdout capture
3. **Output capture**: Full output written to `~/.gobby/test_runs/{run_id}.log`
4. **Summarization**: Exit 0 → last few lines; Exit != 0 → Haiku extracts errors
5. **Storage**: Run metadata stored in `test_runs` DB table
6. **Retrieval**: `get_run_result` returns summary or paginated raw output

## See Also

- [tasks.md](tasks.md) — Task validation uses verification commands
- [rules.md](rules.md) — How blocking rules work
- [configuration.md](configuration.md) — Full config reference

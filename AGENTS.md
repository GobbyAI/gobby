# Repository Guidelines

## Project Structure & Module Organization
Core code lives in `src/gobby/`. Key areas include `cli/` for Click commands, `servers/` for HTTP/WebSocket endpoints, `mcp_proxy/` and `tools/` for tool execution, `sessions/`, `tasks/`, `workflows/`, `agents/`, `worktrees/`, `memory/`, and `storage/`. Tests live under `tests/`, usually grouped by module (`tests/tasks/`, `tests/workflows/`, `tests/memory/`). Project metadata and synced task state live in `.gobby/`.

## Build, Test, and Development Commands
Use `uv` for local development.

- `uv sync`: install runtime and dev dependencies for Python 3.13+.
- `uv run gobby start --verbose`: start the daemon with verbose logs.
- `uv run gobby status`: check daemon health.
- `uv run ruff format src/`: apply formatting.
- `uv run ruff check src/`: run lint checks.
- `uv run mypy src/`: run strict type checking.
- `uv run pytest tests/tasks/test_validation.py -v`: run a focused test file.
- `uv run pytest tests/workflows/ --cov=gobby --cov-report=term-missing`: run a module with coverage.

## Coding Style & Naming Conventions
Follow Python 3.13 conventions with full type hints and `async`/`await` for I/O-heavy paths. Use 4-space indentation and keep lines within Ruff’s 100-character limit. Modules and functions use `snake_case`; classes use `PascalCase`; test files follow `test_*.py`. Prefer small, focused modules in existing package boundaries rather than new top-level directories.

## Testing Guidelines
Pytest is the test runner, with markers including `unit`, `slow`, `integration`, `e2e`, and `cli`. Coverage below 80% fails CI, so add or update tests with code changes. Keep tests near the affected domain and use descriptive names such as `test_task_id_generation.py` or `test_worktree_merge_integration.py`. Avoid running the full suite unless necessary; target the relevant file or package first.

## Commit & Pull Request Guidelines
Recent history uses task-linked commits like `[gobby-#11184] fix: stop retrying transcript processing when JSONL file is missing`. Keep that pattern: `[gobby-#NNNNN] <type>: <summary>`. Typical types include `fix`, `feat`, `refactor`, and `chore`. PRs should explain the behavioral change, reference the task or issue, list validation performed, and include screenshots only for UI changes.

## Agent-Specific Workflow
Before editing files, create or claim a Gobby task and work under that task. For AI agents, use the `gobby-tasks` MCP server for task lifecycle operations, not the `gobby tasks` CLI and not direct storage/SQL/REST mutations. The MCP path updates workflow/session state such as claims, session links, and `task_claimed`; bypassing it can leave the repo in an inconsistent state.

When working task state as an agent:
- Prefer lifecycle MCP tools such as `create_task` with `claim=true`, `claim_task`, `mark_task_needs_review`, `close_task`, `reopen_task`, and `escalate_task`.
- When code changes are complete and the task is ready to finish, prefer `close_task(task_id, commit_sha="...")` so the commit is linked and the task is closed in one step.
- Use `link_commit` only when you intentionally need to attach a commit while keeping the task open, such as handing work off for review or continuing follow-up changes later.
- Do not claim work by setting generic `status`/`assignee` fields through `update_task`, CLI commands, database writes, or ad hoc scripts.
- Treat `gobby tasks ...` as a human/operator interface. Use it for manual inspection only when MCP is unavailable, not for agent task lifecycle writes.
- If the `gobby-tasks` MCP server is unavailable, stop and surface that as the blocker instead of mutating task state through another path.

If you change code, ensure the resulting commit is linked to the task before it is closed; `close_task(..., commit_sha=...)` is the default path for that. If blocked, document the blocker in the task rather than bypassing the workflow.

This file provides guidance for developing the Gobby codebase.

## Guiding Principles

These are enforced by hooks, rules and workflows.

1. **ALWAYS use progressive tool discovery.** Do not try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).
2. **NEVER create or leave monoliths.** Keep files under 1,000 lines. You *MUST* create a refactor tasks if one does not already exist in gobby-tasks.
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

The repo has over 11,000 tests. Running the full suite takes over 30 minutes. Do not run the full suite unless explicitly asked to do so.

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

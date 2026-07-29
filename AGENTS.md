## Guiding Principles

These are enforced by hooks, rules and workflows.

1. **ALWAYS use context-aware progressive tool discovery.** Call leased known tools directly; call `get_tool_schema` directly for known unleased tools; use `list_tools` only when the tool name is unknown and `list_mcp_servers` only when the server or registry is unknown. Skill bootstrap tools are exempt. Do not try to call one step through another (e.g., don't use `call_tool` to invoke `get_tool_schema`).
2. **NEVER create or leave monoliths.** Keep non-test Python, TypeScript, and CSS source files under 1,000 lines. For non-test `.py`, `.ts`, `.tsx`, and `.css` files only, you *MUST* search for an existing refactor task or create it if one does not already exist in gobby-tasks. Leave these tasks for another agent to pick up. Markdown files, including `docs/guides/*.md` and repo-root instruction files, are documentation artifacts and are not subject to this 1,000-line source-file rule; do not create refactor tasks or block docs work based only on Markdown line count.
3. **ALWAYS create or claim a task before editing a file.** This applies to file edits only — no task needed for plan mode, research, investigation, or answering questions unless the user explicitly requests one.
4. **Closing a leaf task is a checklist:** linked commit, no uncommitted task edits, a clean validation run visible in your session transcript, and a bounded criteria review.
5. **NEVER close a task without a commit if there are diffs.** If you changed something, you have to commit it.
6. **NEVER stop while you have a claimed task in progress.** Your stop hook is blocked while you have a claimed task. Task must be closed before stopping. If you claim a task, you finish a task.
7. **Escalate only when the user explicitly needs to review your work, your agent skill/workflow/pipeline directs escalation, or you are genuinely stuck and need guidance.** Do not use escalation as a workaround for committing, closing, or completing required validation.
8. **You found it, you own it — within clean or session-owned paths.** Every error,
   test failure, lint warning, or type error you encounter in a path that was clean
   when your task began, or that is attributed to your current session or task, is
   yours to fix before closing. A path already dirty in the shared worktree when
   your task begins is excluded and remains owned by the session or agent that
   dirtied it: do not modify, format, stage, commit, or destructively roll back that
   path. Resolve its holder from session/task file-attribution metadata and notify
   that session or agent with the exact failing command, diagnostics, and affected
   paths. If no holder can be resolved, notify the user or project operator.
   Failures confined to excluded dirty paths do not block your task's validation or
   close gates after a passing scoped rerun against owned or clean paths demonstrates
   that confinement. The only exception for an owned path is something that genuinely
   requires multi-session architectural planning; even then, investigate thoroughly
   and attempt the fix before filing a task to defer it.
9. **ALWAYS use gobby-memory to record valuable memories.** You have access to a sophisticated memory system via gobby-memory through the MCP proxy. Use it to store and retrieve facts about the codebase, design decisions, and other relevant information.
10. **NEVER be a sycophant.** Do not agree with the user just for the sake of agreement. If you disagree with the user, you *MUST* voice your concerns and provide alternative solutions.
11. **NEVER leave options or unanswered questions in plans.** Plans are for execution, not exploration. If there are unanswered questions or ideas that need to be explored, explore them before finalizing the plan.
12. **ALWAYS solve the whole problem with the least mechanism that solves it.** Correctness and completeness are non-negotiable — a shortcut that dodges the root cause, skips edge cases, or ships a partial fix is a cop-out, not simplicity. Among approaches that fully solve the problem, prefer the one with the least unjustified mechanism. Complexity must earn its place; so must every line of code.
13. **ALWAYS remember: Rule templates are not rules.** Templates must be installed in the rules engine to function. Templates are enabled by default and sync to the DB on first startup. The DB is the source of truth — before telling the user a rule is disabled, check the installed version in the DB.
14. **ALWAYS prefer gcode over grep/rg/sed/awk/nl.** gcode is an advanced code index/graph tool and is *FAR* superior to grep/rg/sed/awk/nl for code search and analysis.
15. **NEVER guess or assume unless explicitly asked.** Only state things you *KNOW* to be true, otherwise challenge your guess or assumption through exploration, research, and/or tool use.
16. **DO NOT CREATE BACKWARD COMPATIBILITY.** We haven't shipped 0.5.0 yet. There is no backward compatibility to maintain.
17. **Agent depth limit of 5.** No recursive agent chains deeper than 5 levels.

## Progressive Tool Discovery Enforced by Hooks

Gobby uses an MCP proxy with context-aware progressive discovery. A current-context schema lease permits direct `call_tool` use. For a known unleased tool, call `get_tool_schema` directly, then `call_tool`. Use `list_tools` only to discover an unknown tool name and `list_mcp_servers` only to inspect an unknown server or registry. `get_skill`, `list_skills`, and `search_skills` are enforcement-exempt and may be called directly.

`list_mcp_servers`, `list_tools`, `get_tool_schema`, and `call_tool` are separate top-level tools (for example, `mcp__gobby__get_tool_schema`). Load each via ToolSearch before first use. Do NOT try to call one step through another (for example, don't use `call_tool` to invoke `get_tool_schema`).

## DO NOT RUN THE FULL PYTEST SUITE

The repo has over 15,000 tests. Running the full suite takes over 30 minutes. Do not run the full suite unless explicitly asked to do so.

When running pytest as an agent, always prefix pytest commands with `GOBBY_TEST_PROTECT=1`.

Pytest must be isolated from the user’s running Gobby daemon and real local daemon state. Tests that need daemon behavior must start/use an isolated test
daemon with temporary state and ports; they must not talk to the existing user daemon.

Daemon logs are in `~/.gobby/logs/`.

## Plan Mode

Task management MCP calls (gobby-tasks) are allowed during plan mode. Planning includes organizing work, not just designing it.

## Project Overview

A local-first daemon to unify your AI coding tools. Session tracking and handoffs across Claude Code, Codex, Droid, Grok, Qwen, and AGY. An MCP proxy that discovers tools without flooding context. Task management with dependencies, validation, and TDD expansion. Agent spawning and worktree orchestration. Persistent memory, extensible workflows, and hooks.

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
- `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`: ratchet Python test typing.
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

- Prefer lifecycle MCP tools such as `create_task` with `claim=true`, `claim_task`, `close_task`, `reopen_task`, and `escalate_task`; use `gobby-tasks-ops` review tools such as `submit_for_review(stage_name="...")` when handing a stage to review.
- When code changes are complete and the task is ready to finish, prefer `close_task(task_id, commit_sha="...")` so the commit is linked and the task is closed in one step.
- Use `link_commit` only when you intentionally need to attach a commit while keeping the task open, such as handing work off for review or continuing follow-up changes later.
- Do not claim work by setting generic `status`/`assignee` fields through `update_task`, CLI commands, database writes, or ad hoc scripts.
- Treat `gobby tasks ...` as a human/operator interface. Use it for manual inspection only when MCP is unavailable, not for agent task lifecycle writes.
- If the `gobby-tasks` MCP server is unavailable, stop and surface that as the blocker instead of mutating task state through another path.

If you change code, ensure the resulting commit is linked to the task before it is closed; `close_task(..., commit_sha=...)` is the default path for that. If blocked, document the blocker in the task rather than bypassing the workflow.

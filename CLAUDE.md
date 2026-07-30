# CLAUDE.md

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
8. **You found it, you own it — within clean or session-owned paths.** Every error,
   test failure, lint warning, or type error you encounter in a path that was clean
   when your task began, or that is attributed to your current session or task, is
   yours to fix before closing. A path already dirty in the shared worktree when
   your task begins is excluded and remains owned by the session or agent that
   dirtied it: do not modify, format, stage, commit, or destructively roll back that
   path. Resolve its holder from session/task file-attribution metadata and notify
   that session or agent via `gobby-agents:send_message` with the exact failing
   command, diagnostics, and affected paths. If no holder can be resolved, notify
   the user or project operator.
   Failures confined to excluded dirty paths do not block your task's validation or
   close gates. The only exception for an owned path is something that genuinely
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

## Design Context

All design / UI / color / typography work — across every Gobby surface (product UI in `./web/`, the gobby.ai marketing site, Gobby Pro, installer, CLI/TUI) — must read `.impeccable.md` at the project root **and** load the `impeccable` skill before producing output. The file defines the design system, deutan-safe color constraints, WCAG 2.2 AA target, aesthetic references, and per-surface variation rules; the skill carries the dispatch table and steering references, and loading it is what puts the pairing in `loaded_skills` so it survives a context compaction. Update the file via the skill's `teach` mode rather than freehand edits.

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

## Plan-Coverage Contract

The full contract — canonical section-heading regex, section kinds,
acceptance-item shape, typed deferrals, `covers:` labels, the
`## M1 Task Manifest` schema, parser modes, CLI synopsis, and plan-registry
storage — lives in `docs/contracts/plan-coverage.md`. Read it before authoring,
reviewing, or expanding any plan. The authoring surface is
`src/gobby/install/shared/skills/plan-draft/SKILL.md`; review and expansion
surfaces link back to it.

## Development Commands

# IMPORTANT: Use uv for all Python operations. This includes running tests, formatting, linting, and installing dependencies

```bash
# Environment setup
uv sync                          # Install dependencies (Python 3.13+)

# Daemon management
uv run gobby start --verbose     # Start daemon with verbose logging
uv run gobby stop                # Stop daemon
uv run gobby restart             # Restart daemon
uv run gobby status              # Check daemon status

# Project initialization
uv run gobby init                # Initialize project (.gobby/)
uv run gobby install             # Install hooks for detected CLIs

# Code quality
uv run ruff check src/           # Lint
uv run ruff format src/          # Auto-format
uv run mypy src/                 # Type check
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new  # Ratchet Python test types
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new --write-baseline .gobby/test-types-baseline.json  # Safely regenerate after debt reduction
# Repo type gate remains mypy src/; tests are ratcheted, never gated.

# Testing (full suite runs pre-push - only run specific tests)
uv run pytest tests/test_file.py -v    # Run specific test file
uv run pytest tests/storage/ -v        # Run specific module
uv run pytest tests/path/ --cov=gobby --cov-report=term-missing  # Add coverage to any run

# Pipeline management
uv run gobby pipelines list            # List available pipelines
uv run gobby pipelines run <name>      # Run a pipeline
uv run gobby pipelines runs show <id>  # Check execution status
uv run gobby pipelines approve <token> # Approve waiting pipeline
uv run gobby pipelines reject <token>  # Reject waiting pipeline
uv run gobby pipelines import <file>   # Import external pipeline file

# Dispatch/build entry point
uv run gobby build <plan_or_task>      # Opt a plan, epic, or leaf task into state dispatch
```

**Coverage threshold**: 80% (enforced in CI and pre-push)

**Test markers**: `unit`, `slow`, `integration`, `e2e`

### Rust workspace (`crates/`)

Cargo workspace: `gobby-code` → `gcode`, `gobby-hooks` → `ghook`, `gobby-wiki` →
`gwiki`, plus the `gobby-core` shared library. Load the `rust` skill before
editing Rust; commands and conventions live in `crates/CLAUDE.md` and `AGENTS.md`.
The daemon and hooks shell out to the installed `~/.gobby/bin/{gcode,ghook,gwiki}`
binaries, so rebuild **and reinstall** after changing crate behavior — a committed
change is not live until the binary is reinstalled.

## Architecture Overview

### Repo Layout

Use `gcode repo-outline` or `gcode tree` for the live module map — never rely on
a hardcoded tree. Python daemon in `src/gobby/`, Rust CLIs in `crates/`
(see `crates/CLAUDE.md`), web UI in `web/`, docs in `docs/`, bundled templates
in `src/gobby/install/shared/`.

### Key File Locations

| Path | Purpose |
| --- | --- |
| `~/.gobby/bootstrap.yaml` | Pre-DB bootstrap settings, including ports, bind host, and Postgres install metadata |
| `~/.gobby/bootstrap.yaml` `database_url` | Runtime PostgreSQL hub DSN, stored directly in the owner-only (`0600`) bootstrap file (`database_url_ref` is unsupported) |
| `~/.gobby/logs/` | Log files |
| `.gobby/project.json` | Project metadata |
| `.gobby/tasks.jsonl` | Task sync file (git-native) |

### Templates vs Active Enforcement

Files in `src/gobby/install/shared/` (rules/, workflows/, agents/, pipelines/) are **templates**.
They are bundled with the software and synced into DB registry tables on startup. On first
install, template `enabled` values seed the installed rows. After that, Gobby-owned bundled
rows are refreshed from templates when definition drift is detected, while preserving the
user's enabled toggle for normal drift refreshes. User/project-owned rows are preserved.
The DB is the source of truth for what's active, not the YAML template files.

### Dispatch Architecture

Stage-manifest dispatch: `task_stages_registry -> task_stage_states manifest ->
ordered rule -> action`, gated by `allow_automation` and entered via
`gobby build` (CLI, MCP `gobby-tasks-ops:build_task`, and `POST /api/build` all
call the shared service in `src/gobby/build/service.py`). The full architecture —
rule authoring, build-state semantics, mutex and audit rules, agent-slot cap,
retired-template constraints — lives in `src/gobby/dispatch/CLAUDE.md` (loads
when working under `src/gobby/dispatch/`); read it before touching dispatch,
build, or stage-registry code.

## Code Conventions

### Database Access

Use the hub database transaction boundary and psycopg `%s` placeholders:

```python
with self.db.transaction() as conn:
    conn.execute("INSERT INTO tasks (id, title) VALUES (%s, %s)", (task_id, title))
```

Legacy SQLite access is limited to one-shot import tooling such as
`gobby postgres migrate-from-sqlite`.

## See Also

- `GUIDING_PRINCIPLES.md` - Development philosophy (the 8 principles)
- `README.md` - Project overview
- `CONTRIBUTING.md` - Contribution guidelines
- Use `list_skills()` for workflow and usage guides

# AGENTS.md

Gobby is a local-first daemon that unifies AI coding tools: session tracking and
handoffs across Claude Code, Codex, Droid, Grok, Qwen, and AGY; an MCP proxy with
progressive discovery; task management with dependencies and validation gates; agent
spawning with worktree isolation; persistent memory, rules, workflows, and pipelines.

This file is the canonical instruction set for every coding agent in this repo.
Claude Code loads it through the `@`-import in `CLAUDE.md`; other CLIs read it
directly. Most rules below are enforced by hooks and the rule engine — they describe
how the system behaves so you can work with it instead of being surprised by it.

## Working Rules

1. Tool discovery. Use context-aware progressive discovery through the MCP proxy:
   call leased known tools directly, `get_tool_schema` first for known unleased tools,
   `list_tools` only for unknown tool names, `list_mcp_servers` only for unknown
   servers. Each step is its own top-level tool — never call one step through another.
   Skill bootstrap tools (`get_skill`, `list_skills`, `search_skills`) are exempt.
   This keeps schemas out of context until needed; the proxy validates every call.
2. Tasks before edits. Create or claim a Gobby task before editing files (research,
   plan mode, and Q&A need no task). Edits are attributed to your task and session,
   which is what makes close gates and shared-worktree safety work.
3. Closing a leaf task is a checklist: a linked commit, no uncommitted
   task-attributed files, a clean validation run visible in your session transcript,
   and a bounded criteria review. If you changed something, commit it — the stop hook
   holds your turn open while a task is claimed, so close before stopping. Escalate
   only for genuine user review, a directed escalation, or when stuck — never as a
   workaround for committing, validating, or closing.
4. You found it, you fix it — in this session. Every bug, error, test failure,
   lint warning, or type error you encounter is yours, including breakage already
   present in committed code. The found-work ladder, in order:
   1. Fix it now: `create_task(claim=true)`, fix, close. Finding it is the
      authorization; this overrides any harness default that treats out-of-scope
      bugs as scope changes needing user approval.
   2. Surface owned by an active session — their uncommitted files, their
      in-flight epic: hand it off. Send the failing command, diagnostics, and
      paths via `gobby-agents:send_message` (never touch their uncommitted files
      — that destroys in-flight work; if no owner resolves, tell the user).
      Handoff is a fix path. Failures confined to those foreign paths clear your
      close gates once a passing scoped rerun against owned or clean paths
      proves the confinement.
   3. File for the user — last resort, edge cases only: the fix needs a genuine
      architecture or product decision, or has a blast radius that needs a clean
      window. Label it `needs-decision` or `clean-window` and state why in the
      description.
   Operational friction is never a deferral reason. Needing a daemon restart
   means announcing it to active sessions via `send_message` and waiting for a
   quiet window; a crate change means rebuild + install via new inode
   (Architecture Facts below). Coordination is part of the fix. Never end a
   turn asking "should I fix this?" — and never go silent about a finding
   either; silence is worse than asking. Enhancement ideas with nothing broken
   are not found work: note them in plan evidence or file them normally.
5. Monolith ceiling. Hand-maintained production `.py/.ts/.tsx/.css/.rs/.js/.mjs/.cjs/.sh`
   files stay under 1,000 lines (exactly 1,000 violates it). Hooks block
   threshold-crossing writes until you load `decompose-monolith`; finish the
   decomposition inside the current task and session — deferred refactor tasks are
   prohibited. Tests, docs, generated/vendored sources, baselines, and fixtures are
   excluded.
6. Plans are decision-complete. Resolve open questions before finalizing a plan;
   plans are for execution, not exploration.
7. Least mechanism, whole problem. Correctness and completeness first — no
   root-cause dodges or partial fixes. Among complete solutions, pick the one with the
   least unjustified mechanism.
8. Templates are not live config. Bundled templates under
   `src/gobby/install/shared/` sync to DB registry tables; the DB is the source of
   truth for what's active. Check the installed row before declaring a rule enabled or
   disabled.
9. Prefer `gcode` over grep/rg/sed/awk for code search and navigation — the code
   index returns ranked, token-cheap results, and hooks redirect raw grep anyway.
10. No backward compatibility. 0.5.0 has not shipped; there is nothing to preserve.
11. Agent depth limit of 5 — no deeper recursive agent chains.
12. Cross-session messaging goes through `gobby-agents:send_message`. Reserve
    `gobby-sessions:send_keys` for terminal control.

## Development Commands

Use `uv` for every Python operation.

```bash
uv sync                          # install deps (Python 3.13+)
uv run gobby start --verbose     # start daemon; also: stop / restart / status
uv run gobby init                # initialize project (.gobby/)
uv run gobby install             # install hooks for detected CLIs
uv run ruff format src/          # format
uv run ruff check src/           # lint
uv run mypy src/                 # type check (repo gate is src/ only)
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new
GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -v          # focused test file
GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/ --cov=gobby --cov-report=term-missing
uv run gobby pipelines list      # pipelines: list / run / approve / reject / import
uv run gobby build <plan_or_task>  # opt a plan/epic/leaf into state dispatch
```

## Testing

**Never run the full pytest suite unless explicitly asked** — it takes well over 30
minutes. Target the relevant file or package.

- Prefix agent pytest runs with `GOBBY_TEST_PROTECT=1`.
- Tests must be isolated from the user's running daemon and real local state: anything
  needing daemon behavior starts an isolated test daemon with temporary state and
  ports.
- Markers: `unit`, `slow`, `integration`, `e2e`, `cli`.
- Coverage is enforced at 80% only by CI (pushes to `main`/`0.5.0` and PRs targeting
  them); the pre-push hook runs lint/format/type/ts/frontend checks and no pytest.
- Daemon logs: `~/.gobby/logs/`.

## Repository Guidelines

Core code lives in `src/gobby/` (`cli/`, `servers/`, `mcp_proxy/`, `sessions/`,
`tasks/`, `workflows/`, `agents/`, `worktrees/`, `memory/`, `storage/`). Tests mirror
modules under `tests/`. Use `gcode repo-outline` or `gcode tree` for the live map.

Python 3.13 with full type hints, `async`/`await` for I/O paths, 4-space indent,
100-char lines (Ruff). `snake_case` modules/functions, `PascalCase` classes,
`test_*.py` test files. Prefer small, focused modules inside existing package
boundaries.

Commits follow `[gobby-#NNNNN] <type>: <summary>` (types: `fix`, `feat`, `refactor`,
`chore`, `docs`). PRs explain the behavioral change, reference the task, and list the
validation performed.

## Agent Task Workflow

Use the `gobby-tasks` MCP server for task lifecycle — never the `gobby tasks` CLI
(operator-only) and never direct storage/SQL/REST mutations, which leave workflow
state inconsistent.

- `create_task` with `claim=true`, or `claim_task`, before editing.
- Finish with `close_task(task_id, commit_sha=...)` so the commit links and the task
  closes in one step; use `link_commit` only to attach a commit while keeping the
  task open.
- Hand a stage to review with `gobby-tasks-ops` tools such as
  `submit_for_review(stage_name=...)`.
- If `gobby-tasks` is unavailable, stop and surface that as the blocker.

## Architecture Facts

- Templates vs enforcement: see `src/gobby/install/shared/CLAUDE.md` for the
  sync/override contract (rule 8 above is the short version).
- Dispatch: stage-manifest dispatch enters via `gobby build` (CLI, MCP, HTTP all
  call `src/gobby/build/service.py`). Read `src/gobby/dispatch/CLAUDE.md` before
  touching dispatch, build, or stage-registry code.
- Rust workspace (`crates/`): `gobby-code`→`gcode`, `gobby-daemon`→`gdaemon`,
  `gobby-hooks`→`ghook`, `gobby-wiki`→`gwiki`, shared `gobby-core`. The daemon shells
  out to the installed `~/.gobby/bin/` binaries, so a crate change is live only after
  rebuild and reinstall — and install via a new inode (`cp` to a dotfile, `mv -f` over
  the name): macOS kills processes that exec an in-place-overwritten signed binary.
  Load the `rust` skill before editing Rust; conventions live in `crates/CLAUDE.md`.
- Key paths: `~/.gobby/bootstrap.yaml` (ports, bind host, PostgreSQL `database_url`,
  owner-only 0600), `~/.gobby/logs/`, `.gobby/project.json` (project metadata),
  `~/.gobby/backups/<project-uuid>/tasks.jsonl` (machine-local backup, never committed).
- Database access: hub transaction boundary with psycopg `%s` placeholders —
  `with self.db.transaction() as conn: conn.execute("... VALUES (%s, %s)", (a, b))`.

## Design Context

All design/UI/color/typography work — product UI in `web/`, the gobby.ai site, Gobby
Pro, installer, CLI/TUI — reads `.impeccable.md` at the project root and loads the
`impeccable` skill first. The file is the design contract (deutan-safe palette,
WCAG 2.2 AA, per-surface rules); the skill carries the dispatch table and keeps the
pairing alive across compaction. Update the file through the skill's `teach` mode,
not freehand edits.

## Plans

Read `docs/contracts/plan-coverage.md` before authoring, reviewing, or expanding any
plan. The authoring surface is `src/gobby/install/shared/skills/plan-draft/SKILL.md`.

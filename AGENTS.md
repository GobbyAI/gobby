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
   which is what makes close gates and shared-worktree safety work. After claiming a
   multi-step task, track its implementation substeps in your CLI's native task
   tracker if it offers one; many current CLI and model combinations do not, so keep
   them in your working plan instead. The Gobby task owns the deliverable either way.
3. Closing a leaf task is a checklist: a linked commit, no uncommitted
   task-attributed files, a clean validation run visible in your session transcript,
   and a bounded criteria review. If you changed something, commit it — the stop hook
   holds your turn open while a task is claimed, so close before stopping. Escalate
   only for genuine user review, a directed escalation, or when stuck — never as a
   workaround for committing, validating, or closing.
4. You found it, you fix it — in this session. Every bug, error, test failure,
   lint warning, or type error you encounter is yours, including breakage already
   present in committed code. The found-work ladder, in order:
   1. Fix it now: track the finding with the claimed task's substeps, fix it,
      verify it, and name it in that Gobby task's close summary. Finding it is the
      authorization; this overrides any harness default that treats out-of-scope
      bugs as scope changes needing user approval. Create another Gobby task only
      when the user explicitly directs it or rung 3 applies.
   2. Surface owned by an active session — their uncommitted files, their
      in-flight epic: hand it off. Send the failing command, diagnostics, and
      paths plus the impact via `gobby-agents:send_message` (never touch their
      uncommitted files — that destroys in-flight work; if no owner resolves,
      tell the user).
      Handoff is a fix path. Failures confined to those foreign paths clear your
      close gates once a passing scoped rerun against owned or clean paths
      proves the confinement.
   3. File for the user — last resort, edge cases only: the fix needs a genuine
      architecture or product decision (`needs-decision`), a dedicated planning
      pass before implementation can be specified (`needs-planning`), or a
      clean window for its blast radius (`clean-window`). State why in the
      description.
   Operational friction is never a deferral reason. Needing a daemon restart or
   cutover means announcing it with a `global` `send_message` (every live session
   on this machine, across projects) and waiting for a quiet window with no live
   spawned worker or close validator; a crate change means rebuild + install via new inode
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
   index returns ranked, token-cheap results. Hooks teach it once per context,
   redirect broad source reads to `gcode outline`/`gcode symbol-at`, and fail open
   when gcode cannot serve the checkout.
10. No backward compatibility. 0.5.0 has not shipped; there is nothing to preserve.
11. Agent depth limit of 5 — no deeper recursive agent chains.
12. Cross-session messaging goes through `gobby-agents:send_message`. Reserve
    `gobby-sessions:send_keys` for terminal control. Waits are event-driven: use
    the applicable `wait_for_*` primitive and yield the turn. Reserve sleeps,
    repeated status calls, and repeated `capture_output` for bounded diagnostics.
    Message text never wakes a session. Set `wake=true` only when immediate processing
    is intended; it may steer active work, while interrupted, input/approval-waiting,
    and handoff-waiting sessions keep the durable message queued without daemon input.
    Use one targetless `project` send for repository coordination and `global` for
    machine-local coordination across projects, which includes every daemon restart
    or cutover announcement.
13. A denied call is about that call, never a standing policy. Approval prompts
    do not always name the tool being invoked, so a rejection can mean "not that,
    not now" or simply a misread. Adjust and continue. If you decide to stop
    invoking something a hook, gate, or rule keeps asking for, say so in text on
    that turn and ask — silently carrying a denial forward as an unstated rule
    hides the conflict from the one person who can resolve it.

## Session Handoff

`gobby-sessions:set_handoff` compacts the session into a structured handoff —
current state, next steps, key decisions, blockers, notes, references — and the
next session reads it with no-argument `gobby-sessions:get_handoff`. Under context
pressure during planning, review, or ongoing task work, use
`set_handoff(clear_session=false)`. Use `set_handoff(clear_session=true)` only after
closing the current task, when a root or coordinator moves to another task or epic
child. A spawned worker ending cooperatively or handing off a blocker supplies the
same structured fields to `gobby-agents:end_agent_run`.
Load `gobby:references/sessions/handoffs.md` before authoring a handoff. Fetch
`gobby-skills:get_skill_file` with `get_tool_schema`, then call
`get_skill_file(name="gobby", path="references/sessions/handoffs.md")` and follow
each `page.next_cursor` with only `cursor` until null. Derive concise, readable
handoffs from the native tracker or working plan. Canonical usage lives in
`docs/guides/sessions.md` (§Creating And Reading Handoffs), with compaction, `/clear`,
and provider-handoff semantics in `docs/contracts/session-boundary.md`.

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

Start the daemon only from the main checkout: `gobby start`/`restart` and
`python -m gobby.runner` refuse a linked-worktree source tree because startup sync
would publish that branch's templates to the shared DB. `GOBBY_ALLOW_WORKTREE_DAEMON=1`
overrides it for announced testing. `gobby stop`/`restart` also refuse while a
restart-protected cron run (the nightly memory dream) is active: `--wait` defers
until it finishes, `--force` interrupts it (it resumes after the next start).
`restart` and `cutover` prove the start half first — worktree guard, installed
set, schema identity, and a read-only `gdaemon schema plan` — and refuse before
stopping or promoting anything when it would fail, leaving the running daemon
alone. `cutover` also refuses to build from uncommitted schema inputs unless
`--allow-dirty` is passed.

## Testing

**Never run the full pytest suite unless explicitly asked** — it takes well over 30
minutes. Target the relevant file or package.

- Prefix agent pytest runs with `GOBBY_TEST_PROTECT=1`, and point `DATABASE_URL` at the
  isolated test hub so no test touches the daemon database:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <path>`.
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

- Templates vs enforcement: see `src/gobby/install/shared/AGENTS.md` for the
  sync/override contract (rule 8 above is the short version).
- Dispatch: stage-manifest dispatch enters via `gobby build` (CLI, MCP, HTTP all
  call `src/gobby/build/service.py`). Read `src/gobby/dispatch/AGENTS.md` before
  touching dispatch, build, or stage-registry code.
- Rust workspace (`crates/`): `gobby-code`→`gcode`, `gobby-daemon`→`gdaemon`,
  `gobby-hooks`→`ghook`, `gobby-terminal`→`gterm`,
  `gobby-client`→`gclient`, shared `gobby-core`. The daemon shells
  out to the installed `~/.gobby/bin/` binaries, so a crate change is live only after
  rebuild and reinstall. Install through `promote_workspace_binary_set`
  (`src/gobby/install/bin_set_coherence.py`) — that function is the contract, and
  new-inode replacement (`cp` to a dotfile, `mv -f` over the name) is one step inside
  it, not the whole of it. It also ad-hoc signs each staged binary, and when promoting
  the complete set it writes the identity stamp
  `~/.gobby/bin/.gdaemon-schema-identity.json`. Copying by hand skips
  the stamp, and the next start is refused with `mixed installed binary set`. New-inode
  replacement stays required because macOS kills processes that exec an
  in-place-overwritten signed binary. Two consequences worth knowing before you
  verify an install: promotion signs the binary, so the installed bytes differ from the
  cargo artifact — read `sha256` from `~/.gobby/bin/` after promoting, never from
  `target/release/`; and the stamped coherent set is exactly `gcode`, `gdaemon`,
  `ghook`, so other `gobby-core` dependents such as `gclient` must be rebuilt alongside
  them but are promoted separately.
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
plan. The authoring surface is
`src/gobby/install/shared/skills/gobby/references/plan/drafting.md`. Load it through
`get_skill_file(name="gobby", path="references/plan/drafting.md")` after fetching
the schema, and follow each cursor to completion.

# CLAUDE.md

This file provides guidance for developing the Gobby codebase.

## Guiding Principles

These are enforced by hooks, rules and workflows.

1. **ALWAYS use progressive tool discovery.** Do not try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).
2. **NEVER create or leave monoliths.** Keep non-test Python, TypeScript, and CSS source files under 1,000 lines. For non-test `.py`, `.ts`, `.tsx`, and `.css` files only, you *MUST* search for an existing refactor task or create it if one does not already exist in gobby-tasks. Leave these tasks for another agent to pick up. Markdown files, including `docs/guides/*.md` and repo-root instruction files, are documentation artifacts and are not subject to this 1,000-line source-file rule; do not create refactor tasks or block docs work based only on Markdown line count.
3. **ALWAYS create or claim a task before editing a file.** This applies to file edits only — no task needed for plan mode, research, investigation, or answering questions unless the user explicitly requests one.
4. **Validation runs when closing with a commit. If a commit is done, validation must run.** `skip_validation` is silently stripped when commits are attached.
5. **NEVER close a task without a commit if there are diffs.** If you changed something, you have to commit it.
6. **NEVER stop while you have a claimed task in progress.** Your stop hook is blocked while you have a claimed task. Task must be closed before stopping. If you claim a task, you finish a task.
7. **Escalate only when the user explicitly needs to review your work, your agent skill/workflow/pipeline directs escalation, or you are genuinely stuck and need guidance.** Do not use escalation as a workaround for committing, closing, or completing required validation.
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

The repo has over 15,000 tests. Running the full suite takes over 30 minutes. Do not run the full suite unless explicitly asked to do so.

When running pytest as an agent, always prefix pytest commands with `GOBBY_TEST_PROTECT=1`.

Pytest must be isolated from the user’s running Gobby daemon and real local daemon state. Tests that need daemon behavior must start/use an isolated test
daemon with temporary state and ports; they must not talk to the existing user daemon.

## Plan Mode

Task management MCP calls (gobby-tasks) are allowed during plan mode. Planning includes organizing work, not just designing it.

## Design Context

All design / UI / color / typography work — across every Gobby surface (product UI in `./web/`, the gobby.ai marketing site, Gobby Pro, installer, CLI/TUI) — must read `.impeccable.md` at the project root before producing output. It defines the design system, deutan-safe color constraints, WCAG 2.2 AA target, aesthetic references, and per-surface variation rules. Update via the `impeccable` skill's `teach` mode rather than freehand edits.

## Project Overview

A local-first daemon to unify your AI coding tools. Session tracking and handoffs across Claude Code, Codex, Droid, Gemini, and QwenCode. An MCP proxy that discovers tools without flooding context. Task management with dependencies, validation, and TDD expansion. Agent spawning and worktree orchestration. Persistent memory, extensible workflows, and hooks.

- **Session management** that survives restarts and context compactions
- **Task system** with dependency graphs, TDD expansion, and validation gates
- **MCP proxy** with progressive discovery (tools stay lightweight until needed)
- **Rule engine** with declarative enforcement (block, set_variable, inject_context, mcp_call)
- **On-demand workflows** for structured multi-step processes (plan-execute, TDD, etc.)
- **Pipeline system** for deterministic automation with approval gates
- **Agent spawning** with P2P messaging, command coordination, and worktree isolation
- **Memory system** for persistent facts across sessions

## Plan-Coverage Contract

The full reference is `docs/contracts/plan-coverage.md`. The authoring surface
is `src/gobby/install/shared/skills/plan-draft/SKILL.md`; review and expansion
surfaces link back to it.

Canonical section-heading regex:

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
```

- Section kind enum: `deliverable | framing | verification | deferred`.
  `deliverable` sections require an `**Acceptance:**` block; `framing` and
  `verification` sections do not carry acceptance items; `deferred` sections
  require the typed deferral object.
- Acceptance-item shape: IDs use `A<section>.<n>` dotted suffixes — read the
  shorthand as "section ID followed by `.<n>`" verbatim, with no synthetic
  letter added. Section `A1` (letter-prefixed) emits `A1.1`, `A1.2`; section
  `1.1` (numeric) emits `1.1.1`, `1.1.2` (no `A`). The parser enforces
  `item_id.startswith(f"{section_id}.")`. Each item names at least one
  artifact kind: `file`, `symbol`, `test`, or `behavior`.
- Typed deferral object fields: `task_ref`, `reason`, `owner`,
  `original_acceptance_items`; the task must be open and carry provenance label
  `deferred-from:<plan-id>:<section-id>`. A closed task fails the gate.
- Structured coverage record format:
  `covers:<plan-id>:<section-id>:<item-id>`. Free-form `plan-ref:` labels are
  not honored.
- Manifest section: implementation plans carry a single `## M1 Task Manifest`
  section at the end of the document with `kind: manifest` and one YAML entry
  per `kind: deliverable` section (fields: `title`, `category`, `task_type`,
  `depends_on`, `validation_criteria`, `labels`, `assigned_agent`, `tdd`,
  `source_section`). Planners author narrative only; `plan-adversary` writes
  the manifest as the final act of approval. Parser modes:
  `parse_mode="draft"` tolerates a missing manifest (used by adversary
  pre-verdict review and `/gobby plan` Phase 3a); `parse_mode="expansion"` and
  default `parse_mode="strict"` require the manifest and enforce the
  deliverable→entry 1:1 invariant plus `covers:` label resolution. Full
  schema, invariants, and adversary-writes-on-approval contract live in
  `docs/contracts/plan-coverage.md`.
- CLI synopsis:
  `gobby plan coverage --plan <path> --plan-id <id> --plan-hash <sha256> --task-tree <db|jsonl|path> [--root-task <ref>] [--project-id <id>] [--matrix-file <path>] [--evidence <kind>] [--manifest <path>] [--regenerate]`.
  Required flags: `--plan`, `--plan-id`, `--plan-hash`, `--task-tree`.
  Optional flags: `--root-task`, `--project-id`, `--matrix-file`,
  `--evidence`, `--manifest`, `--regenerate`. Exit codes: `0`, `2`, `3`, `4`,
  `5`, `6`, `7`, `8`.
- Evidence kinds: `commits | task-diff | worktree-diff | coverage-matrix | none`.
- Bootstrap-ledger requirement: every new epic plan ships a
  `.coverage-ledger.yaml` companion file, adversary-reviewed before expansion,
  until the contract tooling is mature.
- Plan storage: the `plans` table is the authoritative registry. Use the
  `gobby-plans` MCP server or `gobby plans` CLI to create, list, update, and
  archive plan records. Each row carries `plan_id`, `project_id`,
  `root_task_ref`, `plan_path`, `plan_hash`, `plan_kind`, and `state`.
  `plan_kind` is one of:
  - `implementation` — parsed strict; requires a generated manifest with
    matching `plan_hash` and every row `status: covered`.
  - `strategy` — parsed permissive; no manifest permitted.
  `state` is one of `active` or `archived`; archived plans live under
  `.gobby/plans/completed/`.
- Table-row decomposition rule: any `deliverable` section whose body uses a
  markdown table to enumerate work items MUST emit one acceptance item per data
  row with stable IDs. Plan-adversary qualitatively rejects deliverables that
  enumerate work in tables without per-row acceptance items.
  Table-row decomposition requires one acceptance item per table data row.

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

# Testing (full suite runs pre-push - only run specific tests)
uv run pytest tests/test_file.py -v    # Run specific test file
uv run pytest tests/storage/ -v        # Run specific module
uv run pytest tests/path/ --cov=gobby --cov-report=term-missing  # Add coverage to any run

# Pipeline management
uv run gobby pipelines list            # List available pipelines
uv run gobby pipelines run <name>      # Run a pipeline
uv run gobby pipelines status <id>     # Check execution status
uv run gobby pipelines approve <token> # Approve waiting pipeline
uv run gobby pipelines reject <token>  # Reject waiting pipeline
uv run gobby pipelines import <file>   # Import external pipeline file

# Dispatch/build entry point
uv run gobby build <plan_or_task>      # Opt a plan, epic, or leaf task into state dispatch
```

**Coverage threshold**: 80% (enforced in CI and pre-push)

**Test markers**: `unit`, `slow`, `integration`, `e2e`

## Architecture Overview

### Directory Structure

```text
src/gobby/
├── cli/                    # CLI commands (Click, ~25 modules)
│   ├── __init__.py        # Main CLI group
│   ├── daemon.py          # start, stop, restart, status
│   ├── agents.py          # Agent management
│   ├── rules.py           # Rule management
│   ├── sessions.py        # Session management
│   └── ...                # worktrees, memory, pipelines, etc.
│
├── runner.py              # Main daemon entry point (GobbyRunner)
├── runner_broadcasting.py # WebSocket event broadcasting wiring
├── runner_maintenance.py  # Background maintenance jobs
│
├── servers/               # HTTP and WebSocket servers
│   ├── http.py           # FastAPI HTTP server
│   ├── routes/           # HTTP API routes (tasks, sessions, agents, etc.)
│   └── websocket/        # WebSocket server (broadcast, chat, voice, tmux)
│
├── mcp_proxy/            # MCP proxy layer
│   ├── server.py         # FastMCP server implementation
│   ├── manager.py        # MCPClientManager (connection pooling)
│   ├── instructions.py   # MCP server instructions (progressive discovery)
│   ├── tools/            # 20+ internal tool modules
│   └── transports/       # HTTP, stdio, WebSocket transports
│
├── hooks/                # Hook event system
│   ├── hook_manager.py   # Central coordinator
│   ├── events.py         # HookEvent, HookResponse models
│   ├── skill_manager.py  # Skill discovery for hooks
│   └── ...               # Broadcasting, git, health, verification
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
│   ├── registry.py       # Agent registry (DB-backed)
│   ├── isolation.py      # Worktree/clone isolation
│   └── ...               # Session, context, lifecycle monitor
│
├── sessions/             # Session lifecycle
│   ├── lifecycle.py      # Background jobs
│   ├── processor.py      # SessionMessageProcessor
│   └── transcripts/      # Parsers for Claude/Gemini/Codex
│
├── tasks/                # Task system
│   ├── expansion.py      # TaskExpander (LLM-based decomposition)
│   ├── validation.py     # TaskValidator
│   └── prompts/          # LLM prompts for expansion
│
├── workflows/            # Rule engine and workflow system (~47 modules)
│   ├── rule_engine.py    # RuleEngine (declarative enforcement)
│   ├── definitions.py    # Rule/workflow/agent definition models
│   ├── safe_evaluator.py # Safe expression evaluator (AST-based)
│   ├── engine.py         # WorkflowEngine (on-demand state machines)
│   ├── pipeline_executor.py  # PipelineExecutor (sequential execution)
│   ├── loader.py         # YAML workflow/rule loading and sync
│   └── ...               # Actions, observers, state, templates
│
├── dispatch/             # State-driven task dispatch
│   ├── rules.py          # Ordered lifecycle dispatch rules
│   └── dispatcher.py     # Cron heartbeat scanner and action executor
│
├── build/                # gobby build shared service
│   └── service.py        # CLI, MCP, and HTTP build core
│
├── memory/               # Persistent memory system
│   ├── manager.py        # MemoryManager
│   └── embeddings.py     # Embedding-based recall
│
├── skills/               # Skill management
│   ├── loader.py         # SkillLoader (filesystem, GitHub, ZIP)
│   ├── parser.py         # SKILL.md parser
│   └── sync.py           # Bundled skill sync on startup
│
├── storage/              # SQLite storage layer (~20 modules)
│   ├── database.py       # LocalDatabase (connection management)
│   ├── migrations.py     # Schema migrations
│   ├── sessions.py       # Session CRUD
│   ├── tasks.py          # Task CRUD
│   └── ...               # Memory, skills, agents, workflows, etc.
│
├── llm/                  # Multi-provider LLM abstraction
│   ├── service.py        # LLMService manager
│   ├── claude.py         # Claude provider
│   ├── gemini.py         # Gemini provider
│   └── litellm.py        # LiteLLM fallback
│
├── config/               # Configuration (~15 modules)
│   ├── app.py            # DaemonConfig (YAML config model)
│   ├── bootstrap.py      # Pre-DB bootstrap settings
│   └── ...               # Features, logging, MCP, tasks, etc.
│
├── autonomous/           # Autonomous execution support
├── clones/               # Git clone management
├── scheduler/            # Cron job scheduler
├── search/               # TF-IDF and semantic search
├── sync/                 # Task/memory sync (JSONL)
├── voice/                # Voice chat support
├── worktrees/            # Git worktree management
└── utils/                # Utilities (git, daemon client, etc.)
```

### Key File Locations

| Path | Purpose |
| --- | --- |
| `~/.gobby/bootstrap.yaml` | Pre-DB bootstrap settings (5 fields: ports, db_path, bind_host) |
| `~/.gobby/gobby-hub.db` | SQLite database |
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

Gobby task automation is stage-manifest dispatch. The dispatcher is a
deterministic heartbeat that scans tasks with `allow_automation=true`, reads the
task's current manifest row, evaluates ordered rules, acquires a per-task mutex,
and executes the selected action.

The dispatch chain is:

```text
task_stages_registry -> task_stage_states manifest -> ordered rule -> action
```

Dispatch rules live in `src/gobby/dispatch/rules.py`. To add a rule:

1. Add a small rule function that checks the current manifest row, artifacts,
   labels, and automation fields.
2. Return an explicit action such as start stage, spawn agent, start expansion,
   create isolation, advance stage, append audit marker, or escalate.
3. Register it in the ordered rule list near the manifest stage it belongs to.
4. Keep the rule deterministic. Prompting belongs in spawned agents; the
   dispatcher only routes manifest state.

Build state is resolved before dispatch:

- `allow_automation` is the opt-in gate. Backlog tasks stay invisible until `gobby build`
  enables them.
- `yolo` means rules choose deterministic fallbacks instead of escalating when possible.
- `isolation` is explicit task state: `none`, `worktree`, or `clone`.
- `stages` is the ordered manifest materialized in `task_stage_states` from the
  stage registry. The current stage is the first row whose state is not `done`.
- Build profiles are DB-backed registry rows synced from
  `src/gobby/install/shared/registry/build_profiles.yaml`. The `default`
  profile resolves unless a caller supplies another profile; explicit CLI,
  MCP, and HTTP build fields override profile defaults for the same fields.
  Profile skip stages only shape a new lifecycle. Existing manifests must be
  cleaned or restarted before their stage shape changes.
- `assigned_agent` and `additional_skills` route leaf work. Missing leaf assignment
  falls back to `backend-developer` with an audit marker.

`gobby build` is the single entry point for turning a plan, epic, or leaf task into
dispatchable state. The CLI command, MCP tool (`gobby-build:build_task`), and HTTP
route (`POST /api/build`) must all call the shared build service in
`src/gobby/build/service.py`, returning the same `BuildResult`.
`gobby unbuild <ref>` is the task-scoped CLI inverse for an existing built task:
it stops automation for the resolved task or subtree through the shared build
control path without deleting task history or build artifacts.

Concurrency and audit data are adjacent to tasks:

- `task_dispatch_mutex` stores short-lived leases. Dispatcher code is the normal writer:
  acquire before side effects, release in `finally`, sweep expired leases on startup, and
  use the force-release escape hatch only for operator recovery.
- `task_artifacts` stores sparse pointers such as `plan_file_path`, `target_branch`,
  worktree/clone path and ID pairs, expansion run IDs, PR URL, and future merge SHA.
  Write related fields atomically, especially worktree/clone pairs.
- Audit rows are append-only. Stage helpers write them when changing manifest
  rows; readers use them for history and diagnostics.

The dispatcher enforces a global agent-slot cap (`max_active_agents`, default 10). When
the cap is full, no persistent queue is needed; the next heartbeat re-evaluates task
manifest state.

Retired orchestration templates live under `workflows/*/deprecated/` as archival
tombstones. Active bundled sync reads only top-level YAML and soft-deletes installed rows
for retired definitions, so `orchestrator.yaml`, `front-half-orchestrator.yaml`,
`dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, the conductor pipeline, and the
retired `conductor`, `developer`, and `pipeline-worker` agents must stay out of active
install roots. Real PR creation and richer merge/conflict handling are tracked in task
#13552; this dispatcher only reaches the PR/merge boundary and uses existing merge tools
where they are already available.

## Code Conventions

### Type Hints

All functions require type hints:

```python
def process_task(task_id: str, config: TaskConfig) -> Task:
    """Process a task with given configuration."""
    ...
```

### Error Handling

Use specific exceptions, not bare `except`:

```python
# Good
try:
    result = process_data()
except ValueError as e:
    logger.error(f"Invalid data: {e}")
    raise

# Bad
try:
    result = process_data()
except:
    pass
```

### Async/Await

Use async for I/O-bound operations:

```python
async def fetch_data(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()
```

### SQLite Connections

Always use connection context manager:

```python
with self.db.transaction() as conn:
    conn.execute("INSERT INTO tasks VALUES (?, ?)", (task_id, title))
```

### Logging

Use structured logging with context:

```python
logger.info(f"Created task {task_id} in project {project_id}")
logger.error(f"Failed to expand task {task_id}: {error}", exc_info=True)
```

## Testing Patterns

### Test Structure

```python
def test_task_creation(task_manager: LocalTaskManager) -> None:
    """Test creating a task with required fields."""
    task = task_manager.create_task(
        title="Test task",
        task_type="task"
    )

    assert task.id is not None
    assert task.title == "Test task"
    assert task.status == "open"
```

### Fixtures

Use pytest fixtures from `tests/conftest.py`:

```python
def test_with_database(db: LocalDatabase) -> None:
    """Test using database fixture."""
    ...

def test_with_task_manager(task_manager: LocalTaskManager) -> None:
    """Test using task manager fixture."""
    ...
```

### Async Tests

Mark async tests with `pytest.mark.asyncio`:

```python
@pytest.mark.asyncio
async def test_async_operation() -> None:
    """Test async operation."""
    result = await async_function()
    assert result is not None
```

### Test Markers

Use markers to categorize tests:

```python
@pytest.mark.slow
def test_expensive_operation() -> None:
    """This test takes a long time."""
    ...

@pytest.mark.integration
def test_integration() -> None:
    """This test requires multiple components."""
    ...
```

## Troubleshooting

### Common Issues

| Issue | Solution |
| --- | --- |
| Import errors | Run `uv sync` |
| Test failures | Check fixtures in `tests/conftest.py` |
| Type errors | Run `uv run mypy src/` |
| Lint errors | Run `uv run ruff check src/ --fix` |
| Daemon not starting | Check logs in `~/.gobby/logs/` |
| MCP connection issues | Verify daemon is running: `gobby status` |

### Debugging Tips

- Enable verbose logging: `gobby start --verbose`
- Check daemon logs: `tail -f ~/.gobby/logs/gobby.log`
- Test MCP tools: Use `list_mcp_servers()` to verify connections

## See Also

- `GUIDING_PRINCIPLES.md` - Development philosophy (the 8 principles)
- `README.md` - Project overview
- `CONTRIBUTING.md` - Contribution guidelines
- Use `list_skills()` for workflow and usage guides

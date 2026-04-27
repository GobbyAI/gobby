# CLAUDE.md

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
- Acceptance-item shape: IDs use `A<section>.<n>` dotted suffixes and each item
  names at least one artifact kind: `file`, `symbol`, `test`, or `behavior`.
- Typed deferral object fields: `task_ref`, `reason`, `owner`,
  `original_acceptance_items`; the task must be open and carry provenance label
  `deferred-from:<plan-id>:<section-id>`. A closed task fails the gate.
- Structured coverage record format:
  `covers:<plan-id>:<section-id>:<item-id>`. Free-form `plan-ref:` labels are
  not honored.
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
- `.grandfathered` mechanism: reserved for already-merged epics; additions
  require a paired `# remove-by: <task-ref>` annotation and an open task.
  Epic 1 (#13175) is not grandfathered.
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
They are bundled with the software and synced to the `workflow_definitions` DB table on first
startup with `enabled: true` by default. Existing DB rows are never overwritten — drift is
detected via hash comparison at runtime. The DB is the source of truth for what's active,
not the YAML template files.

### Dispatch Architecture

Gobby task automation is state-driven dispatch. The dispatcher is a deterministic
heartbeat that scans tasks with `allow_automation=true`, evaluates ordered lifecycle
rules, acquires a per-task mutex, and executes the selected action.

Dispatch rules live in `src/gobby/dispatch/rules.py`. To add a rule:

1. Add a small rule function that checks task lifecycle, status, labels, artifacts,
   and automation fields.
2. Return an explicit action such as spawn, start expansion, create isolation,
   advance lifecycle, append audit marker, or escalate.
3. Register it in the ordered rule list near the stage it belongs to.
4. Keep the rule deterministic. Prompting belongs in spawned agents; the dispatcher
   only routes state.

Build state is resolved before dispatch:

- `allow_automation` is the opt-in gate. Backlog tasks stay invisible until `gobby build`
  enables them.
- `yolo` means rules choose deterministic fallbacks instead of escalating when possible.
- `isolation` is explicit task state: `none`, `worktree`, or `clone`.
- Stage skips are stored as `stage-:<name>` labels. Profiles such as `quick`, `review`,
  `full`, and `full-yolo` are CLI/MCP/HTTP sugar that resolve to stage skips, isolation,
  and yolo at build time.
- `assigned_agent` and `additional_skills` route leaf work. Missing leaf assignment
  falls back to `backend-developer` with an audit marker.

`gobby build` is the single entry point for turning a plan, epic, or leaf task into
dispatchable state. The CLI command, MCP tool (`gobby-tasks-ops:build_task`), and HTTP
route (`POST /api/build`) must all call the shared build service in
`src/gobby/build/service.py`, returning the same `BuildResult`.

Concurrency and audit data are adjacent to tasks:

- `task_dispatch_mutex` stores short-lived leases. Dispatcher code is the normal writer:
  acquire before side effects, release in `finally`, sweep expired leases on startup, and
  use the force-release escape hatch only for operator recovery.
- `task_artifacts` stores sparse pointers such as `plan_file_path`, `target_branch`,
  worktree/clone path and ID pairs, expansion run IDs, PR URL, and future merge SHA.
  Write related fields atomically, especially worktree/clone pairs.
- `task_lifecycle_events` is append-only audit. Lifecycle helpers write it when
  advancing stages; readers use it for history and diagnostics.

The dispatcher enforces a global agent-slot cap (`max_active_agents`, default 10). When
the cap is full, no persistent queue is needed; the next heartbeat re-evaluates task
state.

Retired orchestration templates stay in place as disabled tombstones so bundled sync keeps
their DB rows stable. `orchestrator.yaml`, `front-half-orchestrator.yaml`,
`dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, and the old `conductor.yaml` agent
must be `enabled: false`, `deprecated: true`, and contain no active steps. Real PR
creation and richer merge/conflict handling are tracked in task #12728; this dispatcher
only reaches the PR/merge boundary and uses existing merge tools where they are already
available.

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

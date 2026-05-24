# Pipeline Heartbeat & Tick-Based Conductor System

## Context

The orchestrator pipeline (dev-loop) is event-driven via continuations. It works when events flow correctly but stalls silently when they don't. The user wants truly **fire-and-forget orchestration**: fire off a pipeline, walk away, come back to results.

The vision: a **persistent SDK conductor agent** (cheap haiku) that:
- Gets ticked by gobby-cron on a schedule
- Checks task states, dispatches dev/QA agents, detects stuck agents
- Autocompresses when context gets full (not respawned — it's persistent)
- Notifies humans when done (broadcast, email, telegram, etc.)
- Gets replaced by daemon only if it crashes

Orchestration leaves the terminal entirely. Pipelines become deterministic fire-and-forget steps. Interactive sessions just fire and move on.

**Infrastructure readiness: ~90%.** ChatSession already handles multi-turn, autocompress, and SDK resume. gobby-cron has scheduling. The conductor is just a ChatSession that receives tick messages from cron via WebSocket/HTTP instead of a browser. No new session types or abstractions — just glue.

**OpenClaw parity status:**
- Lobster workflows: full compatibility via `lobster_compat.py` (import + direct execution)
- Cron scheduling: gobby-cron exists with same 3 schedule types (cron/interval/one-shot), same action types, but untested in production
- Messaging platforms: not yet (OpenClaw has Signal/Telegram/Discord/Slack/etc.)
- Skill registry: not yet (OpenClaw has ClawHub)
- Gobby advantages: LLM-powered pipeline steps, full MCP integration, rule engine, memory system, task system with TDD expansion, workflow composition

## What We're Building (This Round)

Foundation: make gobby-cron the backbone for all periodic work, add pipeline heartbeat safety net, validate everything E2E. The persistent conductor (Phase 2+) is small once this foundation lands — it's ~50 lines of glue plus a YAML agent definition.

---

## Phase 1: Add `handler` Action Type to gobby-cron

CronExecutor currently supports `shell`, `agent_spawn`, `pipeline`. Add a fourth: `handler` — registered async callables with access to daemon internals. This is what makes cron powerful enough to replace bespoke daemon loops AND serve as the conductor's tick mechanism later.

### Modify: `src/gobby/scheduler/executor.py` (~35 lines)

```python
# New type
CronHandler = Callable[[CronJob], Awaitable[str]]

class CronExecutor:
    def __init__(self, storage, agent_runner=None, pipeline_executor=None):
        # ... existing ...
        self._handlers: dict[str, CronHandler] = {}

    def register_handler(self, name: str, handler: CronHandler) -> None:
        """Register a named handler for the 'handler' action type."""
        self._handlers[name] = handler

    async def _execute_handler(self, job: CronJob) -> str:
        name = job.action_config.get("handler")
        if not name:
            raise ValueError("handler action requires 'handler' in action_config")
        handler = self._handlers.get(name)
        if not handler:
            raise ValueError(f"No handler registered: '{name}'. Available: {list(self._handlers)}")
        return await handler(job)
```

Add `"handler"` case to `execute()` dispatch (line 46-53).

Also enhance `_execute_agent_spawn()` to support `agent_definition` in `action_config` (tick-agent prep):
```python
agent_def = config.get("agent_definition")
# If provided, resolve definition and forward to spawn_headless
```

### Modify: `tests/scheduler/test_cron_executor.py` (~40 lines)

- Test handler registration and dispatch (happy path)
- Test missing handler name → ValueError
- Test unregistered handler name → ValueError
- Test `agent_spawn` with `agent_definition` field (mock spawn_headless)

---

## Phase 2: Pipeline Heartbeat as Cron Handler

The heartbeat logic runs through cron — same scheduling, backoff, concurrency control, and run history as every other cron job. No bespoke daemon loop.

### New: `src/gobby/workflows/pipeline_heartbeat.py` (~150 lines)

```python
class PipelineHeartbeat:
    """Safety net for event-driven pipeline execution.

    Callable cron handler. On each tick:
    1. Detects stalled RUNNING executions (no updated_at change)
    2. Checks if associated agents are alive
    3. Fires lost continuations or fails truly dead executions
    4. Cleans up orphaned pipeline_continuations rows
    """

    def __init__(
        self,
        execution_manager: LocalPipelineExecutionManager,
        completion_registry: CompletionEventRegistry,
        agent_registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        stall_threshold_seconds: float = 120.0,
    ): ...

    async def __call__(self, job: CronJob) -> str:
        """Cron handler entry point."""
        stalled = await self.check_stalled_executions()
        orphaned = await self.check_orphaned_continuations()
        return f"Heartbeat: {stalled} stalled, {orphaned} orphaned fired"

    async def check_stalled_executions(self) -> int: ...
    async def check_orphaned_continuations(self) -> int: ...
```

**`check_stalled_executions()`**:
1. Query `pipeline_executions` for `status='running'` + stale `updated_at`
2. For each: check if agents alive via `RunningAgentRegistry`
3. Agents alive → touch `updated_at` (slow, not stalled)
4. Agents dead + continuations in DB → fire continuation (event was lost)
5. Agents dead + no continuations → mark FAILED, log warning

**`check_orphaned_continuations()`**:
1. Query `pipeline_continuations` table
2. For each run_id, check `agent_runs` status
3. If agent completed/failed but continuation still exists → fire it now

### Modify: `src/gobby/storage/pipelines.py` (~20 lines)

Add `get_stalled_executions(stall_threshold_seconds: int) -> list[PipelineExecution]`

### Modify: `src/gobby/runner.py` (~25 lines)

Wire heartbeat during daemon init (after CronExecutor created, ~line 569):

```python
heartbeat = PipelineHeartbeat(
    execution_manager=..., completion_registry=...,
    agent_registry=..., agent_run_manager=..., db=...,
)
cron_executor.register_handler("pipeline_heartbeat", heartbeat)
```

Auto-create system cron job on startup if missing:
```python
existing = cron_storage.get_job_by_name("gobby:pipeline-heartbeat")
if not existing:
    cron_storage.create_job(
        name="gobby:pipeline-heartbeat",
        schedule_type="interval", interval_seconds=60,
        action_type="handler",
        action_config={"handler": "pipeline_heartbeat"},
        enabled=True,
    )
```

`gobby:` prefix = system job convention.

### New: `tests/workflows/test_pipeline_heartbeat.py` (~200 lines)

- Stalled execution + no alive agents + no continuations → FAILED
- Stalled execution + alive agents → `updated_at` touched
- Stalled execution + dead agents + orphaned continuation → continuation fired
- Non-stalled execution → untouched
- Orphaned continuation for completed agent_run → fired and cleaned up

---

## Phase 3: Validate gobby-cron E2E

### New: `tests/scheduler/test_cron_integration.py` (~150 lines)

Integration tests with real DB:
- Shell job → `run_now()` → CronRun with status=completed + output
- Interval job → advance time → `get_due_jobs()` returns it
- Handler action type → dispatches to registered callback
- Backoff: fail job, verify `consecutive_failures` increments
- Cleanup: old runs get deleted

### Modify: `src/gobby/storage/cron.py` (if needed)

Add `get_job_by_name(name: str) -> CronJob | None` for the system job lookup in Phase 2.

### Manual smoke test

```bash
uv run gobby cron add "test-tick" --schedule-type interval --interval 30 \
  --action-type shell --action-config '{"command": "echo", "args": ["tick"]}'
uv run gobby cron list          # Should also show gobby:pipeline-heartbeat
uv run gobby cron runs <job-id>
```

---

## Phase 4+ Roadmap: Persistent Conductor & Observability

Everything below builds on Phases 1-3. Enough detail for the next agent to pick up and run.

The conductor is just a `ChatSession` — the same infrastructure that powers web chat. No new session type needed. `ChatSession` already handles multi-turn, autocompress via PreCompact hooks, and SDK resume. The only new piece is cron delivering tick messages to it.

### Architecture

```
gobby-cron tick completes
    |
    v
CronExecutor emits WebSocket broadcast: {"type": "cron_event", "job": "...", "result": "..."}
    |
    v
WebSocket subscribers (pub/sub, existing BroadcastMixin)
    |
    ├── Conductor ChatSession (subscribes to cron_event)
    │   ├── Receives tick → checks task states, dispatches agents, notifies
    │   ├── Autocompresses when context fills (PreCompact preserves epic state)
    │   └── If crashed/dead → daemon detects, spawns replacement
    │
    ├── Web UI dashboard (subscribes to cron_event) → shows tick status
    ├── Interactive CLI sessions → can ignore or act on cron events
    └── Any future listener → just subscribe
```

### Wake Mechanism & Reentrancy

The cron handler sends a user message directly to the conductor's ChatSession (same as a user typing in web chat). Between ticks, the conductor is idle — SDK client finished, ChatSession waiting for next `send_message()`.

If a tick fires while the conductor is busy (mid-response, calling tools), the handler behavior depends on the task type:

- **Skippable** (`skip_if_busy: true`, default for periodic checks): handler returns immediately. Conductor catches up next tick. Simple `_busy` flag.
- **Must-execute** (`skip_if_busy: false`, for scheduled reports/deadlines): handler waits for ChatSession's `_lock` and queues the message. Might be 30-60s late but guaranteed to run.
- **Bypass conductor** (for deterministic scheduled work): use `action_type: "pipeline"` or `"agent_spawn"` directly on the cron job. No dependency on conductor availability. The 8am report doesn't need the conductor's context — it's just a pipeline on a timer.

### What Needs Building

1. **Cron event broadcast** (~10 lines) — CronScheduler emits a WebSocket broadcast after each job run (observability, not wake mechanism). Other clients see ticks happen.
2. **Conductor tick handler** (~40 lines) — registered on CronExecutor, sends tick message to conductor's ChatSession. Skips if conductor busy. Creates ChatSession on first tick if not exists.
3. **Conductor agent definition** (YAML) — `model: haiku`, restricted tool allowlist (check_tasks, suggest_next_tasks, spawn_agent, dispatch_batch, broadcast, run_pipeline)
4. **Conductor ChatSession setup** — standard web chat session, created by the tick handler on first tick, persists across ticks.
4. **Multi-CLI dispatch** — conductor must spawn agents with different providers (Claude, Gemini, Codex). `spawn_agent` already supports `provider` field in agent definitions — conductor just needs definitions for each (e.g., `gemini-developer.yaml`, `claude-developer.yaml`).
5. **Notification channels** — broadcast to active sessions, email, telegram (via MCP servers or built-in)
6. **Simplified dispatch pipelines** — simple "spawn dev on task" and "spawn QA on task" pipelines the conductor invokes

### Observability & Execution Log

The conductor and pipeline system need proper visibility:

1. **Pipeline execution timeline** — a log view (like the tasks list web UI) showing: which pipeline ran when, what agents it dispatched, what tasks they worked on, outcomes. Data is already in `pipeline_executions` + `step_executions` + `agent_runs` + `cron_runs` tables — needs a clean UI that joins them into a timeline.
2. **Conductor observability** — the conductor's ChatSession conversation should be viewable in the web UI (it's already a chat session, so it should show up). Its decisions, tool calls, and dispatches are visible as chat messages.
3. **Cron run history** — `GET /api/cron/jobs/{id}/runs` already exists. Needs a UI panel showing tick history, success/failure, output.
4. **Pipeline tool cleanup** — audit `src/gobby/mcp_proxy/tools/workflows/_pipelines.py` and related files. Remove or consolidate tools that are cruft from the old orchestration model. The conductor replaces some of dev-loop's complexity, so tools that only existed for YAML pipeline decision-making may be unnecessary.

### Why This Is Small (Foundation)

- ChatSession already handles multi-turn, autocompress, SDK resume
- WebSocket broadcast already supports parametric subscriptions
- Cron event emission is ~10 lines in the scheduler
- Agent definition is a YAML file
- The conductor is literally just a web chat session that listens for cron broadcasts
- No new session types, no HTTP endpoints, no conversation routing
- Multi-CLI dispatch already works (provider field on agent definitions)
- Observability data already exists in DB — needs UI, not new backend

### Dead Code in the Conductor Paradigm

When the conductor replaces dev-loop's decision-making, ~600+ lines of the most complex, bug-prone code become dead — exactly the code that caused the 371-retry spiral (bugs #9937-9939):

| What | File | Why Dead |
|------|------|----------|
| `register_pipeline_continuation` tool (~200 lines) | `_pipelines.py:196-394` | Conductor ticks, no event-driven re-invocation |
| `_pending_dead_end_retries` + dead-end retry logic | `_pipelines.py:38,236-333` | Conductor's tick IS the retry mechanism |
| `_auto_subscribe_lineage` (~50 lines) | `_pipelines.py:56-105` | Conductor doesn't subscribe to completion events |
| `register_continuation()` + `load_persisted_continuations()` | `completion_registry.py:77-134` | No continuations to register or load |
| `pipeline_continuations` DB table | migrations | No longer needed |
| `dev-loop.yaml` (~300 lines) | pipelines/ | Replaced by conductor |
| `orchestrator.yaml` (~150 lines) | pipelines/ | Wrapper around dev-loop |
| `coordinator.yaml` (~100 lines) | pipelines/ | Event-driven dispatch, replaced |

**What stays:**
- `wait_for_completion` tool + `CompletionEventRegistry.wait()` — used by `expand-task.yaml`'s `wait` step type (spawn researcher → wait for completion → validate spec). Legitimate sequential dependency.
- `run_pipeline`, `get_pipeline_status`, `approve/reject_pipeline`, `resume_pipeline` — conductor invokes simple pipelines
- Pipeline CRUD tools — still valid
- `expand-task.yaml` — independent of orchestration
- `CompletionEventRegistry.register()/.notify()` — still needed for `wait` step type and one-shot completion tracking
- `_execute_pipeline_background` — background execution

**Don't delete yet.** This code stays until the conductor is proven. Phase 4+ deprecates it; a later cleanup pass removes it.

### OpenClaw Parity Items (Separate)

- Messaging platform integration (Signal/Telegram/Discord/Slack)
- Validate Lobster importer E2E
- Public skill registry (ClawHub equivalent)

---

## Dependency Graph

```
Phase 1 (handler action type)
    |
    ├── Phase 2 (heartbeat handler) ──┐
    |                                  ├── [parallel, this round]
    └── Phase 3 (cron E2E validation) ┘
         |
         v
Phase 4 (conductor + cron broadcast)
    |
    ├── Phase 5 (observability UI + execution timeline)
    |
    └── Phase 6 (pipeline tool cleanup + simplified dispatch)
```

## Files Changed Summary

| File | Action | Phase | Est. Lines |
|------|--------|-------|------------|
| `src/gobby/scheduler/executor.py` | Modify | 1 | +40 |
| `tests/scheduler/test_cron_executor.py` | Modify | 1 | +40 |
| `src/gobby/workflows/pipeline_heartbeat.py` | New | 2 | ~150 |
| `src/gobby/storage/pipelines.py` | Modify | 2 | +20 |
| `src/gobby/runner.py` | Modify | 2 | +25 |
| `tests/workflows/test_pipeline_heartbeat.py` | New | 2 | ~200 |
| `tests/scheduler/test_cron_integration.py` | New | 3 | ~150 |
| `src/gobby/storage/cron.py` | Modify | 3 | +10 |

## Verification

### Phase 1
```bash
uv run pytest tests/scheduler/test_cron_executor.py -v
```

### Phase 2
```bash
uv run pytest tests/workflows/test_pipeline_heartbeat.py -v
```
Then: run orchestrator pipeline, kill an agent's tmux without completion event, verify heartbeat catches it. Check `uv run gobby cron runs` for heartbeat job ticks.

### Phase 3
```bash
uv run pytest tests/scheduler/test_cron_integration.py -v
```
Plus manual CLI smoke test above.

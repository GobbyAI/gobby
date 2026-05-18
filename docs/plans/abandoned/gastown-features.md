# Gastown-Inspired Multi-Agent Orchestration Features

Design document for five multi-agent orchestration features inspired by [Gastown](https://github.com/steveyegge/gastown), building on Gobby's existing infrastructure.

## Executive Summary

Gastown is a multi-agent workspace manager with compelling patterns for agent coordination. This document designs five features that bring similar capabilities to Gobby while leveraging existing infrastructure.

| Feature | Gastown Equivalent | Gobby Gap |
|---------|-------------------|-----------|
| Peer-to-Peer Mailboxes | Mailboxes | Only parent↔child messaging |
| Convoy Pattern | Beads/Convoys | No batch assignment primitive |
| Agent Checkpointing | Hooks (git worktrees) | No periodic state snapshots |
| Named Agent Identity | Polecats with names | Anonymous agent sessions |
| Coordinator Role | Mayor | No elevated orchestrator |

---

## Current State Analysis

### What Already Exists

**Agent Messaging** (`src/gobby/mcp_proxy/tools/agent_messaging.py`):
- `send_to_parent()` - Child sends to parent
- `send_to_child()` - Parent sends to specific child
- `poll_messages()` - Check incoming messages
- `broadcast_to_children()` - Parent broadcasts to all children
- `inter_session_messages` table supports arbitrary from/to sessions

**Agent Spawning** (`src/gobby/mcp_proxy/tools/spawn_agent.py`):
- `spawn_agent_impl()` with isolation modes: current, worktree, clone
- `AgentRun` model with run_id, session_id, parent_session_id, status
- `RunningAgentRegistry` for process tracking
- Environment variable passing (GOBBY_SESSION_ID, GOBBY_PARENT_SESSION_ID, etc.)

**Task System** (`src/gobby/storage/tasks/`):
- `assignee` field for task ownership
- `session_tasks` junction table for session-task links
- Dependency graph with cycle detection
- `suggest_next_task()` for AI-powered task suggestion

**Session Lifecycle** (`src/gobby/sessions/lifecycle.py`):
- `compact_handoff` for context preservation on compaction
- Memory/task sync to JSONL files
- Session status tracking (active, paused, completed)

### Gaps to Fill

1. **P2P Messaging**: `send_to_child()` validates parent-child relationship - blocks sibling communication
2. **Batch Assignment**: No way to assign N tasks to N agents atomically
3. **Checkpointing**: No periodic snapshots of agent state for recovery
4. **Named Identity**: Agents are anonymous - no "worker-1" that survives restarts
5. **Coordinator Role**: All sessions are peers - no elevated visibility/control

---

## Feature 1: Peer-to-Peer Mailboxes

### Overview

Extend existing messaging to allow ANY agent to message ANY other agent without hierarchy constraints. Enables sibling coordination, convoy coordination, and cross-team communication.

### Why It Matters

Currently, if two sibling agents need to coordinate, they must route through their parent:
```
Agent A → Parent → Agent B  (current)
Agent A → Agent B           (desired)
```

### Database Schema Changes

```sql
-- Migration: Add message_type for routing categorization
ALTER TABLE inter_session_messages ADD COLUMN message_type TEXT DEFAULT 'direct';
-- Types: 'direct', 'broadcast', 'convoy', 'coordinator'

CREATE INDEX idx_inter_session_messages_type ON inter_session_messages(message_type);
```

### New MCP Tools

| Tool | Description |
|------|-------------|
| `send_message(from_session, to_session, content, *, priority, message_type)` | Unrestricted P2P messaging |
| `list_siblings(session_id)` | Find agents with same parent |
| `broadcast_to_siblings(session_id, content)` | Send to all siblings |
| `discover_agents(project_id, status, role)` | Find agents by criteria |

### Code Changes

**File: `src/gobby/mcp_proxy/tools/agent_messaging.py`**

```python
@registry.tool(name="send_message")
async def send_message(
    from_session_id: str,
    to_session_id: str,
    content: str,
    *,
    priority: str = "normal",
    message_type: str = "direct",
) -> dict[str, Any]:
    """Direct P2P messaging without parent/child validation."""
    # Validate both sessions exist and are in same project
    from_session = session_manager.get(from_session_id)
    to_session = session_manager.get(to_session_id)

    if from_session.project_id != to_session.project_id:
        raise ValueError("Cross-project messaging not allowed")

    # Create message without hierarchy check
    message = message_manager.create(
        from_session=from_session_id,
        to_session=to_session_id,
        content=content,
        priority=priority,
        message_type=message_type,
    )
    return {"success": True, "message_id": message.id}
```

**File: `src/gobby/storage/sessions.py`**

```python
def find_siblings(self, session_id: str) -> list[Session]:
    """Find all sessions with the same parent_session_id."""
    session = self.get(session_id)
    if not session or not session.parent_session_id:
        return []

    rows = self.db.fetchall(
        """
        SELECT * FROM sessions
        WHERE parent_session_id = ? AND id != ? AND status = 'active'
        """,
        (session.parent_session_id, session_id),
    )
    return [Session.from_row(row) for row in rows]
```

### Security Considerations

- All sessions in same project can message each other
- Cross-project messaging blocked
- Consider rate limiting per session
- Optional message TTL for auto-cleanup

---

## Feature 2: Convoy Pattern (Batch Work Assignment)

### Overview

Create "convoys" that bundle multiple tasks and distribute them to N agents running in parallel, with unified tracking and completion status. This is Gastown's primary orchestration primitive.

### Why It Matters

Currently, spawning 5 agents on 10 tasks requires manual orchestration:
```python
# Current: manual, error-prone
for i in range(5):
    spawn_agent(task_id=tasks[i*2], ...)
    spawn_agent(task_id=tasks[i*2+1], ...)
    # No unified tracking, no auto-completion

# Desired: atomic, tracked
convoy = create_convoy(task_ids=[...], agent_count=5)
start_convoy(convoy_id)  # Auto-distributes, auto-tracks
```

### Database Schema Changes

```sql
-- Convoys table
CREATE TABLE convoys (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT,
    coordinator_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    -- 'pending', 'running', 'completed', 'failed', 'cancelled'
    task_ids TEXT NOT NULL,  -- JSON array
    agent_count INTEGER NOT NULL DEFAULT 1,
    distribution_strategy TEXT DEFAULT 'round_robin',
    -- 'round_robin', 'load_balance', 'priority'
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

-- Convoy-agent junction
CREATE TABLE convoy_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    convoy_id TEXT NOT NULL REFERENCES convoys(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    worktree_id TEXT REFERENCES worktrees(id) ON DELETE SET NULL,
    assigned_task_ids TEXT,  -- JSON array
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(convoy_id, session_id)
);
```

### New MCP Tools

| Tool | Description |
|------|-------------|
| `create_convoy(project_id, task_ids, agent_count, strategy, name)` | Create convoy |
| `start_convoy(convoy_id, isolation, base_branch)` | Spawn agents with worktrees |
| `get_convoy_status(convoy_id)` | Overall progress |
| `list_convoys(project_id, status)` | List convoys |
| `cancel_convoy(convoy_id)` | Cancel all agents |
| `join_convoy(convoy_id, session_id)` | Add existing session |

### New Files

**File: `src/gobby/storage/convoys.py`**

```python
@dataclass
class Convoy:
    id: str
    project_id: str
    name: str | None
    coordinator_session_id: str | None
    status: str  # pending, running, completed, failed, cancelled
    task_ids: list[str]
    agent_count: int
    distribution_strategy: str
    created_at: str
    updated_at: str
    completed_at: str | None

@dataclass
class ConvoyAgent:
    id: int
    convoy_id: str
    session_id: str
    worktree_id: str | None
    assigned_task_ids: list[str]
    status: str

class LocalConvoyManager:
    def create_convoy(self, ...) -> Convoy: ...
    def add_agent(self, convoy_id, session_id, task_ids) -> ConvoyAgent: ...
    def distribute_tasks(self, convoy_id, strategy) -> dict[str, list[str]]: ...
    def check_completion(self, convoy_id) -> bool: ...
```

**File: `src/gobby/mcp_proxy/tools/convoys.py`**

```python
@registry.tool(name="start_convoy")
async def start_convoy(
    convoy_id: str,
    isolation: str = "worktree",
    base_branch: str | None = None,
) -> dict[str, Any]:
    """Spawn all agents for a convoy."""
    convoy = convoy_manager.get(convoy_id)
    distribution = convoy_manager.distribute_tasks(convoy_id, convoy.distribution_strategy)

    results = []
    for i, (session_placeholder, task_ids) in enumerate(distribution.items()):
        result = await spawn_agent_impl(
            prompt=f"Work on tasks: {task_ids}",
            task_id=task_ids[0],
            isolation=isolation,
            convoy_id=convoy_id,
            agent_name=f"{convoy.name or 'convoy'}-{i+1}",
        )
        results.append(result)

    convoy_manager.update_status(convoy_id, "running")
    return {"success": True, "agents_spawned": len(results), "results": results}
```

### Distribution Strategies

| Strategy | Description |
|----------|-------------|
| `round_robin` | Tasks distributed evenly in order |
| `priority` | High-priority tasks to first agents |
| `load_balance` | Estimate complexity, balance load |

---

## Feature 3: Agent Crash Recovery / Checkpointing

### Overview

Periodic agent state snapshots that survive crashes, enabling resume from last checkpoint. Includes workflow state, task progress, and uncommitted git changes (stash).

### Why It Matters

Currently, if an agent crashes mid-task:
- Workflow state is lost
- Task progress must be inferred from commits
- Uncommitted changes are lost (unless in worktree)

With checkpointing:
- Resume from exact state
- Restore uncommitted work via git stash
- Context summary injected on resume

### Database Schema Changes

```sql
-- Agent checkpoints
CREATE TABLE agent_checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    checkpoint_type TEXT NOT NULL,  -- 'auto', 'manual', 'pre_risky_op'
    workflow_state_json TEXT,
    task_progress_json TEXT,  -- current task, status, notes
    context_summary TEXT,
    git_stash_ref TEXT,
    git_branch TEXT,
    git_commit_sha TEXT,
    files_modified TEXT,  -- JSON array
    created_at TEXT NOT NULL,
    is_latest INTEGER DEFAULT 1
);

CREATE INDEX idx_checkpoints_session ON agent_checkpoints(session_id);
CREATE INDEX idx_checkpoints_latest ON agent_checkpoints(session_id, is_latest)
    WHERE is_latest = 1;

-- Add to sessions
ALTER TABLE sessions ADD COLUMN last_checkpoint_id TEXT REFERENCES agent_checkpoints(id);
ALTER TABLE sessions ADD COLUMN recovery_count INTEGER DEFAULT 0;
```

### New MCP Tools

| Tool | Description |
|------|-------------|
| `create_checkpoint(session_id, type, summary)` | Manual checkpoint |
| `list_checkpoints(session_id, limit)` | View history |
| `get_checkpoint(checkpoint_id)` | Get details |
| `recover_from_checkpoint(checkpoint_id, target_session)` | Resume |
| `delete_old_checkpoints(session_id, keep_count)` | Cleanup |

### New Files

**File: `src/gobby/storage/checkpoints.py`**

```python
@dataclass
class AgentCheckpoint:
    id: str
    session_id: str
    checkpoint_type: str
    workflow_state_json: str | None
    task_progress_json: str | None
    context_summary: str | None
    git_stash_ref: str | None
    git_branch: str | None
    git_commit_sha: str | None
    files_modified: list[str] | None
    created_at: str
    is_latest: bool

class LocalCheckpointManager:
    def create_checkpoint(self, session_id, checkpoint_type, **kwargs) -> AgentCheckpoint: ...
    def get_latest(self, session_id) -> AgentCheckpoint | None: ...
    def restore_checkpoint(self, checkpoint_id, target_session_id) -> bool: ...
    def cleanup_old(self, session_id, keep_count=5) -> int: ...
```

**File: `src/gobby/agents/checkpoint_service.py`**

```python
class CheckpointService:
    """Handles checkpoint creation with git integration."""

    async def create_auto_checkpoint(self, session_id: str) -> AgentCheckpoint:
        """Create checkpoint with git stash if uncommitted changes."""
        # 1. Get current workflow state
        workflow_state = await self.workflow_manager.get_state(session_id)

        # 2. Get current task progress
        task_progress = await self._get_task_progress(session_id)

        # 3. Check for uncommitted git changes
        cwd = await self._get_session_cwd(session_id)
        has_changes = await self._has_uncommitted_changes(cwd)

        # 4. If changes, create git stash
        stash_ref = None
        if has_changes:
            stash_ref = await self._create_stash(cwd, session_id)

        # 5. Generate context summary
        summary = await self._generate_summary(session_id, task_progress)

        # 6. Store checkpoint
        return self.checkpoint_manager.create_checkpoint(
            session_id=session_id,
            checkpoint_type="auto",
            workflow_state_json=json.dumps(workflow_state),
            task_progress_json=json.dumps(task_progress),
            context_summary=summary,
            git_stash_ref=stash_ref,
            git_branch=await self._get_current_branch(cwd),
            git_commit_sha=await self._get_head_sha(cwd),
        )
```

### Checkpoint Triggers

| Trigger | Type | Description |
|---------|------|-------------|
| Timer | `auto` | Every N minutes of activity |
| Task completion | `auto` | After closing a task |
| Pre-risky operation | `pre_risky_op` | Before destructive actions |
| User request | `manual` | Explicit checkpoint |
| Session pause | `auto` | Before pausing session |

---

## Feature 4: Named Agent Identity with Resume

### Overview

Persistent agent names that survive restarts. When spawning `agent_name="worker-1"`, if a previous worker-1 session exists in resumable state, resume it instead of creating new.

### Why It Matters

Currently:
```python
spawn_agent(prompt="...", task_id=123)  # Creates new anonymous session
spawn_agent(prompt="...", task_id=123)  # Creates ANOTHER anonymous session
```

With named identity:
```python
spawn_agent(agent_name="worker-1", prompt="...", task_id=123)  # Creates worker-1
spawn_agent(agent_name="worker-1", prompt="continue")          # Resumes worker-1!
```

### Database Schema Changes

```sql
-- Add agent_name to sessions
ALTER TABLE sessions ADD COLUMN agent_name TEXT;
CREATE UNIQUE INDEX idx_sessions_agent_name ON sessions(project_id, agent_name)
    WHERE agent_name IS NOT NULL AND status IN ('active', 'paused');

-- Agent identities for persistent metadata
CREATE TABLE agent_identities (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    agent_definition TEXT,  -- Reference to .gobby/agents/*.yaml
    default_workflow TEXT,
    last_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    total_sessions INTEGER DEFAULT 0,
    total_tasks_completed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, agent_name)
);
```

### New MCP Tools

| Tool | Description |
|------|-------------|
| `spawn_agent` (enhanced) | Add `agent_name` parameter with resume logic |
| `list_agent_identities(project_id)` | List named agents |
| `get_agent_identity(project_id, name)` | Get agent history |
| `retire_agent(project_id, name)` | Mark as retired |

### Code Changes

**File: `src/gobby/mcp_proxy/tools/spawn_agent.py`**

```python
async def spawn_agent_impl(
    prompt: str,
    ...,
    agent_name: str | None = None,  # NEW: Named identity for resume
) -> dict[str, Any]:

    # Check for existing session with this agent_name
    if agent_name:
        existing = session_manager.find_by_agent_name(project_id, agent_name)
        if existing and existing.status in ('active', 'paused'):
            logger.info(f"Resuming existing agent: {agent_name}")
            return await _resume_agent_session(existing, prompt)

        # Update or create agent identity
        identity = identity_manager.get_or_create(project_id, agent_name)
        identity_manager.increment_stats(project_id, agent_name, sessions=1)

    # Create new session with agent_name
    session = session_manager.create(
        project_id=project_id,
        agent_name=agent_name,  # NEW field
        ...
    )

    # Link identity to new session
    if agent_name:
        identity_manager.update_last_session(project_id, agent_name, session.id)

    # ... continue with spawn
```

**File: `src/gobby/storage/sessions.py`**

```python
def find_by_agent_name(
    self, project_id: str, agent_name: str, resumable_only: bool = True
) -> Session | None:
    """Find session by agent name for resume."""
    statuses = "('active', 'paused')" if resumable_only else "('active', 'paused', 'completed')"
    row = self.db.fetchone(
        f"""
        SELECT * FROM sessions
        WHERE project_id = ? AND agent_name = ? AND status IN {statuses}
        ORDER BY updated_at DESC LIMIT 1
        """,
        (project_id, agent_name),
    )
    return Session.from_row(row) if row else None
```

### Naming Conventions

Suggested patterns:
- `{role}-{number}`: worker-1, reviewer-2, tester-3
- `{task}-{id}`: auth-worker, api-builder
- `{convoy}-{index}`: convoy-feature-1, convoy-feature-2

---

## Feature 5: Coordinator Role Pattern

### Overview

Designated coordinator session with elevated visibility and control over sibling agents. Coordinator can read all sibling progress, reassign tasks from stuck agents, and orchestrate completion.

### Why It Matters

Currently all sessions are peers with no visibility into siblings:
```
Parent
├── Agent A (can't see B's progress)
├── Agent B (can't see A's progress)
└── Agent C (can't see anyone's progress)
```

With coordinator:
```
Parent
├── Coordinator (sees all, can reassign)
├── Worker A (reports to coordinator)
├── Worker B (reports to coordinator)
└── Worker C (reports to coordinator)
```

### Database Schema Changes

```sql
-- Add role to sessions
ALTER TABLE sessions ADD COLUMN role TEXT DEFAULT 'worker';
-- Values: 'coordinator', 'worker'
CREATE INDEX idx_sessions_role ON sessions(role);

-- Add coordinator relationship
ALTER TABLE sessions ADD COLUMN coordinator_session_id TEXT REFERENCES sessions(id);
CREATE INDEX idx_sessions_coordinator ON sessions(coordinator_session_id);
```

### New MCP Tools

| Tool | Description |
|------|-------------|
| `designate_coordinator(session_id)` | Mark as coordinator |
| `get_coordinated_agents(coordinator_session_id)` | List workers |
| `get_agent_progress(session_id)` | Read any sibling's state |
| `reassign_task(task_id, from, to)` | Move task between agents |
| `request_status_report(coordinator_session_id)` | Broadcast status request |
| `submit_status_report(session_id, report)` | Worker submits status |

### New Files

**File: `src/gobby/mcp_proxy/tools/coordinator.py`**

```python
@registry.tool(name="get_agent_progress")
async def get_agent_progress(
    session_id: str,
    requester_session_id: str,
) -> dict[str, Any]:
    """Get detailed progress for an agent (coordinator only)."""
    # Verify requester is coordinator
    requester = session_manager.get(requester_session_id)
    if requester.role != "coordinator":
        raise PermissionError("Only coordinators can view agent progress")

    # Get target session's state
    target = session_manager.get(session_id)
    workflow_state = workflow_manager.get_state(session_id)
    current_tasks = task_manager.get_session_tasks(session_id)

    return {
        "session_id": session_id,
        "agent_name": target.agent_name,
        "status": target.status,
        "workflow_step": workflow_state.current_step if workflow_state else None,
        "current_tasks": [t.to_brief() for t in current_tasks],
        "last_activity": target.updated_at,
    }

@registry.tool(name="reassign_task")
async def reassign_task(
    task_id: str,
    from_session_id: str | None,
    to_session_id: str,
    requester_session_id: str,
) -> dict[str, Any]:
    """Reassign a task from one agent to another (coordinator only)."""
    # Verify requester is coordinator
    requester = session_manager.get(requester_session_id)
    if requester.role != "coordinator":
        raise PermissionError("Only coordinators can reassign tasks")

    # Update task assignee
    task = task_manager.get(task_id)
    to_session = session_manager.get(to_session_id)
    task_manager.update(task_id, assignee=to_session.agent_name)

    # Notify both agents
    if from_session_id:
        await send_message(
            requester_session_id, from_session_id,
            f"Task {task_id} has been reassigned to {to_session.agent_name}",
            message_type="coordinator"
        )
    await send_message(
        requester_session_id, to_session_id,
        f"Task {task_id} has been assigned to you",
        message_type="coordinator"
    )

    return {"success": True, "task_id": task_id, "new_assignee": to_session.agent_name}
```

### Coordinator Election

Options:
1. **Explicit**: First spawned agent designated as coordinator
2. **Automatic**: Parent can designate any child as coordinator
3. **Self-election**: Agent claims coordinator role if none exists

### Failure Handling

If coordinator crashes:
- Workers continue independently
- New coordinator can be elected
- Previous coordinator's state available via checkpoints

---

## Implementation Phases

### Phase 1: Foundation (Migrations + P2P Messaging)

**Scope**: Database migrations, peer-to-peer messaging, sibling discovery

**Files**:
- `src/gobby/storage/migrations.py` - Add migrations
- `src/gobby/mcp_proxy/tools/agent_messaging.py` - Add `send_message`, `list_siblings`
- `src/gobby/storage/sessions.py` - Add `find_siblings()`

**Tests**:
- Test P2P messaging between non-parent-child sessions
- Test sibling discovery
- Test cross-project blocking

### Phase 2: Named Agents

**Scope**: Agent identity storage, session agent_name, resume logic

**Files**:
- `src/gobby/storage/agent_identities.py` (new)
- `src/gobby/storage/sessions.py` - Add agent_name field
- `src/gobby/mcp_proxy/tools/spawn_agent.py` - Add resume logic

**Tests**:
- Test spawning with agent_name creates identity
- Test re-spawning same name resumes session
- Test unique constraint on active sessions

### Phase 3: Checkpointing

**Scope**: Checkpoint storage, git stash integration, auto-checkpoint loop

**Files**:
- `src/gobby/storage/checkpoints.py` (new)
- `src/gobby/agents/checkpoint_service.py` (new)
- `src/gobby/sessions/lifecycle.py` - Add checkpoint loop

**Tests**:
- Test checkpoint creation with git stash
- Test recovery from checkpoint
- Test auto-checkpoint timing

### Phase 4: Coordinator Pattern

**Scope**: Session roles, coordinator tools, status reporting

**Files**:
- `src/gobby/storage/sessions.py` - Add role, coordinator_session_id
- `src/gobby/mcp_proxy/tools/coordinator.py` (new)

**Tests**:
- Test coordinator can read worker progress
- Test coordinator can reassign tasks
- Test workers can't use coordinator tools

### Phase 5: Convoy Pattern

**Scope**: Convoy storage, distribution strategies, completion tracking

**Files**:
- `src/gobby/storage/convoys.py` (new)
- `src/gobby/mcp_proxy/tools/convoys.py` (new)
- `src/gobby/mcp_proxy/tools/spawn_agent.py` - Add convoy_id parameter

**Tests**:
- Test convoy creation with task distribution
- Test convoy start spawns correct number of agents
- Test convoy auto-completes when all agents done

### Phase 6: Integration & Testing

**Scope**: End-to-end tests, documentation, performance

**Tests**:
- E2E: Convoy with coordinator, multiple workers, checkpointing
- E2E: Named agent crash and resume
- Performance: Message throughput, checkpoint overhead

---

## Migration Summary

| Migration | Description |
|-----------|-------------|
| 83 | Add message_type to inter_session_messages |
| 84 | Create convoys table |
| 85 | Create convoy_agents table |
| 86 | Create agent_checkpoints table |
| 87 | Add last_checkpoint_id, recovery_count to sessions |
| 88 | Add agent_name to sessions with unique index |
| 89 | Create agent_identities table |
| 90 | Add role to sessions |
| 91 | Add coordinator_session_id to sessions |

---

## Verification Plan

### Manual Testing

1. **P2P Messaging**
   - Spawn 2 sibling agents
   - Have agent A send message to agent B
   - Verify agent B receives via poll_messages

2. **Convoy**
   - Create convoy with 5 tasks, 2 agents
   - Start convoy
   - Verify 2 worktrees created, tasks distributed
   - Complete tasks, verify convoy auto-completes

3. **Checkpointing**
   - Spawn agent, start work
   - Manually create checkpoint
   - Kill agent process
   - Recover from checkpoint
   - Verify git stash restored

4. **Named Identity**
   - `spawn_agent(agent_name="test-1", prompt="start")`
   - Pause session
   - `spawn_agent(agent_name="test-1", prompt="continue")`
   - Verify same session resumed

5. **Coordinator**
   - Spawn coordinator + 2 workers
   - Coordinator calls `get_agent_progress` on workers
   - Coordinator reassigns task from worker-1 to worker-2
   - Verify task assignee updated, messages sent

### Automated Testing

```python
# tests/test_p2p_messaging.py
async def test_sibling_messaging():
    parent = await create_session()
    child_a = await spawn_agent(parent_session_id=parent.id)
    child_b = await spawn_agent(parent_session_id=parent.id)

    await send_message(child_a.id, child_b.id, "hello sibling")
    messages = await poll_messages(child_b.id)

    assert len(messages) == 1
    assert messages[0].content == "hello sibling"

# tests/test_convoy.py
async def test_convoy_completion():
    tasks = [await create_task() for _ in range(4)]
    convoy = await create_convoy(task_ids=[t.id for t in tasks], agent_count=2)
    await start_convoy(convoy.id)

    # Simulate agents completing their tasks
    for agent in convoy.agents:
        for task_id in agent.assigned_task_ids:
            await close_task(task_id)

    convoy = await get_convoy(convoy.id)
    assert convoy.status == "completed"
```

---

## References

- [Gastown Repository](https://github.com/steveyegge/gastown)
- Existing Gobby messaging: `src/gobby/mcp_proxy/tools/agent_messaging.py`
- Existing spawn system: `src/gobby/mcp_proxy/tools/spawn_agent.py`
- Existing session lifecycle: `src/gobby/sessions/lifecycle.py`

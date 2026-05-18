# Multi-Agent Orchestration: P2P Messaging, Parties, and Leaders

## Context

Gastown demonstrates multi-agent patterns (convoys, mailboxes, Mayor coordinator), but its "Convoy" model spawns N identical workers. Gobby has richer infrastructure — pipelines, workflow engine with triggers/rules/tool blocks, agent definitions with roles. The opportunity is a **heterogeneous** orchestration model: **Parties** — where different agent roles collaborate with workflow-driven handoffs and configurable recovery.

**Existing infrastructure we build on:**
- Pipeline executor with `spawn_session` and `activate_workflow` step types (`workflows/pipeline_executor.py`)
- Agent definitions with named workflows, modes, isolation, orchestrator enforcement (`agents/definitions.py`, `storage/agent_definitions.py`)
- Workflow engine with step transitions, tool blocking, behavioral rules (`workflows/engine.py`)
- Parent-child messaging (`mcp_proxy/tools/agent_messaging.py`)
- Worktree/clone isolation with CLI hook copying (`agents/isolation.py`)
- `RunningAgentRegistry` with event callbacks (`agents/registry.py`)
- Task system with `escalated` status (`storage/tasks/`)

**Key design decisions:**
- **DB is source of truth** — no YAML loaders. MCP tools for CRUD. UI-ready from day 1.
- **Isolation comes from agent definitions** — party roles don't specify isolation. Each agent definition sets its own isolation mode (e.g., Gemini agents use `clone` due to worktree limitations per GitHub Issue #12050).
- **HITL via both task escalation (v1) and approval gates (future)** — different mechanisms for different purposes.

---

## Feature 1: P2P Messaging

Remove parent-child validation. Any session can message any session in the same project.

### Schema Change (migration 102)

```sql
ALTER TABLE inter_session_messages ADD COLUMN message_type TEXT DEFAULT 'direct';
ALTER TABLE inter_session_messages ADD COLUMN party_id TEXT;
CREATE INDEX idx_ism_type ON inter_session_messages(message_type);
CREATE INDEX idx_ism_party ON inter_session_messages(party_id) WHERE party_id IS NOT NULL;
```

### New MCP Tools (added to `gobby-agents` registry)

| Tool | Purpose |
|------|---------|
| `send_message(from_session, to_session, content, *, priority, message_type)` | P2P message — validates same project, no hierarchy check |
| `list_siblings(session_id)` | Find sessions sharing same parent |
| `discover_agents(project_id, status?, party_id?)` | Find active sessions by criteria |
| `broadcast_to_party(party_id, content)` | Message all party members |

### Files

| Action | File |
|--------|------|
| Create | `src/gobby/mcp_proxy/tools/p2p_messaging.py` |
| Modify | `src/gobby/storage/migrations.py` — migration 102 |
| Modify | `src/gobby/storage/inter_session_messages.py` — add `message_type`, `party_id` to model + `create_message()` |
| Modify | `src/gobby/mcp_proxy/registries.py` — wire P2P tools into `gobby-agents` |

Keep existing `send_to_parent`, `send_to_child`, `broadcast_to_children` unchanged.

---

## Feature 2: Party Pattern

A **Party** is a heterogeneous group of agents with different roles, orchestrated via a dependency DAG with configurable recovery and HITL escalation.

### Storage Model

**Database is source of truth.** Party definitions stored in `party_definitions` table. Created/managed via MCP tools (`create_party_definition`, `update_party_definition`). No YAML loader — UI and agents use the same MCP API.

### Example Party Definition (conceptual)

```
name: feature-development
description: "Leader plans, developers implement, QA reviews, merger lands"

roles:
  leader:
    agent: coordinator-def       # agent definition (has its own mode, isolation, etc.)
    workflow: planning
    is_leader: true

  developer:
    agent: dev-def               # agent def sets isolation: worktree
    workflow: implement
    count: 2                     # spawn 2 in parallel
    on_crash: restart
    retry_attempts: 3

  qa:
    agent: qa-def                # agent def sets isolation: clone (for Gemini compat)
    workflow: review
    on_crash: pause
    notify: leader

  merger:
    agent: merge-def
    workflow: merge-work
    on_crash: pause
    notify: user                 # HITL

flow:                            # DAG: role -> [dependencies]
  leader: []                     # starts first
  developer: [leader]            # after leader signals
  qa: [developer]                # after ALL developer instances complete
  merger: [qa]                   # after ALL qa instances complete

recovery:                        # party-wide defaults (roles override)
  on_crash: pause
  notify: leader
  max_retries: 2
```

**Isolation is NOT in the party definition** — it comes from each role's agent definition. A Gemini-based QA agent uses `isolation: clone` in its agent def; a Claude-based developer uses `isolation: worktree` in its agent def. The party executor just calls `spawn_agent_impl()` which reads the agent definition.

### How the DAG Works

The `flow` field is a dependency graph. The executor topologically sorts it and spawns roles when all dependencies are met.

**Parallel execution with count > 1:**
```
leader (1 agent)
       ↓ completes
developer-0  developer-1     (2 agents, parallel — isolation per agent def)
       ↓ ALL complete
qa-0                          (1 agent reviews all work)
       ↓ completes
merger (1 agent, HITL on conflicts)
```

**V1 spawn mode:** `all_at_once` — all instances of a role spawn when dependencies met, next role waits for ALL instances to complete.

**Future spawn modes** (not v1):
- `on_demand` — spawn one instance per completed dependency item (e.g., 1 QA per developer branch)
- `sequential` — spawn instances one at a time
- `work_queue` — agents claim from shared queue

### Recovery Strategies

Configurable at two levels — party-wide defaults and per-role overrides. Leader can also override at runtime.

| Strategy | Behavior |
|----------|----------|
| `restart` | Re-spawn agent with workflow state. `retry_attempts` limits retries. |
| `pause` | Pause the role. Notify `leader`, `user`, or `party`. Wait for intervention. |
| `abort` | Kill all party members, mark party failed. Clean up. |

**Role-level `on_crash`** overrides party-wide `recovery.on_crash`. The leader can call `override_recovery(party_id, role, strategy)` at runtime.

### HITL Escalation (Phased)

**Phase 1 — Task escalation** (already exists):
- Agent marks task as `escalated` status when stuck
- Party executor detects escalated tasks, notifies leader/user
- Async — party continues while escalated task awaits human

**Phase 2 — Approval gates** (future):
- Reuse pipeline approval system (tokens, webhooks)
- For blocking decisions: "merge has conflicts, approve resolution?"
- Agent pauses, human approves/rejects via CLI or webhook

### Schema Changes (migration 103)

```sql
-- Party definitions (source of truth, managed via MCP tools)
CREATE TABLE party_definitions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT,                         -- NULL = global template
    description TEXT,
    definition_json TEXT NOT NULL,            -- full definition as JSON
    version TEXT DEFAULT '1.0',
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, project_id)
);

-- Party executions (launched instances)
CREATE TABLE parties (
    id TEXT PRIMARY KEY,
    definition_id TEXT REFERENCES party_definitions(id),
    name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/running/completed/failed/cancelled
    definition_snapshot_json TEXT NOT NULL,   -- frozen copy at launch time
    inputs_json TEXT,
    recovery_json TEXT,                      -- party-wide recovery config
    leader_session_id TEXT REFERENCES sessions(id),
    task_id TEXT REFERENCES tasks(id),
    created_by_session_id TEXT REFERENCES sessions(id),
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_parties_project ON parties(project_id);
CREATE INDEX idx_parties_status ON parties(status);

-- Party member instances
CREATE TABLE party_members (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    role_name TEXT NOT NULL,
    instance_index INTEGER NOT NULL DEFAULT 0,
    session_id TEXT REFERENCES sessions(id),
    agent_run_id TEXT REFERENCES agent_runs(id),
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/waiting/running/completed/failed/crashed
    task_id TEXT REFERENCES tasks(id),
    workflow_name TEXT,
    worktree_id TEXT,
    branch_name TEXT,
    crash_count INTEGER DEFAULT 0,
    on_crash TEXT,                            -- role-level override
    max_retries INTEGER,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, role_name, instance_index)
);
CREATE INDEX idx_party_members_party ON party_members(party_id);
CREATE INDEX idx_party_members_session ON party_members(session_id);
```

### Party Executor — DAG Lifecycle

```
1. LAUNCH
   - Validate definition (agents exist, flow is valid DAG — no cycles)
   - Create parties record (status=pending)
   - Create party_members records (status=pending)
   - Topologically sort flow

2. START ROOTS
   - For each root role (no dependencies), spawn all instances
   - Uses spawn_agent_impl() — reads agent def for isolation, mode, etc.
   - Update party_members with session_id, agent_run_id (status=running)
   - If is_leader + mode=self → activate workflow on caller session

3. MONITOR (async loop)
   - Subscribe to RunningAgentRegistry events (agent_completed, agent_failed)
   - On role instance completion:
     a. Update party_members status
     b. If ALL instances of role complete → check dependents
     c. Spawn newly unblocked roles
     d. Notify leader via P2P message
   - On role instance crash:
     a. Increment crash_count
     b. Apply recovery strategy (restart/pause/abort)
     c. Notify per strategy config

4. COMPLETE
   - All roles done → parties.status = completed
   - Emit party_completed event

5. ERROR
   - Role exceeds retry_attempts → mark failed
   - If recovery=abort → cancel_party (kill all, cleanup)
   - If recovery=pause → notify leader/user, wait
```

### Files

| Action | File | Purpose |
|--------|------|---------|
| Create | `src/gobby/parties/__init__.py` | Package |
| Create | `src/gobby/parties/definitions.py` | `PartyDefinition`, `PartyRoleDefinition`, `RecoveryConfig` Pydantic models |
| Create | `src/gobby/parties/executor.py` | `PartyExecutor` — DAG scheduling, spawn, monitor, recovery |
| Create | `src/gobby/storage/parties.py` | `LocalPartyManager` — CRUD for all 3 tables |
| Modify | `src/gobby/storage/migrations.py` | Migration 103 |
| Modify | `src/gobby/agents/registry.py` | Add optional `party_id` to `RunningAgent` |

---

## Feature 3: Leader Role

The leader is a party role with `is_leader: true`. Gets elevated MCP tools via a new `gobby-party` registry.

### New MCP Tools (`gobby-party` registry)

| Tool | Purpose |
|------|---------|
| `create_party_definition(name, roles, flow, recovery?)` | Create party definition in DB |
| `update_party_definition(name, ...)` | Update existing definition |
| `get_party_definition(name)` | Read definition |
| `list_party_definitions()` | List all definitions |
| `delete_party_definition(name)` | Remove definition |
| `launch_party(name, task_id?, inputs?)` | Launch party from stored definition |
| `get_party_status(party_id)` | All roles, sessions, statuses, task progress |
| `signal_role(party_id, role_name, signal)` | Trigger phase ("proceed", "retry", "abort") |
| `reassign_task(party_id, task_id, from_role, to_role)` | Move task between members |
| `override_recovery(party_id, role_name, strategy)` | Change recovery strategy at runtime |
| `cancel_party(party_id)` | Cancel and kill all members, cleanup |
| `list_parties(status?)` | List parties for current project |

### Files

| Action | File |
|--------|------|
| Create | `src/gobby/mcp_proxy/tools/party.py` — `create_party_registry()` |
| Modify | `src/gobby/mcp_proxy/registries.py` — wire `gobby-party` into `setup_internal_registries()` |

---

## Gemini Worktree Limitation

**Confirmed:** Gemini CLI restricts file system tools to the primary workspace directory and cannot access worktree paths (GitHub Issue #12050, P2 priority, unresolved). No `--cwd` flag exists.

**Resolution:** Not a party-level concern. Agent definitions for Gemini-based agents should set `isolation: clone`. The party executor just calls `spawn_agent_impl()` which respects the agent definition's isolation setting. CodeRabbit's approach (git-worktree-runner) also doesn't solve this — they target Claude Code.

---

## Implementation Phases

### Phase 1: P2P Messaging (foundation)
- Migration 102, `p2p_messaging.py`, extend `InterSessionMessageManager`
- Tests: `tests/mcp_proxy/tools/test_p2p_messaging.py`

### Phase 2: Party Definitions + Storage
- `parties/definitions.py` (Pydantic models), `storage/parties.py` (CRUD)
- Migration 103 (all 3 tables)
- Definition CRUD via MCP tools
- Tests: `tests/parties/test_definitions.py`, `tests/storage/test_parties.py`

### Phase 3: Party Executor
- `parties/executor.py` — DAG scheduling, spawn via `spawn_agent_impl`, monitor via registry events
- Recovery strategy execution (restart/pause/abort)
- Extend `RunningAgent` with `party_id`
- Tests: `tests/parties/test_executor.py`

### Phase 4: Party MCP Tools + Leader
- `mcp_proxy/tools/party.py` with `gobby-party` registry
- Wire into `setup_internal_registries()`
- Tests: `tests/mcp_proxy/tools/test_party.py`

### Phase 5: Built-in Example + Integration Test
- Create a `feature-development` party definition via MCP tool
- E2E test: launch party, verify DAG execution, recovery on simulated crash

### Future (not v1)
- On-demand spawn mode (1 QA per completed developer branch)
- Work-claiming queue (agents claim from shared pool)
- Approval gates for blocking HITL decisions (reuse pipeline approval system)
- Dynamic leader recovery on leader crash
- UI party builder/editor

---

## Verification

1. **P2P Messaging**: Spawn 2 sibling agents, A messages B directly, B receives via `poll_messages`
2. **Party Definition CRUD**: `create_party_definition` → `get_party_definition` → matches
3. **Party Launch**: 2-role party (developer → qa), verify sequential spawn with correct workflows
4. **DAG Parallelism**: Party with `count: 2` developers, verify both spawn concurrently, QA waits for both
5. **Recovery**: Simulate agent crash, verify restart strategy re-spawns, pause strategy notifies leader
6. **Leader**: Launch party with leader, verify `get_party_status()` returns member progress
7. **HITL**: Agent escalates task, verify leader/user notification
8. **Unit tests**: `pytest tests/parties/ tests/mcp_proxy/tools/test_p2p_messaging.py tests/mcp_proxy/tools/test_party.py -v`

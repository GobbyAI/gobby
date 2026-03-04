# Party System v2: On-Demand Spawning, Work Queues, Approval Gates, Leader Recovery

## Context

The Party system v1 (`docs/plans/party-time.md`) designs heterogeneous multi-agent orchestration: roles referencing agent definitions, a dependency DAG, configurable recovery, and a leader role with elevated tools. V1 uses `all_at_once` spawn mode (all N instances of a role spawn when dependencies met, next role waits for ALL to complete). V1 explicitly deferred four capabilities to v2. This document designs them.

**V1 is not yet implemented** — `src/gobby/parties/` does not exist. V1 schema lives in planned migrations 102 (P2P messaging) and 103 (party_definitions, parties, party_members tables). This design adds migration 104.

**Existing infrastructure we build on:**
- `PartyExecutor` subscribes to `RunningAgentRegistry` event callbacks (`agents/registry.py`)
- `spawn_agent_impl()` spawns agents with isolation, workflows, sessions (`mcp_proxy/tools/spawn_agent.py`)
- Pipeline approval system: token-based pause/resume (`workflows/pipeline_executor.py:619-809`)
- `InterSessionMessageManager` for P2P messaging (`storage/inter_session_messages.py`)
- SQLite `transaction_immediate()` for atomic operations (`storage/database.py`)

**Naming note:** V1's `is_coordinator` / `coordinator_session_id` is renamed to `is_leader` / `leader_session_id` throughout both v1 and v2. The coordinator concept was confusing with the generic term — "leader" clearly denotes the party orchestration role.

---

## Feature 1: On-Demand Spawn Mode

### Concept

Invert v1's batch model to a streaming model: each upstream member completion triggers one downstream spawn. Example: 3 developers each finish a branch; as each finishes, one QA agent spawns to review *that specific branch* — not all 3 QA agents at once.

### Trigger Mechanism

The `PartyExecutor` already subscribes to `RunningAgentRegistry` event callbacks. When `agent_completed` fires, the executor:

1. Looks up the completed agent's `session_id` in `party_members` to identify the member
2. Harvests outputs from the member row (`branch_name`, `worktree_id`, `session_id`) and session record
3. Stores outputs in `party_members.outputs_json`
4. For each downstream role with `spawn_mode: on_demand`: spawns a new instance, passing upstream outputs as `step_variables` on the workflow activation

The downstream agent's workflow accesses upstream context via `{{ variables.upstream_branch }}`, `{{ variables.upstream_session_id }}`, etc.

### Count Semantics

| Field | `all_at_once` meaning | `on_demand` meaning |
|-------|----------------------|---------------------|
| `count` | Exact spawn count | Max concurrent instances |
| `max_instances` | N/A (= count) | Lifetime total cap |

If 5 developers complete but `count=2`, only 2 QA agents run concurrently; remaining queue in `party_pending_spawns` and drain as slots free. If `max_instances=3`, only 3 ever spawn — remaining outputs are dropped.

### Fan-In

Optional `fan_in_count` field controls batching:
- **Not set**: 1 upstream completion -> 1 downstream spawn
- **`fan_in_count: N`**: accumulate N upstream completions, then spawn 1 instance with all N outputs
- **`fan_in_count: "all"`**: equivalent to `all_at_once` behavior (wait for all, then spawn one)

### DAG Propagation Rules

For each downstream role, when an upstream member completes:
1. If downstream is `all_at_once`: check if ALL instances of ALL dependency roles are complete -> spawn batch (v1 behavior)
2. If downstream is `on_demand` (no fan_in): check concurrency cap, spawn or queue
3. If downstream is `on_demand` + `fan_in_count: N`: accumulate in pending queue; when N pending, spawn one with all N outputs

Mixed modes work freely — a party can have `on_demand` developers feeding `all_at_once` QA.

### Schema (part of migration 104)

```sql
ALTER TABLE party_members ADD COLUMN spawn_mode TEXT DEFAULT 'all_at_once';
ALTER TABLE party_members ADD COLUMN spawned_by_member_id TEXT REFERENCES party_members(id);
ALTER TABLE party_members ADD COLUMN outputs_json TEXT;

CREATE INDEX idx_pm_spawned_by ON party_members(spawned_by_member_id)
    WHERE spawned_by_member_id IS NOT NULL;

CREATE TABLE party_pending_spawns (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    target_role TEXT NOT NULL,
    triggered_by_member_id TEXT NOT NULL REFERENCES party_members(id),
    outputs_json TEXT,
    priority INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, triggered_by_member_id, target_role)
);
CREATE INDEX idx_pps_party_role ON party_pending_spawns(party_id, target_role);
```

### Model Changes (`src/gobby/parties/definitions.py`)

```python
class SpawnMode(str, Enum):
    ALL_AT_ONCE = "all_at_once"
    ON_DEMAND = "on_demand"

class PartyRoleDefinition(BaseModel):
    # ... existing v1 fields ...
    spawn_mode: SpawnMode = SpawnMode.ALL_AT_ONCE
    max_instances: int | None = None        # on_demand only: lifetime cap
    fan_in_count: int | Literal["all"] | None = None  # batch N upstreams per spawn
```

### MCP Tools (added to `gobby-party`)

| Tool | Purpose |
|------|---------|
| `get_member_outputs(party_id, member_id)` | Read outputs from a completed member |
| `list_pending_spawns(party_id, role?)` | View queued on-demand spawns |
| `flush_pending_spawns(party_id, role)` | Force-spawn all pending (leader override) |

### Edge Cases

- **Upstream crashes**: `agent_failed` fires, NOT `agent_completed`. On-demand downstream does NOT spawn. Recovery strategy on the upstream role handles this; after successful restart + completion, on-demand triggers normally.
- **Downstream crash**: Restarted instance retains `spawned_by_member_id` and `outputs_json` — restart rebuilds from same upstream output.
- **Party cancellation**: CASCADE on `party_pending_spawns.party_id` cleans up.

---

## Feature 2: Work-Claiming Queue

### Concept

On-demand pushes work TO agents. The work queue inverts this: agents PULL work from a shared pool with atomic claiming. Classic worker pool pattern that layers on top of the DAG.

### How It Layers on the DAG

The DAG controls **when** a role becomes eligible (dependencies satisfied). The queue controls **what** instances work on. A role with `work_queue` configured enters a claiming loop: spawn via DAG, activate workflow, then repeatedly `claim_work_item()` -> process -> `complete_work_item()` -> claim next.

### Work Item Lifecycle

`available` -> `claimed` -> `completed` | `failed`

Failed items auto-requeue up to `max_attempts`, then permanently fail and notify the leader.

### How Work Enters the Queue

Three mechanisms:
1. **Agent publishes**: Upstream agent calls `publish_work_item(queue_name, payload)` MCP tool when it finishes a unit of work
2. **Definition pre-populates**: `work_queues.{name}.initial_items` list in the party definition — executor inserts these at launch
3. **Executor auto-publishes**: When an upstream member completes and the downstream role has `work_queue` configured, the executor auto-publishes the upstream's outputs as a work item

### Atomic Claiming

SQLite `transaction_immediate()` prevents double-claims:

```python
def claim_next(self, party_id, queue_name, member_id) -> WorkItem | None:
    with self.db.transaction_immediate() as conn:
        row = conn.execute(
            "SELECT id FROM party_work_queue "
            "WHERE party_id = ? AND queue_name = ? AND status = 'available' "
            "ORDER BY priority ASC, created_at ASC LIMIT 1",
            (party_id, queue_name),
        ).fetchone()
        if not row:
            return None
        result = conn.execute(
            "UPDATE party_work_queue SET status='claimed', "
            "claimed_by_member_id=?, claimed_at=? WHERE id=? AND status='available'",
            (member_id, now, row["id"]),
        )
        if result.rowcount == 0:
            return None  # Lost race: another claimer claimed this row first
        return self._row_to_work_item(row)
```

### Schema (part of migration 104)

```sql
CREATE TABLE party_work_queue (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    queue_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',  -- available|claimed|completed|failed
    claimed_by_member_id TEXT REFERENCES party_members(id),
    claimed_at TEXT,
    completed_at TEXT,
    result_json TEXT,
    error TEXT,
    created_by_member_id TEXT REFERENCES party_members(id),
    priority INTEGER DEFAULT 0,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pwq_claim ON party_work_queue(party_id, queue_name, status, priority, created_at);
CREATE INDEX idx_pwq_member ON party_work_queue(claimed_by_member_id)
    WHERE claimed_by_member_id IS NOT NULL;

CREATE TABLE party_queue_config (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    queue_name TEXT NOT NULL,
    drain_and_exit INTEGER DEFAULT 0,
    visibility_timeout_seconds INTEGER,
    max_items INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, queue_name)
);
```

### Model Changes

```python
class WorkQueueConfig(BaseModel):
    name: str | None = None              # defaults to role name
    drain_and_exit: bool = False         # agents exit when queue empty
    visibility_timeout_seconds: int | None = None
    max_items: int | None = None
    initial_items: list[dict[str, Any]] = Field(default_factory=list)

class PartyRoleDefinition(BaseModel):
    # ... existing fields ...
    work_queue: str | WorkQueueConfig | None = None
```

New file `src/gobby/parties/work_queue.py` with `WorkItem` dataclass and `PartyWorkQueueManager`.

### MCP Tools (added to `gobby-party`)

| Tool | Purpose |
|------|---------|
| `claim_work_item(queue_name?)` | Atomically claim next available item (queue defaults to role's queue) |
| `complete_work_item(item_id, result?)` | Mark claimed item completed |
| `fail_work_item(item_id, error)` | Mark failed; auto-requeues if under max_attempts |
| `release_work_item(item_id)` | Voluntary requeue |
| `publish_work_item(queue_name, payload, priority?)` | Add item to queue |
| `get_queue_status(queue_name?)` | Counts by status |
| `peek_queue(queue_name?, limit?)` | Preview without claiming |

### Edge Cases

- **Agent crashes mid-claim**: `visibility_timeout_seconds` auto-releases stale claims. Additionally, `agent_failed` handler explicitly releases all items claimed by the failed member.
- **Poison items**: After `max_attempts` failures, item permanently fails. Leader notified via P2P message.
- **Queue exhaustion with `drain_and_exit: false`**: Agent's workflow should have a poll interval to avoid busy-waiting. `claim_work_item` returns null when empty.

### Composition with On-Demand

A role can use BOTH: `spawn_mode: on_demand` + `work_queue`. Agents spawn reactively as upstream completes AND claim from a shared queue. Useful when a pool of agents should process multiple upstream items.

---

## Feature 3: Approval Gates in Parties

### Concept

Blocking gates on DAG edges that pause execution using the same token-based pattern as pipeline approval (ref: `pipeline_executor.py:619-809`). Unlike task escalation (async, party continues), gates block the DAG edge until approved or rejected.

### Where Gates Go: DAG Edges Only

Gates are properties of edges in the dependency graph. "After developer completes, require approval before QA starts." This maps naturally to the DAG. Party completion gates are unnecessary (put a gate before the final role instead). Per-member gates belong inside the role's workflow, not at the party DAG level.

### How Gates Interact with the DAG

When role A completes but the A->B edge has a gate:
- Role A members: `completed` (work done)
- Edge A->B: `waiting` (gate active, token generated)
- Role B members: `pending` (cannot spawn)
- Other non-gated edges from A proceed normally (A->C fires)
- Dependents of B blocked transitively

### Gate Definition

Both declarative (in party definition) and dynamic (runtime injection by leader):

```yaml
flow:
  leader: []
  developer: [leader]
  qa: [developer]
  merger: [qa]

approval_gates:
  developer->qa:
    message: "Review developer output before QA begins"
    auto_approve_by: leader    # leader gets P2P message with token
  qa->merger:
    message: "Approve QA results before merge"
    # no auto_approve_by — requires human or explicit action
```

### Who Approves

Same token, multiple consumers:
- **Leader**: calls `approve_party_gate(token)` MCP tool
- **Human**: uses CLI `gobby parties approve <token>` or HTTP API
- **Webhook**: optional notification to external URL

`approved_by` records who did it (session_id, "cli:user", "webhook:...").

### Schema (part of migration 104)

```sql
CREATE TABLE party_approval_gates (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|waiting|approved|rejected
    message TEXT,
    token TEXT UNIQUE,
    auto_approve_by TEXT,         -- role name
    source TEXT NOT NULL DEFAULT 'definition',  -- definition|dynamic
    approved_by TEXT,
    approved_at TEXT,
    rejected_by TEXT,
    rejected_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, from_role, to_role)
);
CREATE INDEX idx_pag_party ON party_approval_gates(party_id);
CREATE INDEX idx_pag_token ON party_approval_gates(token) WHERE token IS NOT NULL;
```

### Executor Logic

Modified DAG advancement (pseudocode):

```text
on role_completed(role_name):
  for dependent_role in get_dependents(role_name):
    gate = get_gate(party_id, role_name, dependent_role)
    if gate and gate.status == "pending":
      activate_gate(gate)  # -> waiting, generate token, notify
      continue             # do NOT spawn dependent_role
    if gate and gate.status in ("waiting", "rejected"):
      continue             # still blocked
    # gate is None or approved
    if all_dependencies_satisfied(dependent_role):
      spawn_role(dependent_role)

on gate_approved(gate):
  re-check if to_role can now be spawned

on gate_rejected(gate):
  mark to_role and transitive dependents as blocked
  evaluate party viability — if no path to completion, mark party failed
```

### MCP Tools

| Tool | Purpose | Who |
|------|---------|-----|
| `approve_party_gate(token)` | Approve a waiting gate | Anyone with token |
| `reject_party_gate(token)` | Reject a waiting gate | Anyone with token |
| `add_party_gate(party_id, from_role, to_role, message)` | Inject gate at runtime | Leader |
| `list_party_gates(party_id, status?)` | List gates | Leader |

CLI: `gobby parties approve <token>`, `gobby parties reject <token>`

### Edge Cases

- **Dynamic gate after from_role completed but before to_role spawned**: Gate activates immediately (`waiting`). If to_role already spawned: creation rejected with error.
- **Gate rejection cascade**: Rejected gate blocks `to_role` and all transitive dependents. Executor checks if any path to all terminal roles still exists; if not, party fails.
- **Auto-approve with crashed leader**: Falls back to manual approval. `activate_gate` checks if auto-approve role's session is alive before sending P2P.
- **Concurrent approval**: SQLite transaction isolation — first `UPDATE` wins, second call finds gate already approved and raises.

---

## Feature 4: Dynamic Leader Recovery

### Concept

V1 has static `is_leader: true` on one role. If the leader crashes and restart recovery is exhausted, the party is stuck. V2 solves this by **spawning a fresh leader instance** with a recovery workflow that bootstraps from DB state — not by promoting an existing member (which would awkwardly dual-purpose a QA or developer agent into a role they weren't designed for).

### Recovery Strategy: Re-Spawn + Optional Fallback

Default behavior: re-spawn the **same leader agent definition** with a **recovery workflow** instead of the normal workflow. The recovery workflow reads party state from the DB and resumes DAG management.

Optional: a `fallback_agents` list of alternative agent definition names. Used when the original agent def's provider/model is unavailable (e.g., API outage). The executor tries each in order.

```yaml
roles:
  leader:
    agent: coordinator-def
    workflow: planning
    is_leader: true
    on_crash: restart
    retry_attempts: 2

leader_recovery:
  recovery_workflow: leader-recovery    # workflow for the re-spawned leader
  fallback_agents: [backup-coord-def]   # optional: alternative agent defs to try
  max_recoveries: 3                     # max leader re-spawns (across all agent defs)
```

If `leader_recovery` is not specified, the party enters `paused` status and waits for human intervention after restart retries are exhausted.

### How the New Leader Gets Context

Key principle: **leader workflows should be stateless/recoverable — key state lives in DB, not workflow variables.**

The executor builds a context snapshot from:
- `parties` table: status, definition_snapshot, inputs
- `party_members` table: all member statuses, sessions, crash counts, tasks
- `party_approval_gates` table: pending/waiting gates
- Computed DAG state: which roles are completed/running/pending/blocked

This snapshot is passed as `step_variables` to the recovery workflow. The new leader's `on_enter` action calls `get_party_status(party_id)` for full live details.

### How Members Learn About the New Leader

1. **DB update**: `parties.leader_session_id` updated to new leader's session
2. **P2P broadcast**: `leader_changed` message to all active members
3. **Tool resolution**: `gobby-party` tools check `leader_session_id` at call time — new leader's calls pass automatically

### Recovery Trigger Flow

```text
leader crashes
  -> on_crash: restart attempted up to retry_attempts times (v1 recovery)
  -> all retries exhausted
  -> _handle_leader_recovery() called
  -> spawn new leader instance (same agent def, recovery workflow)
  -> if spawn fails: try each fallback_agents entry
  -> if all fail: party enters paused status
  -> on success: update DB, broadcast leader_changed, log recovery event
```

Also triggered by: `agent_timeout` event, periodic health check detecting dead leader session, or manual `trigger_leader_recovery` MCP tool.

### Schema (part of migration 104)

```sql
ALTER TABLE parties ADD COLUMN leader_recovery_json TEXT;  -- serialized LeaderRecoveryConfig
ALTER TABLE parties ADD COLUMN leader_role TEXT;            -- current leader's role name
ALTER TABLE parties ADD COLUMN recovery_count INTEGER DEFAULT 0;

CREATE TABLE party_leader_recoveries (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    previous_session_id TEXT,
    previous_agent_def TEXT,
    new_session_id TEXT NOT NULL,
    new_agent_def TEXT NOT NULL,
    recovery_workflow TEXT,
    reason TEXT NOT NULL,  -- crash_recovery_exhausted|timeout|manual
    context_snapshot_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_plr_party ON party_leader_recoveries(party_id);
```

### Model Changes

```python
class LeaderRecoveryConfig(BaseModel):
    recovery_workflow: str | None = None       # workflow for re-spawned leader
    fallback_agents: list[str] = Field(default_factory=list)  # alternative agent defs
    max_recoveries: int = 3                    # lifetime cap on leader re-spawns

class PartyDefinition(BaseModel):
    # ... existing fields ...
    leader_recovery: LeaderRecoveryConfig | None = None
```

### Executor Logic

```python
async def _handle_leader_recovery(self, party_id: str, failed_member: PartyMember) -> None:
    party = self.party_manager.get_party(party_id)
    config = self._get_leader_recovery_config(party)

    if not config:
        self.party_manager.update_party_status(party_id, "paused")
        await self._notify_user(f"Leader crashed with no recovery config. Party {party_id} paused.")
        return

    if party.recovery_count >= config.max_recoveries:
        self.party_manager.update_party_status(party_id, "paused")
        await self._notify_user(f"Leader recovery limit reached. Party {party_id} paused.")
        return

    # Build context snapshot from DB
    context = await self._build_leader_context(party_id)

    # Try spawning: original agent def first, then fallbacks
    agent_defs_to_try = [failed_member.agent_def]
    if config.fallback_agents:
        agent_defs_to_try.extend(config.fallback_agents)

    for agent_def_name in agent_defs_to_try:
        result = await self._spawn_leader_instance(
            party_id=party_id,
            agent_def=agent_def_name,
            workflow=config.recovery_workflow or failed_member.workflow_name,
            context=context,
        )
        if result.success:
            self.party_manager.update_leader(
                party_id=party_id,
                leader_session_id=result.child_session_id,
                recovery_count_increment=True,
            )
            self.party_manager.create_recovery_record(...)
            await self._broadcast_leader_changed(party_id, result.child_session_id, "recovery")
            return

    # All agent defs failed
    self.party_manager.update_party_status(party_id, "paused")
    await self._notify_user(f"All leader recovery attempts failed. Party {party_id} paused.")
```

### MCP Tools

| Tool | Purpose |
|------|---------|
| `trigger_leader_recovery(party_id, reason?)` | Manually trigger leader re-spawn |
| `get_leader_history(party_id)` | Recovery audit log |

`get_party_status` extended to include: `leader_role`, `leader_session_id`, `recovery_count`.

### Edge Cases

- **New leader also crashes**: Recovery repeats (up to `max_recoveries` total). Each attempt tries the agent def list in order. After max reached, party pauses.
- **Recovery during pending gate**: Gate remains pending, token still valid. New leader reads gates from DB and can approve/reject.
- **Original leader resurrects** (e.g., terminal reconnect): Old leader's session checks `parties.leader_session_id` on reconnect. If mismatched, deactivates its leader workflow (prevents split-brain).
- **No `leader_recovery` configured**: Party pauses after restart retries exhausted. Human intervenes via CLI or `trigger_leader_recovery`.
- **Fallback agent def unavailable**: Spawn failure is caught, next agent def in list tried. If all fail, party pauses.
- **Race condition**: Leader recovery uses optimistic locking — `UPDATE parties SET leader_session_id = ? WHERE id = ? AND leader_session_id = ?` (old value). Zero rows updated = another monitor already handled it.

---

## Consolidated Migration 104

All v2 schema changes in one migration (depends on migration 103 / v1 party tables):

```sql
-- On-demand spawn mode
ALTER TABLE party_members ADD COLUMN spawn_mode TEXT DEFAULT 'all_at_once';
ALTER TABLE party_members ADD COLUMN spawned_by_member_id TEXT REFERENCES party_members(id);
ALTER TABLE party_members ADD COLUMN outputs_json TEXT;
CREATE INDEX idx_pm_spawned_by ON party_members(spawned_by_member_id)
    WHERE spawned_by_member_id IS NOT NULL;

CREATE TABLE party_pending_spawns (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    target_role TEXT NOT NULL,
    triggered_by_member_id TEXT NOT NULL REFERENCES party_members(id),
    outputs_json TEXT,
    priority INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, triggered_by_member_id, target_role)
);
CREATE INDEX idx_pps_party_role ON party_pending_spawns(party_id, target_role);

-- Work queue
CREATE TABLE party_work_queue (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    queue_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    claimed_by_member_id TEXT REFERENCES party_members(id),
    claimed_at TEXT,
    completed_at TEXT,
    result_json TEXT,
    error TEXT,
    created_by_member_id TEXT REFERENCES party_members(id),
    priority INTEGER DEFAULT 0,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pwq_claim ON party_work_queue(party_id, queue_name, status, priority, created_at);
CREATE INDEX idx_pwq_member ON party_work_queue(claimed_by_member_id)
    WHERE claimed_by_member_id IS NOT NULL;

CREATE TABLE party_queue_config (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    queue_name TEXT NOT NULL,
    drain_and_exit INTEGER DEFAULT 0,
    visibility_timeout_seconds INTEGER,
    max_items INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, queue_name)
);

-- Approval gates
CREATE TABLE party_approval_gates (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT,
    token TEXT UNIQUE,
    auto_approve_by TEXT,
    source TEXT NOT NULL DEFAULT 'definition',
    approved_by TEXT,
    approved_at TEXT,
    rejected_by TEXT,
    rejected_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(party_id, from_role, to_role)
);
CREATE INDEX idx_pag_party ON party_approval_gates(party_id);
CREATE INDEX idx_pag_token ON party_approval_gates(token) WHERE token IS NOT NULL;

-- Leader recovery
ALTER TABLE parties ADD COLUMN leader_recovery_json TEXT;
ALTER TABLE parties ADD COLUMN leader_role TEXT;
ALTER TABLE parties ADD COLUMN recovery_count INTEGER DEFAULT 0;

CREATE TABLE party_leader_recoveries (
    id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    previous_session_id TEXT,
    previous_agent_def TEXT,
    new_session_id TEXT NOT NULL,
    new_agent_def TEXT NOT NULL,
    recovery_workflow TEXT,
    reason TEXT NOT NULL,
    context_snapshot_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_plr_party ON party_leader_recoveries(party_id);
```

BASELINE_VERSION bumps to 104 (audit: add all new columns to baseline table defs, add all new tables).

---

## New Files

| File | Purpose |
|------|---------|
| `src/gobby/parties/work_queue.py` | `WorkItem` dataclass, `PartyWorkQueueManager` |
| `src/gobby/parties/approval.py` | `PartyApprovalGate`, `GateStatus`, `PartyGateApprovalRequired` |
| `src/gobby/parties/recovery.py` | `LeaderRecovery` dataclass |

## Modified Files

| File | Changes |
|------|---------|
| `docs/plans/party-time.md` | Rename `is_coordinator` -> `is_leader`, `coordinator_session_id` -> `leader_session_id` throughout. Update "Coordinator Role" section heading to "Leader Role". |
| `src/gobby/parties/definitions.py` | Add `SpawnMode`, `WorkQueueConfig`, `ApprovalGateDefinition`, `LeaderRecoveryConfig`. Rename `is_coordinator` -> `is_leader`. |
| `src/gobby/parties/executor.py` | On-demand trigger logic, pending spawn queue drain, queue initialization, gate activation/approval/rejection, leader crash -> re-spawn recovery flow, health check |
| `src/gobby/storage/parties.py` | CRUD for all new tables. Rename coordinator fields to leader. |
| `src/gobby/storage/migrations.py` | Migration 104 |
| `src/gobby/mcp_proxy/tools/party.py` | Queue tools, gate tools, leader recovery tools, spawn output tools |
| `src/gobby/cli/parties.py` | `gobby parties approve <token>`, `gobby parties reject <token>`, `gobby parties gates <party_id>` |

## Composition Summary

All four features compose with v1 and each other:
- **On-demand + work queue**: A role can use both — agents spawn reactively AND claim from a shared queue
- **On-demand + gates**: A gate on an edge blocks on-demand spawning. When approved, pending spawns drain.
- **Gates + leader recovery**: New leader reads pending gates from DB; tokens remain valid
- **Queue + leader recovery**: New leader can `peek_queue` and `publish_work_item` for visibility and intervention
- **All + recovery**: Recovery strategies (restart/pause/abort) work identically for all spawn modes. Queue-aware recovery releases claimed items on crash. Gate approval is idempotent across leader changes.

## Future (not v2)

- UI party builder/editor
- Sequential spawn mode (`spawn_mode: sequential`)
- Gate timeout with auto-reject
- Cross-party work queues (shared between multiple parties)
- Leader voluntary step-down

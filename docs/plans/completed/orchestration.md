# Gobby Conductor: Unified Orchestration System

## 1. Overview

### 1.1 What is the Conductor?

The Conductor is Gobby's persistent orchestration daemon that monitors tasks, coordinates agents, and tracks resources. Think of it as a persistent daemon that keeps the task system tidy and enables autonomous task processing.

**Key responsibilities:**

- Monitor task backlog for stale/blocked work
- Watch agent health and detect stuck processes
- Track token usage and enforce budgets
- Coordinate worktree agents and review loops
- Alert humans via callme when intervention needed

### 1.2 Two Operational Modes

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        GOBBY ORCHESTRATION LAYER                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   ┌─────────────────────┐    ┌─────────────────────────────────────┐   │
│   │   INTERACTIVE MODE  │    │         AUTONOMOUS MODE             │   │
│   │                     │    │                                     │   │
│   │ - Human triggers    │    │ - ConductorLoop daemon              │   │
│   │   workflows         │    │ - Token budget throttling           │   │
│   │ - Reviews at gates  │    │ - Auto-spawn on ready tasks         │   │
│   │ - /gobby commands   │    │ - Alerts via callme                 │   │
│   └─────────────────────┘    └─────────────────────────────────────┘   │
│                                                                         │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │                    SHARED INFRASTRUCTURE                        │   │
│   │                                                                 │   │
│   │  Inter-Agent Messaging    Task Status Extensions   Token Track  │   │
│   │  - send_to_parent         - review         - Usage API  │   │
│   │  - send_to_child          - wait_for_task          - Budget %   │   │
│   │  - poll_messages          - reopen_task            - Throttle   │   │
│   │  - mark_read              - approve_and_cleanup                 │   │
│   │                                                                 │   │
│   │  WebSocket Broadcasting   Workflow System          Merge System │   │
│   │  - Agent events           - worktree-agent         - Already    │   │
│   │  - Message notifications  - sequential-orch        - complete   │   │
│   │  - Task updates           - parallel-orch          - Live test  │   │
│   └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

**Interactive Mode**: Human developer monitors agent outputs, deliberately triggers workflows, reviews at gates. Best for learning the system, complex decisions, and when you want control.

**Autonomous Mode**: Conductor farms out work based on task backlog with resource throttling. Auto-spawns agents on ready tasks, alerts via callme when stuck. Best for batch processing independent tasks overnight.

### 1.3 Key Capabilities Summary

| Capability | Description |
| :--- | :--- |
| Inter-agent messaging | Parent↔child message passing during execution |
| Blocking wait tools | Synchronous wait for task completion |
| `review` status | Review gates in task flow |
| Token aggregation/pricing | Sum across sessions + cost calculation |
| Conductor daemon loop | Persistent monitoring and autonomous spawning |

---

## 2. Architecture

### 2.1 System Diagram

```text
                    ┌─────────────────────────────────────┐
                    │        Gobby the Conductor          │
                    │       (Persistent LLM Loop)         │
                    └─────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
        ▼                          ▼                          ▼
┌───────────────┐      ┌───────────────────┐      ┌───────────────────┐
│ Task Monitor  │      │  Agent Watcher    │      │  Token Tracker    │
│ • Stale tasks │      │ • Stuck agents    │      │ • Usage per sess  │
│ • Orphans     │      │ • Depth limits    │      │ • Budget alerts   │
│ • Blockers    │      │ • Health checks   │      │ • Cost estimates  │
└───────────────┘      └───────────────────┘      └───────────────────┘
        │                          │                          │
        └──────────────────────────┼──────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │        Alert System         │
                    │ • Log to conductor.log      │
                    │ • Desktop notifications     │
                    │ • callme → USER (you!)      │
                    └─────────────────────────────┘
```

### 2.2 Core Components

| Component | File | Purpose |
| :--- | :--- | :--- |
| **ConductorLoop** | `src/gobby/conductor/loop.py` | Main async loop with configurable interval (default 30s). Calls monitors, aggregates findings. |
| **TaskMonitor** | `src/gobby/conductor/monitors/tasks.py` | Detect stale tasks (in_progress > threshold), find orphaned subtasks, check blocked task chains. |
| **AgentWatcher** | `src/gobby/conductor/monitors/agents.py` | Check RunningAgentRegistry for stuck processes, monitor agent depth limits, detect hung terminal sessions. |
| **TokenTracker** | `src/gobby/conductor/monitors/tokens.py` | Aggregate token usage from session metadata, budget warnings, cost estimation. |
| **AlertDispatcher** | `src/gobby/conductor/alerts.py` | Log to file, desktop notifications, callme integration for urgent alerts. |

### 2.3 Existing Infrastructure (DO NOT Implement)

The following already exist and should be leveraged:

- WebSocket server with broadcasting (`src/gobby/servers/websocket.py`)
- Agent event broadcasting via `RunningAgentRegistry`
- Merge system (gobby-merge) - needs live testing only
- Per-session token capture from JSONL (but no aggregation/pricing)
- Task type commands (`/bug`, `/feat`, etc.) - committed in `9d5d73f`

---

## 3. CLI Commands

### 3.1 `gobby conductor start`

Start the conductor daemon loop.

```bash
gobby conductor start [--interval=30s] [--autonomous]
```

**Options:**

- `--interval`: Monitoring frequency (default: 30s)
- `--autonomous`: Enable auto-spawning of agents on ready tasks

**Example:**

```bash
# Start conductor in interactive mode (monitoring only)
gobby conductor start

# Start in autonomous mode with 60s interval
gobby conductor start --autonomous --interval=60s
```

### 3.2 `gobby conductor stop`

Stop the conductor daemon.

```bash
gobby conductor stop
```

### 3.3 `gobby conductor restart`

Restart the conductor (stop + start with same options).

```bash
gobby conductor restart
```

### 3.4 `gobby conductor status`

Show current state: active agents, pending tasks, token usage.

```bash
gobby conductor status
```

**Example output:**

```text
🎭 Conductor Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Active Agents: 2
  • gemini-abc123 (feature/auth) - 12m running
  • claude-def456 (fix/bug-42) - 3m running

Pending Tasks: 5
  • #2130 (ready) - Add user avatar upload
  • #2128 (blocked by #2127) - Integrate S3 storage
  • #2125 (review) - Auth middleware refactor

Token Usage (7d): $12.45 / $50.00 (24.9%)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 3.5 `gobby conductor chat`

Interactive dashboard showing current tasks, agents/workflows in use, progress, token usage. Without a message argument, enters live dashboard mode.

```bash
gobby conductor chat [message]
```

**Examples:**

```bash
# Ask a question
gobby conductor chat "What should I work on next?"

# Enter interactive dashboard mode
gobby conductor chat
```

**Interactive mode features:**

- Real-time task/agent status updates
- Token usage graph
- Command input for queries

---

## 5. Task Status Flow & Review Gates

### 5.1 Status Flow Diagram

```text
pending → in_progress → review → completed
                 ↑              │
                 └──────────────┘  (reopen if review fails)
```

**Flow explanation:**

1. Task starts as `pending`
2. Agent picks up task → `in_progress`
3. Agent completes work → `review` (awaiting orchestrator review)
4. Orchestrator approves → `completed`
5. Orchestrator finds issues → `reopen` back to `in_progress`, fix, close again

### 5.2 `review` Status

When `close_task` is called by an agent (session.agent_depth > 0), it transitions to `review` instead of `completed`. This creates a review gate where the orchestrator must approve the work.

**Schema change:**

```sql
-- Add review_at timestamp
ALTER TABLE tasks ADD COLUMN review_at TIMESTAMP;
```

**Logic in close_task:**

```python
def close_task(task_id: str, commit_sha: str, force_complete: bool = False):
    session = get_current_session()

    if session.agent_depth > 0 and not force_complete:
        # Agent closes to review
        task.status = "review"
        task.review_at = datetime.utcnow()
    else:
        # Orchestrator closes to completed
        task.status = "completed"
        task.completed_at = datetime.utcnow()
```

### 5.3 Blocking Wait Tools

**`wait_for_task(task_id, timeout_seconds=300, poll_interval=5)`**

Polls task status until it leaves `in_progress`. Returns when task becomes `review` or `completed`.

```python
wait_for_task(task_id: str, timeout_seconds: int = 300) -> TaskStatus
# Returns: task data with status, commit_sha, worktree_path
# Timeout: returns with current state + timed_out=True
```

**`wait_for_any_task(task_ids, timeout_seconds=300)`**

Polls multiple tasks, returns when ANY task leaves `in_progress`.

```python
wait_for_any_task(task_ids: List[str], timeout_seconds: int) -> (task_id, status)
# Returns: first completed task data + remaining task_ids still in progress
```

**`wait_for_all_tasks(task_ids, timeout_seconds=600)`**

Polls multiple tasks, returns when ALL tasks leave `in_progress` (or timeout).

```python
wait_for_all_tasks(task_ids: List[str], timeout_seconds: int) -> Dict[str, status]
# Returns: dict of task_id -> task data with their statuses
```

### 5.4 Task Reopen Capability

**`reopen_task(task_id, reason=None)`**

Transitions from `review` → `in_progress`. Used when orchestrator finds issues during review.

```python
reopen_task(task_id: str, reason: str = None) -> Task
# - Transitions status back to in_progress
# - Clears commit_sha (new work incoming)
# - Logs reopen reason for debugging
# - Allows orchestrator to fix and re-close
```

---

## 6. Worktree Agent Restrictions

### 6.1 Design Principle (Agent Sandboxing)

**Critical design principle**: Agents spawned in worktrees are sandboxed to ONE task. They cannot navigate the task tree, spawn agents, or manage worktrees. This prevents runaway agent chains and keeps orchestration authority with the parent.

### 6.2 ALLOWED Tools (Complete List)

```text
# Task tools (minimal set)
gobby-tasks.get_task          # See assigned task details
gobby-tasks.update_task       # Set status to in_progress
gobby-tasks.close_task        # Signal completion with commit_sha

# Memory (optional)
gobby-memory.remember         # Store learnings
gobby-memory.recall           # Retrieve context
gobby-memory.forget           # Remove memories

# All upstream MCP tools (context7, etc.)
# All native file/code tools (read, write, edit, bash, glob, grep)
```

### 6.3 BLOCKED Tools (Complete List)

```text
# Task navigation (orchestrator's job)
gobby-tasks.list_tasks
gobby-tasks.list_ready_tasks
gobby-tasks.suggest_next_task
gobby-tasks.create_task
gobby-tasks.expand_task
gobby-tasks.validate_task_tree

# Agent/worktree management (orchestrator's job)
gobby-agents.*                # No spawning subagents
gobby-worktrees.*             # No managing worktrees
gobby-workflows.set_*         # No changing workflows

# Wait tools (orchestrator only)
gobby-tasks.wait_for_task
gobby-tasks.wait_for_any_task
gobby-tasks.wait_for_all_tasks
```

### 6.4 Worktree Agent Workflow YAML

```yaml
# src/gobby/workflows/definitions/worktree-agent.yaml
name: worktree-agent
description: Restricted workflow for agents working in worktrees
auto_activate_on: worktree_spawn

tool_allowlist:
  gobby-tasks:
    - get_task
    - update_task
    - close_task
  gobby-memory:
    - remember
    - recall
    - forget
  # All other gobby-* servers blocked by default

# Allow all upstream MCP servers
upstream_servers: allow_all

steps:
  - name: work
    description: Execute the assigned task
    # No step transitions - single continuous work phase
```

**Auto-activation**: When `spawn_agent_in_worktree` is called, the worktree-agent workflow auto-activates on the spawned session. The workflow tool filtering enforces the restrictions.

---

## 7. Inter-Agent Messaging

### 7.1 Architecture Diagram

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Inter-Session Messages                        │
├─────────────────────────────────────────────────────────────────┤
│ id | from_session | to_session | content | sent_at | read_at    │
└─────────────────────────────────────────────────────────────────┘
```

**Storage**: PostgreSQL table `inter_session_messages` with:

- `id` (UUID)
- `from_session` (session_id of sender)
- `to_session` (session_id of recipient)
- `content` (message text)
- `priority` (normal, urgent)
- `sent_at` (timestamp)
- `read_at` (timestamp, nullable)

### 7.2 Message Tools

**`send_to_parent(message, priority="normal")`**

Child agent sends message to parent session.

```python
send_to_parent(message: str, priority: str = "normal") -> dict
# Returns: {"message_id": "...", "sent_at": "..."}
```

**`send_to_child(run_id, message)`**

Parent sends message to child agent session.

```python
send_to_child(run_id: str, message: str) -> dict
# Returns: {"message_id": "...", "sent_at": "..."}
```

**`poll_messages(unread_only=True)`**

Get pending messages for current session.

```python
poll_messages(unread_only: bool = True) -> list[dict]
# Returns: [{"id": "...", "from": "...", "content": "...", "sent_at": "...", "priority": "..."}]
```

**`mark_read(message_id)`**

Mark message as read.

```python
mark_read(message_id: str) -> dict
# Returns: {"success": true, "read_at": "..."}
```

### 7.3 Integration with Review Loop (Sequence Diagram)

```text
┌────────────┐                              ┌────────────┐
│   Claude   │                              │   Gemini   │
│ (Parent)   │                              │  (Child)   │
└─────┬──────┘                              └─────┬──────┘
      │                                           │
      │ spawn_agent_in_worktree(task_id)          │
      ├──────────────────────────────────────────►│
      │                                           │
      │                           [does work]     │
      │                                           │
      │◄────────── close_task(review) ────┤
      │                                           │
      │ [reviews code]                            │
      │                                           │
      │ send_to_child("Fix the error handling") ──┤►
      │                                           │
      │                           [polls msgs]    │
      │                           [fixes code]    │
      │                                           │
      │◄────────── close_task(review) ────┤
      │                                           │
      │ approve_and_cleanup()                     │
      │                                           │
```

**WebSocket Integration**: Messages broadcast `message_sent` events on WebSocket for real-time notification. Polling fallback for CLI agents that don't maintain WS connection.

---

## 8. Orchestration Workflows

### 8.1 Sequential Orchestrator Pattern

**When to use**: Dependent tasks, limited resources, simpler review, learning the system.

**Step-by-step workflow**:

1. Set `session_task` to epic ID, activate `sequential-orchestrator` workflow
2. Loop:
   a. `suggest_next_task()` → get ready subtask
   b. `create_worktree(branch=f"feature/{task_id}")`
   c. `spawn_agent_in_worktree(task_id, provider="gemini")`
   d. `wait_for_task(task_id, timeout=600)`
   e. Review code at worktree_path
   f. If good: `merge_worktree()` → `approve_and_cleanup()`
   g. If bad: `reopen_task()`, fix in worktree, `close_task()`
3. Repeat until no ready tasks

**YAML Definition**:

```yaml
# src/gobby/workflows/definitions/sequential-orchestrator.yaml
name: sequential-orchestrator
description: Process epic subtasks one at a time with worktree agents

steps:
  - name: select_task
    allowed_tools: [list_ready_tasks, suggest_next_task, get_task]

  - name: spawn_agent
    allowed_tools: [create_worktree, spawn_agent_in_worktree]

  - name: wait
    allowed_tools: [wait_for_task]

  - name: review
    allowed_tools: [read, glob, grep, get_worktree_status]

  - name: decide
    allowed_tools: [merge_worktree, approve_and_cleanup, reopen_task, close_task]

  - name: loop
    transitions:
      - condition: "has_ready_tasks"
        next: select_task
      - condition: "no_ready_tasks"
        next: complete
```

### 8.2 Parallel Orchestrator Pattern (Clone-Based)

> [!NOTE]
> This pattern uses **Git Clones** (see [Section 16](#16-clone-based-parallel-agents)) instead of Worktrees to ensure thread safety during parallel execution.

**When to use**: Independent tasks, faster throughput, available resources, overnight batch processing.

**Workflow with max_parallel**:

1. Set `session_task` to epic ID, activate `parallel-orchestrator` workflow
2. Spawn phase:
   a. `list_ready_tasks()` → get up to N independent tasks
   b. For each: `create_clone()` + `spawn_agent_in_clone()`
   c. Track: {task_id: clone_id} mapping
3. Wait phase:
   a. `wait_for_any_task(task_ids)` → returns first completed
   b. `sync_clone(pull)` → ensure latest changes
   c. Review completed task's clone
   d. Merge/approve or reopen/fix
   e. If agents still running: goto 3a
4. Refill phase:
   a. If ready tasks remain and slots available: spawn more
   b. Goto wait phase
5. Complete when all tasks done

**YAML Definition**:

```yaml
# src/gobby/workflows/definitions/parallel-orchestrator.yaml
name: parallel-orchestrator
description: Process multiple subtasks in parallel using isolated clones

config:
  max_parallel_agents: 3
  isolation_mode: clone          # "clone" (default) or "worktree"
  auto_sync_interval: 300        # Sync clones every 5 min (optional)
  branch_retention_days: 7       # Keep remote branches after merge

steps:
  - name: select_batch
    allowed_tools: [list_ready_tasks, get_task]

  - name: spawn_batch
    allowed_tools:
      # Clone-based (default)
      - create_clone
      - spawn_agent_in_clone
      # Worktree fallback (when isolation_mode: worktree)
      - create_worktree
      - spawn_agent_in_worktree

  - name: wait_any
    allowed_tools: [wait_for_any_task, wait_for_all_tasks]

  - name: sync_and_review
    allowed_tools:
      - sync_clone               # Pull latest before review
      - read
      - glob
      - grep
      - get_clone
      - get_worktree             # Fallback

  - name: process_completed
    allowed_tools:
      - merge_clone    # Clone merge
      - merge_worktree           # Worktree fallback
      - approve_and_cleanup
      - reopen_task
      - delete_clone
      - delete_worktree

  - name: loop
    transitions:
      - condition: "agents_still_running"
        next: wait_any
      - condition: "has_ready_tasks"
        next: select_batch
      - condition: "all_done"
        next: complete
```

### 8.3 Target Workflow Diagram

```text
┌─────────────────────────────────────────────────────────────────┐
│  Claude (Orchestrator) - auto-task mode with session_task=epic │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
    ┌─────────────────┐
    │ 1. Create       │
    │    worktree     │
    └────────┬────────┘
             │
         ▼
    ┌─────────────────┐
    │ 2. Spawn Agent  │
    │    on task      │
    └────────┬────────┘
             │
         ▼
    ┌─────────────────┐
    │ 3. Wait for     │  ← BLOCKING WAIT (wait_for_task)
    │    completion   │
    └────────┬────────┘
             │
         ▼
    ┌─────────────────┐
    │ 4. Review code  │  ← Read files from worktree path
    │    in worktree  │
    └────────┬────────┘
             │
         ▼
    ┌──────────────────────────────────────┐
    │ 5. Quality check                     │
    │    ├─ GOOD → merge to dev, close     │
    │    └─ BAD → reopen, fix, close       │
    └────────┬─────────────────────────────┘
             │
         ▼
    ┌─────────────────┐
    │ 6. Delete       │
    │    worktree     │
    └────────┬────────┘
             │
         ▼
    ┌─────────────────┐
    │ 7. Next task    │  ← Loop until epic exhausted
    │    in epic      │
    └─────────────────┘
```

---

## 9. Token Budget & Throttling

### 9.1 Token Tracking

We already capture per-session tokens in the `sessions` table via `SessionLifecycleManager`. Need to add:

1. Model tracking (for pricing)
2. Aggregation queries
3. Pricing calculation
4. Budget enforcement

**Schema change**:

```sql
ALTER TABLE sessions ADD COLUMN model TEXT;  -- e.g., "claude-opus-4-5-20251101"
```

### 9.2 Pricing Data

```python
# src/gobby/conductor/pricing.py
ANTHROPIC_PRICING = {
    # Per 1M tokens (from Anthropic pricing page)
    "claude-opus-4-5-20251101": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_read": 1.50
    },
    "claude-sonnet-4-20250514": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30
    },
    "claude-3-5-haiku-20241022": {
        "input": 0.80,
        "output": 4.00,
        "cache_write": 1.00,
        "cache_read": 0.08
    },
}

GOOGLE_PRICING = {
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
}
```

### 9.3 Budget Configuration

```yaml
# ~/.gobby/config.yaml
conductor:
  token_budget:
    weekly_limit: null       # null = no limit, or dollar amount (e.g., 50.00)
    warning_threshold: 0.8   # alert at 80%
    throttle_threshold: 0.9  # pause spawning at 90%
    tracking_window_days: 7
```

### 9.4 Throttling Logic

```python
# src/gobby/conductor/token_tracker.py
class TokenTracker:
    def get_usage_summary(self, days: int = 7) -> UsageSummary:
        """Aggregate token usage across sessions in time window."""
        query = """
        SELECT
            model,
            SUM(usage_input_tokens) as input_tokens,
            SUM(usage_output_tokens) as output_tokens,
            SUM(usage_cache_creation_tokens) as cache_write,
            SUM(usage_cache_read_tokens) as cache_read
        FROM sessions
        WHERE created_at > datetime('now', ?)
        GROUP BY model
        """
        # Calculate costs using pricing data

    def get_budget_status(self) -> BudgetStatus:
        """Get current budget utilization."""
        usage = self.get_usage_summary()
        limit = self.config.token_budget.weekly_limit

        if limit is None:
            return BudgetStatus(used=usage.total_cost_usd, limit=None, percentage=0.0, can_spawn=True)

        percentage = usage.total_cost_usd / limit
        can_spawn = percentage < self.config.token_budget.throttle_threshold

        return BudgetStatus(used=usage.total_cost_usd, limit=limit, percentage=percentage, can_spawn=can_spawn)

    def can_spawn_agent(self) -> bool:
        """Check if we're under throttle threshold."""
        return self.get_budget_status().can_spawn
```

### 9.5 API & MCP Tools

```python
@dataclass
class UsageSummary:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    total_cost_usd: float
    by_model: dict[str, ModelUsage]
    window_days: int

# MCP Tool (gobby-metrics server)
def get_usage_report(days: int = 7) -> UsageSummary:
    """Get token usage report for the specified time window."""
    return token_tracker.get_usage_summary(days)

def get_budget_status() -> BudgetStatus:
    """Get current budget utilization status."""
    return token_tracker.get_budget_status()
```

---

## 10. Alert System & callme Integration

### 10.1 What is callme?

[callme](https://github.com/ZeframLou/call-me) is a Claude Code plugin that enables Claude to initiate **phone calls** to notify you about task completion, blockers, or when decisions are needed. It supports multi-turn voice conversations during task execution.

**Key capability:** "Start a task, walk away. Your phone/watch rings when Claude is done, stuck, or needs a decision."

### 10.2 Alert Priorities

| Priority | Behavior |
| :--- | :--- |
| `info` | Log only, no notification |
| `normal` | Log + optional terminal bell |
| `urgent` | Log + WebSocket broadcast (for dashboards) |
| `critical` | **Phone call via callme** (if configured) |

### 10.3 callme Installation

```bash
# Install the callme plugin
/plugin marketplace add ZeframLou/call-me
/plugin install callme@callme
# Restart Claude Code after installation
```

### 10.4 callme Configuration

**Required accounts:**

- **Phone Provider**: Telnyx (recommended, ~$0.007/min) or Twilio (~$0.014/min)
- **OpenAI API Key**: For speech-to-text and text-to-speech (~$0.03/min)
- **ngrok Account**: Free tier for webhook tunneling

**Environment variables** (store in `~/.claude/settings.json`):

| Variable | Purpose |
| :--- | :--- |
| `CALLME_PHONE_PROVIDER` | `"telnyx"` or `"twilio"` |
| `CALLME_PHONE_ACCOUNT_SID` | Provider account identifier |
| `CALLME_PHONE_AUTH_TOKEN` | Provider authentication credential |
| `CALLME_PHONE_NUMBER` | Claude's outbound phone number (E.164 format) |
| `CALLME_USER_PHONE_NUMBER` | Your receiving phone number |
| `CALLME_OPENAI_API_KEY` | OpenAI credentials for voice processing |
| `CALLME_NGROK_AUTHTOKEN` | ngrok tunnel authentication |

**Optional variables:**

- `CALLME_TTS_VOICE`: Voice selection (default: `"onyx"`)
- `CALLME_PORT`: Server port (default: `3333`)
- `CALLME_TRANSCRIPT_TIMEOUT_MS`: Speech timeout (default: `180000`ms)

### 10.5 callme Tools

The conductor uses these MCP tools from the callme plugin:

```python
# Start a phone call with initial message
initiate_call(message: str) -> {call_id: str, response: str}

# Continue conversation on active call
continue_call(call_id: str, message: str) -> str

# One-way message (Claude proceeds without waiting)
speak_to_user(message: str) -> None

# End the call gracefully
end_call(call_id: str) -> None
```

### 10.6 Integration Flow Diagram

```text
Conductor Loop
    │
    ├─► Detects stuck agent (15 min threshold)
    │
    ├─► Priority = critical? Yes →
    │                              │
    │                              ▼
    │                    ┌─────────────────────┐
    │                    │  initiate_call()    │
    │                    │  "Hey! Your Gemini  │
    │                    │  agent seems stuck. │
    │                    │  Want me to check?" │
    │                    └─────────┬───────────┘
    │                              │
    │                              ▼
    │                    ┌─────────────────────┐
    │                    │  User responds      │
    │                    │  via phone          │
    │                    └─────────┬───────────┘
    │                              │
    │                              ▼
    │                    ┌─────────────────────┐
    │                    │  continue_call()    │
    │                    │  with follow-up     │
    │                    └─────────────────────┘
    │
    └─► Priority < critical → Log + WebSocket broadcast
```

### 10.7 Use Cases

```text
┌─────────────────────────────────────────────────────────────────┐
│  Conductor detects problem → CALLS YOU (phone) via callme        │
├─────────────────────────────────────────────────────────────────┤
│  • Agent stuck for 15+ min → "Hey! Your Gemini agent seems       │
│    stuck on task forty-seven. Should I restart it or wait?"      │
│                                                                  │
│  • Task blocked → "Task forty-seven needs a decision. The auth   │
│    system could use OAuth or JWT. Which do you prefer?"          │
│                                                                  │
│  • Token budget critical → "You're at ninety percent of your     │
│    weekly budget. Should I pause spawning new agents?"           │
│                                                                  │
│  • Epic complete → "Great news! All five subtasks in the auth    │
│    epic are done and merged. Ready for you to review."           │
└─────────────────────────────────────────────────────────────────┘
```

### 10.8 Cost Estimates

| Service | Cost |
| :--- | :--- |
| Telnyx outbound | ~$0.007/min |
| Twilio outbound | ~$0.014/min |
| Monthly phone number | ~$1.00-1.15 |
| OpenAI transcription | ~$0.006/min |
| OpenAI synthesis | ~$0.02/min |
| **Total per call** | **~$0.03-0.04/min** |

A typical alert call (30 seconds) costs approximately $0.02.

---

## 11. Documentation Updates

### 11.1 GEMINI.md (Worktree Agent Mode)

Add this section to GEMINI.md:

```markdown
## Worktree Agent Mode

When spawned in a worktree by an orchestrator (Claude):

**Your scope is LIMITED to one task.** You cannot create tasks, expand epics,
spawn agents, or manage worktrees. Focus on the assigned task only.

### Available Tools
- `get_task(task_id)` - View your assigned task details
- `update_task(task_id, status="in_progress")` - Mark task active
- `close_task(task_id, commit_sha)` - Signal completion
- All file/code tools and upstream MCP servers

### Workflow
1. `get_task(<your-task-id>)` - understand the requirement
2. `update_task(status="in_progress")` - BEFORE any edits
3. Do the work, commit with `[task-id]` prefix
4. `close_task(commit_sha="<sha>")` - signals orchestrator you're done
5. Session ends, orchestrator reviews your work

### You CANNOT
- Create, expand, or navigate tasks
- Spawn subagents or manage worktrees
- Pick your next task (orchestrator decides)
```

### 11.2 CLAUDE.md (Orchestrator Patterns)

Add this section to CLAUDE.md:

```markdown
## Autonomous Task Orchestration

### Sequential Pattern (One at a time)

Best for: dependent tasks, limited resources, simpler review

1. Set `session_task` to epic ID, activate `sequential-orchestrator` workflow
2. Loop:
   a. `suggest_next_task()` → get ready subtask
   b. `create_worktree(branch=f"feature/{task_id}")`
   c. `spawn_agent_in_worktree(task_id, provider="gemini")`
   d. `wait_for_task(task_id, timeout=600)`
   e. Review code at worktree_path
   f. If good: `merge_worktree()` → `approve_and_cleanup()`
   g. If bad: `reopen_task()`, fix in worktree, `close_task()`
3. Repeat until no ready tasks

### Parallel Pattern (Multiple simultaneous)

Best for: independent tasks, faster throughput, available resources

1. Set `session_task` to epic ID, activate `parallel-orchestrator` workflow
2. Spawn phase:
   a. `list_ready_tasks()` → get up to N independent tasks
   b. For each: `create_worktree()` + `spawn_agent_in_worktree()`
   c. Track: {task_id: worktree_id} mapping
3. Wait phase:
   a. `wait_for_any_task(task_ids)` → returns first completed
   b. Review completed task's worktree
   c. Merge/approve or reopen/fix
   d. If agents still running: goto 3a
4. Refill phase:
   a. If ready tasks remain and slots available: spawn more
   b. Goto wait phase
5. Complete when all tasks done
```

### 11.3 gobby-merge Skill

Add skill file `src/gobby/install/claude/skills/gobby-merge/SKILL.md`:

```markdown
---
description: This skill should be used when the user asks to "/gobby-merge",
"merge worktree", "resolve conflicts", "merge to dev". Manage AI-powered
merge conflict resolution - start merges, resolve conflicts, apply resolutions.
version: "1.0"
---

# /gobby-merge - Merge Conflict Resolution Skill

## Core Subcommands

### `/gobby-merge start <worktree-id>` - Start merge operation
Call `gobby-merge.merge_start` with:

- `worktree_id`: (required) Worktree to merge
- `source_branch`: Branch being merged (auto-detected from worktree)
- `target_branch`: Target branch (default: "dev")
- `strategy`: "auto", "conflict_only", "full_file", "manual"

### `/gobby-merge status <resolution-id>` - Get merge status
Call `gobby-merge.merge_status` with:

- `resolution_id`: (required) Resolution ID from merge_start

### `/gobby-merge resolve <conflict-id>` - Resolve conflict
Call `gobby-merge.merge_resolve` with:

- `conflict_id`: (required) Conflict to resolve
- `resolved_content`: Manual resolution (skips AI)
- `use_ai`: Use AI resolution (default: true)

### `/gobby-merge apply <resolution-id>` - Apply and complete
Call `gobby-merge.merge_apply` with:

- `resolution_id`: (required) Resolution to apply

### `/gobby-merge abort <resolution-id>` - Abort merge
Call `gobby-merge.merge_abort` with:

- `resolution_id`: (required) Resolution to abort

## Resolution Tiers
1. **git_auto** - Git handles it (no conflicts)
2. **conflict_only_ai** - Send only conflict hunks to LLM
3. **full_file_ai** - Send full file for complex conflicts
4. **human_review** - Escalate to human

## Workflow Example
1. `merge_start(worktree_id)` - Start merge, get resolution_id
2. If conflicts: `merge_status(resolution_id)` - See conflicts
3. For each conflict: `merge_resolve(conflict_id)` - AI or manual
4. `merge_apply(resolution_id)` - Complete merge
```

---

## 12. Implementation Phases

### Phase Dependencies

```text
A: Messaging ──► B: Status ──► C: Workflows ──┐
                                              ├──► E: Conductor ──► F: Testing
D: Token Budget (independent) ────────────────┘
```

- **Phase A** (Inter-Agent Messaging) must complete before **Phase B** (Task Status)
- **Phase B** must complete before **Phase C** (Workflows)
- **Phase D** (Token Budget) is independent and can run in parallel with A-C
- **Phase E** (Conductor Daemon) requires both **C** and **D** to complete
- **Phase F** (Testing) runs last after all implementation phases

### Phase A: Inter-Agent Messaging Foundation

**Goal**: Enable parent↔child message passing during agent execution

**Files to create/modify**:

- `src/gobby/storage/inter_session_messages.py` - Message storage with `from_session`, `to_session`, `content`, `sent_at`, `read_at`
- `src/gobby/storage/migrations.py` - Add `inter_session_messages` table
- `src/gobby/mcp_proxy/tools/agents.py` - Add messaging tools

**New MCP Tools**:

```python
send_to_parent(message: str, priority: str = "normal") -> message_id
send_to_child(run_id: str, message: str) -> message_id
poll_messages(unread_only: bool = True) -> List[Message]
mark_read(message_id: str) -> bool
```

**WebSocket Integration**:

- Broadcast `message_sent` events on WebSocket for real-time notification
- Polling fallback for CLI agents that don't maintain WS connection

**Live Test Deliverable**: Terminal split demo

- Left pane: Claude (parent) session
- Right pane: Gemini (child) agent spawned in worktree
- Demo flow:
  1. Claude spawns Gemini via `spawn_agent_in_worktree`
  2. Claude sends message: `send_to_child(run_id, "Please confirm you received this")`
  3. Gemini polls: `poll_messages()` → sees message
  4. Gemini responds: `send_to_parent("Message received, starting work")`
  5. Claude polls: `poll_messages()` → sees response

### Phase B: Task Status Extensions

**Goal**: Support review workflow with `review` status

**Files to modify**:

- `src/gobby/storage/tasks.py` - Add `review` status + `review_at` timestamp
- `src/gobby/mcp_proxy/tools/tasks.py` - Modify `close_task` behavior based on agent context
- `src/gobby/mcp_proxy/tools/task_sync.py` - Handle `review` in JSONL

**Logic Change**:

- When `close_task` called by agent (session.agent_depth > 0) → transitions to `review`
- When called by orchestrator → transitions to `completed`

**New MCP Tools**:

```python
wait_for_task(task_id: str, timeout_seconds: int = 300) -> TaskStatus
wait_for_any_task(task_ids: List[str], timeout_seconds: int) -> (task_id, status)
wait_for_all_tasks(task_ids: List[str], timeout_seconds: int) -> Dict[str, status]
reopen_task(task_id: str, reason: str) -> Task
approve_and_cleanup(task_id: str, worktree_id: str) -> bool
```

### Phase C: Interactive Orchestration Workflows

**Goal**: Enable human-driven sequential and parallel review loops

**Files to create**:

- `src/gobby/workflows/definitions/worktree-agent.yaml` - Tool restrictions for spawned agents (Section 6.4)
- `src/gobby/workflows/definitions/sequential-orchestrator.yaml` - Step-by-step orchestration (Section 8.1)
- `src/gobby/workflows/definitions/parallel-orchestrator.yaml` - Multi-agent orchestration (Section 8.2)

**Changes to spawn_agent_in_worktree**:

- Auto-set `workflow="worktree-agent"` if not specified
- Pass task_id via environment or prompt injection
- Workflow activates on session start hook

### Phase D: Token Budget & Throttling

**Goal**: Enable resource-aware autonomous operation with accurate cost tracking

**Files to create/modify**:

- `src/gobby/storage/sessions.py` - Add `model` column, aggregation queries
- `src/gobby/storage/migrations.py` - Migration for model column
- `src/gobby/sessions/transcripts/claude.py` - Extract model from JSONL
- `src/gobby/conductor/token_tracker.py` - Aggregation + pricing + budget API
- `src/gobby/conductor/pricing.py` - Model pricing data
- `src/gobby/config/app.py` - Budget configuration

### Phase E: Conductor Daemon (Autonomous Mode)

**Goal**: Persistent daemon that monitors and acts on task backlog

**Files to create**:

- `src/gobby/conductor/__init__.py` - Module init
- `src/gobby/conductor/loop.py` - Main ConductorLoop class
- `src/gobby/conductor/monitors/tasks.py` - Stale task detection
- `src/gobby/conductor/monitors/agents.py` - Stuck agent detection
- `src/gobby/conductor/alerts.py` - callme integration
- `src/gobby/cli/conductor.py` - CLI commands

**ConductorLoop Behavior**:

```python
class ConductorLoop:
    """Runs as part of gobby daemon, checks every 30s"""

    async def tick(self):
        # 1. Check token budget
        if not self.token_tracker.can_spawn_agent():
            return  # Throttled

        # 2. Check for stuck agents
        for agent in self.registry.get_stuck_agents(threshold_minutes=15):
            await self.alert("agent_stuck", agent, priority="urgent")

        # 3. Check for ready tasks (no blockers)
        ready_tasks = await self.task_manager.list_ready_tasks()

        # 4. Auto-spawn if autonomous mode enabled
        if self.config.autonomous_mode and ready_tasks:
            await self.spawn_next_agent(ready_tasks[0])

        # 5. Broadcast status
        await self.broadcast_status()
```

### Phase F: Live Integration Testing

**Goal**: Validate the new system end-to-end

**Test File Mapping**:

| Test Scenario | File | Validates |
| :--- | :--- | :--- |
| WebSocket Message Test | `tests/e2e/test_inter_agent_messages.py` | Phase A |
| Sequential Review Loop | `tests/e2e/test_sequential_review_loop.py` | Phases B+C |
| Autonomous Mode | `tests/e2e/test_autonomous_mode.py` | Phase E |
| Token Budget Test | `tests/e2e/test_token_budget.py` | Phase D |
| Merge System Live Test | `tests/e2e/test_worktree_merge_live.py` | Existing merge |

**Test Scenarios**:

1. **WebSocket Message Test** (Phase A validation):
   - Start gobby daemon
   - Spawn agent in terminal (terminal split: left=Claude, right=Gemini)
   - Send message from parent to child via `send_to_child`
   - Verify child receives via `poll_messages`
   - Child responds via `send_to_parent`
   - Verify parent receives

2. **Sequential Review Loop** (Phases B+C validation):
   - Create epic with 2 subtasks
   - Activate `sequential-orchestrator` workflow
   - Watch Claude spawn Gemini on task 1
   - Verify task transitions: `in_progress` → `review`
   - Claude reviews, approves
   - Verify merge and task completion
   - Repeat for task 2

3. **Autonomous Mode** (Phases D+E validation):
   - Seed task backlog with ready tasks
   - Enable autonomous mode in config
   - Start daemon with ConductorLoop
   - Verify auto-spawning respects token budget
   - Trigger alert by making agent "stuck"
   - Verify callme notification

4. **Merge System Live Test** (validate existing gobby-merge - no new code):
   - Create worktree, make conflicting changes
   - Run merge workflow via MCP tools
   - Verify AI conflict resolution works

5. **Token Budget Test** (Phase D validation):
   - Run `get_usage_report(days=7)` via MCP
   - Verify aggregation across recent sessions
   - Verify cost calculation matches expected
   - Test throttling by setting low budget limit

---

## 13. File Summary

### 13.1 New Files to Create

| File | Phase | Purpose |
| :--- | :--- | :--- |
| `src/gobby/storage/inter_session_messages.py` | A | Message storage layer |
| `src/gobby/conductor/__init__.py` | D | Conductor module init |
| `src/gobby/conductor/loop.py` | E | Main ConductorLoop daemon |
| `src/gobby/conductor/token_tracker.py` | D | Token aggregation + pricing |
| `src/gobby/conductor/pricing.py` | D | Model pricing data |
| `src/gobby/conductor/alerts.py` | E | callme integration |
| `src/gobby/conductor/monitors/__init__.py` | E | Monitors module init |
| `src/gobby/conductor/monitors/tasks.py` | E | Stale task detection |
| `src/gobby/conductor/monitors/agents.py` | E | Stuck agent detection |
| `src/gobby/cli/conductor.py` | E | CLI commands |
| `src/gobby/workflows/definitions/worktree-agent.yaml` | C | Agent tool restrictions |
| `src/gobby/workflows/definitions/sequential-orchestrator.yaml` | C | Sequential workflow |
| `src/gobby/workflows/definitions/parallel-orchestrator.yaml` | C | Parallel workflow |
| `src/gobby/install/claude/skills/gobby-merge/SKILL.md` | C | Merge skill |

### 13.2 Files to Modify

| File | Changes |
| :--- | :--- |
| `src/gobby/storage/migrations.py` | Add `inter_session_messages` table, `model` column |
| `src/gobby/storage/sessions.py` | Add model column, aggregation queries |
| `src/gobby/storage/tasks.py` | Add `review` status |
| `src/gobby/sessions/transcripts/claude.py` | Extract model from JSONL |
| `src/gobby/mcp_proxy/tools/agents.py` | Add messaging tools |
| `src/gobby/mcp_proxy/tools/tasks.py` | Add wait/reopen tools |
| `src/gobby/mcp_proxy/tools/task_sync.py` | Handle review in JSONL |
| `src/gobby/mcp_proxy/tools/worktrees.py` | Auto-activate worktree-agent workflow |
| `src/gobby/config/app.py` | Add conductor config section |
| `src/gobby/runner.py` | Start ConductorLoop |
| `src/gobby/cli/__init__.py` | Register conductor commands |
| `GEMINI.md` | Add worktree agent instructions |
| `CLAUDE.md` | Add orchestrator workflow docs |

### 13.3 Test Files

| File | Validates |
| :--- | :--- |
| `tests/e2e/test_inter_agent_messages.py` | Phase A - messaging |
| `tests/e2e/test_sequential_review_loop.py` | Phases B+C |
| `tests/e2e/test_token_budget.py` | Phase D |
| `tests/e2e/test_autonomous_mode.py` | Phase E |
| `tests/e2e/test_worktree_merge_live.py` | Existing merge (validation only) |
| `tests/conductor/test_token_tracker.py` | Token aggregation |
| `tests/conductor/test_loop.py` | ConductorLoop behavior |

---

## 14. Verification & Testing

### 14.1 Unit Test Requirements

- `close_task` → `review` when session.agent_depth > 0
- `wait_for_task` returns on status change or timeout
- `wait_for_any_task` returns when first task completes
- `wait_for_all_tasks` returns when all tasks complete
- `merge_worktree` performs git merge correctly
- `reopen_task` transitions status back
- SESSION_END handler updates agent_runs
- Token aggregation calculates costs correctly
- Budget throttling respects thresholds

### 14.2 Integration Tests

**Sequential Workflow**:

```bash
# 1. Create epic with subtasks
gobby tasks create "Epic" --type=epic
gobby tasks expand <epic-id>

# 2. Activate sequential workflow
gobby workflows set sequential-orchestrator

# 3. Run orchestration loop manually
gobby tasks suggest-next
gobby worktrees create feature/task-1
gobby worktrees spawn feature/task-1 --task=<task-id> --provider=gemini

# 4. Wait for Gemini to complete, then verify
gobby tasks list --status=review

# 5. Merge using gobby-merge
# Via MCP: merge_start(worktree_id, source_branch, target_branch="dev")

# 6. Approve and cleanup
# Via MCP: approve_and_cleanup(task_id, worktree_id)
```

**Parallel Workflow**:

```bash
# 1. Create epic with 3 independent subtasks
gobby tasks create "Parallel Epic" --type=epic
# ... add subtasks

# 2. Spawn multiple agents
gobby worktrees spawn feature/task-1 --task=<task-1> --provider=gemini
gobby worktrees spawn feature/task-2 --task=<task-2> --provider=gemini

# 3. Wait for any to complete
# (via MCP tool: wait_for_any_task)

# 4. Process completed, spawn more if ready tasks remain
```

### 14.3 End-to-End Tests

**Full Sequential Loop**:

1. Create epic with 2 subtasks
2. Claude activates `sequential-orchestrator` workflow
3. Spawns Gemini for subtask 1, waits, reviews, merges
4. Spawns Gemini for subtask 2, waits, reviews, merges
5. Epic marked complete when all subtasks done

**Full Parallel Loop**:

1. Create epic with 4 independent subtasks
2. Claude activates `parallel-orchestrator` workflow
3. Spawns Gemini in 2 worktrees (max_parallel=2)
4. As each completes: review, merge, spawn next
5. All 4 tasks processed with max 2 concurrent agents

**Autonomous Mode**:

1. Seed task backlog
2. Enable autonomous mode
3. Start conductor: `gobby conductor start --autonomous`
4. Observe auto-spawning
5. Trigger stuck agent alert
6. Verify callme notification

---

## 15. Configuration Reference

### 15.1 Complete YAML Example

```yaml
# ~/.gobby/config.yaml
conductor:
  # Core settings
  enabled: true
  interval_seconds: 30
  autonomous_mode: false

  # Monitoring thresholds
  thresholds:
    stale_task_minutes: 60
    stuck_agent_minutes: 15

  # Token budget
  token_budget:
    weekly_limit: 50.00   # null = no limit
    warning_threshold: 0.8
    throttle_threshold: 0.9
    tracking_window_days: 7

  # Alerts (callme is configured separately as a Claude Code plugin)
  alerts:
    critical_threshold_minutes: 15  # When to escalate to phone call
    use_callme: true                # Enable phone calls for critical alerts
```

### 15.2 Environment Variables

| Variable | Purpose | Default |
| :--- | :--- | :--- |
| `GOBBY_CONDUCTOR_ENABLED` | Enable conductor | `false` |
| `GOBBY_CONDUCTOR_AUTONOMOUS` | Enable auto-spawn | `false` |
| `GOBBY_TOKEN_BUDGET` | Weekly limit in USD | `null` |

**callme environment variables** (see Section 10.4 for full list):

| Variable | Purpose |
| :--- | :--- |
| `CALLME_PHONE_PROVIDER` | `"telnyx"` or `"twilio"` |
| `CALLME_USER_PHONE_NUMBER` | Your phone number (E.164 format) |
| `CALLME_OPENAI_API_KEY` | OpenAI for voice processing |

---

## Appendix A: Design Decisions

### Confirmed Decisions

1. **Token tracking**: Use existing session-level data + add aggregation/pricing layer (not ccusage dependency)
2. **E2E test location**: `tests/e2e/` directory
3. **WebSocket live test format**: Terminal split - two panes showing Claude parent and Gemini child exchanging messages
4. **Message delivery guarantee**: Persist to database + WebSocket broadcast for real-time + polling fallback
5. **Merge target**: All worktree branches merge to `dev` (not main). Main branch updated via normal PR flow.
6. **Auto-cleanup**: Worktrees auto-deleted after successful merge

### Open Questions

1. **Autonomous mode activation**: CLI flag (`gobby conductor start --autonomous`) AND config file (conductor.autonomous_mode). CLI takes precedence.
2. **Budget units**: Track by dollar amount for user-friendliness, calculate from token counts internally.

---

## Appendix B: Deliverables Summary

| Phase | Key Output | Validates |
| :--- | :--- | :--- |
| A | Inter-agent messaging MCP tools | WebSocket + message passing |
| B | `review` status + wait tools | Review gates |
| C | Orchestration workflow definitions | Interactive mode |
| D | Token tracking + throttling | Budget awareness |
| E | ConductorLoop daemon | Autonomous mode |
| F | Live integration tests | End-to-end system |

---

## Appendix C: Verification Commands

After implementation:

```bash
# 1. Start daemon with verbose logging
gobby start --verbose

# 2. Start conductor
gobby conductor start

# 3. Check conductor status
gobby conductor status

# 4. Run E2E tests
uv run pytest tests/e2e/ -v

# 5. Test autonomous mode
gobby conductor start --autonomous
# Observe auto-spawning behavior

# 6. Chat with conductor
gobby conductor chat "What's the status?"
```

---

## 16. Clone-Based Parallel Agents

### 16.1 Problem Statement

During E2E testing of parallel agent orchestration, fundamental issues emerged with git worktrees:

#### Problem 1: Git is NOT Thread-Safe

All worktrees share a single `.git` directory, leading to:

- Race conditions during concurrent operations (checkout, commit)
- Lock file contention (`index.lock`, `HEAD.lock`)
- Potential repository corruption with aggressive parallel agents

#### Problem 2: CLI Worktree Detection

Gemini CLI's `findProjectRoot()` searches for a `.git` **directory**. Worktrees have a `.git` **file** pointing back to main repo, causing agents to operate on wrong directory.

**Key Finding**:
> "For truly parallel agent work, separate repository clones are safer than worktrees."

### 16.2 Architecture: Clones vs Worktrees

```text
┌─────────────────────────────────────────────────────────────────┐
│                     PARALLEL ISOLATION MODES                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────────────┐    ┌─────────────────────────────┐   │
│   │   WORKTREES         │    │         CLONES              │   │
│   │   (Sequential)      │    │         (Parallel)          │   │
│   │                     │    │                             │   │
│   │ - Shared .git       │    │ - Isolated .git per clone   │   │
│   │ - Fast setup        │    │ - Thread-safe operations    │   │
│   │ - Lock contention   │    │ - Explicit sync required    │   │
│   │ - One agent at time │    │ - Multiple agents safe      │   │
│   └─────────────────────┘    └─────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 16.3 When to Use Each

| Scenario | Recommendation | Reason |
| :--- | :--- | :--- |
| Sequential orchestrator | Worktree | Fast setup, single agent |
| Parallel orchestrator (2+ agents) | **Clone** | Thread safety |
| Short-lived tasks (<30 min) | Worktree | Minimal overhead |
| Long-running tasks | Either | Based on parallelism |
| CI/CD environments | Clone | Full isolation |
| Overnight batch processing | **Clone** | Multiple agents |

### 16.4 Clone Storage Layout

```text
/tmp/gobby-clones/
  <project-name>/
    <task-id-or-branch>/           # Full shallow clone
      .git/                         # Isolated git directory
      .gobby/project.json           # Copy with parent_project_path
      ... project files ...
```

### 16.5 Database Schema

```sql
-- Add to migrations.py
CREATE TABLE clones (
    id TEXT PRIMARY KEY,                    -- clone-<uuid>
    project_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    clone_path TEXT NOT NULL,
    base_branch TEXT DEFAULT 'main',
    task_id TEXT,                           -- Optional linked task
    agent_session_id TEXT,                  -- Owning session
    status TEXT DEFAULT 'active',           -- active, synced, merged, abandoned
    remote_url TEXT NOT NULL,               -- Origin URL for sync
    last_sync_at TIMESTAMP,
    cleanup_after TIMESTAMP,                -- 7 days after merge
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX idx_clones_project ON clones(project_id);
CREATE INDEX idx_clones_task ON clones(task_id);
CREATE INDEX idx_clones_status ON clones(status);
CREATE INDEX idx_clones_cleanup ON clones(cleanup_after);
```

### 16.6 New MCP Server: gobby-clones

#### create_clone

```python
create_clone(
    branch_name: str,
    base_branch: str = "main",
    task_id: str | None = None,
    depth: int = 1,                 # Shallow clone depth
    project_path: str | None = None
) -> CloneResult
```

**Implementation**:

1. Get remote URL from local repo: `git remote get-url origin`
2. Generate clone path: `/tmp/gobby-clones/<project>/<branch>/`
3. Execute: `git clone --depth=<depth> --branch=<base_branch> <url> <path>`
4. Create new branch: `git checkout -b <branch_name>`
5. Copy `.gobby/project.json` with `parent_project_path` reference
6. Install provider hooks
7. Record in `clones` table

#### spawn_agent_in_clone

```python
spawn_agent_in_clone(
    prompt: str,
    branch_name: str,
    base_branch: str = "main",
    task_id: str | None = None,
    parent_session_id: str | None = None,
    mode: str = "terminal",
    provider: str = "claude",
    model: str | None = None,
    workflow: str | None = None,
    timeout: float = 120.0,
    max_turns: int = 10,
    project_path: str | None = None
) -> SpawnResult
```

**Implementation**:

1. Call `create_clone()` if clone doesn't exist
2. Build enhanced prompt with clone context
3. Use same spawner logic as `spawn_agent_in_worktree()`
4. Claim clone for child session
5. Pre-save workflow state with `session_task`

#### sync_clone

```python
sync_clone(
    clone_id: str,
    direction: Literal["pull", "push"] = "pull"
) -> SyncResult
```

**Implementation**:

- **pull**: `git fetch origin && git rebase origin/<base_branch>`
- **push**: `git push origin <branch_name>`

Unlike worktrees, clones require explicit sync since they don't share refs.

#### merge_clone

```python
merge_clone(
    clone_id: str,
    target_branch: str = "dev",
    strategy: str = "merge"
) -> MergeResult
```

**Implementation**:

1. `sync_clone(clone_id, "push")` - Ensure branch is on remote
2. In main repo: `git fetch origin && git checkout <target_branch>`
3. `git merge origin/<branch_name>` or use gobby-merge for conflicts
4. Set `cleanup_after = now + 7 days`
5. Mark clone as "merged"

#### delete_clone

```python
delete_clone(
    clone_id: str,
    force: bool = False,
    delete_remote_branch: bool = False
) -> DeleteResult
```

**Implementation**:

1. Check for uncommitted changes (unless force)
2. `rm -rf <clone_path>`
3. Optionally: `git push origin --delete <branch_name>`
4. Remove from `clones` table

#### Other Tools

```python
list_clones(project_id, status, limit) -> List[Clone]
get_clone(clone_id) -> Clone
get_clone_by_task(task_id) -> Clone
cleanup_stale_clones(hours=24, dry_run=True) -> CleanupResult
cleanup_merged_clones() -> CleanupResult  # Delete where cleanup_after < now
```

### 16.7 gobby-merge Integration

#### Key Difference: Worktrees vs Clones

| Aspect | Worktree Merge | Clone Merge |
| :--- | :--- | :--- |
| Branch location | Local (shared .git) | Remote (isolated .git) |
| Pre-merge step | None | `git fetch origin <branch>` |
| Source ref | `branch_name` | `origin/branch_name` |
| Conflict resolution | In-place | Push resolution back to clone |
| Post-merge cleanup | Delete worktree | Delete clone + optionally remote branch |

#### Updated merge_start()

```python
def merge_start(
    source: str,                    # worktree_id OR clone_id
    target_branch: str = "dev",
    strategy: str = "auto",         # auto, conflict_only, full_file, manual
    ...
) -> Resolution:
    if source.startswith("clone-"):
        clone = clone_storage.get(source)
        if not clone:
            return Resolution(success=False, error="Clone not found")

        # Step 1: Ensure clone changes are pushed to remote
        sync_result = sync_clone(clone.id, direction="push")
        if not sync_result.success:
            return Resolution(success=False, error=f"Failed to push clone: {sync_result.error}")

        # Step 2: Fetch the branch into main repo
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", clone.branch_name],
            cwd=main_repo_path,
            capture_output=True
        )
        if fetch_result.returncode != 0:
            return Resolution(success=False, error="Failed to fetch branch from remote")

        source_branch = f"origin/{clone.branch_name}"
        source_type = "clone"
    else:
        worktree = worktree_storage.get(source)
        if not worktree:
            return Resolution(success=False, error="Worktree not found")

        source_branch = worktree.branch_name  # Already local
        source_type = "worktree"

    # Continue with existing merge logic...
    return _do_merge(source_branch, target_branch, strategy, source_type)
```

#### Conflict Resolution Flow for Clones

```text
┌─────────────────────────────────────────────────────────────────┐
│                   CLONE MERGE CONFLICT FLOW                     │
└─────────────────────────────────────────────────────────────────┘

1. Agent completes work in clone
   └─► close_task() triggers merge flow

2. sync_clone(clone_id, "push")
   └─► Pushes clone's commits to origin/<branch>

3. merge_start(clone_id, target="dev")
   ├─► git fetch origin <branch>
   ├─► git checkout dev
   └─► git merge origin/<branch>
       │
       ├─► NO CONFLICTS → merge_apply() → Mark clone "merged"
       │
       └─► CONFLICTS DETECTED
           │
           ▼
   ┌─────────────────────────────────────────────────────────┐
   │              CONFLICT RESOLUTION TIERS                  │
   ├─────────────────────────────────────────────────────────┤
   │                                                         │
   │  Tier 1: conflict_only_ai                               │
   │  └─► Send only conflict hunks to LLM                    │
   │      • Input: <<<HEAD ... === ... >>>branch markers     │
   │      • LLM chooses resolution strategy                  │
   │      • Fast, cheap (~100 tokens per conflict)           │
   │                                                         │
   │  Tier 2: full_file_ai (escalation)                      │
   │  └─► Send full file content to LLM                      │
   │      • When hunk-only resolution fails                  │
   │      • Complex semantic conflicts                       │
   │      • More expensive (~1000+ tokens per file)          │
   │                                                         │
   │  Tier 3: human_review (escalation)                      │
   │  └─► Task enters "review" status                        │
   │      • Alert via callme if configured                   │
   │      • Human resolves manually                          │
   │      • Agent cannot proceed until resolved              │
   │                                                         │
   └─────────────────────────────────────────────────────────┘

4. After resolution:
   merge_apply(resolution_id)
   └─► git commit (merge commit)
   └─► Set clone.cleanup_after = now + 7 days
   └─► Mark clone status = "merged"
```

#### merge_resolve() for Clones

```python
def merge_resolve(
    conflict_id: str,
    resolved_content: str | None = None,  # Manual resolution
    use_ai: bool = True,
    tier: str = "conflict_only"           # conflict_only, full_file
) -> ConflictResolution:
    conflict = get_conflict(conflict_id)

    if resolved_content:
        # Manual resolution provided
        return _apply_manual_resolution(conflict, resolved_content)

    if not use_ai:
        # Mark for human review
        return ConflictResolution(
            status="pending_human",
            message="Conflict marked for human review"
        )

    if tier == "conflict_only":
        # Tier 1: Send only conflict markers to LLM
        prompt = f"""
        Resolve this git merge conflict. Return ONLY the resolved code, no explanations.

        File: {conflict.file_path}

        Conflict:
        {conflict.hunk}

        Context: Merging feature branch into dev. Preserve both changes if possible.
        """
        resolved = llm.complete(prompt)

        if _validate_resolution(resolved, conflict):
            return _apply_ai_resolution(conflict, resolved)
        else:
            # Escalate to full file
            return merge_resolve(conflict_id, use_ai=True, tier="full_file")

    elif tier == "full_file":
        # Tier 2: Send full file for complex conflicts
        base_content = _get_base_version(conflict.file_path)
        ours_content = _get_ours_version(conflict.file_path)
        theirs_content = _get_theirs_version(conflict.file_path)

        prompt = f"""
        Merge these three versions of {conflict.file_path}.
        Return the fully merged file content.

        BASE (common ancestor):
        ```
        {base_content}
        ```

        OURS (dev branch):
        ```
        {ours_content}
        ```

        THEIRS (feature branch):
        ```
        {theirs_content}
        ```

        Preserve functionality from both branches. Resolve conflicts intelligently.
        """
        resolved = llm.complete(prompt)

        if _validate_resolution(resolved, conflict):
            return _apply_ai_resolution(conflict, resolved)
        else:
            # Escalate to human
            return ConflictResolution(
                status="pending_human",
                message="AI resolution failed validation, requires human review"
            )
```

#### Handling Clone-Specific Merge Scenarios

##### Scenario 1: Multiple clones modifying same file

```text
Clone A modifies: src/auth.py (lines 10-20)
Clone B modifies: src/auth.py (lines 50-60)
                                  │
                                  ▼
        ┌─────────────────────────────────────────┐
        │  Merge Order Matters for Clones         │
        │                                         │
        │  1. Clone A merges first → succeeds     │
        │  2. Clone B merges → may conflict       │
        │     └─► sync_clone("pull") recommended  │
        │         before final merge              │
        └─────────────────────────────────────────┘
```

**Recommendation**: Before merging Clone B, sync it with latest dev:

```python
# In parallel orchestrator, before merge:
sync_clone(clone_id, direction="pull")  # Rebase on latest dev
sync_clone(clone_id, direction="push")  # Push rebased changes
merge_start(clone_id, target="dev")     # Now merge cleanly
```

##### Scenario 2: Clone branch diverged significantly

When clone's branch is many commits behind dev:

```python
def merge_clone_with_rebase(clone_id: str, target: str = "dev") -> MergeResult:
    """Rebase clone onto target before merge for cleaner history."""
    clone = get_clone(clone_id)

    # 1. Fetch latest target into clone
    run_in_clone(clone, ["git", "fetch", "origin", target])

    # 2. Rebase clone's work onto target
    rebase_result = run_in_clone(clone, ["git", "rebase", f"origin/{target}"])

    if rebase_result.conflicts:
        # Handle rebase conflicts (similar to merge conflicts)
        return MergeResult(status="rebase_conflicts", conflicts=rebase_result.conflicts)

    # 3. Push rebased branch
    sync_clone(clone_id, "push", force=True)  # Force push after rebase

    # 4. Now merge is fast-forward
    return merge_start(clone_id, target)
```

#### Error Handling

```python
class CloneMergeError(Exception):
    """Errors specific to clone merge operations."""
    pass

def merge_clone(clone_id: str, target: str = "dev") -> MergeResult:
    try:
        clone = clone_storage.get(clone_id)
        if not clone:
            raise CloneMergeError(f"Clone {clone_id} not found")

        if clone.status == "merged":
            raise CloneMergeError(f"Clone {clone_id} already merged")

        if clone.status == "abandoned":
            raise CloneMergeError(f"Clone {clone_id} is abandoned")

        # Check for uncommitted changes in clone
        if _has_uncommitted_changes(clone.clone_path):
            raise CloneMergeError(
                "Clone has uncommitted changes. Commit or stash before merge."
            )

        # Proceed with merge...
        return _do_clone_merge(clone, target)

    except subprocess.CalledProcessError as e:
        return MergeResult(
            success=False,
            error=f"Git command failed: {e.stderr.decode()}"
        )
    except CloneMergeError as e:
        return MergeResult(success=False, error=str(e))
```

### 16.8 CLI Commands

```bash
# Clone management
gobby clones create <branch-name> [--base main] [--task <id>]
gobby clones list [--status active|merged|abandoned]
gobby clones spawn <branch-name> "<prompt>" [--provider claude]
gobby clones sync <clone-id> [--direction pull|push]
gobby clones merge <clone-id> [--target dev]
gobby clones delete <clone-id> [--force] [--delete-remote]
gobby clones cleanup [--hours 24] [--dry-run]
gobby clones cleanup-merged                    # Delete clones past retention period
```

### 16.9 Context Injection

```python
def _build_clone_context_prompt(
    original_prompt: str,
    clone_path: str,
    branch_name: str,
    task_id: str | None,
    main_repo_path: str | None = None,
) -> str:
    """Build enhanced prompt with clone context."""
    context_lines = [
        "## CRITICAL: Clone Context",
        "You are working in an ISOLATED git clone, NOT the main repository.",
        "",
        f"**Your workspace:** {clone_path}",
        f"**Your branch:** {branch_name}",
    ]

    if task_id:
        context_lines.append(f"**Your task:** {task_id}")

    context_lines.extend([
        "",
        "**IMPORTANT RULES:**",
        f"1. ALL file operations must be within {clone_path}",
        "2. Your commits are LOCAL until synced - don't worry about conflicts yet",
        "3. Run `pwd` to verify your location before any file operations",
        f"4. Commit to YOUR branch ({branch_name}), not main/dev",
        "5. When your assigned task is complete, STOP - orchestrator handles merge",
        "",
        "---",
        "",
    ])

    return "\n".join(context_lines) + original_prompt
```

### 16.10 Performance Comparison

| Metric | Worktree | Shallow Clone (depth=1) |
| :--- | :--- | :--- |
| Disk space | ~50MB (shared .git) | ~100-200MB |
| Setup time | ~2-5 seconds | ~10-30 seconds |
| Concurrent safety | ❌ Lock contention | ✅ Fully isolated |
| Network required | No | Yes (initial clone) |
| Sync complexity | Implicit | Explicit (pull/push) |

**Recommendation**: Use shallow clones (`depth=1`) to minimize disk/network overhead.

### 16.11 Cleanup Strategy

1. **On task completion**:
   - `merge_clone()` sets `cleanup_after = now + 7 days`
   - Mark clone as "merged"

2. **Periodic cleanup** (ConductorLoop):
   - `cleanup_merged_clones()`: Delete where `cleanup_after < now`
   - `cleanup_stale_clones(hours=24)`: Mark abandoned, optionally delete

3. **Manual cleanup**:
   - `gobby clones cleanup-merged`: Delete merged clones past retention
   - `gobby clones delete <id> --delete-remote`: Full cleanup including remote branch

### 16.12 Authentication & SSH Handling

#### The Problem

Clones require network access to the remote repository. Unlike worktrees (which share the local `.git` directory), clones must authenticate with the remote for:

- Initial `git clone`
- `sync_clone("pull")` - fetch latest changes
- `sync_clone("push")` - push commits
- Remote branch deletion

#### Authentication Strategy

**Principle**: Use the same authentication method as the local repository.

```python
def _get_remote_url(repo_path: str) -> str:
    """Get the remote URL from local repo config."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_path,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()
```

#### URL Format Detection

```python
def _detect_auth_method(url: str) -> AuthMethod:
    """Detect authentication method from remote URL."""
    if url.startswith("git@") or url.startswith("ssh://"):
        return AuthMethod.SSH
    elif url.startswith("https://"):
        if "@" in url:
            # https://user:token@github.com/...
            return AuthMethod.HTTPS_TOKEN_IN_URL
        else:
            # https://github.com/... (relies on credential helper)
            return AuthMethod.HTTPS_CREDENTIAL_HELPER
    elif url.startswith("http://"):
        return AuthMethod.HTTP_INSECURE
    else:
        return AuthMethod.UNKNOWN
```

#### SSH Authentication

**How it works**: SSH keys are system-wide; clones automatically use them.

```text
Local repo uses: git@github.com:user/project.git
                            │
                            ▼
Clone inherits same URL → SSH agent provides key automatically
```

**Requirements**:

- SSH key added to ssh-agent (`ssh-add`)
- Key authorized on GitHub/GitLab
- `~/.ssh/config` configured if using non-default key

**No additional configuration needed** - clones just work.

#### HTTPS with Credential Helper

**How it works**: Git credential helpers (macOS Keychain, Windows Credential Manager, `git-credential-store`) cache tokens.

```text
Local repo uses: https://github.com/user/project.git
                            │
                            ▼
Clone uses same URL → Credential helper provides token
```

**Common credential helpers**:

```bash
# macOS (uses Keychain)
git config --global credential.helper osxkeychain

# Windows (uses Credential Manager)
git config --global credential.helper manager-core

# Linux (caches in memory for 15 min)
git config --global credential.helper cache

# Store in plaintext file (less secure)
git config --global credential.helper store
```

#### HTTPS with Token in URL

**Format**: `https://oauth2:TOKEN@github.com/user/project.git`

**Handling**: Token is part of URL, clones inherit it automatically.

```python
def create_clone(...):
    remote_url = _get_remote_url(project_path)

    # URL may contain token - clone will inherit it
    # Example: https://oauth2:ghp_xxx@github.com/user/repo.git
    subprocess.run([
        "git", "clone",
        "--depth", str(depth),
        "--branch", base_branch,
        remote_url,  # Token embedded if present
        clone_path
    ])
```

**Security Note**: Token-in-URL is visible in git config. For CI/CD, prefer credential helpers or SSH.

#### GitHub CLI Integration (gh)

For GitHub repos, `gh` can provide authentication:

```python
def _setup_gh_auth_for_clone(clone_path: str) -> bool:
    """Configure clone to use gh for authentication."""
    try:
        # Check if gh is authenticated
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True
        )
        if result.returncode != 0:
            return False

        # Configure git to use gh as credential helper
        subprocess.run([
            "git", "config", "--local",
            "credential.https://github.com.helper",
            "!/usr/bin/gh auth git-credential"
        ], cwd=clone_path)

        return True
    except FileNotFoundError:
        return False  # gh not installed
```

#### Environment Variable Passthrough

For CI/CD or automated environments, ensure auth environment variables are available:

```python
def _get_clone_env() -> dict:
    """Get environment variables for clone operations."""
    env = os.environ.copy()

    # SSH agent socket
    if "SSH_AUTH_SOCK" in os.environ:
        env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]

    # GitHub token (for gh cli or direct API)
    for var in ["GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN"]:
        if var in os.environ:
            env[var] = os.environ[var]

    # Git askpass for non-interactive auth
    if "GIT_ASKPASS" in os.environ:
        env["GIT_ASKPASS"] = os.environ["GIT_ASKPASS"]

    return env

def create_clone(...):
    env = _get_clone_env()
    subprocess.run(
        ["git", "clone", ...],
        env=env,
        ...
    )
```

#### Troubleshooting Authentication

**Problem**: Clone fails with "Authentication failed"

```python
def diagnose_auth_failure(remote_url: str) -> AuthDiagnosis:
    """Diagnose why authentication failed."""
    auth_method = _detect_auth_method(remote_url)

    if auth_method == AuthMethod.SSH:
        # Check SSH agent
        agent_check = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True
        )
        if agent_check.returncode != 0:
            return AuthDiagnosis(
                method="SSH",
                error="No SSH keys in agent",
                fix="Run: ssh-add ~/.ssh/id_ed25519"
            )

        # Test SSH connection
        host = _extract_host(remote_url)  # github.com, gitlab.com, etc.
        ssh_test = subprocess.run(
            ["ssh", "-T", f"git@{host}"],
            capture_output=True
        )
        if "successfully authenticated" not in ssh_test.stderr.decode():
            return AuthDiagnosis(
                method="SSH",
                error=f"SSH key not authorized on {host}",
                fix=f"Add SSH key to {host} account settings"
            )

    elif auth_method == AuthMethod.HTTPS_CREDENTIAL_HELPER:
        # Check credential helper config
        helper = subprocess.run(
            ["git", "config", "--get", "credential.helper"],
            capture_output=True,
            text=True
        )
        if not helper.stdout.strip():
            return AuthDiagnosis(
                method="HTTPS",
                error="No credential helper configured",
                fix="Run: git config --global credential.helper osxkeychain"
            )

    return AuthDiagnosis(method=auth_method.value, error="Unknown", fix="Check git logs")
```

#### Private Repository Considerations

For private repos, ensure:

1. **SSH method** (recommended for agents):
   - SSH key has read/write access
   - Key passphrase is cached in ssh-agent (or use keyless)
   - For deploy keys: must be repo-specific

2. **HTTPS with PAT**:
   - Token has `repo` scope (full access) or fine-grained permissions
   - Token not expired
   - Token stored in credential helper or URL

3. **GitHub App** (enterprise):
   - App installed on repo
   - Installation token generated and cached

#### Configuration in gobby

```yaml
# ~/.gobby/config.yaml
clones:
  # Preferred auth method (auto-detected if not set)
  auth_method: auto  # auto, ssh, https, gh

  # For HTTPS: path to token file (alternative to credential helper)
  token_file: ~/.gobby/github_token

  # SSH key to use (if not default)
  ssh_key: ~/.ssh/gobby_deploy_key

  # Passthrough environment variables
  env_passthrough:
    - SSH_AUTH_SOCK
    - GITHUB_TOKEN
    - GH_TOKEN
```

### 16.13 Comparison with CodeRabbit GTR

[git-worktree-runner](https://github.com/coderabbitai/git-worktree-runner) (GTR) is CodeRabbit's solution for parallel development.

| Feature | Gobby Clones | GTR |
| :--- | :--- | :--- |
| Isolation | Full clones | Worktrees |
| Thread safety | ✅ Isolated .git | ❌ Shared .git |
| AI integration | Native (spawn_agent_in_clone) | Via `git gtr ai` |
| Merge handling | gobby-merge with AI resolution | Manual |
| Task linking | Built-in task_id | None |
| Cleanup | 7-day retention + auto-cleanup | `--merged` flag |

**Key difference**: Gobby uses clones for true parallelism safety, while GTR wraps worktrees with convenience commands.

---

## 17. Implementation Files

### New Files

| File | Purpose |
| :--- | :--- |
| `src/gobby/storage/clones.py` | Clone model + LocalCloneManager |
| `src/gobby/clones/__init__.py` | Module init |
| `src/gobby/clones/git.py` | CloneGitManager (shallow clone ops) |
| `src/gobby/mcp_proxy/tools/clones.py` | MCP tools for gobby-clones server |
| `src/gobby/cli/clones.py` | CLI commands |
| `src/gobby/install/shared/skills/gobby-clones/SKILL.md` | Skill documentation |

### Modified Files

| File | Changes |
| :--- | :--- |
| `docs/plans/orchestration.md` | Add Section 16 |
| `docs/research/investigate-gtr-ccmanager.md` | Mark action items as addressed |
| `src/gobby/storage/migrations.py` | Add `clones` table |
| `src/gobby/worktrees/merge/resolver.py` | Support clone sources |
| `src/gobby/runner.py` | Register gobby-clones server |
| `src/gobby/mcp_proxy/registry.py` | Add clones registry |
| `src/gobby/workflows/definitions/parallel-orchestrator.yaml` | Add clone support |
| `CLAUDE.md` | Add gobby-clones to MCP server table |

### Test Files

| File | Validates |
| :--- | :--- |
| `tests/storage/test_clones.py` | Clone storage layer |
| `tests/clones/test_git.py` | CloneGitManager operations |
| `tests/mcp_proxy/tools/test_clones.py` | MCP tool behavior |
| `tests/e2e/test_parallel_clones.py` | Full parallel orchestration with clones |

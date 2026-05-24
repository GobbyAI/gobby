# Plan: Task Commands, Gobby the Task Goblin, and Inter-Agent Communication

## Summary

Three features requested:
1. **Task Type Commands** - Quick slash commands for creating tasks by type
2. **Gobby the Task Goblin** - Persistent LLM loop for task orchestration and monitoring
3. **Inter-Agent Communication** - Messaging between parent/child agents in worktrees

---

## Feature 1: Task Type Commands ✅ DONE

### Commands Created
| Command | Task Type | Notes |
|---------|-----------|-------|
| `/bug` | bug | Bug/defect task |
| `/feat` | feature | New feature task |
| `/nit` | chore | Nitpick with label |
| `/ref` | chore | Refactoring with label |
| `/epic` | epic | Parent container task |
| `/chore` | chore | Maintenance/cleanup |

### File Structure
```
.claude/commands/
├── bug.md       # /bug <title> [description]
├── feat.md      # /feat <title> [description]
├── nit.md       # /nit <title> [description] (type=chore, label=nitpick)
├── ref.md       # /ref <title> [description] (type=chore, label=refactor)
├── epic.md      # /epic <title> [description]
└── chore.md     # /chore <title> [description]
```

### Implementation
Each command file calls `gobby-tasks.create_task` with appropriate `task_type` and labels.

---

## Feature 2: Gobby the Task Goblin

### Concept
A persistent Python LLM loop that monitors tasks, checks agent health, tracks tokens, and speaks in haiku. A friendly daemon that keeps the task system tidy.

### Architecture

```
                    ┌─────────────────────────────────┐
                    │     Gobby the Task Goblin       │
                    │     (Persistent LLM Loop)       │
                    └─────────────────────────────────┘
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
                    │ • Log to goblin.log         │
                    │ • Desktop notifications     │
                    │ • callme → USER (you!)      │
                    └─────────────────────────────┘
```

### Personality: TARS Mode (15% Humor)
Gobby speaks in haiku with dry, deadpan wit. Think TARS from Interstellar - helpful, competent, occasionally catches you off guard with subtle sass.

**Normal status:**
```
Three tasks await you
Code review blocks the feature
Dependencies clear
```

**When you've ignored a task for 3 days:**
```
Task forty-seven
Has waited three days for you
It's starting to judge
```

**When an agent is stuck:**
```
Gemini sits still
Perhaps it found enlightenment
Or perhaps it crashed
```

**After you close 5 tasks in a row:**
```
Five tasks completed
Your productivity frightens
Even the machines
```

**Configuration:**
```yaml
goblin:
  personality:
    mode: haiku
    humor_setting: 0.15  # TARS-style dry wit
    sass_on_stale_tasks: true
    sass_threshold_hours: 24
```

### CLI Interface
```bash
gobby goblin start [--interval=30s]   # Start the goblin daemon
gobby goblin stop                      # Stop the daemon
gobby goblin status                    # Current state (haiku!)
gobby goblin talk "what should I do?"  # Chat with Gobby
gobby goblin log                       # View recent activity
```

### Claude Code Integration
- `/gobby` skill for interactive chat
- `/gobby status` for task/agent summary
- `/gobby suggest` for next action recommendation
- Uses callme for proactive alerts

### Core Components

1. **GoblinLoop** (`src/gobby/goblin/loop.py`)
   - Main async loop with configurable interval
   - Calls monitors, aggregates findings, generates haiku
   - Stores state in `~/.gobby/goblin_state.json`

2. **TaskMonitor** (`src/gobby/goblin/monitors/tasks.py`)
   - Detect stale tasks (in_progress > threshold)
   - Find orphaned subtasks
   - Check blocked task chains

3. **AgentWatcher** (`src/gobby/goblin/monitors/agents.py`)
   - Check RunningAgentRegistry for stuck processes
   - Monitor agent depth limits
   - Detect hung terminal sessions

4. **TokenTracker** (`src/gobby/goblin/monitors/tokens.py`)
   - Aggregate token usage from session metadata
   - Budget warnings
   - Cost estimation

5. **HaikuGenerator** (`src/gobby/goblin/haiku.py`)
   - LLM-powered status summarization (uses Claude Haiku model - *chef's kiss*)
   - TARS-style personality with 15% humor setting
   - Template library for common states (with sass variants)
   - Humor injection based on context (stale tasks, stuck agents, productivity streaks)

6. **AlertDispatcher** (`src/gobby/goblin/alerts.py`)
   - Log to file
   - Desktop notifications
   - callme integration (notify user via phone/SMS/Slack)

### Configuration
```yaml
# ~/.gobby/config.yaml
goblin:
  enabled: true
  interval_seconds: 30
  personality: haiku  # haiku | prose | terse
  stale_task_threshold_minutes: 60
  stuck_agent_threshold_minutes: 15
  token_budget_warning: 0.8  # Warn at 80% of budget
  llm_mode: hybrid  # template | api | hybrid
  api_model: claude-3-haiku-20240307  # For complex analysis
  callme_alerts: true
```

### Hybrid LLM Strategy
```
┌─────────────────────────────────────────────────────────────┐
│                    Haiku Generation                          │
├─────────────────────────────────────────────────────────────┤
│  Common States (Templates - FREE):                          │
│  • "all_clear" → "No tasks await / All agents rest quietly │
│                   / Peace in the codebase"                  │
│  • "tasks_waiting" → "N tasks await you / Ready for your   │
│                       attention / Choose wisely, friend"    │
│  • "agent_stuck" → "Agent sits idle / Minutes pass without │
│                     word / Perhaps check on them"           │
│                                                              │
│  Novel Situations (Claude API):                             │
│  • Complex multi-issue summaries                            │
│  • Unexpected error patterns                                │
│  • Custom user queries via `gobby goblin talk`              │
└─────────────────────────────────────────────────────────────┘
```

---

## Feature 3: Inter-Agent Communication

### Current State
- Agents in worktrees are isolated terminal processes
- Parent uses `wait_for_task` to poll completion status
- No real-time messaging between sessions

### Problem
For auto-review-loop, we need:
- Child signals completion → Parent reviews
- Parent sends feedback → Child acts on it
- Questions from child → Parent can answer

### Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Inter-Session Messages                        │
├─────────────────────────────────────────────────────────────────┤
│ id | from_session | to_session | content | sent_at | read_at   │
└─────────────────────────────────────────────────────────────────┘
```

### New MCP Tools (gobby-agents)

```python
# Child → Parent
send_to_parent(message: str, priority: str = "normal") -> dict
  """Send message to parent session. Returns message_id."""

# Parent → Child
send_to_child(run_id: str, message: str) -> dict
  """Send message to child agent session. Returns message_id."""

# Either direction
poll_messages(unread_only: bool = True) -> list[dict]
  """Get pending messages for current session."""

mark_read(message_id: str) -> dict
  """Mark message as read."""
```

### Integration with Auto-Review Loop

```
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
      │◄────────── close_task(pending_review) ────┤
      │                                           │
      │ [reviews code]                            │
      │                                           │
      │ send_to_child("Fix the error handling") ──┤►
      │                                           │
      │                           [polls msgs]    │
      │                           [fixes code]    │
      │                                           │
      │◄────────── close_task(pending_review) ────┤
      │                                           │
      │ approve_and_cleanup()                     │
      │                                           │
```

### User Notifications via callme
For urgent situations, notify the human user:
- Agent stuck for extended time → callme alerts user
- Task blocked waiting for decision → callme asks user
- Gobby detects critical problem → callme calls user

### Storage
Extend `session_messages.py` or create new `inter_session_messages.py`:
- PostgreSQL table for messages
- Cleanup on session end
- Max message age (24h default)

---

## Implementation Order

### Phase 1: Task Commands ✅ DONE
1. ~~Create 6 command files in `.claude/commands/`~~
2. ~~Test each command works~~

### Phase 2: Inter-Agent Messaging (Foundation)
1. Add inter_session_messages table
2. Implement send/receive MCP tools
3. Add poll_messages to auto-review workflow
4. Test parent-child communication

### Phase 3: Gobby the Task Goblin (Major Feature)
1. Create `src/gobby/goblin/` module structure
2. Implement monitors (task, agent, token)
3. Add haiku generator
4. Create CLI commands
5. Add /gobby skill for Claude Code
6. Integrate callme for alerts
7. Full testing

---

## Design Decisions (User Confirmed)

1. **Gobby's LLM**: **Hybrid approach** - Templates for common states (fast, free), Claude API (haiku model) for complex analysis and novel situations

2. **Gobby's Personality**: **TARS Mode (15% humor)** - Dry, deadpan wit. Helpful and competent with occasional sass.

3. **Inter-agent sync**: **Polling (5-10s intervals)** - Simple, low overhead, acceptable latency for review loops

4. **callme**: **Include setup** - Add installation and configuration instructions

---

## Verification

### Task Commands
```bash
# Test each command
claude "/bug Fix the login timeout"
claude "/feat Add dark mode toggle"
claude "/nit Rename confusing variable"
```

### Inter-Agent Messaging
```bash
# Unit tests
uv run pytest tests/mcp_proxy/tools/test_agent_messaging.py -v

# Integration test
# 1. Spawn child agent
# 2. Child sends message to parent
# 3. Parent receives and responds
# 4. Child receives response
```

### Gobby the Task Goblin
```bash
# Start goblin
gobby goblin start

# Check status (should see haiku)
gobby goblin status

# Chat
gobby goblin talk "What's the status?"

# Check logs
gobby goblin log

# Stop
gobby goblin stop
```

---

## Files to Create/Modify

### Task Commands ✅
- `.claude/commands/bug.md` ✅
- `.claude/commands/feat.md` ✅
- `.claude/commands/nit.md` ✅
- `.claude/commands/ref.md` ✅
- `.claude/commands/epic.md` ✅
- `.claude/commands/chore.md` ✅

### Inter-Agent Messaging
- `src/gobby/storage/inter_session_messages.py` (new)
- `src/gobby/storage/migrations.py` (modify)
- `src/gobby/mcp_proxy/tools/agents.py` (modify)
- `tests/mcp_proxy/tools/test_agent_messaging.py` (new)

### Gobby the Task Goblin
- `src/gobby/goblin/__init__.py` (new)
- `src/gobby/goblin/loop.py` (new)
- `src/gobby/goblin/monitors/tasks.py` (new)
- `src/gobby/goblin/monitors/agents.py` (new)
- `src/gobby/goblin/monitors/tokens.py` (new)
- `src/gobby/goblin/haiku.py` (new)
- `src/gobby/goblin/alerts.py` (new)
- `src/gobby/cli/goblin.py` (new)
- `.claude/commands/gobby.md` (new skill)
- `tests/goblin/` (new test directory)

---

## Appendix: callme Integration

### What is callme?
callme allows Claude Code (and Gobby) to **call YOU the user** - send notifications, alerts, or even phone calls when something needs your attention. You don't have to be watching the terminal.

### Use Cases for Gobby
```
┌─────────────────────────────────────────────────────────────┐
│  Gobby detects problem → Calls YOU (human) via callme       │
├─────────────────────────────────────────────────────────────┤
│  • Agent stuck for 15+ min → "Hey, your Gemini agent seems  │
│    stuck. Want me to check on it?"                          │
│                                                              │
│  • Task stalled → "Task #47 has been in_progress for 2hrs.  │
│    Should I escalate or ping the assignee?"                 │
│                                                              │
│  • Token budget warning → "You're at 80% token budget.      │
│    Wrap up or should I extend?"                             │
│                                                              │
│  • Epic complete → "All subtasks done! Ready for review."   │
└─────────────────────────────────────────────────────────────┘
```

### Configuration
```yaml
# ~/.gobby/config.yaml
goblin:
  callme:
    enabled: true
    notification_method: desktop  # desktop | sms | phone | slack
    urgent_threshold: critical    # When to escalate to phone/urgent

    # Desktop notifications (default)
    desktop:
      sound: true

    # SMS/Phone (requires callme account)
    phone:
      number: "+1234567890"

    # Slack webhook
    slack:
      webhook_url: "https://hooks.slack.com/..."
```

### Alert Priorities
- **info**: Log only, no notification
- **normal**: Desktop notification
- **urgent**: Desktop + sound + badge
- **critical**: Phone call / SMS (if configured)

### Integration Flow
```
Gobby Loop
    │
    ├─► Detects stuck agent (15 min threshold)
    │
    ├─► Generates haiku: "Agent sits idle /
    │   Minutes pass without a word / Perhaps check on them"
    │
    └─► Calls user via callme (priority based on severity)
            │
            └─► User gets notification
                    │
                    └─► User: "/gobby check agent"
                            │
                            └─► Gobby responds with status
```

### Setup Instructions
1. Check callme GitHub for installation: https://github.com/anthropics/callme (or similar)
2. Configure notification method in `~/.gobby/config.yaml`
3. Test with: `gobby goblin test-alert "Test message"`

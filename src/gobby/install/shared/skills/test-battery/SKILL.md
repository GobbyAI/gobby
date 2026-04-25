---
name: test-battery
description: "Fire-and-forget orchestrator test battery. Interactive wizard sets up a cron-driven orchestrator pipeline, triggers the first tick, and exits. Use '/gobby test-battery cleanup' to finalize after completion."
version: "4.0.0"
category: testing
triggers: test battery, orchestrator test, run test battery, e2e test, test the orchestrator
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby test-battery — Orchestrator Test Battery v4

Fire-and-forget orchestrator battery. Sets up a cron-driven orchestrator pipeline, triggers the first tick, and exits. Monitor progress from the web UI. Run `/gobby test-battery cleanup` when the epic completes.

**Two modes:**
1. **Setup** (default) — Interactive wizard → create cron → trigger first tick → done
2. **Cleanup** (`/gobby test-battery cleanup`) — Merge isolation env → close epic → tear down cron → final report

---

## Mode Detection

1. If arguments contain "cleanup", skip to **Cleanup Mode**
2. Otherwise, check for existing `test-battery.md`:
   - If it exists and `Status: RUNNING` → warn that a battery is already active, ask to tear down first or abort
   - If it doesn't exist or status is COMPLETE → proceed to **Setup Mode**

---

## Setup Mode

### Phase 1: Interactive Setup Wizard

Collect configuration through 10 sequential prompts. Show the default for each and accept enter/confirmation for defaults.

#### Prompt 1: Reset Environment

Ask: "Reset environment? This will delete existing clones/worktrees, kill running agents, and remove orchestrator cron jobs. (y/N)"

If yes:
```python
# Delete all clones
clones = call_tool("gobby-clones", "list_clones", {})
for clone in clones.clones:
    call_tool("gobby-clones", "delete_clone", {"clone_id": clone.id, "force": true})

# Delete all worktrees
worktrees = call_tool("gobby-worktrees", "list_worktrees", {})
for wt in worktrees.worktrees:
    call_tool("gobby-worktrees", "delete_worktree", {"worktree_id": wt.id})

# Kill running agents
agents = call_tool("gobby-agents", "list_running_agents", {})
for agent in agents.runs:
    call_tool("gobby-agents", "kill_agent", {"run_id": agent.id})

# Delete orchestrator cron jobs (skip system jobs with gobby: prefix)
jobs = call_tool("gobby-cron", "list_cron_jobs", {})
for job in jobs where job.name contains "orchestrator" or "test-battery":
    if job.name.startswith("gobby:"):
        continue  # NEVER delete system cron jobs
    call_tool("gobby-cron", "delete_cron_job", {"job_id": job.id})
```

#### Prompt 2: Commit Changes

Run `git status`. If there are uncommitted changes:
- Ask: "You have uncommitted changes. Commit them before proceeding? (Y/n)"
- If yes, use the source-control skill to commit everything
- If no, warn that uncommitted changes won't be in the worktree/clone

#### Prompt 3: Target Task/Epic

Ask: "What task or epic should the orchestrator target? Enter a ref (#N) or 'new' to create one."

- If ref provided: fetch the task with `get_task` and confirm it exists
- If "new": ask for a title and create an epic task

#### Prompt 4: Expansion

Ask: "How should subtasks be created?"
- **(a) Run expand-task pipeline now** — Starts expansion and resumes from the daemon completion wake
- **(b) Provide a plan file** — Path to a .md plan file for the expansion run to use
- **(c) Skip** — Subtasks already exist under this epic

If (a): Run expand-task pipeline and store the returned `execution_id`:
```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "inputs": {"task_id": "<epic_id>"}
})
```

Then end the turn. On the daemon completion wake, inspect the run with
`get_pipeline_status(execution_id=<execution_id>)`.

If (b): Run expand-task with the plan file and store the returned `execution_id`:
```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "inputs": {"task_id": "<epic_id>", "plan_file": "<path>"}
})
```

Then end the turn. On the daemon completion wake, inspect the run with
`get_pipeline_status(execution_id=<execution_id>)`.

If (c): Verify subtasks exist with `list_tasks(parent_task_id=epic_id)`. Warn if zero subtasks found.

#### Prompt 5: Developer Provider/Model

Ask: "Developer agent provider and model? (default: gemini / provider-default)"
- Parse as `provider / model` or just `provider`
- Default: `developer_provider="gemini"`, `developer_model=null`

#### Prompt 6: QA Provider/Model

Ask: "QA agent provider and model? (default: claude / opus)"
- Default: `qa_provider="claude"`, `qa_model="opus"`

#### Prompt 7: Agent Timeout

Ask: "Agent timeout in seconds? (default: 1200)"
- Default: 1200

#### Prompt 8: Cron Interval

Ask: "Cron interval — how often should the orchestrator tick? (default: 5m)"
- Accept formats: "5m", "300", "300s", "10m"
- Convert to seconds
- Default: 300

#### Prompt 9: Isolation Mode

Ask: "Agent isolation mode? (default: worktree)"
- **worktree** — Each agent gets a git worktree (fast, shared .git, recommended)
- **clone** — Each agent gets a full git clone (fully isolated, slower setup)
- **none** — Agents work in the main directory (no isolation, use for debugging)
- Default: `isolation="worktree"`

#### Prompt 10: Confirm

Display the full configuration:

```
============================================
  Test Battery Configuration
============================================
Epic:           #<N> "<title>"
Subtasks:       <count> tasks
Dev Agent:      <provider> / <model>
QA Agent:       <provider> / <model>
Timeout:        <N>s
Cron Interval:  <N>s (<N>m)
Max Concurrent: 5
Merge Target:   <current_branch>
Isolation:      <worktree|clone|none>
============================================
```

Ask: "Proceed? (Y/n)"

---

### Phase 2: Launch

Execute these steps in order. If any step fails, report the error and stop.

#### Step 1: Create Cron Job

```python
job = call_tool("gobby-cron", "create_cron_job", {
    "name": "test-battery-orchestrator",
    "action_type": "pipeline",
    "action_config": {
        "pipeline_name": "orchestrator",
        "inputs": {
            "task_id": "<epic_id>",
            "developer_agent": "developer",
            "qa_agent": "qa-reviewer",
            "developer_provider": "<dev_provider>",
            "qa_provider": "<qa_provider>",
            "developer_model": "<dev_model>",
            "qa_model": "<qa_model>",
            "agent_timeout": <timeout>,
            "max_concurrent": 5,
            "merge_target": "<current_branch>",
            "isolation": "<isolation>"
        }
    },
    "schedule_type": "interval",
    "interval_seconds": <interval>,
    "description": "Orchestrator tick for test battery epic #<N>"
})
```

#### Step 2: Write State File

Create `test-battery.md` in the project root using the **State File Template** below. Fill in all configuration values.

#### Step 3: Trigger First Tick

```python
call_tool("gobby-cron", "run_cron_job", {"job_id": "<cron_job_id>"})
```

#### Step 4: Report and Exit

Report:
```
Battery launched! First orchestrator tick triggered.

  Cron Job:  <cron_job_id> (every <interval>s)
  Epic:      #<N>
  Isolation: <mode>

Monitor progress in the web UI or run:
  gobby tasks list --parent #<N>
  gobby cron runs <cron_job_id>

When complete, run: /gobby test-battery cleanup
```

**Stop.** The battery is fire-and-forget.

---

## Cleanup Mode

Invoked via `/gobby test-battery cleanup`. Finalizes a completed (or stalled) battery.

### Steps

#### 1. Read State File

Read `test-battery.md` from the project root. If it doesn't exist, abort with error.
Extract: `epic_id`, `cron_job_id`, `isolation`, `merge_target`.

#### 2. Check Epic Status

```python
# Get subtask counts
for s in ["open", "in_progress", "needs_review", "review_approved", "closed"]:
    tasks = call_tool("gobby-tasks", "list_tasks", {
        "parent_task_id": "<epic_id>",
        "status": s
    })
```

Report current state. If there are still `open` or `in_progress` tasks, warn:
"Epic is not complete yet. <N> tasks still open/in progress. Continue cleanup anyway? (y/N)"

#### 3. Disable Cron

```python
call_tool("gobby-cron", "toggle_cron_job", {"job_id": "<cron_job_id>"})
```

#### 4. Kill Running Agents

```python
agents = call_tool("gobby-agents", "list_running_agents", {})
# Kill any agents associated with this battery's session
for agent in agents.agents:
    call_tool("gobby-agents", "kill_agent", {"run_id": agent.run_id})
```

Kill orphaned tmux sessions:
```bash
tmux -L gobby list-sessions 2>/dev/null | grep -v attached | cut -d: -f1 | while read -r sess; do tmux -L gobby kill-session -t "$sess" 2>/dev/null; done
```

#### 5. Merge Isolation Environment

Based on `isolation` from state file:

**If worktree:**
```python
wt = call_tool("gobby-worktrees", "get_worktree_by_task", {"task_id": "<epic_id>"})
if wt.worktree:
    call_tool("gobby-worktrees", "merge_worktree", {
        "worktree_id": wt.worktree.id,
        "target_branch": "<merge_target>"
    })
    call_tool("gobby-worktrees", "delete_worktree", {"worktree_id": wt.worktree.id})
```

**If clone:**
```python
clone = call_tool("gobby-clones", "get_clone_by_task", {"task_id": "<epic_id>"})
if clone.clone:
    call_tool("gobby-clones", "merge_clone", {
        "clone_id": clone.clone.id,
        "target_branch": "<merge_target>"
    })
    call_tool("gobby-clones", "delete_clone", {"clone_id": clone.clone.id})
```

#### 6. Close Epic

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "<epic_id>",
    "changes_summary": "Orchestration complete via test battery. All tasks processed."
})
```

If close fails (e.g., subtasks still open), report and skip.

#### 7. Delete Cron Job

```python
call_tool("gobby-cron", "delete_cron_job", {"job_id": "<cron_job_id>"})
```

#### 8. Update State File

Update `test-battery.md`:
- Set `Status: COMPLETE`
- Add final report section with:
  - Task counts by status
  - Total cron runs (from `list_cron_runs`)
  - Duration (started_at → now)
  - Any remaining open/stuck tasks

#### 9. Final Report

Display summary:
```
============================================
  Test Battery Complete
============================================
Epic:           #<N> "<title>"
Duration:       <hours>h <minutes>m
Cron Ticks:     <count>
Tasks Closed:   <N>/<total>
Tasks Remaining: <N> (list refs if any)
Isolation:      merged and cleaned up
============================================
```

---

## State File Template

Create as `test-battery.md` in the project root during Phase 2:

```markdown
# Test Battery State

> Configuration record for the test battery.
> Used by `/gobby test-battery cleanup` to finalize.

## Status: RUNNING

## Configuration
- **Epic**: #<N> "<title>"
- **Epic ID**: <uuid>
- **Cron Job ID**: <cron_job_id>
- **Current Branch**: <branch>
- **Merge Target**: <branch>
- **Dev Provider/Model**: <provider> / <model>
- **QA Provider/Model**: <provider> / <model>
- **Agent Timeout**: <N>s
- **Cron Interval**: <N>s
- **Max Concurrent**: 5
- **Isolation**: <worktree|clone|none>
- **Started At**: <ISO timestamp>

## Prior Batteries

(Reference previous test-battery runs here for context)
```

---

## Debugging Commands

Useful commands for checking battery progress:

```bash
# Pipeline execution history
gobby pipelines history orchestrator

# Specific execution details
gobby pipelines status <execution_id>

# Running agents
gobby agents ps

# Task states
gobby tasks list --parent <epic_id>

# Watch for issues
tail -f ~/.gobby/logs/gobby.log | grep -E "(ERROR|dead.end|lineage|retry)"

# Tmux agent sessions
tmux -L gobby list-sessions

# Cron job status
gobby cron list
gobby cron runs <job_id>
```

---

## Known Issues to Watch For

| Check | What to verify | Bug ref |
|-------|---------------|---------|
| Dead-end retry counter | Increments across retries (not stuck at 1/10) | #9937 |
| Session lineage | No "Lineage exceeded" warnings, depth < 5 | #9938 |
| No parallel retries | Single retry chain per epic | #9939 |
| Agent clean exit | Exits within 30s, no 3-min idle wait | #9940 |
| Pipeline efficiency | Total executions < 50 for 3-task epic | — |
| Stop hook scoping | No stop hook errors in agent logs | #9918 |
| Idle detection | Sees through status bar, detects true idle | #9932 |
| Dependency satisfaction | `review_approved` satisfies blocked tasks | #9933 |

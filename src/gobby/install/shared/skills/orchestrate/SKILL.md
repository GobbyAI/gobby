---
name: orchestrate
description: "Production orchestration wizard. Sets up cron-driven pipelines to process epics with dev+QA agents. Supports multiple concurrent named orchestrations. Subcommands: setup (default), status, list, pause, resume, cleanup."
version: "1.1.0"
category: orchestration
triggers: orchestrate, orchestration, run orchestrator, set up orchestration, dispatch agents
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby orchestrate — Orchestration Manager v1

Fire-and-forget orchestration. Sets up a cron-driven orchestrator pipeline, triggers the first tick, and exits. Supports multiple concurrent named orchestrations with independent lifecycle management.

**Subcommands:**
1. **Setup** (default) — Interactive wizard → create cron → trigger first tick → done
2. **Status** (`/gobby orchestrate status {slug}`) — Read-only progress dashboard
3. **List** (`/gobby orchestrate list`) — All active orchestrations
4. **Pause** (`/gobby orchestrate pause {slug}`) — Disable cron, agents finish naturally
5. **Resume** (`/gobby orchestrate resume {slug}`) — Re-enable cron + trigger tick
6. **Cleanup** (`/gobby orchestrate cleanup {slug}`) — Merge isolation → close epic → tear down cron

---

## Named Orchestrations

Every orchestration gets a **slug** — a short kebab-case identifier (e.g., `auth-rewrite`, `otel-migration`). The slug threads through all resources:

| Resource | Convention | Example |
|----------|-----------|---------|
| State file | `.gobby/orchestrations/{slug}.md` | `.gobby/orchestrations/auth-rewrite.md` |
| Cron job name | `orchestrate:{slug}` | `orchestrate:auth-rewrite` |
| Cron description | `Orchestrator tick for "{slug}" (epic #{N})` | |

---

## Mode Detection

Evaluate arguments in order:

1. Contains "cleanup" → **Cleanup Mode** (slug required)
2. Contains "status" → **Status Mode** (slug required)
3. Contains "list" → **List Mode**
4. Contains "pause" → **Pause Mode** (slug required)
5. Contains "resume" → **Resume Mode** (slug required)
6. Otherwise → **Setup Mode**

When a slug is required but not provided, run **List Mode** first, then ask the user to pick one.

---

## Setup Mode

### Phase 1: Interactive Setup Wizard

Collect configuration through 8 sequential prompts. Show the default for each and accept enter/confirmation for defaults.

#### Prompt 1: Commit Changes

Run `git status`. If there are uncommitted changes:
- Ask: "You have uncommitted changes. Commit them before proceeding? (Y/n)"
- If yes, use the source-control skill to commit everything
- If no, warn that uncommitted changes won't be in the worktree/clone

#### Prompt 2: Target Task/Epic

Ask: "What task or epic should the orchestrator target? Enter a ref (#N) or 'new' to create one."

- If ref provided: fetch the task with `get_task` and confirm it exists
- If "new": ask for a title and create an epic task

After resolving the epic, generate a default slug from the title:
- Lowercase, replace spaces with hyphens, strip non-alphanumeric chars, truncate to 30 chars
- Example: "Add OAuth2 Support" → `add-oauth2-support`

#### Prompt 3: Orchestration Name

Ask: "Orchestration name? (default: {generated-slug})"

Validate:
- Kebab-case, 3-40 characters, alphanumeric and hyphens only
- No existing `.gobby/orchestrations/{slug}.md` with `Status: RUNNING` or `Status: PAUSED`
- No existing cron job named `orchestrate:{slug}`

If validation fails, explain why and re-prompt.

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

#### Prompt 5: Isolation Mode

Ask: "Agent isolation mode? (default: worktree)"
- **worktree** — Each agent gets a git worktree (fast, shared .git, recommended)
- **clone** — Each agent gets a full git clone (fully isolated, slower setup)
- **none** — Agents work in the main directory (no isolation, use for debugging)
- Default: `isolation="worktree"`

#### Prompt 6: Developer Agent

First, fetch available agent definitions:
```python
defs = call_tool("gobby-workflows", "list_agent_definitions", {"enabled": true})
```

Filter to developer-class agents (exclude agents whose name contains "qa", "reviewer", "conductor", "merge", "expansion", "default", "pipeline", "web-chat", "codex", "nightly"). Show the filtered list:

```text
Available developer agents:
  1. developer        — Developer agent: implements tasks, writes tests, commits
  2. python-dev       — Python developer agent with hub skills for testing, perf, best practices
  ...
```

Ask: "Developer agent definition? (default: developer)"
- Accept number or name
- Default: `developer_agent="developer"`

Then ask: "Developer provider/model override? (default: inherit from agent definition)"
- Parse as `provider / model`, just `provider`, or empty for inherit
- If the chosen agent already has a provider/model set in its definition, show it: "python-dev defines no provider — override? (default: gemini / provider-default)"
- Default: `developer_provider="gemini"`, `developer_model=null`
- If user enters "inherit" or empty: `developer_provider=null`, `developer_model=null`

Then ask: "Developer agent mode? (default: interactive)"
- **interactive** — Agent runs in a tmux terminal session (visible)
- **autonomous** — Agent runs as a background process (headless, auto-approve)
- Default: `developer_mode="interactive"`

#### Prompt 7: QA Agent

Show QA-class agents from the same definitions list (include agents whose name contains "qa" or "reviewer"):

```
Available QA agents:
  1. qa-reviewer  — Reviews code, approves/rejects
  2. qa-dev       — Reviews code AND fixes issues (claude)
  ...
```

Ask: "QA agent definition? (default: qa-reviewer)"
- Accept number or name
- Default: `qa_agent="qa-reviewer"`

Then ask: "QA provider/model override? (default: claude / opus)"
- Same parsing as Prompt 6
- Default: `qa_provider="claude"`, `qa_model="opus"`

Then ask: "QA agent mode? (default: interactive)"
- **interactive** — Agent runs in a tmux terminal session (visible)
- **autonomous** — Agent runs as a background process (headless, auto-approve)
- Default: `qa_mode="interactive"`

#### Prompt 8: Operational Parameters + Confirm

Ask three values, then confirm:

1. "Agent timeout in seconds? (default: 1200)"
   - Default: 1200

2. "Cron interval — how often should the orchestrator tick? (default: 5m)"
   - Accept formats: "5m", "300", "300s", "10m"
   - Convert to seconds
   - Default: 300

3. "Max concurrent dev agents? (default: 5)"
   - Default: 5

Then display the full configuration:

```
============================================
  Orchestration: {slug}
============================================
Epic:           #{N} "{title}"
Subtasks:       {count} tasks
Isolation:      {worktree|clone|none}
Dev Agent:      {agent_name} → {provider} / {model} ({mode})
QA Agent:       {agent_name} → {provider} / {model} ({mode})
Timeout:        {N}s
Cron Interval:  {N}s ({N}m)
Max Concurrent: {N}
Merge Target:   {current_branch}
============================================
```

Ask: "Proceed? (Y/n)"

---

### Phase 2: Launch

Execute these steps in order. If any step fails, report the error and stop.

#### Step 1: Create Cron Job

```python
job = call_tool("gobby-cron", "create_cron_job", {
    "name": "orchestrate:{slug}",
    "action_type": "pipeline",
    "action_config": {
        "pipeline_name": "orchestrator",
        "inputs": {
            "task_id": "<epic_id>",
            "developer_agent": "<developer_agent>",
            "qa_agent": "<qa_agent>",
            "developer_provider": "<dev_provider>",
            "developer_mode": "<dev_mode>",
            "qa_provider": "<qa_provider>",
            "qa_mode": "<qa_mode>",
            "developer_model": "<dev_model>",
            "qa_model": "<qa_model>",
            "agent_timeout": <timeout>,
            "max_concurrent": <max_concurrent>,
            "merge_target": "<current_branch>",
            "isolation": "<isolation>"
        }
    },
    "schedule_type": "interval",
    "interval_seconds": <interval>,
    "description": "Orchestrator tick for \"{slug}\" (epic #{N})"
})
```

#### Step 2: Write State File

Create `.gobby/orchestrations/{slug}.md` using the **State File Template** below. Ensure the `.gobby/orchestrations/` directory exists first (`mkdir -p`). Fill in all configuration values.

#### Step 3: Trigger First Tick

```python
call_tool("gobby-cron", "run_cron_job", {"job_id": "<cron_job_id>"})
```

#### Step 4: Report and Exit

Report:
```
Orchestration "{slug}" launched! First tick triggered.

  Cron Job:  {cron_job_id} (every {interval}s)
  Epic:      #{N}
  Isolation: {mode}

Monitor:
  /gobby orchestrate status {slug}
  /gobby orchestrate list

When complete:
  /gobby orchestrate cleanup {slug}
```

**Stop.** The orchestration is fire-and-forget.

---

## Status Mode

Invoked via `/gobby orchestrate status {slug}`. Read-only progress dashboard.

### Steps

#### 1. Read State File

Read `.gobby/orchestrations/{slug}.md`. If it doesn't exist, abort with error.
Extract: `epic_id`, `cron_job_id`, configuration values.

#### 2. Gather Data

```python
# Task counts by status
for s in ["open", "in_progress", "needs_review", "review_approved", "closed"]:
    tasks = call_tool("gobby-tasks", "list_tasks", {
        "parent_task_id": "<epic_id>",
        "status": s
    })

# Recent cron runs (last 5)
runs = call_tool("gobby-cron", "list_cron_runs", {"job_id": "<cron_job_id>", "limit": 5})

# Cron job state
job = call_tool("gobby-cron", "get_cron_job", {"job_id": "<cron_job_id>"})

# Running agents
agents = call_tool("gobby-agents", "list_running_agents", {})
```

#### 3. Display Dashboard

```
============================================
  Orchestration: {slug}
  Status: {RUNNING|PAUSED|COMPLETE}
============================================
Epic:           #{N} "{title}"
Started:        {timestamp} ({duration} ago)

Tasks:
  Open:             {N}
  In Progress:      {N}
  Needs Review:     {N}
  Review Approved:  {N}
  Closed:           {N} / {total}

Cron:
  Job ID:     {id}
  Enabled:    {yes/no}
  Last Run:   {timestamp} ({status})
  Next Run:   {timestamp}
  Total Ticks: {N}

Active Agents: {N}
============================================
```

**Read-only. No mutations.**

---

## List Mode

Invoked via `/gobby orchestrate list`. Shows all orchestrations.

### Steps

#### 1. Scan State Files

Read all files matching `.gobby/orchestrations/*.md`. For each, extract: slug (from filename), Status, Epic ref, Started At.

Also check for legacy `test-battery.md` at project root. If found, include it with slug `(legacy)`.

#### 2. Display

```
Active Orchestrations:

  {slug}     #{N} "{title}"  {STATUS}  started {duration} ago
  {slug}     #{N} "{title}"  {STATUS}  started {duration} ago

Commands:
  /gobby orchestrate status {slug}
  /gobby orchestrate pause {slug}
  /gobby orchestrate cleanup {slug}
```

If no state files found: "No active orchestrations. Run `/gobby orchestrate` to set one up."

---

## Pause Mode

Invoked via `/gobby orchestrate pause {slug}`. Disables cron ticking — running agents finish naturally but no new ticks fire.

### Steps

#### 1. Read State File

Read `.gobby/orchestrations/{slug}.md`. Verify `Status: RUNNING`. Extract `cron_job_id`.

#### 2. Disable Cron

```python
call_tool("gobby-cron", "toggle_cron_job", {"job_id": "<cron_job_id>"})
```

#### 3. Update State File

Change `Status: RUNNING` to `Status: PAUSED` in the state file.

#### 4. Report

```
Orchestration "{slug}" paused. Running agents will finish but no new ticks will fire.

Resume: /gobby orchestrate resume {slug}
```

---

## Resume Mode

Invoked via `/gobby orchestrate resume {slug}`. Re-enables cron and triggers an immediate tick.

### Steps

#### 1. Read State File

Read `.gobby/orchestrations/{slug}.md`. Verify `Status: PAUSED`. Extract `cron_job_id`.

#### 2. Enable Cron

```python
call_tool("gobby-cron", "toggle_cron_job", {"job_id": "<cron_job_id>"})
```

#### 3. Trigger Tick

```python
call_tool("gobby-cron", "run_cron_job", {"job_id": "<cron_job_id>"})
```

#### 4. Update State File

Change `Status: PAUSED` to `Status: RUNNING` in the state file.

#### 5. Report

```
Orchestration "{slug}" resumed. Tick triggered.
```

---

## Cleanup Mode

Invoked via `/gobby orchestrate cleanup {slug}`. Finalizes a completed (or stalled) orchestration. Scoped to the specific orchestration — does not affect other orchestrations or global resources.

### Steps

#### 1. Read State File

Read `.gobby/orchestrations/{slug}.md`. If it doesn't exist, abort with error.
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
"Epic is not complete yet. {N} tasks still open/in progress. Continue cleanup anyway? (y/N)"

#### 3. Disable Cron

```python
call_tool("gobby-cron", "toggle_cron_job", {"job_id": "<cron_job_id>"})
```

#### 4. Kill Agents (Scoped)

Only kill agents associated with this orchestration's epic:

```python
# Get subtask IDs for this epic
subtasks = call_tool("gobby-tasks", "list_tasks", {"parent_task_id": "<epic_id>"})
subtask_ids = [t.id for t in subtasks]

# List running agents
agents = call_tool("gobby-agents", "list_running_agents", {})

# Kill only agents working on this epic's tasks
for agent in agents.agents:
    if agent.task_id in subtask_ids or agent.task_id == epic_id:
        call_tool("gobby-agents", "kill_agent", {"run_id": agent.run_id})
```

Do NOT kill agents belonging to other orchestrations or unrelated work.

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
    "changes_summary": "Orchestration '{slug}' complete. All tasks processed."
})
```

If close fails (e.g., subtasks still open), report and skip.

#### 7. Delete Cron Job

```python
call_tool("gobby-cron", "delete_cron_job", {"job_id": "<cron_job_id>"})
```

#### 8. Update State File

Update `.gobby/orchestrations/{slug}.md`:
- Set `Status: COMPLETE`
- Add final report section with:
  - Task counts by status
  - Total cron runs (from `list_cron_runs`)
  - Duration (started_at to now)
  - Any remaining open/stuck tasks

#### 9. Final Report

```
============================================
  Orchestration Complete: {slug}
============================================
Epic:           #{N} "{title}"
Duration:       {hours}h {minutes}m
Cron Ticks:     {count}
Tasks Closed:   {N}/{total}
Tasks Remaining: {N} (list refs if any)
Isolation:      merged and cleaned up
============================================
```

---

## State File Template

Create as `.gobby/orchestrations/{slug}.md` during Phase 2:

```markdown
# Orchestration: {slug}

> Configuration record for this orchestration.
> Used by `/gobby orchestrate` subcommands to manage lifecycle.

## Status: RUNNING

## Configuration
- **Slug**: {slug}
- **Epic**: #{N} "{title}"
- **Epic ID**: {uuid}
- **Cron Job ID**: {cron_job_id}
- **Current Branch**: {branch}
- **Merge Target**: {branch}
- **Dev Agent**: {agent_name} → {provider} / {model} ({mode})
- **QA Agent**: {agent_name} → {provider} / {model} ({mode})
- **Agent Timeout**: {N}s
- **Cron Interval**: {N}s
- **Max Concurrent**: {N}
- **Isolation**: {worktree|clone|none}
- **Started At**: {ISO timestamp}
```

---

## Debugging Commands

Useful commands for checking orchestration progress:

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

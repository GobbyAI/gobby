---
name: task-creation
description: "How to create tasks effectively via gobby-tasks MCP. Covers task types, priorities, when to use each, and writing effective validation criteria."
category: core
metadata:
  gobby:
    audience: all
---

# Task Creation

Create and manage tasks via `call_tool("gobby-tasks", ...)`. Fetch schemas with `get_tool_schema("gobby-tasks", "create_task")` before first use.

> **Note:** TodoWrite is disabled by Gobby rules. Native task tools (TaskCreate, TaskUpdate, TaskGet, TaskList) are
> available for sub-step tracking once a Gobby task is claimed. The gobby-task tracks the deliverable (what gets
> committed, reviewed, and closed); native task tools track sub-steps along the way.

## Creating a Task

```python
# Bug — high priority, code category
call_tool("gobby-tasks", "create_task", {
    "title": "Fix null pointer in session cleanup",
    "category": "code",
    "task_type": "bug",
    "priority": 1,
    "validation_criteria": "SessionManager.cleanup() no longer raises on empty sessions"
}, session_id="#2333")

# Feature — create and claim in one call
call_tool("gobby-tasks", "create_task", {
    "title": "Add webhook support for pipeline completion",
    "category": "code",
    "task_type": "feature",
    "claim": true,
    "validation_criteria": "POST /webhooks endpoint accepts pipeline_completed events and returns 201"
}, session_id="#2333")
```

`session_id` is a parameter of `call_tool`, not the inner tool. Pass your Gobby session ref (e.g. `#2333`).

---

## Task Types — When to Use Each

| Type | When | Priority | Example |
|------|------|----------|---------|
| `bug` | Something is broken | 1 (high) | "Fix null pointer in session cleanup" |
| `feature` | New capability | 2 (medium) | "Add webhook support for pipeline completion" |
| `epic` | Large feature needing subtasks | 2 (medium) | "Implement knowledge graph integration" |
| `chore` | Maintenance, cleanup | 3 (low) | "Update dependencies to latest versions" |
| `refactor` | Restructure without behavior change | 3 (low) | "Extract validation logic from pipeline executor" |
| `task` | General work that doesn't fit above | 2 (medium) | "Investigate memory leak in long sessions" |

**Nitpicks** — minor cleanup, typos, style fixes — use type `chore` with priority 4 (backlog) and label `["nitpick"]`.

### Priority Scale

| Priority | Meaning | Typical types |
|----------|---------|---------------|
| 1 | High — fix now | Bugs, blockers |
| 2 | Medium — next up | Features, epics |
| 3 | Low — when convenient | Chores, refactors |
| 4 | Backlog — someday | Nitpicks, nice-to-haves |

## Required Fields

| Field | When | Notes |
|-------|------|-------|
| `title` | Always | Imperative form: "Fix X", "Add Y" |
| `category` | Always | Determines validation behavior |
| `validation_criteria` | `category=code` | Creation fails without it |

> **Note:** Pass `session_id` to `call_tool` as a sibling parameter (not inside `arguments`).
> Hooks-based sessions (Claude Code, Codex hooks) may propagate it automatically, but always pass it explicitly to be safe.

### Categories

| Category | Use for |
|----------|---------|
| `code` | Implementation — requires `validation_criteria` |
| `config` | Configuration file changes |
| `docs` | Documentation |
| `test` | Test writing |
| `research` | Investigation, no code output expected |
| `planning` | Design, architecture |
| `manual` | Requires manual verification |

## Writing Effective Validation Criteria

Validation criteria are checked against the diff when closing a task. Write them so an independent reviewer can verify completion.

**Good:** "The `close_task` tests in `test_tasks_coverage.py` pass. `LocalSessionManager` is patched at the correct import path in both tests."

**Bad:** "Tests pass." / "It works." / "Bug is fixed."

Criteria should be:
- **Observable** — can be verified by reading code or running tests
- **Specific** — names files, functions, or behaviors
- **Complete** — covers all acceptance conditions, not just the happy path

## Labels

Use labels for cross-cutting concerns:

| Label | When |
|-------|------|
| `nitpick` | Minor cleanup, typos, style |
| `refactor` | Code restructuring |
| `security` | Security-related work |
| `performance` | Performance improvements |

## Plan Mode

Do **not** create tasks during plan mode unless the user explicitly asks you to. Plan mode is for designing an approach, not organizing work into tasks. Only use task tools in plan mode if the user requests it.

## Claiming

Auto-claim at creation with `claim: true` (shown above), or claim an existing task:

```python
call_tool("gobby-tasks", "claim_task", {"task_id": "#42"}, session_id="#2333")
```

## Error Triage

When you encounter bugs unrelated to your current task, create a task and continue:

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Fix pre-existing lint warning in session.py",
    "category": "code",
    "task_type": "bug",
    "priority": 3,
    "validation_criteria": "ruff check on session.py passes with no warnings"
}, session_id="#2333")
```

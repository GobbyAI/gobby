# Orchestrator Test Battery

A structured test battery for verifying the orchestrator pipeline end-to-end. Use this as a reference document or invoke it interactively via the `/gobby test-battery` skill.

---

## Prerequisites

- Gobby daemon running (`gobby status`)
- At least one LLM provider configured (Claude recommended)
- Git repository with clean working tree
- No other orchestrator pipelines running

---

## Section 1: Setup

### 1.1 Create a Plan

Create a small test plan with 2-3 code tasks and 1 docs task:

```bash
# Create plan file
cat > .gobby/plans/test-battery.md << 'EOF'
# Test Battery Feature

## Overview
A minimal feature for testing the orchestrator pipeline.

## Phase 1: Implementation

### 1.1 Create greeting module [category: code]
Target: `src/test_battery_greeting.py`
Create a simple greeting function that returns "Hello, {name}!".

### 1.2 Add greeting CLI command [category: code] (depends: 1.1)
Target: `src/test_battery_cli.py`
Add a CLI command that calls the greeting function.

### 1.3 Document the greeting feature [category: docs]
Target: `docs/test-battery-greeting.md`
Write brief documentation for the greeting module.
EOF
```

**Pass criteria:**
- [ ] Plan file created at `.gobby/plans/test-battery.md`
- [ ] Contains 2 code tasks and 1 docs task
- [ ] Dependencies specified correctly

### 1.2 Create Epic

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Test Battery Feature",
    "description": "Minimal feature for orchestrator test battery",
    "task_type": "epic",
    "session_id": "<session_id>"
})
```

**Pass criteria:**
- [ ] Epic created with valid task ID
- [ ] Task type is "epic"
- [ ] Status is "open"

---

## Section 2: Expansion

### 2.1 Run Expansion

```python
result = call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "inputs": {"task_id": "<epic_id>", "session_id": "<session_id>"}
})
# Block until expansion completes
call_tool("gobby-workflows", "wait_for_completion", {
    "execution_id": result["execution_id"],
    "timeout": 600
})
```

**Pass criteria:**
- [ ] Pipeline completes successfully (status: "completed")
- [ ] Subtasks created under the epic

### 2.2 Verify Subtasks

```python
call_tool("gobby-tasks", "list_tasks", {"parent_task_id": "<epic_id>"})
```

**Pass criteria:**
- [ ] At least 2 subtasks created
- [ ] Each has `validation_criteria`
- [ ] Each has `category` assigned
- [ ] Dependencies wired (1.2 depends on 1.1)

### 2.3 Verify TDD (if enabled)

If `enforce_tdd = true`:

**Pass criteria:**
- [ ] Code tasks have TDD sandwich ([TEST] → [IMPL] → [REFACTOR])
- [ ] Docs task has no TDD wrapper
- [ ] No duplicate test tasks

### 2.4 Verify File Annotations

```python
# Check affected files for a subtask
call_tool("gobby-tasks", "get_affected_files", {"task_id": "<subtask_id>"})
```

**Pass criteria:**
- [ ] At least one subtask has `affected_files` populated
- [ ] File paths are relative to repo root
- [ ] No overlapping files between parallel-safe tasks

---

## Section 3: Orchestrator Run

### 3.1 Start Orchestrator

```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "orchestrator",
    "inputs": {
        "session_task": "<epic_id>",
        "merge_target": "<current_branch>",
        "max_concurrent": 2,
        "max_iterations": 50
    },
    "continuation_prompt": "Orchestrator completed. Check task states and report results."
})
```

The pipeline returns after its first pass. Subsequent passes are event-driven — each agent completion triggers a fresh pass via continuation callbacks. Check `orchestration_complete` in the execution result to know when the overall work is finished.

**Pass criteria:**
- [ ] Pipeline starts (returns `execution_id`)
- [ ] First pass completes with `orchestration_complete: false`

### 3.2 Verify Worktree Creation

After first pass:

```bash
gobby worktrees list
```

**Pass criteria:**
- [ ] Exactly one worktree created for the epic
- [ ] Branch name follows `epic-{N}` pattern
- [ ] Worktree is based on the correct branch

### 3.3 Verify Developer Dispatch

```python
call_tool("gobby-agents", "list_agents", {"parent_session_id": "<pipeline_session>"})
```

**Pass criteria:**
- [ ] Developer agent(s) spawned
- [ ] Agent uses the epic worktree (not a new worktree)
- [ ] Task claimed by the agent (status: "in_progress")

### 3.4 Verify Step Workflow

Monitor a developer agent's step transitions:

**Pass criteria:**
- [ ] Agent starts in `claim` step
- [ ] After `claim_task` succeeds, transitions to `implement`
- [ ] After `mark_task_needs_review`, transitions to `terminate`
- [ ] Agent calls `kill_agent` to exit

### 3.5 Verify QA Review

After a developer completes:

**Pass criteria:**
- [ ] QA reviewer spawned for `needs_review` tasks
- [ ] QA agent can see code changes (same worktree)
- [ ] Task transitions to `review_approved` or reopened

### 3.6 Verify Merge

When all tasks are approved:

**Pass criteria:**
- [ ] Merge agent spawned
- [ ] Merge agent works in main repo (no isolation)
- [ ] Epic branch merged to target branch
- [ ] All approved tasks closed after merge

### 3.7 Standalone Task Test

Run the orchestrator with a single non-epic task to verify standalone support:

```python
# Create a standalone task
task = call_tool("gobby-tasks", "create_task", {
    "title": "Standalone test task",
    "task_type": "task",
    "category": "code",
    "validation_criteria": "Creates a test file",
    "session_id": "<session_id>"
})

# Run orchestrator on standalone task
call_tool("gobby-workflows", "run_pipeline", {
    "name": "orchestrator",
    "inputs": {
        "session_task": task["ref"],
        "merge_target": "<current_branch>",
        "max_iterations": 20
    }
})
```

**Pass criteria:**
- [ ] Orchestrator detects standalone mode (`is_standalone: true` in result)
- [ ] Developer dispatched for the task itself (not children)
- [ ] QA dispatched when task reaches `needs_review`
- [ ] Merge dispatched when task reaches `review_approved`
- [ ] Final pass returns `orchestration_complete: true`

---

## Section 4: Cleanup

### 4.1 Verify Completion

```python
call_tool("gobby-tasks", "list_tasks", {
    "parent_task_id": "<epic_id>",
    "status": "open"
})
```

**Pass criteria:**
- [ ] All subtasks closed
- [ ] Epic marked as complete
- [ ] Final execution result has `orchestration_complete: true`

### 4.2 Clean Up Test Artifacts

```bash
# Delete test files created by the battery
rm -f src/test_battery_greeting.py src/test_battery_cli.py
rm -f docs/test-battery-greeting.md
rm -f .gobby/plans/test-battery.md

# Delete the epic worktree
gobby worktrees list  # find the worktree
gobby worktrees delete <worktree_id>
```

**Pass criteria:**
- [ ] Test files removed
- [ ] Worktree cleaned up
- [ ] No dangling branches

---

## Section 5: Issue Tracking

### Discovered Issues

Record any issues found during the test battery:

| # | Severity | Description | Component | Task Created? |
|---|----------|-------------|-----------|---------------|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

### Improvement Suggestions

| # | Description | Priority |
|---|-------------|----------|
| 1 | | |
| 2 | | |

---

## Section 6: Results Summary

| Section | Tests | Pass | Fail | Skip |
|---------|-------|------|------|------|
| 1. Setup | 2 | | | |
| 2. Expansion | 4 | | | |
| 3. Orchestrator Run | 7 | | | |
| 4. Cleanup | 2 | | | |
| **Total** | **15** | | | |

### Overall Result

- [ ] **PASS** — All critical tests pass, no blocking issues
- [ ] **PASS WITH ISSUES** — Core flow works, non-critical issues found
- [ ] **FAIL** — Blocking issues prevent orchestrator from completing

### Recommendations

_(Fill in based on results)_

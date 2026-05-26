# Bug: Stop Hook Does Not Enforce Task Closure

## Summary

When an agent declares work "complete" without closing their `in_progress` task, the stop hook should block them - but it doesn't. The lifecycle workflow's `on_stop` trigger calls `require_commit_before_stop`, but this action is misnamed and doesn't actually check task status.

## Root Cause

The stop hook enforcement flow:
```
Stop event → session-lifecycle.yaml on_stop → require_commit_before_stop
```

`require_commit_before_stop` checks:
1. Is there a claimed task? (via `workflow_state.variables.get("claimed_task_id")`)
2. Are there uncommitted changes since baseline?

It does **NOT** check:
- Is the claimed task still `in_progress`?

**Key insight:** The `close_task()` validation already requires a commit - so we don't need to check commits in the stop hook. We just need to verify the task reached `closed` or `review` status.

**Result:** Agent can commit changes, say "I'm done", and stop - leaving task in `in_progress` forever.

## Affected Files

| File | Purpose |
|------|---------|
| `.gobby/workflows/lifecycle/session-lifecycle.yaml` | Lifecycle workflow with `on_stop` trigger |
| `src/gobby/workflows/task_enforcement_actions.py` | Contains `require_commit_before_stop` action (to be renamed) |

## Implementation

### 1. Rename and refactor action in `task_enforcement_actions.py`

**Rename:** `require_commit_before_stop` → `require_task_review_or_close_before_stop`

**New logic:** Simply check if the session has a claimed task that is still `in_progress`. If so, block the stop.

```python
async def require_task_review_or_close_before_stop(
    workflow_state: WorkflowState,
    db: HubDatabase,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Block stop if session has an in_progress task.

    Agents must close their task (or send to review) before stopping.
    The close_task() validation already requires a commit, so we don't
    need to check for uncommitted changes here.
    """
    claimed_task_id = workflow_state.variables.get("claimed_task_id")
    if not claimed_task_id:
        return None  # No claimed task, allow stop

    from gobby.storage.tasks import LocalTaskManager
    task_mgr = LocalTaskManager(db)

    try:
        task = task_mgr.get_task(claimed_task_id)
        if task and task.status == "in_progress":
            return {
                "decision": "block",
                "reason": f"Task {claimed_task_id} is still in_progress. "
                          f"Close it with close_task() or set to review if user intervention is needed.",
                "task_id": claimed_task_id,
                "task_status": task.status,
            }
    except Exception as e:
        logger.warning(f"Failed to check task status: {e}")
        # Allow stop if we can't check - don't block on errors

    return None  # Task is closed or review, allow stop
```

### 2. Update lifecycle workflow

**File:** `.gobby/workflows/lifecycle/session-lifecycle.yaml`

Update the `on_stop` trigger to use the new action name:

```yaml
on_stop:
  - action: require_task_review_or_close_before_stop
    when: "not variables.get('plan_mode')"
```

### 3. Update action registry

Update the action registration to use the new name.

### 4. Related: Make session_id required on close_task

Currently `session_id` is optional on `close_task`. It should be required to ensure proper tracking.

## Verification

1. **Unit test:** Add/update test case in `tests/workflows/test_task_enforcement_actions.py`
   - Test stop blocked when task is `in_progress`
   - Test stop allowed when task is `closed`
   - Test stop allowed when task is `review`
   - Test stop allowed when no claimed task

2. **Manual test:**
   - Create a task, set to `in_progress`
   - Make and commit a change
   - Try to stop without calling `close_task()`
   - Expected: Stop should be blocked with message about unclosed task

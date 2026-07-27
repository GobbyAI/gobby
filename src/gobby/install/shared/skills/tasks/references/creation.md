# Task Creation Reference

Load this reference when task type, category, priority, labels, examples, or
validation criteria need more detail.

## Types and Priorities

| Type | Use | Typical priority |
| --- | --- | --- |
| `bug` | Broken behavior | 1 |
| `feature` | New capability | 2 |
| `epic` | Large capability requiring child tasks | 2 |
| `task` | General bounded work | 2 |
| `chore` | Maintenance or minor cleanup | 3 |
| `refactor` | Structure change without behavior change | 3 |

Priority 1 is urgent, 2 is normal planned work, 3 is low priority, and 4 is
backlog. Use `task_type="chore"`, priority 4, and label `nitpick` for cosmetic
cleanup.

## Categories

| Category | Use |
| --- | --- |
| `code` | Runtime implementation; requires `implementation_domain` and validation criteria |
| `config` | Configuration changes |
| `docs` | Documentation |
| `refactor` | Code restructuring, including updates to existing tests |
| `test` | Test-only work |
| `research` | Investigation with no code deliverable |
| `planning` | Design or architecture |
| `manual` | Human verification is the deliverable |

## Validation Criteria

Write criteria so another engineer can verify the result independently:

- Observable: identifies behavior or artifacts.
- Specific: names relevant functions, files, commands, or interfaces.
- Complete: covers success and important failure/boundary behavior.

Good:

> `close_task` tests pass, a blocked conditional close stays read-only, and a
> ready conditional close reports and links the exact commit set.

Weak:

> Tests pass.

## Expanded Examples

Bug:

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Fix null handling in session cleanup",
    "category": "code",
    "implementation_domain": "backend",
    "task_type": "bug",
    "priority": 1,
    "validation_criteria": (
        "SessionManager.cleanup() handles missing sessions and focused tests pass."
    )
}, session_id="#2333")
```

Feature created and claimed:

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Add pipeline completion webhooks",
    "category": "code",
    "implementation_domain": "backend",
    "task_type": "feature",
    "priority": 2,
    "claim": True,
    "validation_criteria": (
        "POST /webhooks accepts pipeline_completed events and returns 201."
    )
}, session_id="#2333")
```

## Plan Mode and Error Triage

Planning may organize tasks through `gobby-tasks`; file edits still require a
claimed task. When an unrelated defect, warning, or failing check is encountered,
fix it within the current task when practical. Create a separate unclaimed task
only for work that genuinely needs independent architectural planning.

# Non-Work Closures

Load this reference when no repository change is needed.

Allowed reasons:

- `duplicate`
- `already_implemented`
- `wont_fix`
- `obsolete`
- `out_of_repo`

These reasons require no commit when the task has no attributed edits. The
checklist still requires criteria, a useful `changes_summary`, and the bounded
criteria review. A justified deliberate close of an escalated task is the only
review exception. After the close returns `closed=true`, call
`review_task_memories` on `gobby-memory` exactly as in the interactive close
sequence; it requires a closed task.

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#42",
    "reason": "already_implemented",
    "changes_summary": (
        "Focused tests and code inspection show the requested behavior already exists."
    ),
    "preview": True
}, session_id="#2333")
```

The `preview=true` call closes when ready. Repair a returned checklist blocker
before retrying.

# Non-Work Closures

Load this reference when no repository change is needed.

Allowed reasons:

- `duplicate`
- `already_implemented`
- `wont_fix`
- `obsolete`
- `out_of_repo`

These reasons skip commit and commit-link gates. Fresh verification and memory
review still apply, and `changes_summary` must explain the evidence supporting
the reason.

```
call_tool("gobby-tasks", "close_task", {
    "task_id": "#42",
    "reason": "already_implemented",
    "changes_summary": (
        "Focused tests and code inspection show the requested behavior already exists."
    ),
    "preview": True
}, session_id="#2333")
```

Repair preview blockers, then repeat without `preview`.

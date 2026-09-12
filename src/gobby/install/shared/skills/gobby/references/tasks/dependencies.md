# Dependencies and readiness

Load when ordering tasks, investigating blocked work, or changing dependency edges.
Discover `add_dependency`, `remove_dependency`, `get_dependency_tree`,
`check_dependency_cycles`, `list_ready_tasks`, `list_blocked_tasks`, and
`suggest_next_task` on `gobby-tasks`.

In `add_dependency(task_id, depends_on)`, `task_id` is the dependent and
`depends_on` is the blocker that must complete first. The default `blocks`
relationship affects readiness; `related` and `discovered-from` are informational.

```python
call_tool(server_name="gobby-tasks", tool_name="add_dependency", arguments={
    "task_id": "#44", "depends_on": "#42", "dep_type": "blocks",
}, session_id="#2333")
```

Inspect the dependency tree before adding edges and check cycles after changing
an ordering. Never reverse the edge to make a blocked task appear ready.
Readiness includes current-stage eligibility, unresolved external blockers,
closure/escalation, and parent-chain readiness. A parent's unfinished children
are not equivalent to an external blocker; use the task tree for completion.

On unresolved or cyclic dependencies, inspect the named upstream tasks and
repair the actual ordering or finish the blocker. Do not remove necessary edges
just to claim or dispatch work. Use event-driven waits when dependent work is
owned by another active session; arbitrary message text is not a durable wait.

Guide: [Dependencies and Ready Work](../../../../../../../../docs/guides/tasks.md#dependencies-and-ready-work).

_Last verified: 2026-09-12_

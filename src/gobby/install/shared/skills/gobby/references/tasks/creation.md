# Create and claim work

Load when creating tasks, selecting categories or labels, editing task metadata,
claiming work, or recovering claim conflicts.

Discover `create_task`, `get_task`, `update_task`, `claim_task`, `add_label`,
`remove_label`, `reopen_task`, `escalate_task`, `de_escalate_task`, and
`delete_task` on `gobby-tasks`. Use lifecycle tools rather than changing storage
or using the operator CLI. Inspect the schema before each new tool family.

Every task type except `epic` requires meaningful `validation_criteria`, including
docs, research, and planning. `category="code"` also requires
`implementation_domain` (`backend`, `frontend`, or `fullstack`). Choose the
schema's task type and category for the deliverable; priority defaults to 2.
Write observable, specific, complete criteria. Use `test: path::test_symbol`
for named tests and `file: path` for evidence artifacts.

```python
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Correct task examples",
    "category": "docs",
    "task_type": "chore",
    "validation_criteria": "Task examples match registered schemas and all links resolve.",
    "claim": True,
}, session_id="#2333")
```

Create with `claim=true` or claim existing work before edits. A successful
already-claimed response means read the task and continue; do not claim again.
One session cannot accumulate ordinary open claims. Cross-project claims are
rejected. On `TASK_CLAIM_CONFLICT`, inspect the named task and owner, finish the
existing claim or coordinate with its active owner. `force` transfers another
owner's claim; it is an explicit recovery action, not a conflict retry.

Use `update_task` for supported metadata, not `status` or `assignee`. Add/remove
ordinary labels through their tools. The `live-session` label has special root
terminal authorization: load the entire live-work topic before changing it.
Isolation changes affect future dispatch and reject conflicting existing
workspace artifacts; clean up through workspace tools before retargeting.

Escalate only with a concrete unresolved decision or blocker. De-escalation
restores the preserved stage; reopening handles closed/escalated work. Deletion
is destructive: inspect children and dependency consequences and honor the
requested scope. Use closing guidance for no-work dispositions.

Guide: [Create and Claim](../../../../../../../../docs/guides/tasks.md#create-and-claim).

_Last verified: 2026-09-12_

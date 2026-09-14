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
rejected. `TASK_CLAIM_CONFLICT` covers two different claim failures:

- `TaskAlreadyClaimedError` / foreign ownership (`claimed_by`): coordinate with
  the named owner. An authorized receiving session with claim capacity can use
  `claim_task(task_id="<task>", force=true)` to transfer that owner's claim.
- `AgentTaskClaimConflictError` / same-session accumulation (`claimed_task_id`,
  `claimed_task_ref`): your session owns the named different open task. Finish
  and close it normally. For a genuine blocker or explicitly directed recovery,
  use `escalate_task(task_id="<existing claim>", reason="<concrete reason>")`;
  escalation releases canonical ownership, freeing your claim capacity. Do not
  escalate to bypass validation, committing, or closing. Alternatively, arrange
  an authorized transfer of the existing claim to another session with capacity.
  `force=true` does not resolve same-session accumulation, including on a
  delegated claim. A create-and-claim capacity conflict creates no task.

An active pending/running agent run for the task can authorize a parent/child
ownership transfer without `force` in either direction. The receiving session
must still have claim capacity. Neither transfer route bypasses that guard.

Use `update_task` for supported metadata, not `status` or `assignee`. Add/remove
ordinary labels through their tools. The `live-session` label has special root
terminal authorization: load the entire live-work topic before changing it.
Isolation changes affect future dispatch and reject conflicting existing
workspace artifacts; clean up through workspace tools before retargeting.

Escalate only with a concrete unresolved decision or blocker, or explicitly
directed recovery. Escalation releases your canonical ownership: stop treating
the task as claimed. De-escalation normally preserves the current stage and
leaves the task unclaimed; claim it again when ready to resume and capacity is
available. Reopening handles closed/escalated work. Deletion
is destructive: inspect children and dependency consequences and honor the
requested scope. Use closing guidance for no-work dispositions.

Guide: [Create and Claim](../../../../../../../../docs/guides/tasks.md#create-and-claim).

_Last verified: 2026-09-12_

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
for named tests and `file: path` for evidence artifacts. When an
agent-authored description rests on specific source behavior, cite it as
[evidence](../code-index/evidence.md) describes. User-authored text needs no
citations, and the task tools take no citation field.

```python
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={
    "title": "Correct task examples",
    "category": "docs",
    "task_type": "chore",
    "validation_criteria": "Task examples match registered schemas and all links resolve.",
    "claim": True,
}, session_id="#2333")
```

Create with `claim=true` or claim existing work before edits. A session may hold
any number of active claims. Each successful claim selects that task for new
edits and validation commands. Call `claim_task(task_id)` on an already-owned
task to select it again; `already_claimed` confirms ownership and selection.
Other claims retain their ownership and edit history. When the selected claim
ends, remaining claims stay inactive: select one with `claim_task` before editing
or validating it.

Finish or stop native shell/agent calls before explicitly selecting another task.
Live calls stay bound to the task selected at their start, including delayed
results. Completed replay bindings retire at turn end. A selection refusal names
the running calls; create-and-claim rolls back without creating a task. If a
native child loses ownership during a transfer, restore ownership with
`claim_task` or stop that child before retrying its edits.

Codex file changes without a tool-start event use an unchanged matching turn's
selection. Unbound or unclaimed edits still enter the session dirty ledger,
without task attribution.

Paths still live-attributed to another active claim cannot be edited. Select the
owning task or finish it, and split commits by task. A later task may edit a
committed path; linking or closing the earlier commit remains allowed when the
later live attribution began after that path's commit. Git records whole seconds,
so same-second ordering is treated as unproven. Close judges validation
runs started while this task was selected, while
all edits to its owned paths count and stale earlier green evidence.

Cross-project claims are rejected. `TASK_CLAIM_CONFLICT` identifies foreign
ownership (`claimed_by`): coordinate with the named owner. An authorized
receiving session can use `claim_task(task_id="<task>", force=true)` to transfer
that claim. An active pending/running agent run for the task can authorize a
parent/child ownership transfer without `force` in either direction. Transfers
preserve every unrelated claim. Escalate only for a genuine blocker or explicitly
directed recovery, never to bypass validation, committing, or closing.

## Release a parked claim

The Orchestrator or the holder's owning Lane Manager can call
`gobby-tasks:release_task_claim(task_id, holder_session_ref, reason)` to release
a named seat's claim on an open task. Discover the schema first. The caller,
holder, and task must belong to the same project, and the holder must be on the
caller's machine. Lane Manager authority requires current running agent runs
with the same recorded lane; an ordinary agent cannot release claims.

The call checks that the named holder still owns the task and refuses dirty
task-attributed paths, naming them. An unavailable attributed checkout also
refuses release. Success makes the task unclaimed, records the releasing actor,
holder and reason in a lifecycle event, and lets another seat claim it normally.
It preserves edit attribution and unrelated task bindings.

Use this for parked claims outside the holder's current assigned task. Releasing
the assigned task is also accepted, but does not clear `assigned_task_id` or
advance its workflow step; coordinate the holder's recovery separately. This
call does not transfer activation-receipt authority.

Source: `src/gobby/mcp_proxy/tools/tasks/_claim_release.py::release_task_claim`.

## Metadata and recovery

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

_Last verified: 2026-10-17_

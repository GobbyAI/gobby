"""Task claims recorded in a session transcript.

``session_tasks`` keeps one claimed row per task, so a session that leaves a task
and later claims it again leaves no trace of the return in the database. The
transcript still holds every claim call, and close evidence uses them to bound a
linked session's work to the stretches it spent on the task (#23385).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gobby.sessions.transcripts.tool_activity import canonical_tool_name
from gobby.tasks.transcript_evidence_models import TranscriptTaskClaim
from gobby.tasks.transcript_evidence_snapshots import PendingTool
from gobby.tasks.transcript_outcomes import _walk_values

_CLAIM_TASK = "mcp gobby-tasks:claim_task"
_CREATE_TASK = "mcp gobby-tasks:create_task"


def task_claim(pending: PendingTool, result: Any, at: datetime) -> TranscriptTaskClaim | None:
    """The task a successful ``claim_task``, or ``create_task`` with ``claim``, put the session on.

    Only an explicit ``success`` counts. Refused calls, and results that carry any
    error, are not claims. The result's own task id is preferred over the
    argument because the claim resolved it.
    """
    # Codex reports a direct MCP call as a bare tool name plus its server.
    name = f"mcp__{pending.server}__{pending.name}" if pending.server else pending.name
    tool, tool_arguments = canonical_tool_name(name, pending.arguments)
    if pending.server and tool == name:
        tool = f"mcp {pending.server}:{pending.name}"
    if tool == _CLAIM_TASK:
        key = "task_id"
        task_ref = tool_arguments.get("task_id")
    elif tool == _CREATE_TASK and tool_arguments.get("claim") is True:
        key, task_ref = "id", None
    else:
        return None
    succeeded = False
    for value in _walk_values(result):
        if not isinstance(value, dict):
            continue
        if value.get("success") is False or value.get("error"):
            return None
        warning = value.get("warning")
        if isinstance(warning, str) and warning.startswith("claim=true ignored"):
            return None
        succeeded = succeeded or value.get("success") is True
        found = value.get(key)
        if isinstance(found, str) and found:
            task_ref = found
    if not succeeded or not isinstance(task_ref, str) or not task_ref:
        return None
    return TranscriptTaskClaim(task_ref=task_ref, claimed_at=at)

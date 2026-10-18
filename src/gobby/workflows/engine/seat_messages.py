"""Durable message authority for bounded standing-seat effects.

Tool arguments select a stored message; they never supply transition facts.
This module has no mutation endpoint and does not accept variable names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage, InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError

_PREFIX = re.compile(r"^(?:LANE=\S+ )?EVENT=(\S+) TASK=(\S+)(?: |$)")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ENVELOPE_FIELDS = {
    "COMMIT",
    "SHA",
    "VERDICT",
    "REASON",
    "LANDING",
    "BRANCH",
    "RECEIPT",
    "MODE",
    "LANDED_TIP",
    "ACTIVATION",
    "PROJECT_SYNC_REQUIRED",
    "PROVENANCE",
    "RETEST",
    "LANDED_BY_SESSION_ID",
    "RUN",
    "WT",
}


@dataclass(frozen=True)
class CandidateEvent:
    message: InterSessionMessage
    task: Task
    sha: str
    fields: dict[str, str]


def result_body(output: Any) -> dict[str, Any]:
    """Unwrap the normalized MCP result, without trusting supplied tool arguments."""
    if not isinstance(output, dict):
        return {}
    body = output.get("result")
    return body if isinstance(body, dict) else output


def completion_messages(
    db: HubDatabase,
    session_id: str,
    tool: str,
    tool_input: dict[str, Any],
    tool_output: Any,
) -> list[InterSessionMessage]:
    """Load successful sends or explicitly read incoming messages from storage."""
    ids: list[str] = []
    if tool == "send_message":
        values = result_body(tool_output).get("message_ids")
        if isinstance(values, list):
            ids = [value for value in values if isinstance(value, str)]
    elif tool == "get_inter_session_message":
        value = tool_input.get("message_id")
        if isinstance(value, str):
            ids = [value]
    manager = InterSessionMessageManager(db)
    messages: list[InterSessionMessage] = []
    for message_id in ids:
        message = manager.get_message(message_id)
        if message is None or message.from_session == message.to_session:
            continue
        if tool == "send_message" and message.from_session == session_id:
            messages.append(message)
        elif tool == "get_inter_session_message" and message.to_session == session_id:
            messages.append(message)
    return messages


def candidate_event(db: HubDatabase, message: InterSessionMessage) -> CandidateEvent | None:
    """Resolve the task and full SHA from a stored protocol line.

    Only the envelope before TASK_TITLE/NOTE is parsed; prose cannot smuggle an
    EVENT or a second task/SHA into a transition.
    """
    line = message.content.partition("\n")[0]
    prefix = _PREFIX.match(line)
    if prefix is None:
        return None
    fields = {"EVENT": prefix[1], "TASK": prefix[2]}
    tail = line[prefix.end() :]
    if prefix[1] == "CANDIDATE" and tail.startswith("TASK_TITLE="):
        # The canonical lane event has a free title followed by these fixed
        # fields. Reject ambiguous repeated structure, including in prose;
        # never choose a COMMIT embedded in the free title or note.
        if re.findall(r" (RUN|WT|COMMIT)=", tail) != ["RUN", "WT", "COMMIT"]:
            return None
        match = re.fullmatch(
            r"TASK_TITLE=.*? RUN=\S* WT=\S* COMMIT=([0-9a-f]{40})(?: NOTE=.*)?", tail
        )
        if match is None:
            return None
        fields["COMMIT"] = match[1]
    else:
        for token in tail.split(" "):
            key, separator, value = token.partition("=")
            if key in {"TASK_TITLE", "NOTE"}:
                break
            if not separator or key not in _ENVELOPE_FIELDS:
                return None
            if key in fields:
                return None
            fields[key] = value
    if "SHA" in fields and "COMMIT" in fields:
        return None
    task_ref = fields.get("TASK")
    sha = fields.get("SHA") or fields.get("COMMIT")
    if not task_ref or not sha or not _SHA.fullmatch(sha):
        return None
    caller = SessionManager(db).get(message.to_session)
    if caller is None:
        return None
    tasks = LocalTaskManager(db)
    try:
        task_id = tasks.resolve_task_reference(task_ref, caller.project_id)
        task = tasks.get_task(task_id)
    except (ValueError, TaskNotFoundError):
        return None
    return CandidateEvent(message, task, sha, fields)


def is_task_coordinator(event: CandidateEvent) -> bool:
    """Withdrawal/resume comes from the owner, creator or delegator of that task."""
    return event.message.from_session in {
        event.task.claimed_by_session_id,
        event.task.created_in_session_id,
        event.task.delegated_by_session_id,
    }


def is_task_reviewer(db: HubDatabase, event: CandidateEvent) -> bool:
    """A review event must come from the task's persisted reviewing assignment."""
    from gobby.storage.session_tasks import SessionTaskManager

    return any(
        row.get("session_id") == event.message.from_session and row.get("action") == "reviewing"
        for row in SessionTaskManager(db).get_task_sessions(event.task.id)
    )

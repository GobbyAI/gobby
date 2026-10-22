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
_CANDIDATE_GRAMMAR = (
    "expected EVENT=CANDIDATE TASK=<ref> [TASK_TITLE=...] RUN= WT= COMMIT=<40-hex> [NOTE=...]"
)
_ENVELOPE_FIELDS = {
    "COMMIT",
    "SHA",
    "VERDICT",
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


class CandidateMessageError(ValueError):
    """A CANDIDATE envelope cannot safely bind a review unit."""


def _candidate_fields(line: str) -> dict[str, str]:
    tokens = line.split()
    if tokens[0].startswith("LANE="):
        tokens.pop(0)
    fields = {"EVENT": tokens.pop(0).partition("=")[2]}
    if not tokens or not tokens[0].startswith("TASK="):
        raise CandidateMessageError("missing TASK")
    fields["TASK"] = tokens.pop(0).partition("=")[2]
    if tokens and tokens[0].startswith("TASK_TITLE="):
        # The free title ends at the first required binding field. NOTE ends
        # authority even if the title has no binding fields after it.
        while tokens and not tokens[0].startswith(("RUN=", "WT=", "COMMIT=", "NOTE=")):
            tokens.pop(0)
    required = {"EVENT", "TASK", "RUN", "WT", "COMMIT"}
    for token in tokens:
        key, separator, value = token.partition("=")
        if key == "NOTE" and separator:
            break
        if not separator or not key:
            raise CandidateMessageError(f"expected KEY=value token: {token}")
        if key in required and key in fields:
            raise CandidateMessageError(f"duplicate {key}")
        fields[key] = value
    for key in ("TASK", "RUN", "WT", "COMMIT"):
        if not fields.get(key):
            raise CandidateMessageError(f"missing {key}")
    if re.fullmatch(r"[0-9a-fA-F]{40}", fields["COMMIT"]) is None:
        raise CandidateMessageError("COMMIT must be 40 hex characters")
    fields["COMMIT"] = fields["COMMIT"].lower()
    return fields


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

    CANDIDATE accepts keyed metadata after its optional free title. NOTE never
    carries authority. Other events retain their fixed envelope boundary.
    """
    line = message.content.partition("\n")[0]
    is_candidate = re.match(r"^(?:LANE=\S+\s+)?EVENT=CANDIDATE(?:\s|$)", line) is not None
    candidate_intent = re.match(r"^(?:LANE=\S+\s+)?(?:EVENT=)?CANDIDATE\b", line)
    if candidate_intent and not is_candidate:
        raise CandidateMessageError(_CANDIDATE_GRAMMAR)
    if is_candidate:
        try:
            fields = _candidate_fields(line)
        except CandidateMessageError as exc:
            raise CandidateMessageError(_CANDIDATE_GRAMMAR) from exc
    else:
        prefix = _PREFIX.match(line)
        if prefix is None:
            return None
        fields = {"EVENT": prefix[1], "TASK": prefix[2]}
        tail = line[prefix.end() :]
        for token in tail.split(" "):
            key, separator, value = token.partition("=")
            if key in {"TASK_TITLE", "NOTE"}:
                break
            if not separator or key not in _ENVELOPE_FIELDS:
                return None
            if key in fields:
                return None
            fields[key] = value
    if not is_candidate and "SHA" in fields and "COMMIT" in fields:
        return None
    task_ref = fields.get("TASK")
    sha = fields.get("COMMIT") if is_candidate else fields.get("SHA") or fields.get("COMMIT")
    if not task_ref or not sha or not _SHA.fullmatch(sha):
        if fields.get("EVENT") == "CANDIDATE_WITHDRAWN":
            raise CandidateMessageError(_CANDIDATE_GRAMMAR)
        return None
    caller = SessionManager(db).get(message.to_session)
    if caller is None:
        return None
    tasks = LocalTaskManager(db)
    try:
        task_id = tasks.resolve_task_reference(task_ref, caller.project_id)
        task = tasks.get_task(task_id)
    except (ValueError, TaskNotFoundError):
        if is_candidate:
            raise CandidateMessageError("unknown TASK") from None
        return None
    return CandidateEvent(message, task, sha, fields)


def is_task_coordinator(
    db: HubDatabase,
    event: CandidateEvent,
    reviewer_session_id: str,
) -> bool:
    """Only the unit's reviewer, root Orchestrator or owning LM controls its state."""
    from gobby.mcp_proxy.tools.tasks._claim_release import _can_release
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

    sessions = SessionManager(db)
    caller = sessions.get(event.message.from_session)
    reviewer = sessions.get(reviewer_session_id)
    if (
        caller is None
        or reviewer is None
        or caller.project_id != event.task.project_id
        or reviewer.project_id != event.task.project_id
        or caller.machine_id != reviewer.machine_id
        or caller.id == event.task.claimed_by_session_id
    ):
        return False
    if caller.id == reviewer.id:
        return is_task_reviewer(db, event)
    # Reuse the claim-release rule, including the enabled definition, root
    # Orchestrator and bound running LM run with the reviewer's recorded lane.
    return _can_release(RegistryContext(task_manager=LocalTaskManager(db)), caller, reviewer)


def is_task_reviewer(db: HubDatabase, event: CandidateEvent) -> bool:
    """A review event must come from the task's persisted reviewing assignment."""
    from gobby.storage.session_tasks import SessionTaskManager

    return any(
        row.get("session_id") == event.message.from_session and row.get("action") == "reviewing"
        for row in SessionTaskManager(db).get_task_sessions(event.task.id)
    )

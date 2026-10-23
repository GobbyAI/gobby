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


class CandidateMessageError(ValueError):
    """A consumed seat envelope cannot safely bind a review unit.

    ``field`` names the first offending field and ``canonical`` the field order
    that the event requires after EVENT and TASK.
    """

    def __init__(
        self,
        message: str,
        *,
        event: str | None = None,
        field: str | None = None,
        canonical: str | None = None,
    ) -> None:
        super().__init__(message)
        self.event = event
        self.field = field
        self.canonical = canonical


# Canonical field order after EVENT and TASK, as senders are taught. NOTE is last.
_ENVELOPE_CANONICAL: dict[str, str] = {
    "CANDIDATE": "TASK_TITLE RUN WT COMMIT NOTE",
    "CANDIDATE_WITHDRAWN": "SHA NOTE",
    "CANDIDATE_VERDICT": "SHA VERDICT TASK_TITLE NOTE",
    "LANDED": (
        "SHA BRANCH RECEIPT MODE LANDED_TIP ACTIVATION "
        "PROJECT_SYNC_REQUIRED PROVENANCE RETEST TASK_TITLE NOTE"
    ),
    "ALREADY_LANDED": "SHA RECEIPT LANDED_BY_SESSION_ID TASK_TITLE NOTE",
    "REVIEW_COMPLETE": "SHA VERDICT LANDING NOTE",
}
# Ordering the receiver depends on: ranks must not decrease, and unlisted keys are
# unordered. TASK_TITLE is free text, so a binding field must precede it, except on
# CANDIDATE, where RUN, WT and COMMIT follow the title. Fields after the title are
# still read; they are never silently dropped.
_ENVELOPE_RANKS: dict[str, tuple[tuple[str, ...], ...]] = {
    "CANDIDATE": (("TASK_TITLE",), ("RUN", "WT", "COMMIT")),
    "CANDIDATE_WITHDRAWN": (("SHA",),),
    "CANDIDATE_VERDICT": (("SHA",), ("VERDICT",), ("TASK_TITLE",)),
    "LANDED": (("SHA",), ("TASK_TITLE",)),
    "ALREADY_LANDED": (("SHA",), ("TASK_TITLE",)),
    "REVIEW_COMPLETE": (("SHA",), ("VERDICT",)),
}
# Fields that must be non-empty. TASK is required for every event.
_ENVELOPE_REQUIRED: dict[str, tuple[str, ...]] = {
    "CANDIDATE": ("RUN", "WT", "COMMIT"),
    "CANDIDATE_WITHDRAWN": ("SHA",),
    "CANDIDATE_VERDICT": ("SHA", "VERDICT"),
    "LANDED": ("SHA",),
    "ALREADY_LANDED": ("SHA",),
    "REVIEW_COMPLETE": ("SHA",),
}


def _rejected(event: str, field: str, problem: str) -> CandidateMessageError:
    canonical = _ENVELOPE_CANONICAL[event]
    return CandidateMessageError(
        f"EVENT={event} {field} {problem}; canonical order after EVENT and TASK: {canonical}",
        event=event,
        field=field,
        canonical=canonical,
    )


def envelope_fields(text: str) -> dict[str, str] | None:
    """Parse the first line of a consumed seat envelope.

    This is the single grammar that send_message enforces before storing and that
    candidate_event reads. It returns None when the line is not an envelope this
    module consumes. Otherwise it raises CandidateMessageError naming the first
    field that breaks the canonical order.
    """
    line = text.partition("\n")[0]
    tokens = [(match.start(), match.group()) for match in re.finditer(r"\S+", line)]
    index = 0
    if tokens and tokens[0][1].startswith("LANE=") and len(tokens[0][1]) > len("LANE="):
        index = 1
    if index >= len(tokens) or not tokens[index][1].startswith("EVENT="):
        return None
    event = tokens[index][1].removeprefix("EVENT=")
    if event not in _ENVELOPE_RANKS:
        return None
    fields = {"EVENT": event}
    index += 1
    if index >= len(tokens) or not tokens[index][1].startswith("TASK="):
        raise _rejected(event, "TASK", "must follow EVENT")
    fields["TASK"] = tokens[index][1].removeprefix("TASK=")
    rank_of = {key: rank for rank, group in enumerate(_ENVELOPE_RANKS[event]) for key in group}
    # Title words may contain '=', so only the keys that can follow the title end it.
    # CANDIDATE keeps its historical boundary; other envelopes end the title at any field.
    title_ends = set(rank_of) if event == "CANDIDATE" else set(rank_of) | _ENVELOPE_FIELDS
    rank = -1
    last_key = "TASK"
    in_title = False
    for start, token in tokens[index + 1 :]:
        key, separator, value = token.partition("=")
        if separator and key == "NOTE":
            fields["NOTE"] = line[start + len("NOTE=") :]
            break
        if in_title and not (separator and key in title_ends):
            continue
        if not separator or not key:
            raise _rejected(event, last_key, f"value must be one token, found {token!r}")
        if key in fields:
            raise _rejected(event, key, "appears twice")
        if key in rank_of:
            if rank_of[key] < rank:
                raise _rejected(event, key, "is out of canonical order")
            rank = rank_of[key]
        elif event != "CANDIDATE" and key not in _ENVELOPE_FIELDS | {"TASK_TITLE"}:
            # CANDIDATE alone accepts extra keyed metadata.
            raise _rejected(event, key, "is not a field of this event")
        in_title = key == "TASK_TITLE"
        fields[key] = value
        last_key = key
    for key in ("TASK", *_ENVELOPE_REQUIRED[event]):
        if not fields.get(key):
            raise _rejected(event, key, "is missing")
    sha_key = "COMMIT" if event == "CANDIDATE" else "SHA"
    if re.fullmatch(r"[0-9a-fA-F]{40}", fields[sha_key]) is None:
        raise _rejected(event, sha_key, "must be 40 hex characters")
    fields[sha_key] = fields[sha_key].lower()
    return fields


def _loose_envelope_fields(line: str) -> dict[str, str] | None:
    """Read retired park/resume lines with the pre-grammar reader."""
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

    Consumed envelopes are read by envelope_fields, the grammar send_message also
    enforces. CANDIDATE accepts keyed metadata after its optional free title. NOTE
    never carries authority. Retired park/resume lines keep the loose reader.
    """
    line = message.content.partition("\n")[0]
    is_candidate = re.match(r"^(?:LANE=\S+\s+)?EVENT=CANDIDATE(?:\s|$)", line) is not None
    candidate_intent = re.match(r"^(?:LANE=\S+\s+)?(?:EVENT=)?CANDIDATE\b", line)
    if candidate_intent and not is_candidate:
        raise CandidateMessageError(_CANDIDATE_GRAMMAR)
    try:
        parsed = envelope_fields(line)
    except CandidateMessageError as exc:
        if is_candidate or exc.event == "CANDIDATE_WITHDRAWN":
            raise CandidateMessageError(_CANDIDATE_GRAMMAR) from exc
        return None
    fields = parsed if parsed is not None else _loose_envelope_fields(line)
    if fields is None:
        return None
    if not is_candidate and "SHA" in fields and "COMMIT" in fields:
        return None
    task_ref = fields.get("TASK")
    sha = fields.get("COMMIT") if is_candidate else fields.get("SHA") or fields.get("COMMIT")
    if not task_ref or not sha or not _SHA.fullmatch(sha):
        if fields.get("EVENT") in {"CANDIDATE_WITHDRAWN", "CANDIDATE_RESUMED", "CANDIDATE_PARKED"}:
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
    *,
    parked_candidates: list[dict[str, Any]],
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
        return is_task_reviewer(db, event) or any(
            record.get("task") == event.task.id and record.get("sha") == event.sha
            for record in parked_candidates
        )
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

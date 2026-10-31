"""The claim hand-off chain: a seat's own successful hand-off message moves the task claim.

The engine runs ``hand_off_claim`` on the sender's successful ``send_message``. The claim moves
only while the sender still holds it, and only to a seat of the sender's team that the message
addresses and whose definition takes the next step. A claim never moves to a session that made
the work under review. When no addressed seat can take it, the task records
``claim:handoff_pending`` and the claim stays where it is.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from gobby.agents.team_lifecycle import seat_team
from gobby.mcp_proxy.tools.tasks._claim_release import ClaimTransferRefused, transfer_task_claim
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_claim import record_claimant_state
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER, SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks.step_records import (
    TaskStepRecord,
    approved_task_step,
    fixer_round,
    list_task_step_records,
    maker_sessions,
)
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.engine.seat_messages import (
    CandidateEvent,
    CandidateMessageError,
    candidate_event,
    completion_messages,
)

logger = logging.getLogger(__name__)

HOPS = ("review", "next_open", "maker")
_MAKER_STEPS = frozenset({"test", "implementation"})


class HandOff(TypedDict):
    handed_to: str | None


def _nobody() -> HandOff:
    return {"handed_to": None}


def _matches(hop: str, event: CandidateEvent) -> bool:
    """Whether the sent line is the protocol event this hop moves the claim for."""
    fields = event.fields
    if hop == "review":
        return fields.get("EVENT") == "CANDIDATE"
    verdict = "LAND" if hop == "next_open" else "BOUNCE"
    return fields.get("EVENT") == "CANDIDATE_VERDICT" and fields.get("VERDICT") == verdict


def _hop_target(
    hop: str, task: Task, records: list[TaskStepRecord], event: CandidateEvent
) -> tuple[str | None, str | None]:
    """The step the claim moves toward, or the maker a BOUNCE returns it to."""
    if hop == "review":
        return "review", None
    if hop == "next_open":
        # A LAND opens the implementation until its approval, then the closing.
        opened = approved_task_step(task, records, "implementation")
        return ("closing" if opened else "implementation"), None
    named = [
        record
        for record in records
        if event.sha is not None and record.step in _MAKER_STEPS and record.commit_sha == event.sha
    ]
    authors = named or [record for record in records if record.step == "implementation"]
    return None, (authors[-1].session_id if authors else None)


def _on_team(db: HubDatabase, session_id: str, team_id: str) -> bool:
    team = seat_team(db, session_id)
    return team is not None and team.team_id == team_id


def _addressed_seats(
    db: HubDatabase,
    recipients: list[str],
    sender_id: str,
    team_id: str,
    step: str | None,
    maker: str | None,
) -> list[Session]:
    """The addressed seats of the team that take ``step``, or the maker when a BOUNCE returns."""
    sessions = SessionManager(db)
    seats: list[Session] = []
    for recipient in dict.fromkeys(recipients):
        session = sessions.get(recipient)
        if session is None or recipient == sender_id:
            continue
        if step is None:
            takes = recipient == maker
        else:
            definition = session_agent_definition(db, session)
            takes = definition is not None and step in definition[0].task_step_writes
        if takes and _on_team(db, recipient, team_id):
            seats.append(session)
    return seats


def _reason(event: CandidateEvent) -> str:
    kinds = [f"{key}={event.fields[key]}" for key in ("EVENT", "VERDICT") if key in event.fields]
    return " ".join([*kinds, "hand-off"])


def _pending(
    db: HubDatabase, task_id: str, sender_id: str, event: CandidateEvent, why: str
) -> HandOff:
    """Record that a hand-off named no seat that can take the claim, and leave the claim put."""
    TaskLifecycleEventManager(db).record_lifecycle_event(
        task_id,
        "claim:held",
        "claim:handoff_pending",
        f"{_reason(event)} from {sender_id} did not move the claim: {why}",
        by_actor=sender_id,
    )
    return _nobody()


def _transfer(
    db: HubDatabase, sender: Session, task: Task, event: CandidateEvent, recipient: Session
) -> HandOff:
    ctx = RegistryContext(task_manager=LocalTaskManager(db))
    try:
        transfer = transfer_task_claim(
            ctx,
            task_id=task.id,
            holder=sender,
            recipient=recipient,
            project_id=task.project_id,
            actor_id=sender.id,
            reason=_reason(event),
        )
    except ClaimTransferRefused as refused:
        # A claim another session already moved is not a stall; any other refusal is.
        if refused.payload.get("error_code") == "task_claim_conflict":
            return _nobody()
        return _pending(db, task.id, sender.id, event, str(refused.payload.get("error")))
    except (TaskNotFoundError, ValueError) as exc:
        return _pending(db, task.id, sender.id, event, str(exc))
    try:
        record_claimant_state(ctx, recipient.id, transfer.task)
    except Exception as exc:
        # The claim has moved and committed; claimant state is best-effort, as in claim_task.
        logger.warning("Claimant state after hand-off to %s failed: %s", recipient.id, exc)
    return {"handed_to": recipient.id}


def hand_off_claim(
    db: HubDatabase,
    session_id: str,
    hop: str,
    mcp_tool: tuple[str, str],
    tool_output: Any,
) -> HandOff:
    """Move the task claim for the sender's own successful hand-off message.

    Returns the seat that took the claim. A message that moves nothing returns None without a
    record: the sender no longer holds the claim, the message is not this hop's event, or the
    second rejection leaves the reviewer to fix the work it holds.
    """
    sender = SessionManager(db).get(session_id)
    team = seat_team(db, session_id) if sender is not None else None
    if sender is None or team is None or hop not in HOPS:
        return _nobody()
    addressed: list[tuple[CandidateEvent, str]] = []
    for message in completion_messages(db, session_id, mcp_tool[1], {}, tool_output):
        try:
            event = candidate_event(db, message)
        except CandidateMessageError:
            continue
        if event is not None and _matches(hop, event):
            addressed.append((event, message.to_session))
    if not addressed:
        return _nobody()
    event = addressed[0][0]
    task = event.task
    if task.claimed_by_session_id != session_id or task.project_id != sender.project_id:
        return _nobody()
    records = list_task_step_records(db, task.id)
    if hop == "maker" and fixer_round(records, task.review_epoch):
        return _nobody()
    step, maker = _hop_target(hop, task, records, event)
    seats = _addressed_seats(db, [to for _, to in addressed], session_id, team.team_id, step, maker)
    if not seats:
        return _nobody()
    # A seat that made the work never takes review, and never closes the task it made.
    excluded = maker_sessions(records) if step in ("review", "closing") else set()
    ready = [
        seat
        for seat in seats
        if seat.id not in excluded and seat.status in LIVE_SESSION_STATUS_ORDER
    ]
    if not ready:
        return _pending(db, task.id, session_id, event, "no addressed live seat can take it")
    return _transfer(db, sender, task, event, ready[0])

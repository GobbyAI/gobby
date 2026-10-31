"""The claim hand-off chain: a seat's own successful hand-off message moves the task claim.

The engine runs ``hand_off_claim`` on the sender's successful ``send_message``. The claim moves
only while the sender still holds it, and only to a live seat of the sender's team that takes the
next step. An addressed seat takes it first. With none addressed and live, a live seat of the team
that takes the step takes it, and the event is copied to that seat. A claim never moves to a
session that made the work under review. When no live seat can take it, the task records
``claim:handoff_pending`` and the claim stays where it is. The team's Dev Manager is asked to staff
the seat; the task escalates to Triage when no Dev Manager is live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypedDict

from gobby.agents.team_lifecycle import TeamBinding, seat_team
from gobby.mcp_proxy.tools.tasks._claim_release import ClaimTransferRefused, transfer_task_claim
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_claim import record_claimant_state
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER, SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.team_records import ADMISSION_STEP
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


@dataclass(frozen=True)
class _Hop:
    """One hand-off in flight: who sends it, for which team, task and event, toward which step."""

    db: HubDatabase
    sender: Session
    team: TeamBinding
    task: Task
    event: CandidateEvent
    # The step the claim moves toward; None when a BOUNCE returns it to its maker.
    step: str | None


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


def _live(seat: Session) -> bool:
    return seat.status in LIVE_SESSION_STATUS_ORDER


def _takes(db: HubDatabase, seat: Session, step: str | None, maker: str | None) -> bool:
    """Whether a seat takes the step, or is the maker a BOUNCE returns the claim to."""
    if step is None:
        return seat.id == maker
    definition = session_agent_definition(db, seat)
    return definition is not None and step in definition[0].task_step_writes


def _on_team(db: HubDatabase, session_id: str, team_id: str) -> bool:
    team = seat_team(db, session_id)
    return team is not None and team.team_id == team_id


def _team_seats(db: HubDatabase, team: TeamBinding) -> list[Session]:
    """Every session the team's runbook launched, the Dev Manager included, in launch order."""
    executions = LocalPipelineExecutionManager(db, team.project_id)
    runs = LocalAgentRunManager(db)
    sessions = SessionManager(db)
    seats: list[Session] = []
    rows = db.fetchall(
        "SELECT execution_id FROM step_executions WHERE step_id = %s "
        "AND output_json #>> '{team,team_id}' = %s ORDER BY id",
        (ADMISSION_STEP, team.team_id),
    )
    for row in rows:
        execution = executions.get_execution(str(row["execution_id"]))
        if execution is None or execution.session_id is None:
            continue
        for run in runs.list_all_by_parent(execution.session_id, limit=None):
            seat = sessions.get(run.child_session_id) if run.child_session_id else None
            if seat is not None:
                seats.append(seat)
    return seats


def _team_manager(db: HubDatabase, team: TeamBinding, seats: list[Session]) -> Session | None:
    """The team's bound Dev Manager, found among its seats by its declared authority."""
    from gobby.agents.team_dispatch import _is_bound_dev_manager

    return next(
        (seat for seat in seats if _is_bound_dev_manager(db, seat.id, team.task_id)),
        None,
    )


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
        if _takes(db, session, step, maker) and _on_team(db, recipient, team_id):
            seats.append(session)
    return seats


def _roster_takers(db: HubDatabase, hop: _Hop, maker: str | None) -> list[Session]:
    """The team's other seats that take the hop's step, when no addressed seat does."""
    return [
        seat
        for seat in _team_seats(db, hop.team)
        if seat.id != hop.sender.id and _takes(db, seat, hop.step, maker)
    ]


def _reason(event: CandidateEvent) -> str:
    kinds = [f"{key}={event.fields[key]}" for key in ("EVENT", "VERDICT") if key in event.fields]
    return " ".join([*kinds, "hand-off"])


def _staff(hop: _Hop, why: str) -> None:
    """Ask the team's Dev Manager to staff the seat, or escalate the task when none is live."""
    db = hop.db
    step = hop.step or "maker"
    manager = _team_manager(db, hop.team, _team_seats(db, hop.team))
    if manager is not None and _live(manager):
        InterSessionMessageManager(db).create_message(
            hop.sender.id,
            manager.id,
            f"EVENT=HANDOFF_PENDING TASK=#{hop.task.seq_num} STEP={step} "
            f"FROM={hop.sender.id} NOTE={why}",
        )
        return
    LocalTaskManager(db).escalate_task(
        hop.task.id,
        f"claim hand-off to {step} has no live seat and no live Dev Manager: {why}",
    )


def _pending(hop: _Hop, why: str) -> HandOff:
    """Record that a hand-off named no seat that can take the claim, and leave the claim put."""
    TaskLifecycleEventManager(hop.db).record_lifecycle_event(
        hop.task.id,
        "claim:held",
        "claim:handoff_pending",
        f"{_reason(hop.event)} from {hop.sender.id} did not move the claim: {why}",
        by_actor=hop.sender.id,
    )
    _staff(hop, why)
    return _nobody()


def _transfer(hop: _Hop, recipient: Session) -> HandOff:
    ctx = RegistryContext(task_manager=LocalTaskManager(hop.db))
    try:
        transfer = transfer_task_claim(
            ctx,
            task_id=hop.task.id,
            holder=hop.sender,
            recipient=recipient,
            project_id=hop.task.project_id,
            actor_id=hop.sender.id,
            reason=_reason(hop.event),
        )
    except ClaimTransferRefused as refused:
        # A claim another session already moved is not a stall; any other refusal is.
        if refused.payload.get("error_code") == "task_claim_conflict":
            return _nobody()
        return _pending(hop, str(refused.payload.get("error")))
    except (TaskNotFoundError, ValueError) as exc:
        return _pending(hop, str(exc))
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

    Returns ``{"handed_to": <seat>}`` for the seat that took the claim, else ``handed_to`` None.
    Nothing is recorded when the sender no longer holds the claim, the message is not this hop's
    event, or the second rejection leaves the reviewer to fix the work it holds. A hand-off that
    names no live seat records ``claim:handoff_pending`` and staffs the seat.
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
    target = _Hop(db=db, sender=sender, team=team, task=task, event=event, step=step)
    # A seat that made the work never takes review, and never closes the task it made.
    excluded = maker_sessions(records) if step in ("review", "closing") else set()
    addressed_seats = _addressed_seats(
        db, [to for _, to in addressed], session_id, team.team_id, step, maker
    )
    ready = [seat for seat in addressed_seats if seat.id not in excluded and _live(seat)]
    if not ready:
        roster = [
            seat
            for seat in _roster_takers(db, target, maker)
            if seat.id not in excluded and _live(seat)
        ]
        if roster:
            # The copy is written before the claim moves, so it reads as sent by the claimant.
            InterSessionMessageManager(db).create_message(
                session_id, roster[0].id, event.message.content
            )
            ready = roster[:1]
    if not ready:
        return _pending(target, "no addressed or team seat that takes the step is live")
    return _transfer(target, ready[0])

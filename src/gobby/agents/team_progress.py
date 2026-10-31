"""Team progress: per-task stage spans and the stall flags a Dev Manager or Orchestrator watches.

A span runs from one claim boundary to the next: the first claim, each hand-off, and the close.
A span's stage is the stage its holder takes, so a hand-off opens the span of the stage its
recipient writes. The flags name what a monitoring loop must act on; a closed task has none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from gobby.agents.team_lifecycle import TeamBinding, team_binding
from gobby.agents.team_scope import EPIC_TASK_TYPE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER, SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEvent, TaskLifecycleEventManager
from gobby.storage.team_records import EPIC_MODE
from gobby.tasks.state_semantics import is_task_closed
from gobby.tasks.step_records import list_task_step_records
from gobby.workflows.agent_resolver import session_agent_definition

HANDOFF_BOUND_SECONDS = 900
STAGES = ("test", "implementation", "review", "closing")
UNASSIGNED = "unassigned"
_HANDED_OFF = "claim:handed_off"
_PENDING = "claim:handoff_pending"
_CLOSED = "closed"
_PARTIES = re.compile(r"^Handed claim from (\S+) to (\S+):")


@dataclass(frozen=True)
class StageSpan:
    stage: str
    seconds: float


@dataclass(frozen=True)
class TaskProgress:
    task_id: str
    seq_num: int | None
    stages: tuple[StageSpan, ...]
    claim_to_close_seconds: float | None
    flags: frozenset[str]


@dataclass(frozen=True)
class TeamProgress:
    team_id: str
    tasks: tuple[TaskProgress, ...]


def team_progress(db: HubDatabase, team_id: str, *, now: datetime) -> TeamProgress:
    """The stage spans and stall flags of every task a team has claimed."""
    binding = team_binding(db, team_id)
    if binding is None:
        return TeamProgress(team_id=team_id, tasks=())
    progress = (_task_progress(db, task, now) for task in _team_tasks(db, binding))
    return TeamProgress(team_id=team_id, tasks=tuple(item for item in progress if item))


def _team_tasks(db: HubDatabase, binding: TeamBinding) -> list[Task]:
    """A single team's own task, or every non-epic task below an epic team's root."""
    manager = LocalTaskManager(db)
    if binding.mode != EPIC_MODE:
        root = manager.get_task(binding.task_id)
        return [root] if root is not None else []
    rows = db.fetchall(
        """
        WITH RECURSIVE below(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = %s
            UNION
            SELECT t.id FROM tasks t JOIN below ON t.parent_task_id = below.id
        )
        SELECT id FROM tasks WHERE id IN (SELECT id FROM below) AND task_type <> %s
        """,
        (binding.task_id, EPIC_TASK_TYPE),
    )
    tasks = (manager.get_task(str(row["id"])) for row in rows)
    return [task for task in tasks if task is not None]


def _task_progress(db: HubDatabase, task: Task, now: datetime) -> TaskProgress | None:
    events = [
        event
        for event in TaskLifecycleEventManager(db).list_lifecycle_events(task.id)
        if event.to_state in (_HANDED_OFF, _PENDING, _CLOSED, "claim:started")
    ]
    claimants = _claimants(db, task.id)
    first_claims = [event for event in events if event.to_state == "claim:started"]
    if first_claims:
        start = first_claims[0].created_at
    elif claimants:
        start = claimants[0][1]
    elif events:
        start = events[0].created_at
    else:
        return None

    hand_offs = [event for event in events if event.to_state == _HANDED_OFF]
    closes = [event for event in events if event.to_state == _CLOSED]
    closed = is_task_closed(task) or bool(closes)
    end = closes[-1].created_at if closes else now

    parties = [_parties(event) for event in hand_offs]
    first_holder = parties[0][0] if parties else None
    initial = first_holder or (
        first_claims[0].by_actor
        if first_claims
        else (claimants[0][0] if claimants else task.claimed_by_session_id)
    )
    holders = [initial, *(recipient for _, recipient in parties)]
    bounds = [start, *(event.created_at for event in hand_offs), end]
    stages = tuple(
        StageSpan(stage=_stage(db, holder), seconds=_seconds(begin, finish))
        for holder, begin, finish in zip(holders, bounds[:-1], bounds[1:], strict=True)
    )

    flags: frozenset[str] = frozenset()
    if not closed:
        flags = _open_flags(db, task, events, start, now)
    return TaskProgress(
        task_id=task.id,
        seq_num=task.seq_num,
        stages=stages,
        claim_to_close_seconds=_seconds(start, end) if closed else None,
        flags=flags,
    )


def _open_flags(
    db: HubDatabase,
    task: Task,
    events: list[TaskLifecycleEvent],
    start: datetime,
    now: datetime,
) -> frozenset[str]:
    """The stall flags on an open task: a stuck hand-off, a dead holder, or an idle live holder."""
    flags: set[str] = set()
    chain = [event for event in events if event.to_state in (_HANDED_OFF, _PENDING)]
    pending = bool(chain) and chain[-1].to_state == _PENDING
    hand_offs = [event for event in chain if event.to_state == _HANDED_OFF]
    last = hand_offs[-1] if hand_offs else None
    if pending:
        flags.add("handoff_pending")

    holder_id = task.claimed_by_session_id
    holder = SessionManager(db).get(holder_id) if holder_id else None
    holder_live = holder is not None and holder.status in LIVE_SESSION_STATUS_ORDER
    if holder_id and not holder_live:
        flags.add("holder_not_live")

    records = list_task_step_records(db, task.id)
    bound = timedelta(seconds=HANDOFF_BOUND_SECONDS)
    if last is not None and not pending and now - last.created_at > bound:
        recipient = _parties(last)[1]
        acknowledged = any(
            record.session_id == recipient and record.created_at > last.created_at
            for record in records
        )
        if not acknowledged:
            flags.add("handoff_unacknowledged")

    if holder_live and not pending and "handoff_unacknowledged" not in flags:
        since = last.created_at if last is not None else start
        activity = [record.created_at for record in records if record.session_id == holder_id]
        if now - max([since, *activity]) > bound:
            flags.add("idle_on_open_work")
    return frozenset(flags)


def _claimants(db: HubDatabase, task_id: str) -> list[tuple[str, datetime]]:
    """Every session that claimed the task, oldest claim first."""
    rows = db.fetchall(
        "SELECT session_id::text AS session_id, created_at FROM session_tasks "
        "WHERE task_id = %s AND action = 'claimed' ORDER BY created_at",
        (task_id,),
    )
    return [(str(row["session_id"]), row["created_at"]) for row in rows]


def _parties(event: TaskLifecycleEvent) -> tuple[str | None, str | None]:
    """The (holder, recipient) a hand-off event's reason names, or (None, None)."""
    match = _PARTIES.match(event.reason)
    return (match.group(1), match.group(2)) if match else (None, None)


def _stage(db: HubDatabase, session_id: str | None) -> str:
    """The first stage the session's definition writes, in stage order."""
    session: Session | None = SessionManager(db).get(session_id) if session_id else None
    definition = session_agent_definition(db, session) if session is not None else None
    if definition is None:
        return UNASSIGNED
    writes = definition[0].task_step_writes
    return next((stage for stage in STAGES if stage in writes), UNASSIGNED)


def _seconds(begin: datetime, finish: datetime) -> float:
    return max(0.0, (finish - begin).total_seconds())

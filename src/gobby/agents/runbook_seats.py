"""Runbook seat guard: the read-only admission check a runbook runs before it launches seats.

A seat is the ``(workspace_id, canonical title)`` key placement holds. The guard is the
runbook's visible first refusal; seat-level atomicity stays with placement's ``seat_live``
and each launch's ``reserve_agent_slot``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import truncate_title
from gobby.storage.workspaces import WorkspaceManager
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

PIPELINE_CHILD_PREFIX = "pipeline-"
LIVE_EXECUTION_STATUSES = (
    ExecutionStatus.PENDING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_APPROVAL,
)
READ_BOUND = 1000
"""Rows read per status. A full page refuses as truncated, so the bound never caps silently."""

_SeatKey = tuple[str, str]


class RunbookSeatRefusal(Exception):
    """The guard refuses the runbook; the message names each held seat and its holder."""


@dataclass(frozen=True)
class CatalogueSeat:
    name: str
    title: str
    agent: str


@dataclass(frozen=True)
class AdmittedSeats:
    workspace_id: str
    seats: tuple[CatalogueSeat, ...]
    free_slots: int


@dataclass(frozen=True)
class RunbookSeatStores:
    """The storage the guard reads; ``free_slots`` maps a project id to its free agent slots."""

    sessions: SessionManager
    executions: Callable[[str], LocalPipelineExecutionManager]
    runs: LocalAgentRunManager
    definitions: AgentDefinitionManager
    workspaces: WorkspaceManager
    free_slots: Callable[[str], int]


def check_runbook_seats(
    stores: RunbookSeatStores,
    *,
    caller_session_id: str,
    workspace: str,
    requested: str,
    catalogue: Sequence[Mapping[str, Any]],
) -> AdmittedSeats:
    """Admit ``requested`` seats or raise ``RunbookSeatRefusal``.

    ``requested`` is the comma-separated seat names; each must be exactly a catalogue
    name. Checks run in order: sibling executions of the caller's runbook, active runs
    holding a requested seat, each seat's agent definition, then free agent slots. A
    storage error or a truncated read refuses with its cause.
    """
    seats = _requested_seats(requested, catalogue)
    try:
        return _admit(stores, seats, caller_session_id=caller_session_id, workspace=workspace)
    except RunbookSeatRefusal:
        raise
    except Exception as exc:
        raise RunbookSeatRefusal(f"seat lookup failed: {exc}") from exc


def _requested_seats(
    requested: str, catalogue: Sequence[Mapping[str, Any]]
) -> tuple[CatalogueSeat, ...]:
    by_name: dict[str, CatalogueSeat] = {}
    for entry in catalogue:
        seat = _catalogue_seat(entry)
        if seat.name in by_name:
            raise RunbookSeatRefusal(f"catalogue lists seat '{seat.name}' twice")
        by_name[seat.name] = seat
    if not requested:
        raise RunbookSeatRefusal("no seats requested")
    seats: list[CatalogueSeat] = []
    for name in requested.split(","):
        if not name:
            raise RunbookSeatRefusal(f"requested seats {requested!r} include an empty name")
        if name != name.strip():
            raise RunbookSeatRefusal(f"seat '{name}' is padded with whitespace")
        if name not in by_name:
            raise RunbookSeatRefusal(f"seat '{name}' is not in the catalogue")
        if by_name[name] in seats:
            raise RunbookSeatRefusal(f"seat '{name}' is requested twice")
        seats.append(by_name[name])
    titled: dict[str, CatalogueSeat] = {}
    for seat in seats:
        other = titled.setdefault(_canonical(seat.title), seat)
        if other is not seat:
            raise RunbookSeatRefusal(f"seats '{other.name}' and '{seat.name}' share one seat title")
    return tuple(seats)


def _catalogue_seat(entry: Mapping[str, Any]) -> CatalogueSeat:
    fields = [entry.get(field) for field in ("name", "title", "agent")]
    values = [value for value in fields if isinstance(value, str) and value]
    if len(values) != len(fields):
        raise RunbookSeatRefusal(
            f"catalogue entry {dict(entry)!r} needs a non-empty name, title and agent"
        )
    name, title, agent = values
    return CatalogueSeat(name=name, title=title, agent=agent)


def _canonical(title: str) -> str:
    return truncate_title(title) or title


def _admit(
    stores: RunbookSeatStores,
    seats: tuple[CatalogueSeat, ...],
    *,
    caller_session_id: str,
    workspace: str,
) -> AdmittedSeats:
    session = stores.sessions.get(caller_session_id)
    if (
        session is None
        or session.source != "pipeline"
        or not session.external_id.startswith(PIPELINE_CHILD_PREFIX)
    ):
        raise RunbookSeatRefusal(
            f"caller session {caller_session_id} is not a pipeline child session"
        )
    project_id = session.project_id
    executions = stores.executions(project_id)
    execution_id = session.external_id.removeprefix(PIPELINE_CHILD_PREFIX)
    execution = executions.get_execution(execution_id)
    if execution is None:
        raise RunbookSeatRefusal(f"pipeline execution {execution_id} is not in this project")
    workspace_id = _workspace_id(stores.workspaces, workspace)
    keys = {(workspace_id, _canonical(seat.title)): seat for seat in seats}

    _refuse_live_siblings(executions, execution)
    _refuse_held_seats(stores.runs, project_id, keys)
    _refuse_unresolved_agents(stores.definitions, project_id, seats)
    free_slots = stores.free_slots(project_id)
    if free_slots < len(seats):
        raise RunbookSeatRefusal(f"only {free_slots} free agent slot(s) for {len(seats)} seats")
    return AdmittedSeats(workspace_id=workspace_id, seats=seats, free_slots=free_slots)


def _workspace_id(workspaces: WorkspaceManager, workspace: str) -> str:
    try:
        return workspaces.resolve_reference(workspace).workspace.id
    except (LookupError, ValueError) as exc:
        raise RunbookSeatRefusal(f"workspace '{workspace}' does not resolve: {exc}") from exc


def _refuse_truncated(rows: Sequence[object], what: str) -> None:
    if len(rows) >= READ_BOUND:
        raise RunbookSeatRefusal(f"the {what} read is truncated at {READ_BOUND} rows")


def _refuse_live_siblings(
    executions: LocalPipelineExecutionManager, execution: PipelineExecution
) -> None:
    name = execution.pipeline_name
    siblings: list[str] = []
    for status in LIVE_EXECUTION_STATUSES:
        rows = executions.list_executions(status=status, pipeline_name=name, limit=READ_BOUND)
        _refuse_truncated(rows, f"{status.value} '{name}' execution")
        siblings.extend(row.id for row in rows if row.id != execution.id)
    if siblings:
        raise RunbookSeatRefusal(f"another '{name}' execution is live: {', '.join(siblings)}")


def _refuse_held_seats(
    runs: LocalAgentRunManager, project_id: str, keys: Mapping[_SeatKey, CatalogueSeat]
) -> None:
    held: list[str] = []
    for status in ACTIVE_AGENT_RUN_STATUSES:
        rows = runs.list_by_status(status, limit=READ_BOUND, project_id=project_id)
        _refuse_truncated(rows, f"{status} agent run")
        for run in rows:
            key = _run_seat(run)
            seat = keys.get(key) if key is not None else None
            if seat is not None:
                held.append(f"seat '{seat.name}' is held by {status} run {run.id}")
    if held:
        raise RunbookSeatRefusal("; ".join(held))


def _run_seat(run: AgentRun) -> _SeatKey | None:
    placement = (run.resume_metadata_json or {}).get("placement")
    if not isinstance(placement, Mapping):
        return None
    workspace_id, title = placement.get("workspace_id"), placement.get("title")
    if not (isinstance(workspace_id, str) and isinstance(title, str) and title):
        return None
    return workspace_id, _canonical(title)


def _refuse_unresolved_agents(
    definitions: AgentDefinitionManager, project_id: str, seats: Sequence[CatalogueSeat]
) -> None:
    unresolved: list[str] = []
    for seat in seats:
        row = definitions.get_by_name(seat.agent, project_id=project_id)
        if row is None:
            unresolved.append(
                f"seat '{seat.name}' agent definition '{seat.agent}' is not installed"
            )
        elif not row.enabled:
            unresolved.append(f"seat '{seat.name}' agent definition '{seat.agent}' is disabled")
    if unresolved:
        raise RunbookSeatRefusal("; ".join(unresolved))

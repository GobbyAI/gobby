"""Runbook seat guard: the read-only admission check a runbook runs before it launches seats.

Runbooks are fire and forget: once the panes exist and the agents start, the pipeline
completes. Besides refusing a seat whose agent definition is missing or disabled, the
guard refuses only an accidental double-fire: the same runbook still launching for the
same project in the same workspace on the same machine. Seats are told apart by
``project#session_ref``, never by pane title, so seats may share a title; runbooks
enforce no agent slots.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepStatus

PIPELINE_CHILD_PREFIX = "pipeline-"
LIVE_EXECUTION_STATUSES = (
    ExecutionStatus.PENDING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_APPROVAL,
)
READ_BOUND = 1000
"""Rows accepted per status. Each read fetches one extra row, and only that row refuses as truncated."""


class RunbookSeatRefusal(Exception):
    """The guard refuses the runbook; the message names the cause."""


@dataclass(frozen=True)
class CatalogueSeat:
    name: str
    title: str
    agent: str


@dataclass(frozen=True)
class AdmittedSeats:
    workspace_id: str
    seats: tuple[CatalogueSeat, ...]


@dataclass(frozen=True)
class RunbookSeatStores:
    """The storage the guard reads; ``executions`` builds a project's execution manager."""

    sessions: SessionManager
    executions: Callable[[str], LocalPipelineExecutionManager]
    definitions: AgentDefinitionManager
    workspaces: WorkspaceManager


def check_runbook_seats(
    stores: RunbookSeatStores,
    *,
    caller_session_id: str,
    workspace: str,
    requested: str,
    catalogue: Sequence[Mapping[str, Any]],
    report_to: str | None = None,
    require_report_to: bool = False,
) -> AdmittedSeats:
    """Admit ``requested`` seats or raise ``RunbookSeatRefusal``.

    ``requested`` is the comma-separated seat names; each must be exactly a catalogue
    name. Runbooks requiring session reports set ``require_report_to``; an empty ref refuses
    before storage lookup. Checks run in order: a live execution of the caller's runbook
    in the same workspace on the same machine, then each seat's agent definition. A storage error, a
    truncated read, or a live sibling whose machine or workspace is unknown refuses with
    its cause.
    """
    if (require_report_to and report_to is None) or (
        report_to is not None and not report_to.strip()
    ):
        raise RunbookSeatRefusal("report_to must be a nonempty session ref")
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

    _refuse_live_siblings(
        stores, executions, execution, machine_id=session.machine_id, workspace_id=workspace_id
    )
    _refuse_unresolved_agents(stores.definitions, project_id, seats)
    return AdmittedSeats(workspace_id=workspace_id, seats=seats)


def _workspace_id(workspaces: WorkspaceManager, workspace: str) -> str:
    try:
        return workspaces.resolve_reference(workspace).workspace.id
    except (LookupError, ValueError) as exc:
        raise RunbookSeatRefusal(f"workspace '{workspace}' does not resolve: {exc}") from exc


def _refuse_truncated(rows: Sequence[object], what: str) -> None:
    if len(rows) > READ_BOUND:
        raise RunbookSeatRefusal(f"the {what} read is truncated at {READ_BOUND} rows")


def _refuse_live_siblings(
    stores: RunbookSeatStores,
    executions: LocalPipelineExecutionManager,
    execution: PipelineExecution,
    *,
    machine_id: str,
    workspace_id: str,
) -> None:
    """Refuse when the caller's runbook is still launching in the same place.

    ``executions`` is already scoped to the caller's project. A sibling elsewhere,
    on another machine or in another workspace, never refuses.
    """
    name = execution.pipeline_name
    siblings: list[str] = []
    for status in LIVE_EXECUTION_STATUSES:
        rows = executions.list_executions(status=status, pipeline_name=name, limit=READ_BOUND + 1)
        _refuse_truncated(rows, f"{status.value} '{name}' execution")
        siblings.extend(
            row.id
            for row in rows
            if row.id != execution.id
            and _launches_here(
                stores, executions, row, machine_id=machine_id, workspace_id=workspace_id
            )
        )
    if siblings:
        raise RunbookSeatRefusal(f"another '{name}' execution is live: {', '.join(siblings)}")


def _launches_here(
    stores: RunbookSeatStores,
    executions: LocalPipelineExecutionManager,
    sibling: PipelineExecution,
    *,
    machine_id: str,
    workspace_id: str,
) -> bool:
    """Whether ``sibling`` launches on this machine in this workspace; unknown refuses."""
    child = stores.sessions.get(sibling.session_id) if sibling.session_id else None
    if child is None:
        raise RunbookSeatRefusal(f"live execution {sibling.id} has no pipeline child session")
    if child.machine_id != machine_id:
        return False
    inputs = json.loads(sibling.inputs_json or "{}")
    workspace = inputs.get("workspace") if isinstance(inputs, dict) else None
    if not isinstance(workspace, str) or not workspace:
        # Inferred runbooks persist their canonical launch target in the guard output.
        guards = [
            step
            for step in executions.get_steps_for_execution(sibling.id)
            if step.step_id == "guard" and step.status == StepStatus.COMPLETED
        ]
        if len(guards) == 1:
            output = json.loads(guards[0].output_json or "{}")
            if isinstance(output, dict) and output.get("success") is True:
                workspace = output.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        raise RunbookSeatRefusal(f"live execution {sibling.id} has no workspace input")
    return _workspace_id(stores.workspaces, workspace) == workspace_id


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

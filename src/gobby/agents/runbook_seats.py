"""Runbook seat guard: the read-only admission check a runbook runs before it launches seats.

Runbooks are fire and forget: once the panes exist and the agents start, the pipeline
completes. Besides refusing a seat whose agent definition is missing or disabled, the
guard refuses only an accidental double-fire: the same runbook still launching for the
same project in the same workspace on the same machine, and for a runbook launched with
a ``task`` input, for the same task. Seats are told apart by
``project#session_ref``, never by pane title, so seats may share a title; runbooks
enforce no agent slots.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.agents.team_lifecycle import seat_team
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import LIVE_SESSION_STATUSES, SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError
from gobby.storage.workspaces import WorkspaceManager
from gobby.utils.uuid_validation import parse_uuid_reference
from gobby.workflows.agent_resolver import resolve_agent_with_row
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
    launch: tuple[str, ...]
    live: Mapping[str, str]
    tab_title: str | None
    tab_exists: bool


@dataclass(frozen=True)
class RunbookSeatStores:
    """The storage the guard reads; ``executions`` builds a project's execution manager."""

    sessions: SessionManager
    executions: Callable[[str], LocalPipelineExecutionManager]
    definitions: AgentDefinitionManager
    workspaces: WorkspaceManager
    runs: LocalAgentRunManager
    tasks: LocalTaskManager


def check_runbook_seats(
    stores: RunbookSeatStores,
    *,
    caller_session_id: str,
    workspace: str,
    requested: str,
    catalogue: Sequence[Mapping[str, Any]],
    report_to: str | None = None,
    require_report_to: bool = False,
    skip_live_seats: bool = False,
    team_id: str | None = None,
    tab_title: str | None = None,
    task: str | None = None,
) -> AdmittedSeats:
    """Admit ``requested`` seats or raise ``RunbookSeatRefusal``.

    ``requested`` is the comma-separated seat names; each must be exactly a catalogue
    name. Runbooks requiring session reports set ``require_report_to``; an empty ref refuses
    before storage lookup. Checks run in order: a live execution of the caller's runbook
    in the same workspace on the same machine (with a ``task`` input, for the same task),
    then each seat's agent definition. A storage error, a
    truncated read, or a live sibling whose machine or workspace is unknown refuses with
    its cause.

    ``launch`` names the seats to start. With ``skip_live_seats`` it leaves out each requested
    seat whose agent already has an active run on this machine with a live session in this
    project. ``live`` maps each such seat to that session. With ``team_id``, ``live`` covers
    every catalogue seat whose agent runs for that team, whether requested or not, so a fresh
    reviewer's request still reports its team's live Dev Manager; only runs admitted to that team
    count. ``tab_title`` names the runbook tab; with ``task`` it becomes ``<tab_title> - #<seq>``.
    ``tab_exists`` says whether the workspace has that tab; two such tabs refuse.
    """
    if (require_report_to and report_to is None) or (
        report_to is not None and not report_to.strip()
    ):
        raise RunbookSeatRefusal("report_to must be a nonempty session ref")
    seats = _requested_seats(requested, catalogue)
    if team_id is not None:
        team_uuid = parse_uuid_reference(team_id)
        if team_uuid is None:
            raise RunbookSeatRefusal(f"team {team_id} requires a full execution UUID for team_id")
        team_id = str(team_uuid)
    try:
        return _admit(
            stores,
            seats,
            tuple(_catalogue_seat(entry) for entry in catalogue),
            caller_session_id=caller_session_id,
            workspace=workspace,
            skip_live_seats=skip_live_seats,
            team_id=team_id,
            tab_title=tab_title,
            task=task,
        )
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
    catalogue: tuple[CatalogueSeat, ...],
    *,
    caller_session_id: str,
    workspace: str,
    skip_live_seats: bool,
    team_id: str | None,
    tab_title: str | None,
    task: str | None,
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
        stores,
        executions,
        execution,
        project_id=project_id,
        machine_id=session.machine_id,
        workspace_id=workspace_id,
    )
    _refuse_unresolved_agents(stores.definitions, project_id, seats)
    if team_id is not None:
        live = _live_seats(stores, session, catalogue, team_id=team_id)
    elif skip_live_seats:
        live = _live_seats(stores, session, seats, team_id=None)
    else:
        live = {}
    tab_task = task
    if team_id is not None:
        from gobby.agents.team_lifecycle import team_binding

        binding = team_binding(stores.tasks.db, team_id)
        if binding is not None:
            tab_task = binding.task_id
            if binding.parent_team_id is not None:
                if binding.tab_title is not None:
                    tab_title = binding.tab_title
                    tab_task = None  # The stored title already includes the task suffix.
                managers = {
                    seat.name
                    for seat in catalogue
                    if (
                        resolved := resolve_agent_with_row(
                            seat.agent, stores.tasks.db, project_id=project_id
                        )
                    )
                    is not None
                    and resolved[1].enabled
                    and resolved[0].enabled
                    and resolved[0].workflows.variables.get("team_manager") is True
                }
                seats = tuple(seat for seat in seats if seat.name not in managers)
                parent_live = _live_seats(
                    stores, session, catalogue, team_id=binding.parent_team_id
                )
                live.update({name: parent_live[name] for name in managers if name in parent_live})
    tab = _runbook_tab_title(stores.tasks, project_id, tab_title, tab_task)
    return AdmittedSeats(
        workspace_id=workspace_id,
        seats=seats,
        launch=tuple(seat.name for seat in seats if not (skip_live_seats and seat.name in live)),
        live=live,
        tab_title=tab,
        tab_exists=tab is not None and _tab_exists(stores.workspaces, workspace_id, tab),
    )


def _live_seats(
    stores: RunbookSeatStores,
    caller: Session,
    seats: Sequence[CatalogueSeat],
    *,
    team_id: str | None,
) -> dict[str, str]:
    """Map each seat whose agent runs here with a live session in the project to that session."""
    runs = stores.runs.list_active_for_machine(caller.machine_id, limit=READ_BOUND + 1)
    _refuse_truncated(runs, "active agent run")
    live: dict[str, str] = {}
    for seat in seats:
        for run in runs:
            if run.agent_name != seat.agent or not run.child_session_id:
                continue
            child = stores.sessions.get(run.child_session_id)
            if (
                child is not None
                and child.project_id == caller.project_id
                and child.status in LIVE_SESSION_STATUSES
            ):
                if team_id is not None:
                    team = seat_team(stores.sessions.db, child.id)
                    if team is None or team.team_id != team_id:
                        continue
                live[seat.name] = child.id
                break
    return live


def _runbook_tab_title(
    tasks: LocalTaskManager, project_id: str, runbook_tab: str | None, task: str | None
) -> str | None:
    """The tab a task-bound run opens: '<runbook> - #<seq>', or the bare runbook name."""
    if runbook_tab is None or task is None:
        return runbook_tab
    try:
        resolved = tasks.get_task(tasks.resolve_task_reference(task, project_id), project_id)
    except (TaskNotFoundError, ValueError) as exc:
        raise RunbookSeatRefusal(f"task {task} is not in this project: {exc}") from exc
    if resolved.seq_num is None:
        raise RunbookSeatRefusal(f"task {task} has no sequence number")
    return f"{runbook_tab} - #{resolved.seq_num}"


def _tab_exists(workspaces: WorkspaceManager, workspace_id: str, title: str) -> bool:
    tabs = [tab for tab in workspaces.list_tabs(workspace_id) if tab.title == title]
    if len(tabs) > 1:
        raise RunbookSeatRefusal(f"the workspace has {len(tabs)} tabs titled {title!r}")
    return bool(tabs)


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
    project_id: str,
    machine_id: str,
    workspace_id: str,
) -> None:
    """Refuse when the caller's runbook is still launching in the same place.

    ``executions`` is already scoped to the caller's project. A sibling elsewhere,
    on another machine or in another workspace, never refuses. With a ``task``
    input, only a sibling for the same task refuses, so teams for different tasks
    launch in parallel.
    """
    name = execution.pipeline_name
    siblings: list[PipelineExecution] = []
    for status in LIVE_EXECUTION_STATUSES:
        rows = executions.list_executions(status=status, pipeline_name=name, limit=READ_BOUND + 1)
        _refuse_truncated(rows, f"{status.value} '{name}' execution")
        siblings.extend(
            row
            for row in rows
            if row.id != execution.id
            and _launches_here(
                stores, executions, row, machine_id=machine_id, workspace_id=workspace_id
            )
        )
    task = _launch_task(stores.tasks, execution, project_id) if siblings else None
    if task is not None:
        siblings = [row for row in siblings if _same_task(stores.tasks, row, project_id, task)]
    if siblings:
        live = ", ".join(row.id for row in siblings)
        raise RunbookSeatRefusal(f"another '{name}' execution is live: {live}")


def _launch_task(
    tasks: LocalTaskManager, execution: PipelineExecution, project_id: str
) -> str | None:
    """Resolve the execution's ``task`` input to a task id; None without one."""
    inputs = json.loads(execution.inputs_json or "{}")
    ref = inputs.get("task") if isinstance(inputs, dict) else None
    if ref is None:
        return None
    try:
        return tasks.resolve_task_reference(str(ref), project_id)
    except (TaskNotFoundError, ValueError) as exc:
        raise RunbookSeatRefusal(
            f"execution {execution.id} task {ref!r} does not resolve: {exc}"
        ) from exc


def _same_task(
    tasks: LocalTaskManager, sibling: PipelineExecution, project_id: str, task: str
) -> bool:
    """Whether ``sibling`` launches for ``task``; a sibling without a task refuses."""
    sibling_task = _launch_task(tasks, sibling, project_id)
    if sibling_task is None:
        raise RunbookSeatRefusal(f"live execution {sibling.id} has no task input")
    return sibling_task == task


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

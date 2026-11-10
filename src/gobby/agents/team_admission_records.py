"""Team admission records: the binding a team's admit step carries, and its reads and writes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any

from gobby.agents.team_scope import (
    SINGLE_MODE,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.team_records import ADMISSION_STEP
from gobby.workflows.pipeline_state import ExecutionStatus, StepExecution

TREELESS = "none"


@dataclass(frozen=True)
class TeamBinding:
    """One admitted team: its task, branch and worktree, and the admitting execution."""

    team_id: str
    task_id: str
    project_id: str
    branch: str
    base_branch: str
    # None for a treeless team, whose admission records an explicit null for its tree.
    worktree_id: str | None
    worktree_path: str | None
    # Reviewer definitions the team admits; a join must name one of them.
    reviewers: list[str] = field(default_factory=list)
    # "epic" for a team bound to an epic's open leaves; "single" for one task.
    mode: str = SINGLE_MODE
    # Resolved runbook tab title, shared by replacements and later epic-seat spawns.
    tab_title: str | None = None
    parent_team_id: str | None = None


@dataclass(frozen=True)
class SeatTarget:
    """Where a pipeline seat spawn asks to run, as spawn_agent received it."""

    task_id: str | None = None
    checkout_mode: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    clone_id: str | None = None
    worktree_id: str | None = None
    project_path: str | None = None
    main_checkout: bool = False


class TeamAdmissionRefused(Exception):
    """Team admission or a team seat spawn is refused; ``code`` names the cause."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Caller:
    """A running team runbook execution and its admission step row."""

    session: Session
    project_id: str
    executions: LocalPipelineExecutionManager
    execution_id: str
    step: StepExecution


def admission_step(
    executions: LocalPipelineExecutionManager, execution_id: str
) -> StepExecution | None:
    steps = executions.get_steps_for_execution(execution_id)
    return next((step for step in steps if step.step_id == ADMISSION_STEP), None)


def step_output(step: StepExecution | None) -> dict[str, Any]:
    try:
        output = json.loads(step.output_json or "null") if step is not None else None
    except json.JSONDecodeError:
        return {}
    return output if isinstance(output, dict) else {}


def merge_step_output(
    executions: LocalPipelineExecutionManager, step: StepExecution, **keys: Any
) -> None:
    """Set ``keys`` in an admission step's output and keep its other keys."""
    output = {**step_output(step), **keys}
    executions.update_step_execution(step.id, output_json=json.dumps(output))


def recorded_team(step: StepExecution | None) -> dict[str, Any] | None:
    """The team record in an admission step's output, complete or not."""
    record = step_output(step).get("team")
    return record if isinstance(record, dict) else None


def reviewer_names(value: Any) -> list[str]:
    """The reviewer definitions a stored admission names.

    A list names them. A mapping, as persisted before reviewers became a list, names them by key.
    Null names none. Any other stored value is refused rather than read as none.
    """
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return list(value)
    raise TeamAdmissionRefused(
        "team_record_invalid",
        f"reviewers must be a list or a mapping, not {type(value).__name__}",
    )


def binding_from_record(record: dict[str, Any] | None) -> TeamBinding | None:
    if record is None:
        return None
    values: dict[str, Any] = {}
    for item in fields(TeamBinding):
        value = record.get(item.name)
        if item.name == "reviewers":
            # The admission intent carries no reviewers list unless a reviewer is named.
            values[item.name] = reviewer_names(value)
            continue
        if item.name == "mode":
            # Records admitted before epic mode carry no mode: they are single-task teams.
            values[item.name] = value if isinstance(value, str) and value else SINGLE_MODE
            continue
        if item.name in {"tab_title", "parent_team_id"} and value is None:
            values[item.name] = None
            continue
        if item.name in ("worktree_id", "worktree_path") and item.name in record and value is None:
            values[item.name] = None
            continue
        if not isinstance(value, str) or not value:
            return None
        values[item.name] = value
    return TeamBinding(**values)


def own_record(caller: Caller) -> dict[str, Any] | None:
    return recorded_team(admission_step(caller.executions, caller.execution_id))


def record_team(caller: Caller, record: dict[str, Any]) -> None:
    step = admission_step(caller.executions, caller.execution_id) or caller.step
    merge_step_output(caller.executions, step, team=record)


ENDED_EXECUTIONS = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)


def team_binding(db: HubDatabase, team_id: str) -> TeamBinding | None:
    """Read the canonical admission binding for a team execution."""
    return binding_from_record(
        recorded_team(admission_step(LocalPipelineExecutionManager(db, None), team_id))
    )

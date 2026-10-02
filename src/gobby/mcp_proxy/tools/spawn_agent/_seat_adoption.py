"""Pipeline seat spawns reconcile by step invocation id (deploy-runbook 7.2).

A pipeline MCP step calls spawn_agent with its pipeline child as the ambient session
and the step's ``invocation_id`` as ``reserved_run_id``. A step re-run after a restart
adopts the run its first attempt launched instead of launching the seat again.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from gobby.mcp_proxy.tools.workflows._pipeline_execution import _definition_snapshot
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.workspaces import WorkspaceManager
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_state import ExecutionStatus

_PIPELINE_SOURCE = "pipeline"
_EXTERNAL_PREFIX = "pipeline-"
_NO_SEAT: dict[str, str | None] = {"workspace": None, "tab_ref": None, "pane_ref": None}


def _refusal(code: str, error: str) -> dict[str, Any]:
    return {"success": False, "error": error, "error_code": code}


def pipeline_caller(session_manager: Any, caller_session_id: str | None) -> Session | None:
    """The ambient caller when it is a pipeline session, else None."""
    if session_manager is None or caller_session_id is None:
        return None
    caller: Session | None = session_manager.get(caller_session_id)
    return caller if caller is not None and caller.source == _PIPELINE_SOURCE else None


def authorize_pipeline_invocation(
    db: HubDatabase,
    caller: Session,
    *,
    parent_session_id: str | None,
    project_id: str | None,
    reserved_run_id: str,
) -> dict[str, Any] | None:
    """None when ``reserved_run_id`` is a step invocation of the caller's running execution."""
    refusal = _refusal(
        "invocation_unauthorized",
        "reserved_run_id is not a step invocation of the calling pipeline's running execution",
    )
    if caller.status == "deleted" or not caller.external_id.startswith(_EXTERNAL_PREFIX):
        return refusal
    if parent_session_id != caller.id or project_id != caller.project_id:
        return refusal
    try:
        run_id = str(UUID(reserved_run_id))
        execution_id = str(UUID(caller.external_id.removeprefix(_EXTERNAL_PREFIX)))
    except ValueError:
        return refusal
    execution = LocalPipelineExecutionManager(db, caller.project_id).get_execution(execution_id)
    if (
        execution is None
        or execution.status != ExecutionStatus.RUNNING
        or execution.project_id != caller.project_id
    ):
        return refusal
    snapshot = _definition_snapshot(execution)
    if snapshot is None or run_id not in {
        step_invocation_id(execution.id, step.id) for step in snapshot.steps
    }:
        return refusal
    return None


def reconcile_pipeline_invocation(
    db: HubDatabase, session_manager: Any, caller: Session, reserved_run_id: str
) -> dict[str, Any] | None:
    """None when no run holds the id, so the ordinary path launches it; else the reply."""
    run = LocalAgentRunManager(db).get(reserved_run_id)
    if run is None:
        return None
    parent: Session | None = session_manager.get(run.parent_session_id)
    if (
        parent is None
        or parent.source != _PIPELINE_SOURCE
        or parent.project_id != caller.project_id
        or parent.external_id != caller.external_id
    ):
        return _refusal(
            "invocation_conflict", f"Run {run.id} belongs to another pipeline invocation"
        )
    if run.started_at is None:
        return _refusal(
            "seat_launch_unsettled",
            f"Run {run.id} is {run.status} and never started; stop or settle it before a relaunch",
        )
    refs = (
        WorkspaceManager(db).placement_refs_for_terminal(run.terminal_id)
        if run.terminal_id
        else None
    )
    return {
        "success": True,
        "adopted": True,
        "run_id": run.id,
        "status": run.status,
        **(refs or _NO_SEAT),
    }


def pipeline_invocation_reply(
    db: HubDatabase | None,
    session_manager: Any,
    caller: Session,
    *,
    parent_session_id: str | None,
    project_id: str | None,
    reserved_run_id: str,
) -> dict[str, Any] | None:
    """Authorize, then reconcile; None lets the ordinary spawn path launch the run."""
    if db is None:
        return _refusal("invocation_unauthorized", "reserved_run_id needs the hub database")
    refusal = authorize_pipeline_invocation(
        db,
        caller,
        parent_session_id=parent_session_id,
        project_id=project_id,
        reserved_run_id=reserved_run_id,
    )
    if refusal is not None:
        return refusal
    return reconcile_pipeline_invocation(db, session_manager, caller, reserved_run_id)

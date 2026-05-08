"""Pipeline execution query tools — list and search."""

from __future__ import annotations

import logging
from typing import Any

from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepExecution

logger = logging.getLogger(__name__)


def _execution_brief(execution: PipelineExecution) -> dict[str, Any]:
    """Compact summary of an execution."""
    return {
        "id": execution.id,
        "pipeline_name": execution.pipeline_name,
        "status": execution.status.value,
        "created_at": execution.created_at,
        "has_review": execution.review_json is not None,
    }


def _execution_summary(execution: PipelineExecution) -> dict[str, Any]:
    """Full summary of an execution (minus raw JSON blobs)."""
    return {
        "id": execution.id,
        "pipeline_name": execution.pipeline_name,
        "project_id": execution.project_id,
        "status": execution.status.value,
        "session_id": execution.session_id,
        "parent_execution_id": execution.parent_execution_id,
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
        "completed_at": execution.completed_at,
        "has_review": execution.review_json is not None,
    }


def _step_summary(step: StepExecution) -> dict[str, Any]:
    """Compact summary of a step execution."""
    result: dict[str, Any] = {
        "step_id": step.step_id,
        "status": step.status.value,
    }
    if step.error:
        result["error"] = step.error[:200]
    return result


def _validate_status(status_str: str | None) -> ExecutionStatus | None:
    """Validate and convert a status string to ExecutionStatus enum."""
    if status_str is None:
        return None
    try:
        return ExecutionStatus(status_str)
    except ValueError:
        valid = [s.value for s in ExecutionStatus]
        raise ValueError(f"Invalid status '{status_str}'. Valid: {valid}") from None


def list_pipeline_executions(
    execution_manager: Any,
    status: str | None = None,
    pipeline_name: str | None = None,
    session_id: str | None = None,
    parent_execution_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    brief: bool = True,
    include_steps: bool = False,
) -> dict[str, Any]:
    """List pipeline executions with optional filters and offset pagination.

    Args:
        execution_manager: LocalPipelineExecutionManager instance
        status: Filter by status string
        pipeline_name: Filter by pipeline name
        session_id: Filter by triggering session
        parent_execution_id: Filter by parent execution
        limit: Maximum results per page
        offset: Number of leading rows to skip
        brief: Use compact format (default True)
        include_steps: Include step details per execution

    Returns:
        On success: {success, executions, total, limit, offset, status_summary}
        On error: {success: False, error: str}. Error envelope is used for
            invalid status strings and invalid pagination (limit <= 0,
            offset < 0).

    The ``total`` and ``status_summary`` keys are filter-scoped — they
    reflect counts within the currently active filter (status_summary
    drops only the ``status`` predicate so callers can render
    "X running / Y completed" alongside a status-filtered page).
    """
    try:
        status_enum = _validate_status(status)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        executions = execution_manager.list_executions(
            status=status_enum,
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
            limit=limit,
            offset=offset,
        )
        total = execution_manager.count_executions(
            status=status_enum,
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )
        status_summary = execution_manager.status_summary_for_executions(
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )
    except ValueError as e:
        return {"success": False, "error": f"Invalid pagination: {e}"}

    formatter = _execution_brief if brief else _execution_summary
    results = [formatter(ex) for ex in executions]

    if include_steps and executions:
        steps_by_exec = execution_manager.get_steps_for_executions([ex.id for ex in executions])
        for entry, ex in zip(results, executions, strict=True):
            entry["steps"] = [_step_summary(s) for s in steps_by_exec.get(ex.id, [])]

    return {
        "success": True,
        "executions": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "status_summary": status_summary,
    }


def search_pipeline_executions(
    execution_manager: Any,
    query: str,
    search_errors: bool = True,
    search_outputs: bool = False,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_steps: bool = False,
) -> dict[str, Any]:
    """Search pipeline executions by text with offset pagination.

    Args:
        execution_manager: LocalPipelineExecutionManager instance
        query: Search text
        search_errors: Search step error text
        search_outputs: Search step output JSON
        status: Filter by status string
        limit: Maximum results per page
        offset: Number of leading rows to skip
        include_steps: Include step details per execution

    Returns:
        On success: {success, executions, total, limit, offset, query}
        On error: {success: False, error: str}. Error envelope is used for
            empty queries, invalid status strings, and invalid pagination.
    """
    if not query or not query.strip():
        return {"success": False, "error": "Query must not be empty"}

    try:
        status_enum = _validate_status(status)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        executions = execution_manager.search_executions(
            query=query.strip(),
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status_enum,
            limit=limit,
            offset=offset,
        )
        total = execution_manager.count_search_executions(
            query=query.strip(),
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status_enum,
        )
    except ValueError as e:
        return {"success": False, "error": f"Invalid pagination: {e}"}

    results = [_execution_summary(ex) for ex in executions]

    if include_steps and executions:
        steps_by_exec = execution_manager.get_steps_for_executions([ex.id for ex in executions])
        for entry, ex in zip(results, executions, strict=True):
            entry["steps"] = [_step_summary(s) for s in steps_by_exec.get(ex.id, [])]

    return {
        "success": True,
        "executions": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "query": query.strip(),
    }

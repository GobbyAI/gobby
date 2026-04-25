"""
Query and management functions for agent runs.

Extracted from runner.py as part of Strangler Fig decomposition (Wave 2).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.storage.agents import AgentRunStatus

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner

logger = logging.getLogger(__name__)


def get_run(runner: AgentRunner, run_id: str) -> Any | None:
    """Get an agent run by ID."""
    return runner._run_storage.get(run_id)


def get_run_id_by_session(runner: AgentRunner, session_id: str) -> str | None:
    """
    Get agent run_id by child session_id.

    Looks up the agent_runs table for a run with this child_session_id.

    Args:
        runner: The AgentRunner instance.
        session_id: The child session ID (UUID format).

    Returns:
        The run_id if found, None otherwise.
    """
    row = runner.db.fetchone(
        "SELECT id FROM agent_runs WHERE child_session_id = ? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    )
    return row["id"] if row else None


def list_runs(
    runner: AgentRunner,
    parent_session_id: str,
    status: str | None = None,
    limit: int = 100,
) -> list[Any]:
    """List agent runs for a session."""
    return runner._run_storage.list_by_session(
        parent_session_id,
        status=cast(AgentRunStatus | None, status),
        limit=limit,
    )


def cancel_run(runner: AgentRunner, run_id: str) -> bool:
    """Cancel a running agent."""
    run = runner._run_storage.get(run_id)
    if not run:
        return False
    if run.status not in ("pending", "running"):
        return False

    cancelled_run = runner._run_storage.cancel(run_id)
    if cancelled_run is None:
        runner.logger.debug(
            "Cancel no-op for run %s; another terminal state won the race",
            run_id,
        )
        return False

    # Also mark session as cancelled
    if run.child_session_id:
        runner._session_manager.update_status(run.child_session_id, "cancelled")

    runner.logger.info(f"Cancelled agent run {run_id}")

    return True


def complete_run(runner: AgentRunner, run_id: str, result: str | None = None) -> bool:
    """
    Complete a running agent (mark as success).

    Used for clean self-termination, as opposed to cancel_run which is
    for forced cancellation by a parent.

    If no result is provided, checks for an existing result that may have
    been set earlier (e.g. via send_message writing to agent_runs.result).

    Args:
        runner: The AgentRunner instance.
        run_id: The agent run ID.
        result: Optional result text. If None, preserves any existing result.

    Returns:
        True if the run was completed, False otherwise.
    """
    run = runner._run_storage.get(run_id)
    if not run:
        return False
    if run.status not in ("pending", "running"):
        return False

    # Use provided result, or preserve existing result from send_message
    final_result = result if result is not None else (run.result or "")

    # Read session stats (message processor writes these to the sessions table).
    # The agent_runs table may still have 0/0 at this point since stats are
    # written to sessions, not agent_runs, during execution.
    tool_calls_count = run.tool_calls_count or 0
    turns_used = run.turns_used or 0
    if run.child_session_id and (tool_calls_count == 0 or turns_used == 0):
        session = runner._session_manager.get(run.child_session_id)
        if session:
            tool_calls_count = getattr(session, "tool_call_count", 0) or tool_calls_count
            turns_used = getattr(session, "turn_count", 0) or turns_used

    completed_run = runner._run_storage.complete(
        run_id=run_id,
        result=final_result,
        tool_calls_count=tool_calls_count,
        turns_used=turns_used,
    )
    if completed_run is None:
        runner.logger.debug(
            "Completion no-op for run %s; another terminal state won the race",
            run_id,
        )
        return False

    runner.logger.info(f"Completed agent run {run_id} (self-termination)")

    return True

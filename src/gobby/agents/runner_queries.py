"""
Query and management functions for agent runs.

Extracted from runner.py as part of Strangler Fig decomposition (Wave 2).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.run_completion import closed_task_run_completion_result
from gobby.storage.agents import AgentRunStatus, AgentRunTerminalReason
from gobby.utils.uuid_validation import parse_uuid_reference

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner

logger = logging.getLogger(__name__)


def get_run(runner: AgentRunner, run_id: str) -> Any | None:
    """Get an agent run by ID.

    ``agent_runs.id`` is a uuid column, so a reference that is not a full UUID
    is answered here as "not found" instead of reaching PostgreSQL, where it
    would surface as a raw ``invalid input syntax for type uuid`` error (#21097).
    """
    if parse_uuid_reference(run_id) is None:
        return None
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
        "SELECT id FROM agent_runs WHERE child_session_id = %s ORDER BY created_at DESC LIMIT 1",
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

    # Preserve terminal child states, including expiration racing with cancellation.
    if run.child_session_id:
        runner._session_manager.update_status_if_non_terminal(run.child_session_id, "cancelled")

    runner.logger.info("Cancelled agent run %s", run_id)

    return True


def complete_run(
    runner: AgentRunner,
    run_id: str,
    result: str | None = None,
    terminal_reason: AgentRunTerminalReason | None = None,
    *,
    tool_calls_count: int,
    turns_used: int,
) -> bool:
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
        terminal_reason: Optional semantic reason for successful termination.
        tool_calls_count: Resolved tool-call count to persist.
        turns_used: Resolved turn count to persist.

    Returns:
        True if the run was completed, False otherwise.
    """
    run = runner._run_storage.get(run_id)
    if not run:
        return False
    if run.status not in ("pending", "running"):
        return False

    result = closed_task_run_completion_result(runner.run_storage.db, run, result)

    completed_run = runner._run_storage.complete(
        run_id=run_id,
        result=result,
        tool_calls_count=tool_calls_count,
        turns_used=turns_used,
        terminal_reason=terminal_reason,
    )
    if completed_run is None:
        runner.logger.debug(
            "Completion no-op for run %s; another terminal state won the race",
            run_id,
        )
        return False

    runner.logger.info("Completed agent run %s (self-termination)", run_id)

    return True

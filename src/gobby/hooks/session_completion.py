"""Terminal outcome derivation for a finishing agent run.

The session coordinator owns hook-worker orchestration; this module owns the
separate question of *what outcome gets reported* when a run ends — the status
that survives a terminalization race, the canonical result for a closed bound
task, and the failure reasons for an unfinished step workflow or a run that did
no work. Dependencies arrive as parameters so the rules stay testable without a
coordinator.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from gobby.agents.run_completion import closed_task_completion_result
from gobby.storage.coordination_waits import CoordinationWaitManager

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.tasks import LocalTaskManager

_AUTH_PROMPT_RE = re.compile(
    r"/login|Press 1 to trust|not authenticated|Invalid API key|API key required",
    re.IGNORECASE,
)
INCOMPLETE_STEP_WORKFLOW_ERROR = "Agent session ended before step workflow completed"
NO_ACTIVITY_ERROR = "Agent completed with no activity (0 tool calls, 0 turns)"


def format_no_activity_error(result: Any) -> str:
    if not isinstance(result, str) or not result.strip():
        return NO_ACTIVITY_ERROR

    tail = "\n".join(result.splitlines()[-20:])
    if _AUTH_PROMPT_RE.search(tail):
        return (
            f"{NO_ACTIVITY_ERROR} - auth/trust prompt detected in pane output. "
            "Check daemon-visible API/provider credentials or Claude Code login state."
        )
    return f"{NO_ACTIVITY_ERROR} - last pane output:\n{tail}"


def _format_incomplete_step_workflow_error(
    workflow_name: str,
    current_step: str | None,
    exit_condition: str | None,
    *,
    eval_error: Exception | None = None,
) -> str:
    parts = [
        INCOMPLETE_STEP_WORKFLOW_ERROR,
        f"workflow={workflow_name}",
        f"current_step={current_step or 'unknown'}",
    ]
    if exit_condition:
        parts.append(f"exit_condition={exit_condition}")
    else:
        parts.append("exit_condition=<none>")
    if eval_error is not None:
        parts.append(f"exit_condition_error={eval_error}")
    return "; ".join(parts)


def closed_task_result(
    task_manager: LocalTaskManager | None,
    logger: logging.Logger,
    agent_run: Any,
    result: str | None = None,
) -> str | None:
    """Add canonical close metadata when the run's bound task is closed."""
    task_id = getattr(agent_run, "task_id", None)
    if task_manager is None or not isinstance(task_id, str) or not task_id:
        return None
    try:
        task = task_manager.get_task(task_id)
    except Exception as e:
        logger.warning(
            "Failed to load bound task %s while completing agent run %s: %s",
            task_id,
            getattr(agent_run, "id", "<unknown>"),
            e,
        )
        return None
    return closed_task_completion_result(task, result)


def agent_run_notification_status(
    agent_run_manager: LocalAgentRunManager | None,
    logger: logging.Logger,
    run_id: str,
    updated_run: Any | None,
    *,
    default: str,
) -> str:
    """Return the persisted status when another terminalizer won the race."""
    if updated_run is not None:
        status = getattr(updated_run, "status", None)
        return status if isinstance(status, str) else default
    stored_run = agent_run_manager.get(run_id) if agent_run_manager else None
    if stored_run is None:
        logger.warning("Agent run %s disappeared after terminalization race", run_id)
        return default
    logger.debug(
        "Agent run %s terminalized concurrently with status %s",
        run_id,
        stored_run.status,
    )
    return stored_run.status


def incomplete_step_workflow_error(
    agent_run_manager: LocalAgentRunManager | None,
    logger: logging.Logger,
    session_id: str,
) -> str | None:
    """Return a failure reason if an active step workflow is still incomplete."""
    if not agent_run_manager:
        return None

    db = getattr(agent_run_manager, "db", None)
    if db is None:
        return None

    try:
        if CoordinationWaitManager(db).has_active_wait(session_id):
            # Yielding the turn is how a registered wait is served: the step is
            # incomplete precisely because the session is waiting on another
            # session, and the wake resumes it. Failing the run here would punish
            # the coordination the workflow asked for (#22367).
            return None
    except Exception as e:
        logger.warning("Failed to verify coordination hold for session %s: %s", session_id, e)

    try:
        from gobby.workflows.step_context import first_incomplete_step_workflow

        incomplete = first_incomplete_step_workflow(db, session_id)
    except Exception as e:
        logger.warning(
            "Failed to inspect step workflow completion for session %s: %s", session_id, e
        )
        return None

    if incomplete is None:
        return None
    return _format_incomplete_step_workflow_error(
        incomplete.workflow_name,
        incomplete.current_step,
        incomplete.exit_condition,
        eval_error=incomplete.eval_error,
    )

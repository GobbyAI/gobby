"""Shared cancellation helpers for agent MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from gobby.agents.task_recovery import TaskRecoveryHandler

logger = logging.getLogger(__name__)


class _CancelledRunClassifier:
    """Provider classification is irrelevant for cancelled-run recovery."""

    def is_provider_error(self, error_string: str | None) -> bool:
        return False


async def recover_cancelled_agent_task_claim(
    *,
    runner: Any,
    task_manager: Any | None,
    run_id: str,
) -> None:
    """Release task ownership for a cancelled agent when fallback cancellation is used."""
    if task_manager is None:
        return

    run_storage = getattr(runner, "run_storage", None)
    if run_storage is None:
        logger.debug("Cannot recover cancelled run %s claim without run storage", run_id)
        return

    db_run = run_storage.get(run_id)
    if db_run is None:
        logger.debug("Cannot recover cancelled run %s claim; run row not found", run_id)
        return

    recovery = TaskRecoveryHandler(
        task_manager,
        run_storage,
        _CancelledRunClassifier(),
    )
    await recovery.recover_task_from_terminal_agent(db_run, outcome="cancelled")


async def terminalize_cancelled_agent_run(
    *,
    runner: Any,
    run_id: str,
    terminal_reason: str,
    lifecycle_monitor: Any | None,
    completion_registry: Any | None,
    task_manager: Any | None,
    message: str | None = None,
) -> bool:
    """Cancel an agent run and recover its task claim even without lifecycle monitor wiring."""
    if lifecycle_monitor is not None:
        return bool(
            await lifecycle_monitor.terminalize_cancelled_run(
                run_id,
                terminal_reason=terminal_reason,
            )
        )

    transitioned = bool(runner.cancel_run(run_id))
    if not transitioned:
        return False

    await recover_cancelled_agent_task_claim(
        runner=runner,
        task_manager=task_manager,
        run_id=run_id,
    )
    if completion_registry is not None:
        await completion_registry.notify(
            run_id,
            {
                "status": "cancelled",
                "terminal_reason": terminal_reason,
                "run_id": run_id,
            },
            message=message,
        )
    return True


async def terminalize_killed_agent_run(
    *,
    runner: Any,
    run_id: str,
    effective_status: str,
    lifecycle_monitor: Any | None,
    completion_registry: Any | None,
    task_manager: Any | None,
) -> dict[str, Any]:
    """Apply workflow terminal state after an explicit parent-side kill."""
    if effective_status == "error":
        failed_run = runner.run_storage.fail(run_id, error="Agent self-reported error")
        if failed_run is None:
            current = runner.get_run(run_id)
            logger.debug(
                "Error terminalization no-op for run %s; current status=%s",
                run_id,
                current.status if current else "missing",
            )
        return {"status": "error", "workflow_stopped": True}

    log_prefix = "Cancelled" if effective_status == "cancelled" else "Fallback cancelled"
    transitioned = await terminalize_cancelled_agent_run(
        runner=runner,
        run_id=run_id,
        terminal_reason="user_cancelled",
        lifecycle_monitor=lifecycle_monitor,
        completion_registry=completion_registry,
        task_manager=task_manager,
    )
    if not transitioned:
        current = runner.get_run(run_id)
        logger.debug(
            "%s terminalization no-op for run %s; current status=%s",
            log_prefix,
            run_id,
            current.status if current else "missing",
        )
    return {
        "status": "cancelled",
        "terminal_reason": "user_cancelled",
        "workflow_stopped": True,
    }

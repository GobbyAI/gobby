"""Shared cancellation helpers for agent MCP tools."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from gobby.agents.kill import KILL_ERROR_NO_TARGET_PID
from gobby.agents.srt_process_cleanup import reap_srt_runner_process_tree
from gobby.agents.task_recovery import TaskRecoveryHandler

logger = logging.getLogger(__name__)


async def _reap_terminal_srt_runner(run_id: str) -> None:
    try:
        await reap_srt_runner_process_tree(run_id)
    except Exception:
        logger.warning(
            "Failed to reap SRT sandbox runner for terminal agent %s",
            run_id,
            exc_info=True,
        )


class _TerminalRunClassifier:
    """Classify explicit terminalization as a non-provider failure."""

    def for_provider(self, _provider_id: str) -> _TerminalRunClassifier:
        return self

    def is_provider_error(self, error_string: str | None) -> bool:
        return False

    def is_bootstrap_stall(self, error_string: str | None) -> bool:
        return False


async def recover_terminal_agent_task_claim(
    *,
    runner: Any,
    task_manager: Any | None,
    run_id: str,
    outcome: Literal["failed", "cancelled"],
) -> None:
    """Release task ownership when direct terminalization bypasses the lifecycle monitor."""
    if task_manager is None:
        return

    run_storage = getattr(runner, "run_storage", None)
    if run_storage is None:
        logger.debug("Cannot recover terminal run %s claim without run storage", run_id)
        return

    db_run = run_storage.get(run_id)
    if db_run is None:
        logger.debug("Cannot recover terminal run %s claim; run row not found", run_id)
        return

    recovery = TaskRecoveryHandler(
        task_manager,
        run_storage,
        _TerminalRunClassifier(),
    )
    await recovery.recover_task_from_terminal_agent(db_run, outcome=outcome)


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
    from gobby.agents.terminal_delivery import (
        deliver_existing_terminal_run,
        run_terminal_delivery_offload,
    )

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

    await recover_terminal_agent_task_claim(
        runner=runner,
        task_manager=task_manager,
        run_id=run_id,
        outcome="cancelled",
    )
    await _reap_terminal_srt_runner(run_id)
    await deliver_existing_terminal_run(
        db=runner.run_storage.db,
        agent_run_manager=runner.run_storage,
        completion_registry=completion_registry,
        run_id=run_id,
        run_db=run_terminal_delivery_offload,
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
    from gobby.agents.terminal_delivery import (
        deliver_existing_terminal_run,
        run_terminal_delivery_offload,
    )

    if effective_status == "error":
        error = "Agent self-reported error"
        failed_run = await run_terminal_delivery_offload(
            runner.run_storage.fail,
            run_id,
            error=error,
        )
        if failed_run is None:
            current = runner.get_run(run_id)
            logger.debug(
                "Error terminalization no-op for run %s; current status=%s",
                run_id,
                current.status if current else "missing",
            )
        else:
            await recover_terminal_agent_task_claim(
                runner=runner,
                task_manager=task_manager,
                run_id=run_id,
                outcome="failed",
            )
        await _reap_terminal_srt_runner(run_id)
        await deliver_existing_terminal_run(
            db=runner.run_storage.db,
            agent_run_manager=runner.run_storage,
            completion_registry=completion_registry,
            run_id=run_id,
            run_db=run_terminal_delivery_offload,
            message=f"Agent {run_id} failed",
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


async def stop_agent_run(
    *,
    run_id: str,
    runner: Any,
    agent_run_manager: Any,
    db: Any | None,
    lifecycle_monitor: Any | None,
    completion_registry: Any | None,
    task_manager: Any | None,
    session_manager: Any | None,
    kill_agent_process: Any,
    cleanup_terminal_artifacts: Any,
) -> dict[str, Any]:
    """Stop one agent run through the shared cancellation lifecycle."""
    from gobby.agents.terminal_delivery import (
        deliver_existing_terminal_run_in_scope,
        run_terminal_delivery_offload,
        shielded_terminal_delivery,
    )

    run = runner.get_run(run_id)
    if not run:
        return {"success": False, "error": f"Agent run {run_id} not found"}
    if run.status not in ("pending", "running"):
        return {"success": False, "error": f"Cannot stop agent in status: {run.status}"}

    kill_db = db or agent_run_manager.db

    async def stop_and_deliver() -> dict[str, Any]:
        try:
            result = cast(
                dict[str, Any],
                await kill_agent_process(
                    run,
                    kill_db,
                    signal_name="TERM",
                    close_terminal=True,
                ),
            )
            if not result.get("success") and result.get("error_code") != KILL_ERROR_NO_TARGET_PID:
                return result

            transitioned = await terminalize_cancelled_agent_run(
                runner=runner,
                run_id=run_id,
                terminal_reason="user_cancelled",
                lifecycle_monitor=lifecycle_monitor,
                completion_registry=completion_registry,
                task_manager=task_manager,
                message=f"Agent {run_id} cancelled",
            )
            if not transitioned:
                current = runner.get_run(run_id)
                logger.debug(
                    "stop_agent_run no-op for run %s; current status=%s",
                    run_id,
                    current.status if current else "missing",
                )

            result["terminal_reason"] = "user_cancelled"
            await cleanup_terminal_artifacts(
                run_id=run.id,
                db=kill_db,
                tmux_session_name=run.tmux_session_name,
                agent_session_id=run.child_session_id,
                debug=False,
                session_manager=session_manager,
                result=result,
            )
            return {
                "success": True,
                "message": f"Agent run {run_id} stopped",
                "run_id": run_id,
                "status": "cancelled",
                "terminal_reason": "user_cancelled",
                "agent_step_instances_deleted": result.get("agent_step_instances_deleted"),
            }
        finally:
            await deliver_existing_terminal_run_in_scope(
                db=kill_db,
                agent_run_manager=agent_run_manager,
                completion_registry=completion_registry,
                run_id=run_id,
                run_db=run_terminal_delivery_offload,
            )

    response = await shielded_terminal_delivery(run_id, stop_and_deliver)
    if response is None:
        return {"success": False, "error": "Daemon shutdown is in progress"}
    return response

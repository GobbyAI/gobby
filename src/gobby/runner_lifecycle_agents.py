"""Agent restart recovery and shutdown cancellation helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


class _RunStorageWithTmuxCleanup(Protocol):
    def clear_tmux_session_name(self, run_id: str, tmux_session_name: str) -> bool: ...


def _register_persisted_completion_subscribers(
    runner: GobbyRunner,
    completion_id: str,
    *,
    continuation_prompt: str | None = None,
) -> list[str]:
    """Load persisted waiters for a completion ID into the in-memory registry."""
    if not runner.pipeline_execution_manager or not runner.completion_registry:
        return []

    subscribers = runner.pipeline_execution_manager.get_completion_subscribers(completion_id)
    if subscribers:
        runner.completion_registry.register(
            completion_id,
            subscribers=subscribers,
            continuation_prompt=continuation_prompt,
        )
    return subscribers


def _cleanup_persisted_completion_subscribers(
    runner: GobbyRunner,
    completion_id: str,
    subscribers: list[str],
) -> None:
    """Drop persisted/in-memory subscriber state after a restart notification."""
    if not subscribers:
        return
    if runner.pipeline_execution_manager:
        runner.pipeline_execution_manager.remove_completion_subscribers(completion_id)
    if runner.completion_registry:
        runner.completion_registry.cleanup(completion_id)


_RUN_REPLAY_PAGE_SIZE = 500


async def _recover_agent_runs_after_restart(runner: GobbyRunner) -> int:
    """Rehydrate completion events for active agent rows after daemon restart."""
    if runner.agent_runner is None or runner.completion_registry is None:
        return 0

    rehydrated = 0
    seen_ids: set[str] = set()
    while True:
        batch = runner.agent_runner.run_storage.list_active(limit=_RUN_REPLAY_PAGE_SIZE)
        if not batch:
            break
        new_in_batch = 0
        for run in batch:
            if run.id in seen_ids:
                continue
            seen_ids.add(run.id)
            new_in_batch += 1
            if runner.completion_registry.is_registered(run.id):
                continue
            subscribers: list[str] = []
            if runner.pipeline_execution_manager:
                subscribers = runner.pipeline_execution_manager.get_completion_subscribers(run.id)
            runner.completion_registry.register(
                run.id,
                subscribers=subscribers,
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )
            rehydrated += 1
        if new_in_batch < _RUN_REPLAY_PAGE_SIZE:
            break

    return rehydrated


async def _replay_daemon_restart_agent_cancellations(runner: GobbyRunner) -> int:
    """Replay durable wake notifications for daemon-restart agent cancellations."""
    if (
        runner.agent_runner is None
        or runner.pipeline_execution_manager is None
        or runner.completion_registry is None
    ):
        return 0

    replayed = 0
    seen_ids: set[str] = set()
    while True:
        batch = runner.agent_runner.run_storage.list_by_status(
            "cancelled", limit=_RUN_REPLAY_PAGE_SIZE
        )
        if not batch:
            break
        new_in_batch = 0
        for run in batch:
            if run.id in seen_ids:
                continue
            seen_ids.add(run.id)
            new_in_batch += 1
            if getattr(run, "terminal_reason", None) != "daemon_restart":
                continue

            await _cleanup_lingering_daemon_restart_tmux_session(
                run,
                runner.agent_runner.run_storage,
            )

            subscribers = runner.pipeline_execution_manager.get_completion_subscribers(run.id)
            if not subscribers:
                continue

            if not runner.completion_registry.is_registered(run.id):
                runner.completion_registry.register(
                    run.id,
                    subscribers=subscribers,
                    continuation_prompt=getattr(run, "continuation_prompt", None),
                )

            try:
                await runner.completion_registry.notify(
                    run.id,
                    result={
                        "status": "cancelled",
                        "terminal_reason": "daemon_restart",
                        "run_id": run.id,
                        "completion_id": run.id,
                    },
                    message=(
                        f"Agent {run.id} was interrupted by a daemon restart.\n"
                        "Status: cancelled (daemon restarted)"
                    ),
                )
            except Exception as e:
                logger.warning(
                    "Failed to replay daemon-restart cancellation for agent %s: %s",
                    run.id,
                    e,
                )
                continue

            _cleanup_persisted_completion_subscribers(runner, run.id, subscribers)
            replayed += 1
        if new_in_batch < _RUN_REPLAY_PAGE_SIZE:
            break

    return replayed


async def _cleanup_lingering_daemon_restart_tmux_session(
    run: object,
    run_storage: _RunStorageWithTmuxCleanup,
) -> bool:
    """Kill a tmux session left alive by a pre-fix daemon-restart cancellation."""
    tmux_session_name = getattr(run, "tmux_session_name", None)
    if not tmux_session_name:
        return False

    try:
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        session_name = str(tmux_session_name)
        killed = await TmuxSessionManager().kill_session(session_name, missing_ok=True)
        if killed:
            run_storage.clear_tmux_session_name(str(getattr(run, "id", "unknown")), session_name)
    except Exception as e:
        logger.warning(
            "Failed to clean lingering tmux session for cancelled agent %s: %s",
            getattr(run, "id", "unknown"),
            e,
        )
        return False

    if killed:
        logger.info(
            "Cleaned lingering tmux session %s for cancelled agent %s",
            tmux_session_name,
            getattr(run, "id", "unknown"),
        )
    return killed


async def _cancel_active_agent_runs_for_shutdown(runner: GobbyRunner) -> int:
    """Cancel live agent runs before subsystem teardown on daemon shutdown."""
    if runner.agent_lifecycle_monitor is None or runner.agent_runner is None:
        return 0

    from gobby.agents.kill import kill_agent as _kill_agent_process

    cancelled = 0
    for run in runner.agent_runner.run_storage.list_active(limit=1000):
        _register_persisted_completion_subscribers(
            runner,
            run.id,
            continuation_prompt=getattr(run, "continuation_prompt", None),
        )
        result = await _kill_agent_process(
            run,
            runner.database,
            signal_name="TERM",
            close_terminal=True,
        )
        if not result.get("success") and result.get("error") != "No target PID found":
            logger.warning(
                "Failed to stop active agent %s during shutdown: %s",
                run.id,
                result.get("error"),
            )
            continue

        transitioned = await runner.agent_lifecycle_monitor.terminalize_cancelled_run(
            run.id,
            terminal_reason="daemon_restart",
        )
        if transitioned:
            cancelled += 1

    return cancelled

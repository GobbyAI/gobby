"""Agent restart recovery and shutdown cancellation helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


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


def _cleanup_terminal_agent_completion_subscribers(runner: GobbyRunner) -> int:
    """Remove stale subscriber rows for agent runs already in a terminal state."""
    manager = runner.pipeline_execution_manager
    if manager is None:
        return 0
    cleanup = getattr(manager, "remove_completion_subscribers_for_terminal_agent_runs", None)
    if cleanup is None:
        return 0
    try:
        cleaned = cleanup()
    except Exception:
        logger.warning("Failed to clean terminal agent completion subscribers", exc_info=True)
        return 0
    if cleaned:
        logger.info("Cleaned %s terminal agent completion subscriber row(s)", cleaned)
    return int(cleaned)


_RUN_REPLAY_PAGE_SIZE = 500


async def _recover_agent_runs_after_restart(runner: GobbyRunner) -> int:
    """Rehydrate completion events for active agent rows after daemon restart."""
    _cleanup_terminal_agent_completion_subscribers(runner)
    if runner.agent_runner is None or runner.completion_registry is None:
        return 0

    rehydrated = 0
    seen_ids: set[str] = set()
    offset = 0
    while True:
        batch = runner.agent_runner.run_storage.list_active(
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        for run in batch:
            if run.id in seen_ids:
                continue
            seen_ids.add(run.id)
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
        offset += len(batch)
        if len(batch) < _RUN_REPLAY_PAGE_SIZE:
            break

    return rehydrated


async def _reconcile_agent_runs_after_restart(runner: GobbyRunner) -> int:
    """Reconnect active tmux-backed agent runs after daemon restart."""
    if runner.agent_runner is None:
        return 0

    reconciled = await _recover_agent_runs_after_restart(runner)
    active_runs = _list_active_agent_runs_once(runner)
    non_tmux_runs = [run for run in active_runs if not getattr(run, "tmux_session_name", None)]
    for run in non_tmux_runs:
        if _refresh_active_run_dispatch_mutex(runner, run):
            reconciled += 1

    tmux_runs = [run for run in active_runs if getattr(run, "tmux_session_name", None)]
    if not tmux_runs:
        return reconciled

    try:
        from gobby.agents.tmux import get_tmux_session_manager

        live_sessions = await get_tmux_session_manager().list_sessions()
    except Exception as e:
        logger.warning("Failed to list tmux sessions during agent restart reconciliation: %s", e)
        return reconciled

    live_by_name = {session.name: session for session in live_sessions}
    output_reader: Any | None = None
    for run in tmux_runs:
        run_id = str(run.id)
        session_name = str(run.tmux_session_name)
        live_info = live_by_name.get(session_name)
        if live_info is None or getattr(live_info, "pane_dead", False):
            if await _cleanup_missing_tmux_agent_run(runner, run, session_name):
                reconciled += 1
            continue

        run_storage = runner.agent_runner.run_storage
        pane_pid = getattr(live_info, "pane_pid", None)
        update_runtime = getattr(run_storage, "update_runtime", None)
        if pane_pid is not None and pane_pid != getattr(run, "pid", None):
            if callable(update_runtime):
                update_runtime(run_id, pid=pane_pid, tmux_session_name=session_name)
                reconciled += 1

        if output_reader is None:
            from gobby.agents.tmux import get_tmux_output_reader

            output_reader = get_tmux_output_reader()
        try:
            if await output_reader.start_reader(run_id, session_name):
                reconciled += 1
        except Exception as e:
            logger.warning(
                "Failed to restart tmux output reader for recovered agent %s: %s",
                run_id,
                e,
            )
        if _refresh_active_run_dispatch_mutex(runner, run):
            reconciled += 1

    return reconciled


def _refresh_active_run_dispatch_mutex(runner: GobbyRunner, run: Any) -> bool:
    """Extend the dispatch mutex for a run that survived daemon restart."""
    task_id = getattr(run, "task_id", None)
    run_id = getattr(run, "id", None)
    if not task_id or not run_id:
        return False

    db = getattr(runner, "database", None)
    if db is None:
        run_storage = getattr(getattr(runner, "agent_runner", None), "run_storage", None)
        db = getattr(run_storage, "db", None)
    if db is None:
        return False

    try:
        return TaskDispatchMutexManager(db).acquire_mutex(
            str(task_id),
            holder="dispatcher",
            kind="heartbeat",
            ttl_seconds=600,
            run_id=str(run_id),
        )
    except Exception as e:
        logger.warning(
            "Failed to refresh dispatch mutex for recovered agent %s: %s",
            run_id,
            e,
        )
        return False


def _list_active_agent_runs_once(runner: GobbyRunner) -> list[Any]:
    """List one de-duplicated view of active agent runs."""
    if runner.agent_runner is None:
        raise RuntimeError("Cannot list active agent runs: runner.agent_runner is not configured")
    run_storage = runner.agent_runner.run_storage
    active_runs: list[Any] = []
    seen_ids: set[str] = set()
    offset = 0
    while True:
        batch = run_storage.list_active(limit=_RUN_REPLAY_PAGE_SIZE, offset=offset)
        if not batch:
            break
        for run in batch:
            run_id = str(getattr(run, "id", ""))
            if not run_id or run_id in seen_ids:
                continue
            seen_ids.add(run_id)
            active_runs.append(run)
        offset += len(batch)
        if len(batch) < _RUN_REPLAY_PAGE_SIZE:
            break
    return active_runs


async def _cleanup_missing_tmux_agent_run(
    runner: GobbyRunner,
    run: Any,
    session_name: str,
) -> bool:
    """Fail an active run whose persisted tmux session did not survive restart."""
    monitor = runner.agent_lifecycle_monitor
    if monitor is None:
        return False

    get_cleanup_agent = getattr(monitor, "get_cleanup_agent", None)
    cleanup_agent = get_cleanup_agent() if callable(get_cleanup_agent) else None
    if not callable(cleanup_agent):
        logger.warning(
            "Cannot clean missing tmux-backed agent %s: cleanup handler unavailable",
            getattr(run, "id", "unknown"),
        )
        return False

    await cleanup_agent(
        run,
        terminal_payload=(f"tmux session {session_name!r} was missing after daemon restart"),
    )
    return True


async def _cancel_active_agent_runs_for_shutdown(runner: GobbyRunner) -> int:
    """Cancel live agent runs before subsystem teardown on daemon shutdown."""
    if runner.agent_lifecycle_monitor is None or runner.agent_runner is None:
        return 0

    from gobby.agents.kill import kill_agent as _kill_agent_process

    cancelled = 0
    for run in _list_active_agent_runs_once(runner):
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
            terminal_reason="daemon_stop",
        )
        if transitioned:
            cancelled += 1

    return cancelled

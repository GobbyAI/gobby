"""Agent completion recovery and shared restart-replay primitives."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.recovery_state import (
    is_daemon_stop_parked,
    is_reconciliation_pending,
)
from gobby.agents.sandbox_reaper import record_sandbox_retention, sweep_sandbox_run_roots
from gobby.agents.srt_process_cleanup import reap_orphaned_srt_runner_process_trees
from gobby.events.completion_registry import wake_result_is_delivered
from gobby.storage.agents import (
    TERMINAL_AGENT_RUN_STATUSES,
    LocalAgentRunManager,
)
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


async def _run_db(
    runner: GobbyRunner,
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        return await db_executor.run(operation, *args, **kwargs)
    return await asyncio.to_thread(operation, *args, **kwargs)


async def _rehydrate_active_agent_completion_subscribers(runner: GobbyRunner) -> int:
    """Restore durable subscribers for active runs into the live registry."""
    db = getattr(runner, "database", None)
    registry = getattr(runner, "completion_registry", None)
    if db is None or registry is None:
        return 0

    subscriber_manager = CompletionSubscriberManager(db)
    run_manager = LocalAgentRunManager(db)
    rehydrated = 0
    offset = 0
    while True:
        runs = await _run_db(
            runner,
            run_manager.list_active_for_machine,
            require_machine_id(),
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not runs:
            break
        for run in runs:
            if is_reconciliation_pending(run):
                continue
            subscribers = await _run_db(
                runner,
                subscriber_manager.get_completion_subscribers,
                run.id,
            )
            if not subscribers:
                continue
            registry.register(
                run.id,
                subscribers=subscribers,
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )
            rehydrated += 1
        offset += len(runs)
        if len(runs) < _RUN_REPLAY_PAGE_SIZE:
            break
    return rehydrated


async def _cleanup_terminal_agent_completion_subscribers(runner: GobbyRunner) -> int:
    """Redeliver retained terminal notifications and remove acknowledged rows."""
    db = getattr(runner, "database", None)
    wake_dispatcher = getattr(runner, "wake_dispatcher", None)
    wake = getattr(wake_dispatcher, "wake", None)
    if db is None or not callable(wake):
        return 0

    subscriber_manager = CompletionSubscriberManager(db)
    run_manager = LocalAgentRunManager(db)
    machine_id = require_machine_id()
    delivered_count = 0
    completion_ids = await _run_db(runner, subscriber_manager.list_completion_ids)
    for run_id in completion_ids:
        run = await _run_db(runner, run_manager.get, run_id)
        # Each machine recovers only its own runs; a foreign run's rows wait for it.
        if (
            run is None
            or run.machine_id != machine_id
            or run.status not in TERMINAL_AGENT_RUN_STATUSES
            or is_daemon_stop_parked(run)
        ):
            continue
        subscribers = await _run_db(
            runner,
            subscriber_manager.get_completion_subscribers,
            run.id,
        )
        acknowledged: list[str] = []
        payload = {"status": run.status, "run_id": run.id}
        message = f"Agent {run.id} reached terminal status {run.status}"
        for session_id in subscribers:
            try:
                outcome = await wake(session_id, message, payload)
            except Exception:
                logger.warning(
                    "Terminal completion redelivery failed for session %s (run %s)",
                    session_id,
                    run.id,
                    exc_info=True,
                )
                continue
            if wake_result_is_delivered(outcome):
                acknowledged.append(session_id)
        if acknowledged:
            await _run_db(
                runner,
                subscriber_manager.remove_completion_subscribers,
                run.id,
                session_ids=acknowledged,
            )
            delivered_count += len(acknowledged)
    return delivered_count


async def _recover_agent_completion_subscribers_on_startup(runner: GobbyRunner) -> int:
    """Rehydrate active subscribers, then replay retained terminal notifications."""
    recovered = 0
    try:
        recovered += await _rehydrate_active_agent_completion_subscribers(runner)
    except Exception:
        logger.warning("Failed to rehydrate active agent completion subscribers", exc_info=True)
    try:
        recovered += await _cleanup_terminal_agent_completion_subscribers(runner)
    except Exception:
        logger.warning("Failed to redeliver terminal agent completion subscribers", exc_info=True)
    return recovered


_RUN_REPLAY_PAGE_SIZE = 500


async def _recover_agent_runs_after_restart(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
    run_ids: frozenset[str] | None = None,
) -> int:
    """Rehydrate completion events for active agent rows after daemon restart."""
    if runner.agent_runner is None or runner.completion_registry is None:
        return 0

    rehydrated = 0
    seen_ids: set[str] = set()
    offset = 0
    while True:
        batch = await _run_db(
            runner,
            runner.agent_runner.run_storage.list_active_for_machine,
            require_machine_id(),
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        for run in batch:
            if run_ids is not None and str(run.id) not in run_ids:
                continue
            if not include_fenced and is_reconciliation_pending(run):
                continue
            if run.id in seen_ids:
                continue
            seen_ids.add(run.id)
            if runner.completion_registry.is_registered(run.id):
                continue
            runner.completion_registry.register(
                run.id,
                subscribers=[],
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )
            rehydrated += 1
        offset += len(batch)
        if len(batch) < _RUN_REPLAY_PAGE_SIZE:
            break

    return rehydrated


async def _reap_orphaned_srt_runners_on_startup(runner: GobbyRunner) -> int:
    """Reap managed SRT processes and schedule old run-root cleanup."""
    if runner.agent_runner is None:
        return 0
    active_runs = await _run_db(
        runner,
        _list_active_agent_runs_once,
        runner,
        include_fenced=True,
    )
    active_run_ids = {str(run.id) for run in active_runs}
    reaped_processes = await asyncio.to_thread(
        reap_orphaned_srt_runner_process_trees,
        active_run_ids,
    )
    runner._sandbox_run_root_sweep_task = asyncio.create_task(
        _sweep_sandbox_run_roots_on_startup(runner),
        name="sandbox-run-root-sweep",
    )
    return reaped_processes


async def _sweep_sandbox_run_roots_on_startup(runner: GobbyRunner) -> None:
    """Sweep old run roots against active state read when the task starts."""
    if runner.agent_runner is None:
        return
    run_storage = runner.agent_runner.run_storage
    try:
        active_runs = await _run_db(
            runner,
            _list_active_agent_runs_once,
            runner,
            include_fenced=True,
        )
        # Collect inside the sweep thread, persist on the loop through the
        # runner's database executor.
        retained: dict[str, dict[str, str]] = {}

        def collect(run_id: str, retention: dict[str, str]) -> None:
            retained[run_id] = retention

        await sweep_sandbox_run_roots(
            {str(run.id) for run in active_runs},
            record_retention=collect,
        )
        for run_id, retention in retained.items():
            await _run_db(runner, record_sandbox_retention, run_storage.db, run_id, retention)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Sandbox run-root sweep failed during startup")


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
        mutexes = TaskDispatchMutexManager(db)
        mutex = mutexes.get_mutex(str(task_id))
        if mutex is not None:
            if mutex.run_id is not None and str(mutex.run_id) == str(run_id) and mutex.lease_holder:
                return mutexes.refresh_mutex_for_run(
                    str(task_id),
                    str(run_id),
                    lease_holder=mutex.lease_holder,
                    ttl_seconds=600,
                )
            return False
        return mutexes.acquire_mutex(
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


def _list_active_agent_runs_once(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
) -> list[Any]:
    """List one de-duplicated view of active agent runs.

    ``include_fenced`` keeps reconciliation_pending runs in the view; shutdown
    preservation and the reclassification pass must see fenced runs too.
    """
    if runner.agent_runner is None:
        raise RuntimeError("Cannot list active agent runs: runner.agent_runner is not configured")
    run_storage = runner.agent_runner.run_storage
    active_runs: list[Any] = []
    seen_ids: set[str] = set()
    offset = 0
    while True:
        batch = run_storage.list_active_for_machine(
            require_machine_id(),
            limit=_RUN_REPLAY_PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        for run in batch:
            if not include_fenced and is_reconciliation_pending(run):
                continue
            run_id = str(getattr(run, "id", ""))
            if not run_id or run_id in seen_ids:
                continue
            seen_ids.add(run_id)
            active_runs.append(run)
        offset += len(batch)
        if len(batch) < _RUN_REPLAY_PAGE_SIZE:
            break
    return active_runs


_MAX_NON_TASK_RESUME_FAILURES = 3


async def _retry_parked_non_task_resumes(runner: GobbyRunner) -> int:
    """Relaunch parked daemon-stop agents that no task dispatcher owns.

    Task-owned parked runs ride the dispatch tick; runs with no task would
    otherwise sit parked until the recovery-window reaper. Retries share the
    dispatcher's failure budget; exhausted candidates wait for the reaper.
    """
    config = runner.config_runtime.capture().snapshot.active
    if runner.agent_runner is None:
        return 0

    from gobby.agents.resume_executor import resume_agent_run
    from gobby.storage.agent_resume import increment_daemon_resume_failure_count

    run_storage = runner.agent_runner.run_storage
    try:
        candidates = await _run_db(
            runner,
            run_storage.list_parked_non_task_resume_candidates,
            machine_id=require_machine_id(),
        )
    except Exception:
        logger.warning("Failed to list parked non-task resume candidates", exc_info=True)
        return 0

    resumed = 0
    for run in candidates:
        metadata = run.resume_metadata_json or {}
        if not metadata:
            continue
        raw_count = metadata.get("daemon_stop_resume_failure_count")
        failure_count = raw_count if isinstance(raw_count, int) else 0
        if failure_count >= _MAX_NON_TASK_RESUME_FAILURES:
            continue
        try:
            result = await resume_agent_run(
                run,
                resume_metadata=metadata,
                runner=runner.agent_runner,
                session_manager=runner.session_manager,
                daemon_config=config,
                completion_registry=runner.completion_registry,
                agent_pane_reserver=getattr(runner.websocket_server, "agent_pane_reserver", None),
            )
        except Exception:
            logger.warning("Non-task parked resume raised for run %s", run.id, exc_info=True)
            await _run_db(
                runner, increment_daemon_resume_failure_count, runner.database, run_id=run.id
            )
            continue
        if result.success:
            resumed += 1
        else:
            logger.info(
                "Non-task parked resume failed for run %s: %s",
                run.id,
                result.error,
            )
            await _run_db(
                runner, increment_daemon_resume_failure_count, runner.database, run_id=run.id
            )
    return resumed

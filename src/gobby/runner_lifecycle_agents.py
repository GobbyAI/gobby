"""Agent restart recovery and shutdown cancellation helpers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.recovery_state import (
    is_daemon_stop_parked,
    is_reconciliation_pending,
)
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

# Per-process marker deduplicating parent recovery notifications: reconnect
# messages fire once per daemon boot, not once per reconciliation pass.
_BOOT_MARKER = uuid.uuid4().hex


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
    delivered_count = 0
    completion_ids = await _run_db(runner, subscriber_manager.list_completion_ids)
    for run_id in completion_ids:
        run = await _run_db(runner, run_manager.get, run_id)
        if (
            run is None
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
    """Reap managed SRT runners without an active agent-run row."""
    if runner.agent_runner is None:
        return 0
    active_runs = await _run_db(
        runner,
        _list_active_agent_runs_once,
        runner,
        include_fenced=True,
    )
    active_run_ids = {str(run.id) for run in active_runs}
    return await asyncio.to_thread(
        reap_orphaned_srt_runner_process_trees,
        active_run_ids,
    )


async def _reconcile_agent_runs_after_restart(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
    resolved_run_ids: set[str] | None = None,
) -> int:
    """Reconnect active tmux-backed agent runs after daemon restart."""
    credential_manager = getattr(runner, "managed_credential_manager", None)
    if credential_manager is not None:
        try:
            await _run_db(runner, credential_manager.reconcile)
            await _run_db(runner, credential_manager.rotate_due)
        except Exception:
            logger.error("Managed credential startup reconciliation failed")
    if runner.agent_runner is None:
        return 0

    reconciled = await _resolve_provisional_daemon_resumes(
        runner,
        include_fenced=include_fenced,
        resolved_run_ids=resolved_run_ids,
    )
    reconciled += await _recover_agent_runs_after_restart(runner, include_fenced=include_fenced)
    active_runs = await _run_db(
        runner,
        _list_active_agent_runs_once,
        runner,
        include_fenced=include_fenced,
    )
    non_tmux_runs = [run for run in active_runs if not getattr(run, "terminal_id", None)]
    for run in non_tmux_runs:
        mutex_refreshed = await _run_db(runner, _refresh_active_run_dispatch_mutex, runner, run)
        if mutex_refreshed:
            reconciled += 1
        if resolved_run_ids is not None and (mutex_refreshed or not getattr(run, "task_id", None)):
            resolved_run_ids.add(str(run.id))

    tmux_runs = [run for run in active_runs if getattr(run, "terminal_id", None)]
    if not tmux_runs:
        return reconciled

    try:
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        live_sessions = await TmuxSessionManager().list_sessions()
    except Exception as e:
        logger.warning("Failed to list tmux sessions during agent restart reconciliation: %s", e)
        return reconciled

    live_by_name = {session.name: session for session in live_sessions}
    output_reader: Any | None = None
    for run in tmux_runs:
        run_id = str(run.id)
        manager = getattr(runner, "terminal_manager", None)
        row = None if manager is None else manager.get(str(run.terminal_id))
        session_name = str(row.session_name or run.terminal_id) if row is not None else str(run.terminal_id)
        live_info = live_by_name.get(session_name)
        if live_info is None or getattr(live_info, "pane_dead", False):
            if await _cleanup_missing_tmux_agent_run(runner, run, session_name):
                reconciled += 1
                if resolved_run_ids is not None:
                    resolved_run_ids.add(run_id)
            continue

        run_storage = runner.agent_runner.run_storage
        pane_pid = getattr(live_info, "pane_pid", None)
        if pane_pid is not None and pane_pid != getattr(run, "pid", None):
            await _run_db(
                runner,
                run_storage.update_runtime,
                run_id,
                pid=pane_pid,
                terminal_id=str(run.terminal_id),
            )
            reconciled += 1

        if output_reader is None:
            from gobby.agents.tmux import get_tmux_output_reader

            output_reader = get_tmux_output_reader()
        reader_ready = True
        try:
            if await output_reader.start_reader(run_id, session_name):
                reconciled += 1
        except Exception as e:
            reader_ready = False
            logger.warning(
                "Failed to restart tmux output reader for recovered agent %s: %s",
                run_id,
                e,
            )
        mutex_refreshed = await _run_db(runner, _refresh_active_run_dispatch_mutex, runner, run)
        if mutex_refreshed:
            reconciled += 1
        metadata = getattr(run, "resume_metadata_json", None) or {}
        parent_session_id = metadata.get("parent_session_id")
        child_session_id = getattr(run, "child_session_id", None)
        if isinstance(parent_session_id, str) and isinstance(child_session_id, str):
            from gobby.agents.resume_finalization import notify_parent_of_recovery

            await asyncio.to_thread(
                notify_parent_of_recovery,
                runner.database,
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                content=f"Reconnected agent run {run_id} after daemon restart.",
                run_id=run_id,
                event="reconnected",
                dedupe_key=_BOOT_MARKER,
            )
        if (
            resolved_run_ids is not None
            and reader_ready
            and (mutex_refreshed or not getattr(run, "task_id", None))
        ):
            resolved_run_ids.add(run_id)

    return reconciled


def _find_live_tmux_by_planned_name(live_by_name: dict[str, Any], session_name: str) -> Any | None:
    """Correlate a provisional run's tmux session exactly, then by title prefix.

    The spawner appends a uniqueness suffix to the planned title, so a crash
    between spawn and runtime-persist leaves only the planned prefix to match.
    """
    live_info = live_by_name.get(session_name)
    if live_info is not None:
        return live_info
    prefix = f"{session_name}-"
    matches = sorted(
        (name for name in live_by_name if name.startswith(prefix)),
    )
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple live tmux sessions match planned title %r: %s",
            session_name,
            matches,
        )
    return live_by_name[matches[0]]


async def _resolve_provisional_daemon_resume_row(
    runner: GobbyRunner,
    run: Any,
    live_by_name: dict[str, Any],
) -> bool:
    """Resolve one provisional successor to exactly one ownership chain."""
    from gobby.agents.resume_executor import resume_agent_run
    from gobby.agents.resume_finalization import (
        finalize_resume_handoff_async,
        notify_parent_of_recovery,
    )
    from gobby.storage.agent_resume import rollback_prepared_daemon_resume

    config = runner.config_runtime.capture().snapshot.active
    if runner.agent_runner is None:
        return False
    run_storage = runner.agent_runner.run_storage
    metadata = run.resume_metadata_json or {}
    phase = metadata.get("daemon_stop_resume_phase")
    original_run_id = metadata.get("resumed_from_run_id")
    child_session_id = getattr(run, "child_session_id", None)
    if not isinstance(original_run_id, str) or not isinstance(child_session_id, str):
        logger.error("Provisional daemon resume %s has incomplete ownership metadata", run.id)
        return False

    if phase == "prepared":
        return bool(
            await asyncio.to_thread(
                rollback_prepared_daemon_resume,
                runner.database,
                original_run_id=original_run_id,
                successor_run_id=run.id,
                child_session_id=child_session_id,
            )
        )

    session_name = metadata.get("daemon_stop_resume_spawn_key") or getattr(
        run, "terminal_id", None
    )
    live_info = (
        _find_live_tmux_by_planned_name(live_by_name, session_name)
        if isinstance(session_name, str)
        else None
    )
    if live_info is not None and not getattr(live_info, "pane_dead", False):
        pane_pid = getattr(live_info, "pane_pid", None)
        await _run_db(
            runner,
            run_storage.update_runtime,
            run.id,
            pid=pane_pid,
            terminal_id=getattr(run, "terminal_id", None),
        )
        if phase == "launch_requested":
            await _run_db(
                runner,
                run_storage.transition_resume_phase,
                run.id,
                expected_phase="launch_requested",
                new_phase="runtime_persisted",
            )
        if run.status == "pending":
            await _run_db(runner, run_storage.start, run.id)
        await finalize_resume_handoff_async(
            runner.database,
            original_run_id=original_run_id,
            successor_run_id=run.id,
            child_session_id=child_session_id,
            completion_registry=runner.completion_registry,
        )
        parent_session_id = metadata.get("parent_session_id")
        if isinstance(parent_session_id, str):
            await asyncio.to_thread(
                notify_parent_of_recovery,
                runner.database,
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                content=f"Reconnected agent run {run.id} after daemon restart.",
                run_id=run.id,
                event="reconnected",
                dedupe_key=_BOOT_MARKER,
            )
        return True

    await finalize_resume_handoff_async(
        runner.database,
        original_run_id=original_run_id,
        successor_run_id=run.id,
        child_session_id=child_session_id,
        completion_registry=runner.completion_registry,
    )
    monitor = runner.agent_lifecycle_monitor
    if monitor is None:
        raise RuntimeError("Cannot park dead provisional resume without lifecycle monitor")
    await monitor.terminalize_cancelled_run(run.id, terminal_reason="daemon_stop")
    parked = await _run_db(runner, run_storage.get, run.id)
    if parked is None:
        raise RuntimeError(f"Dead provisional resume {run.id} disappeared during parking")
    result = await resume_agent_run(
        parked,
        resume_metadata=parked.resume_metadata_json or metadata,
        runner=runner.agent_runner,
        session_manager=runner.session_manager,
        daemon_config=config,
        completion_registry=runner.completion_registry,
    )
    if not result.success:
        logger.warning(
            "Immediate retry for dead provisional resume %s remains parked: %s",
            run.id,
            result.error,
        )
    return True


async def _resolve_provisional_daemon_resumes(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
    resolved_run_ids: set[str] | None = None,
) -> int:
    """Resolve every durable resume phase before normal run classification."""
    if runner.agent_runner is None:
        return 0

    from gobby.agents.tmux.session_manager import TmuxSessionManager

    run_storage = runner.agent_runner.run_storage
    provisional = await _run_db(
        runner,
        run_storage.list_provisional_daemon_resumes,
        machine_id=require_machine_id(),
        limit=_RUN_REPLAY_PAGE_SIZE,
    )
    if not provisional:
        return 0

    live_sessions = await TmuxSessionManager().list_sessions()
    live_by_name = {session.name: session for session in live_sessions}
    resolved = 0
    for run in provisional:
        if not include_fenced and is_reconciliation_pending(run):
            continue
        try:
            if await _resolve_provisional_daemon_resume_row(runner, run, live_by_name):
                resolved += 1
                if resolved_run_ids is not None:
                    resolved_run_ids.add(str(run.id))
        except Exception:
            # One bad provisional row must not abort boot reconciliation.
            logger.warning(
                "Failed to resolve provisional daemon resume %s",
                run.id,
                exc_info=True,
            )

    return resolved


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


async def _run_agent_hook_replay_barrier(
    runner: GobbyRunner,
    *,
    timeout_seconds: float = 5.0,
) -> bool:
    """Replay hook ingress and fence unresolved runs from restart classification."""
    agent_runner = getattr(runner, "agent_runner", None)
    http_server = getattr(runner, "http_server", None)
    app = getattr(http_server, "app", None)
    if app is None:
        return True

    from gobby.hooks.inbox import drain_hook_inbox_barrier

    result = await drain_hook_inbox_barrier(
        app,
        timeout_seconds=timeout_seconds,
    )
    if not result.timed_out:
        return True

    unresolved_run_ids = set(result.unresolved_run_ids)
    unresolved_session_ids = result.unresolved_session_ids
    session_manager = getattr(runner, "session_manager", None)
    if unresolved_session_ids and session_manager is None:
        logger.warning("Hook replay timed out while session services were unavailable")
        return False
    if session_manager is not None:
        for session_id in unresolved_session_ids:
            session = await _run_db(runner, session_manager.get, session_id)
            run_id = getattr(session, "agent_run_id", None)
            if isinstance(run_id, str) and run_id:
                unresolved_run_ids.add(run_id)

    if not unresolved_run_ids:
        logger.info(
            "Hook inbox replay timed out after replaying %d envelope(s); "
            "%d session identity/identities produced no agent runs",
            result.replayed,
            len(unresolved_session_ids),
        )
        return True
    if agent_runner is None:
        logger.warning("Hook replay timed out while agent services were unavailable")
        return False

    run_storage = agent_runner.run_storage
    active_run_ids: set[str] = set()
    terminal_run_ids: set[str] = set()
    missing_run_ids: set[str] = set()
    unclassified_run_ids: set[str] = set()
    for run_id in unresolved_run_ids:
        try:
            run = await _run_db(runner, run_storage.get, run_id)
        except Exception:
            logger.warning("Failed to load unresolved agent run %s", run_id, exc_info=True)
            unclassified_run_ids.add(run_id)
            continue
        if run is None:
            missing_run_ids.add(run_id)
            continue
        if run.status in TERMINAL_AGENT_RUN_STATUSES:
            terminal_run_ids.add(run_id)
            continue
        if run.status not in {"pending", "running"}:
            logger.warning(
                "Unclassified unresolved agent run %s with status %r",
                run_id,
                run.status,
            )
            unclassified_run_ids.add(run_id)
            continue
        await _run_db(
            runner,
            run_storage.merge_resume_metadata,
            run_id,
            {"reconciliation_pending": True},
        )
        active_run_ids.add(run_id)

    if terminal_run_ids or missing_run_ids:
        logger.info(
            "Agent hook replay barrier settled %d terminal and %d missing run reference(s)",
            len(terminal_run_ids),
            len(missing_run_ids),
        )
    if active_run_ids or unclassified_run_ids:
        logger.warning(
            "Agent hook replay barrier timed out with %d active fenced run(s) and "
            "%d unclassified run lookup(s)",
            len(active_run_ids),
            len(unclassified_run_ids),
        )
        return False
    return True


_RECLASSIFY_SETTLE_TIMEOUT_SECONDS = 5.0


async def _reclassify_reconciliation_pending_runs(runner: GobbyRunner) -> int:
    """Let the lifecycle monitor reclassify fenced runs after inbox replay settles."""
    if runner.agent_runner is None:
        return 0
    run_storage = runner.agent_runner.run_storage
    pending = await _run_db(
        runner,
        run_storage.list_reconciliation_pending,
        machine_id=require_machine_id(),
        limit=_RUN_REPLAY_PAGE_SIZE,
    )
    if not pending:
        # Nothing is fenced: running the replay barrier here would fence
        # healthy runs whenever transient inbox residue trips its timeout.
        return 0
    settled = await _run_agent_hook_replay_barrier(
        runner,
        timeout_seconds=_RECLASSIFY_SETTLE_TIMEOUT_SECONDS,
    )
    if not settled:
        return 0
    resolved_run_ids: set[str] = set()
    reconciled = await _reconcile_agent_runs_after_restart(
        runner,
        include_fenced=True,
        resolved_run_ids=resolved_run_ids,
    )
    for run in pending:
        if str(run.id) not in resolved_run_ids:
            continue
        await _run_db(
            runner,
            run_storage.merge_resume_metadata,
            run.id,
            {"reconciliation_pending": False},
        )
    return reconciled


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


async def _cleanup_missing_tmux_agent_run(
    runner: GobbyRunner,
    run: Any,
    session_name: str,
) -> bool:
    """Park and immediately resume a run whose tmux session did not survive."""
    config = runner.config_runtime.capture().snapshot.active
    monitor = runner.agent_lifecycle_monitor
    if monitor is None or runner.agent_runner is None:
        return False

    from gobby.agents.resume_executor import resume_agent_run

    transitioned = await monitor.terminalize_cancelled_run(
        run.id,
        terminal_reason="daemon_stop",
    )
    parked = await _run_db(runner, runner.agent_runner.run_storage.get, run.id)
    if not transitioned or parked is None:
        return False

    result = await resume_agent_run(
        parked,
        resume_metadata=parked.resume_metadata_json or {},
        runner=runner.agent_runner,
        session_manager=runner.session_manager,
        daemon_config=config,
        completion_registry=runner.completion_registry,
    )
    if not result.success:
        logger.warning(
            "Agent %s remained parked after missing tmux session %r: %s",
            run.id,
            session_name,
            result.error,
        )
    return result.success

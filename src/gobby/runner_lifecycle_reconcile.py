"""Restart reconciliation of active agent runs and their terminals."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.recovery_state import is_reconciliation_pending
from gobby.runner_lifecycle_agents import (
    _RUN_REPLAY_PAGE_SIZE,
    _list_active_agent_runs_once,
    _recover_agent_runs_after_restart,
    _refresh_active_run_dispatch_mutex,
    _run_agent_hook_replay_barrier,
    _run_db,
)
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime, configured_tmux_runtime
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.storage.terminals import Terminal, TerminalManager

logger = logging.getLogger("gobby.runner_lifecycle")

# Per-process marker deduplicating parent recovery notifications: reconnect
# messages fire once per daemon boot, not once per reconciliation pass.
_BOOT_MARKER = uuid.uuid4().hex


async def _reconcile_agent_runs_after_restart(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
    resolved_run_ids: set[str] | None = None,
) -> int:
    """Reconnect active terminal-backed agent runs after daemon restart."""
    credential_manager = getattr(runner, "managed_credential_manager", None)
    if credential_manager is not None:
        try:
            await _run_db(runner, credential_manager.reconcile)
            await _run_db(runner, credential_manager.rotate_due)
        except Exception:
            # Startup deliberately continues past this, but discarding the
            # reason leaves nothing in the log to act on -- the bare message
            # names neither the failure nor which of the two calls raised.
            logger.exception("Managed credential startup reconciliation failed")
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
    manager = getattr(runner, "terminal_manager", None)

    tmux_runs: list[tuple[Any, Terminal | None]] = []
    native_runs: list[tuple[Any, Terminal]] = []
    for run in active_runs:
        terminal_id = getattr(run, "terminal_id", None)
        if not terminal_id:
            reconciled += await _refresh_surviving_run(runner, run, resolved_run_ids)
            continue
        row = _terminal_row(manager, str(terminal_id))
        # A run linked to no terminals row predates the migration: its
        # terminal_id is the tmux session name, so it reconciles as tmux.
        if row is not None and row.backend == "native":
            native_runs.append((run, row))
        else:
            tmux_runs.append((run, row))

    if native_runs:
        reconciled += await _reconcile_native_runs(runner, native_runs, resolved_run_ids)
    if tmux_runs:
        reconciled += await _reconcile_tmux_runs(runner, tmux_runs, resolved_run_ids)
    return reconciled


def _terminal_row(manager: TerminalManager | None, terminal_id: str) -> Terminal | None:
    if manager is None:
        return None
    try:
        return manager.get(terminal_id)
    except ValueError:
        # Pre-migration links carry a tmux session name, never a row id.
        return None


def _tmux_runtime(runner: GobbyRunner) -> TmuxTerminalRuntime:
    """The daemon's tmux runtime; the configured socket when no registry is wired."""
    registry = getattr(runner, "terminal_runtime_registry", None)
    if registry is not None:
        return cast(TmuxTerminalRuntime, registry.resolve("tmux"))
    return configured_tmux_runtime()


async def _refresh_surviving_run(
    runner: GobbyRunner,
    run: Any,
    resolved_run_ids: set[str] | None,
) -> int:
    """Extend the dispatch mutex of a run that survived restart intact."""
    mutex_refreshed = await _run_db(runner, _refresh_active_run_dispatch_mutex, runner, run)
    if resolved_run_ids is not None and (mutex_refreshed or not getattr(run, "task_id", None)):
        resolved_run_ids.add(str(run.id))
    return 1 if mutex_refreshed else 0


async def _reconcile_native_runs(
    runner: GobbyRunner,
    runs: list[tuple[Any, Terminal]],
    resolved_run_ids: set[str] | None,
) -> int:
    """A native run survives restart only while its host terminal is still live."""
    registry = getattr(runner, "terminal_runtime_registry", None)
    if registry is None:
        logger.warning(
            "No terminal runtime registry; %d native agent runs left unreconciled", len(runs)
        )
        return 0
    runtime = registry.resolve("native")
    manager = getattr(runner, "terminal_manager", None)
    reconciled = 0
    for run, row in runs:
        try:
            live = await runtime.is_live(row)
        except Exception:
            logger.debug("native is_live probe failed for %s", run.id, exc_info=True)
            live = False
        if live:
            reconciled += await _refresh_surviving_run(runner, run, resolved_run_ids)
            continue
        if manager is not None:
            await _run_db(runner, manager.mark_orphaned, row.id)
        if await _cleanup_missing_terminal_agent_run(runner, run, row.id):
            reconciled += 1
            if resolved_run_ids is not None:
                resolved_run_ids.add(str(run.id))
    return reconciled


async def _reconcile_tmux_runs(
    runner: GobbyRunner,
    runs: list[tuple[Any, Terminal | None]],
    resolved_run_ids: set[str] | None,
) -> int:
    """Refresh live tmux runs; park and resume the ones whose session is gone."""
    if runner.agent_runner is None:
        return 0
    tmux_runtime = _tmux_runtime(runner)
    try:
        live_sessions = await tmux_runtime.list_sessions()
    except Exception as e:
        logger.warning("Failed to list tmux sessions during agent restart reconciliation: %s", e)
        return 0

    live_by_name = {session.name: session for session in live_sessions}
    output_reader: Any | None = None
    reconciled = 0
    for run, row in runs:
        run_id = str(run.id)
        session_name = (
            str(row.session_name or row.spawn_key or run.terminal_id)
            if row is not None
            else str(run.terminal_id)
        )
        live_info = live_by_name.get(session_name)
        if live_info is None or getattr(live_info, "pane_dead", False):
            if row is not None:
                try:
                    if await tmux_runtime.is_live(row):
                        continue
                except Exception:
                    logger.debug("tmux is_live probe failed for %s", run_id, exc_info=True)
            if await _cleanup_missing_terminal_agent_run(runner, run, session_name):
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

    session_name = metadata.get("daemon_stop_resume_spawn_key") or getattr(run, "terminal_id", None)
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

    run_storage = runner.agent_runner.run_storage
    provisional = await _run_db(
        runner,
        run_storage.list_provisional_daemon_resumes,
        machine_id=require_machine_id(),
        limit=_RUN_REPLAY_PAGE_SIZE,
    )
    if not provisional:
        return 0

    live_sessions = await _tmux_runtime(runner).list_sessions()
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


async def _cleanup_missing_terminal_agent_run(
    runner: GobbyRunner,
    run: Any,
    terminal_ref: str,
) -> bool:
    """Park and immediately resume a run whose terminal did not survive restart."""
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
            "Agent %s remained parked after missing terminal %r: %s",
            run.id,
            terminal_ref,
            result.error,
        )
    return result.success

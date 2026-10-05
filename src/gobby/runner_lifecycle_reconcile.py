"""Restart reconciliation of active agent runs and their terminals."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.recovery_state import is_reconciliation_pending
from gobby.runner_hook_replay import _run_agent_hook_replay_barrier
from gobby.runner_lifecycle_agents import (
    _RUN_REPLAY_PAGE_SIZE,
    _list_active_agent_runs_once,
    _recover_agent_runs_after_restart,
    _refresh_active_run_dispatch_mutex,
    _run_db,
)
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.storage.terminals import Terminal, TerminalManager

logger = logging.getLogger("gobby.runner_lifecycle")

# Per-process marker deduplicating parent recovery notifications: reconnect
# messages fire once per daemon boot, not once per reconciliation pass.
_BOOT_MARKER = uuid.uuid4().hex


async def _rotate_due_managed_credentials(runner: GobbyRunner) -> int:
    """Rotate live managed principals whose bindings entered the rotation window."""
    credential_manager = getattr(runner, "managed_credential_manager", None)
    if credential_manager is None:
        return 0
    try:
        rotated = await _run_db(runner, credential_manager.rotate_due)
    except Exception:
        logger.exception("Managed credential rotation failed")
        return 0
    return len(rotated)


async def _reconcile_agent_runs_after_restart(
    runner: GobbyRunner,
    *,
    include_fenced: bool = False,
    resolved_run_ids: set[str] | None = None,
    run_ids: frozenset[str] | None = None,
) -> int:
    """Reconnect startup runs, or only the captured periodic recovery subset."""
    credential_manager = getattr(runner, "managed_credential_manager", None)
    if credential_manager is not None and run_ids is None:
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
        run_ids=run_ids,
    )
    reconciled += await _recover_agent_runs_after_restart(
        runner, include_fenced=include_fenced, run_ids=run_ids
    )
    active_runs = await _run_db(
        runner,
        _list_active_agent_runs_once,
        runner,
        include_fenced=include_fenced,
    )
    manager = getattr(runner, "terminal_manager", None)

    native_runs: list[tuple[Any, Terminal]] = []
    missing_runs: list[Any] = []
    unsupported_runs: list[tuple[Any, Terminal]] = []
    for run in active_runs:
        if run_ids is not None and str(run.id) not in run_ids:
            continue
        terminal_id = getattr(run, "terminal_id", None)
        if not terminal_id:
            reconciled += await _refresh_surviving_run(runner, run, resolved_run_ids)
            continue
        if manager is None:
            await _fence_reconciliation_run(
                runner,
                run,
                "terminal_manager_unavailable",
                "terminal state cannot be verified during restart reconciliation",
            )
            continue
        row = await _terminal_row(runner, manager, str(terminal_id))
        if row is None:
            await _fence_reconciliation_run(
                runner,
                run,
                "terminal_row_missing",
                f"terminal {terminal_id} has no authoritative exit evidence",
            )
        elif getattr(row, "agent_run_id", None) not in {None, str(run.id)}:
            await _fence_reconciliation_run(
                runner,
                run,
                "terminal_ownership_mismatch",
                f"terminal {row.id} belongs to a different agent run",
            )
        elif row.state == "exited":
            missing_runs.append(run)
        elif row.backend == "native":
            native_runs.append((run, row))
        else:
            unsupported_runs.append((run, row))

    if native_runs:
        reconciled += await _reconcile_native_runs(runner, native_runs, resolved_run_ids)
    for run in missing_runs:
        if await _cleanup_missing_terminal_agent_run(runner, run, str(run.terminal_id)):
            reconciled += 1
            if resolved_run_ids is not None:
                resolved_run_ids.add(str(run.id))
    for run, row in unsupported_runs:
        await _fence_unsupported_terminal(runner, run, row)
    return reconciled


async def _terminal_row(
    runner: GobbyRunner, manager: TerminalManager | None, terminal_id: str
) -> Terminal | None:
    if manager is None:
        return None
    try:
        return cast("Terminal | None", await _run_db(runner, manager.get, terminal_id))
    except ValueError:
        # A stale pre-row terminal reference is missing evidence, not a pane route.
        return None


async def _fence_unsupported_terminal(
    runner: GobbyRunner,
    run: Any,
    row: Terminal,
) -> None:
    """Leave a non-native live or uncertain run fenced without duplicating its CLI."""
    reason = f"unsupported_terminal_backend:{row.backend}:{row.state}"
    await _fence_reconciliation_run(
        runner,
        run,
        reason,
        f"terminal {row.id} has {row.backend} backend in {row.state} state",
    )


async def _fence_reconciliation_run(
    runner: GobbyRunner,
    run: Any,
    reason: str,
    detail: str,
) -> None:
    """Persist one operator-visible hold reason and warn only when it changes."""
    metadata = getattr(run, "resume_metadata_json", None) or {}
    if (
        metadata.get("reconciliation_pending") is True
        and metadata.get("reconciliation_blocked_reason") == reason
    ):
        return
    assert runner.agent_runner is not None
    await _run_db(
        runner,
        runner.agent_runner.run_storage.merge_resume_metadata,
        run.id,
        {
            "reconciliation_pending": True,
            "reconciliation_blocked_reason": reason,
        },
    )
    logger.warning("Agent %s remains reconciliation-fenced: %s", run.id, detail)


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
    """Reconnect live native runs and fence uncertain terminal states."""
    reconciled = 0
    for run, row in runs:
        if row.state == "live":
            reconciled += await _refresh_surviving_run(runner, run, resolved_run_ids)
            continue
        await _fence_reconciliation_run(
            runner,
            run,
            f"native_terminal_{row.state}",
            f"native terminal {row.id} has uncertain liveness",
        )
    return reconciled


async def _resolve_provisional_daemon_resume_row(
    runner: GobbyRunner,
    run: Any,
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

    terminal_manager = getattr(runner, "terminal_manager", None)
    if terminal_manager is None:
        await _fence_reconciliation_run(
            runner,
            run,
            "provisional_terminal_manager_unavailable",
            "terminal state cannot be verified during daemon resume",
        )
        return False
    rows = await _run_db(runner, terminal_manager.list_for_session, child_session_id)
    unsettled = [candidate for candidate in rows if candidate.state != "exited"]
    if len(unsettled) > 1 or (unsettled and unsettled[0].agent_run_id != run.id):
        await _fence_reconciliation_run(
            runner,
            run,
            "provisional_terminal_ownership_mismatch",
            "session has another terminal with uncertain liveness",
        )
        return False
    row = next((candidate for candidate in rows if candidate.agent_run_id == run.id), None)
    if row is not None and row.state == "exited" and unsettled:
        await _fence_reconciliation_run(
            runner,
            run,
            "provisional_terminal_ownership_mismatch",
            "an older terminal for this run has uncertain liveness",
        )
        return False
    if row is not None and row.state == "orphaned":
        await _fence_reconciliation_run(
            runner,
            run,
            "provisional_terminal_orphaned",
            f"terminal {row.id} has uncertain liveness",
        )
        return False
    if row is not None and row.state not in {"pending", "live", "exited"}:
        await _fence_reconciliation_run(
            runner,
            run,
            "provisional_terminal_state_unknown",
            f"terminal {row.id} has unexpected state {row.state}",
        )
        return False
    if row is not None and row.state != "exited" and row.backend != "native":
        await _fence_unsupported_terminal(runner, run, row)
        return False
    if row is not None and row.state == "pending":
        await _fence_reconciliation_run(
            runner,
            run,
            "provisional_native_terminal_pending",
            f"native terminal {row.id} has not settled its spawn",
        )
        return False
    if row is not None and row.state == "live":
        process = row.process if isinstance(row.process, dict) else {}
        row_pid = process.get("pgid")
        pid = row_pid if isinstance(row_pid, int) and row_pid > 0 else getattr(run, "pid", None)
        await _run_db(
            runner,
            run_storage.update_runtime,
            run.id,
            pid=pid,
            terminal_id=row.id,
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
        agent_pane_reserver=getattr(runner.websocket_server, "agent_pane_reserver", None),
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
    run_ids: frozenset[str] | None = None,
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

    resolved = 0
    for run in provisional:
        if run_ids is not None and str(run.id) not in run_ids:
            continue
        if not include_fenced and is_reconciliation_pending(run):
            continue
        try:
            if await _resolve_provisional_daemon_resume_row(runner, run):
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
    # Capture before yielding to inbox replay: fresh runs admitted during the
    # barrier belong to the live monitor, not daemon-restart reconciliation.
    run_ids = frozenset(str(run.id) for run in pending)
    outcome = await _run_agent_hook_replay_barrier(
        runner,
        timeout_seconds=_RECLASSIFY_SETTLE_TIMEOUT_SECONDS,
    )
    if not outcome.settled:
        return 0
    resolved_run_ids: set[str] = set()
    reconciled = await _reconcile_agent_runs_after_restart(
        runner,
        include_fenced=True,
        resolved_run_ids=resolved_run_ids,
        run_ids=run_ids,
    )
    for run in pending:
        if str(run.id) not in resolved_run_ids:
            continue
        await _run_db(
            runner,
            run_storage.merge_resume_metadata,
            run.id,
            {"reconciliation_pending": False, "reconciliation_blocked_reason": None},
        )
    return reconciled


async def _cleanup_missing_terminal_agent_run(
    runner: GobbyRunner,
    run: Any,
    terminal_ref: str,
) -> bool:
    """Park and immediately resume a run whose terminal did not survive restart.

    A one-shot close-review caller or closed-task run is left running instead: a resume
    would start a session its review does not know about. Reporting it resolved
    clears its fence so lifecycle reconciliation terminalizes it. A task-close
    reviewer is parked without a resume for the same reason.
    """
    config = runner.config_runtime.capture().snapshot.active
    monitor = runner.agent_lifecycle_monitor
    if monitor is None or runner.agent_runner is None:
        return False

    if getattr(run, "task_id", None) and not getattr(run, "is_interactive", False):
        from gobby.agents.run_completion import (
            bound_task_is_closed,
            cooperative_close_handoff_pending,
            ended_caller_close_review_outcome,
        )

        db = runner.agent_runner.run_storage.db
        for claims_run in (
            cooperative_close_handoff_pending,
            bound_task_is_closed,
            ended_caller_close_review_outcome,
        ):
            if await _run_db(runner, claims_run, db, run):
                logger.info(
                    "Leaving agent %s to task reconciliation after missing terminal %r",
                    run.id,
                    terminal_ref,
                )
                return True

    from gobby.agents.resume_executor import resume_agent_run
    from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT

    transitioned = await monitor.terminalize_cancelled_run(
        run.id,
        terminal_reason="daemon_stop",
    )
    if getattr(run, "agent_name", None) == TASK_CLOSE_REVIEWER_AGENT:
        # Its review stays bound to this run; close-review reconciliation
        # retries the parked reviewer, so a resumed successor is never started.
        return bool(transitioned)
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
        agent_pane_reserver=getattr(runner.websocket_server, "agent_pane_reserver", None),
    )
    if not result.success:
        logger.warning(
            "Agent %s remained parked after missing terminal %r: %s",
            run.id,
            terminal_ref,
            result.error,
        )
    return result.success

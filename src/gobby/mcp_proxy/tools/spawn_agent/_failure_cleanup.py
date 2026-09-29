"""Cleanup helpers for failed spawn attempts."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

from gobby.storage.terminals import Terminal
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.runtime import TerminalRuntime
from gobby.utils import spawn

logger = logging.getLogger(__name__)

_SPAWN_TERM_GRACE_SECONDS = 0.2
_RUN_STARTTIMES: dict[str, str] = {}


class SpawnCleanupOnce:
    """Owns cleanup for one spawn attempt.

    The first call starts one task and every later call awaits that same task.
    Callers drain it under cancellation and re-raise the first cancellation only
    after it settles, so each cleanup step runs exactly once.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def run(self, cleanup: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self._task is None:
            self._task = asyncio.create_task(cleanup())
        task = self._task
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        if cancellation is not None:
            raise cancellation


def remember_spawn_pid(pid: int | None, *, run_id: str | None = None) -> str | None:
    if pid is None:
        return None
    stamp = _pid_starttime(pid)
    if stamp is None:
        return None
    if run_id is not None:
        _RUN_STARTTIMES[run_id] = stamp
    return stamp


def _pid_starttime(pid: int) -> str | None:
    try:
        completed = spawn.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    stamp = completed.stdout.strip()
    return stamp or None


def _pid_matches_remembered(pid: int, expected: str | None) -> bool:
    if expected is None:
        return False
    return _pid_starttime(pid) == expected


def _forget_spawn_run(run_id: str | None) -> None:
    if run_id is not None:
        _RUN_STARTTIMES.pop(run_id, None)


def _log_step_failure(
    phase: str, run_id: str | None, terminal_id: str | None, exc: BaseException
) -> None:
    # Exception text can carry prompt, environment or command-line content.
    logger.warning(
        "Spawn cleanup step %s failed for run %s (terminal %s): %s",
        phase,
        run_id,
        terminal_id,
        type(exc).__name__,
    )


async def cleanup_created_isolation(
    handler: Any,
    spawn_config: Any,
    *,
    cleanup: bool,
    run_id: str | None,
) -> None:
    """Remove isolation created for a spawn that failed before any terminal existed.

    ``run_id`` is ``None`` when the failure precedes run-id allocation.
    """
    if not cleanup:
        return
    try:
        await handler.cleanup_environment(spawn_config)
    except Exception as exc:
        _log_step_failure("isolation", run_id, None, exc)


async def cleanup_failed_spawn(
    runner: Any,
    run_id: str,
    error: str,
    handler: Any,
    spawn_config: Any,
    *,
    completion_registry: Any | None,
    cleanup_isolation: bool,
    task_manager: Any | None,
    child_session_id: str | None = None,
    pid: int | None = None,
    terminal_id: str | None = None,
    prior_attempt: tuple[int, datetime] | None = None,
    cleanup_once: SpawnCleanupOnce | None = None,
    attempt_terminal_known: bool = False,
) -> None:
    """Clean a failed spawn once; every step runs independently and nothing raises.

    ``cleanup_once`` is the attempt's owner. Without one, this call is its own
    attempt. ``prior_attempt`` is the failed result's pre-bump pair, which lets a
    late isolation step recognize a rolled-back bump. ``attempt_terminal_known``
    says ``terminal_id`` is everything the attempt owns (``None``: no terminal),
    so a run bound to another terminal belongs to another attempt and is left alone.
    """
    once = cleanup_once or SpawnCleanupOnce()
    await once.run(
        functools.partial(
            _cleanup_failed_spawn,
            runner,
            run_id,
            error,
            handler,
            spawn_config,
            completion_registry=completion_registry,
            cleanup_isolation=cleanup_isolation,
            task_manager=task_manager,
            child_session_id=child_session_id,
            pid=pid,
            terminal_id=terminal_id,
            prior_attempt=prior_attempt,
            attempt_terminal_known=attempt_terminal_known,
        )
    )


async def _cleanup_failed_spawn(
    runner: Any,
    run_id: str,
    error: str,
    handler: Any,
    spawn_config: Any,
    *,
    completion_registry: Any | None,
    cleanup_isolation: bool,
    task_manager: Any | None,
    child_session_id: str | None,
    pid: int | None,
    terminal_id: str | None,
    prior_attempt: tuple[int, datetime] | None,
    attempt_terminal_known: bool,
) -> None:
    run_storage = getattr(runner, "run_storage", None)
    terminal_manager = getattr(runner, "terminal_manager", None)
    run = None
    run_unread = False
    if run_storage is not None:
        try:
            run = await asyncio.to_thread(run_storage.get, run_id)
        except Exception as exc:
            run_unread = True
            _log_step_failure("read_run", run_id, terminal_id, exc)
    bound_terminal_id = _string_attr(run, "terminal_id")
    # A run bound to a terminal other than this attempt's belongs to that other
    # attempt: failing, terminalizing or unbinding it would kill that attempt. An
    # unread run may be that other attempt's too. This attempt still terminates its
    # own terminal, and its isolation waits on that proof.
    foreign_run = attempt_terminal_known and (
        run_unread or bound_terminal_id not in {None, terminal_id}
    )
    if foreign_run:
        logger.info(
            "Leaving run %s to the attempt on terminal %s; cleaning only terminal %s",
            run_id,
            bound_terminal_id,
            terminal_id,
        )
        # The run's pid and start time are the other attempt's; this attempt's
        # process is proven through its own terminal.
        pid = None
    else:
        if run_storage is not None:
            try:
                await asyncio.to_thread(run_storage.record_spawn_error, run_id, error)
            except Exception as exc:
                _log_step_failure("record_error", run_id, terminal_id, exc)
        if child_session_id is None:
            child_session_id = _string_attr(run, "child_session_id")
        if pid is None:
            raw_pid = getattr(run, "pid", None)
            pid = raw_pid if isinstance(raw_pid, int) else None
        if terminal_id is None:
            terminal_id = _string_attr(run, "terminal_id")

    held = terminal_id is not None and in_doubt_spawns.holds(terminal_id)
    settled = False
    try:
        terminal = None
        if terminal_id is not None and terminal_manager is not None:
            candidate = await asyncio.to_thread(terminal_manager.get, terminal_id)
            if isinstance(getattr(candidate, "backend", None), str):
                terminal = candidate
        settled = await _terminate_spawn_process(
            run_storage=run_storage if run is not None and not foreign_run else None,
            run_id=run_id,
            pid=pid,
            expected_starttime=_RUN_STARTTIMES.get(run_id),
            terminal_manager=terminal_manager,
            terminal_runtime_registry=getattr(runner, "terminal_runtime_registry", None),
            terminal=terminal,
        )
    except Exception as exc:
        _log_step_failure("terminate", run_id, terminal_id, exc)
    if not foreign_run:
        try:
            _forget_spawn_run(run_id)
        except Exception as exc:
            _log_step_failure("forget_run", run_id, terminal_id, exc)
    if run_storage is not None and not foreign_run:
        from gobby.mcp_proxy.tools.agent_cancellation import (
            terminalize_cancelled_agent_run,
        )

        try:
            await terminalize_cancelled_agent_run(
                runner=runner,
                run_id=run_id,
                terminal_reason="spawn_rollback",
                lifecycle_monitor=getattr(runner, "agent_lifecycle_monitor", None),
                completion_registry=completion_registry,
                task_manager=task_manager,
                message=error,
            )
        except Exception as exc:
            _log_step_failure("terminalize_run", run_id, terminal_id, exc)
        db = getattr(run_storage, "db", None)
        if db is not None:
            from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state

            try:
                await asyncio.to_thread(
                    cleanup_agent_runtime_state,
                    db,
                    run_id=run_id,
                    child_session_id=child_session_id,
                    terminal_reason="spawn_rollback",
                )
            except Exception as exc:
                _log_step_failure("runtime_state", run_id, terminal_id, exc)
    try:
        await _cleanup_isolation_step(
            handler,
            spawn_config,
            cleanup=cleanup_isolation,
            run_id=run_id,
            terminal_id=terminal_id,
            terminal_manager=terminal_manager,
            held=held,
            settled=settled,
            prior_attempt=prior_attempt,
        )
    except Exception as exc:
        _log_step_failure("isolation", run_id, terminal_id, exc)
    if foreign_run:
        return
    try:
        await asyncio.to_thread(
            _delete_child_session, runner, run_storage, run_id, child_session_id
        )
    except Exception as exc:
        _log_step_failure("delete_child_session", run_id, terminal_id, exc)


async def _cleanup_isolation_step(
    handler: Any,
    spawn_config: Any,
    *,
    cleanup: bool,
    run_id: str,
    terminal_id: str | None,
    terminal_manager: Any | None,
    held: bool,
    settled: bool,
    prior_attempt: tuple[int, datetime] | None,
) -> None:
    """Remove created isolation only once no process of this attempt can still use it."""
    if not cleanup:
        return

    async def remove() -> None:
        # Logged here with its context: the in-doubt owner may run this removal later.
        try:
            await handler.cleanup_environment(spawn_config)
        except Exception as exc:
            _log_step_failure("isolation", run_id, terminal_id, exc)

    if held and terminal_id is not None:
        # The in-doubt owner runs the removal once after a proven settlement.
        if in_doubt_spawns.defer(terminal_id, remove):
            return
        if not await _attempt_ended(terminal_manager, terminal_id, prior_attempt):
            logger.info(
                "Keeping created isolation for run %s: terminal %s is still unresolved",
                run_id,
                terminal_id,
            )
            return
    elif not settled:
        logger.warning(
            "Keeping created isolation for run %s: kill of terminal %s was not proven",
            run_id,
            terminal_id,
        )
        return
    await remove()


async def _attempt_ended(
    terminal_manager: Any | None,
    terminal_id: str,
    prior_attempt: tuple[int, datetime] | None,
) -> bool:
    """Decide from the row an ended claim left whether this attempt can still run.

    The owner drained its create or bump and read the row back before releasing,
    so the row is final for this attempt. ``exited``, no row, or a row still at the
    failed result's pre-bump pair (the bump rolled back) mean nothing of this
    attempt runs. Any other row errs toward keeping.
    """
    if terminal_manager is None:
        return False
    row = await asyncio.to_thread(terminal_manager.get, terminal_id)
    if row is None or row.state == "exited":
        return True
    return prior_attempt is not None and (row.attempt_generation, row.attempt_started_at) == (
        prior_attempt
    )


async def start_run_or_cleanup(
    runner: Any,
    run_id: str,
    handler: Any,
    spawn_config: Any,
    *,
    completion_registry: Any | None,
    cleanup_isolation: bool,
    task_manager: Any | None,
    child_session_id: str | None,
    pid: int | None,
    terminal_id: str | None,
    cleanup_once: SpawnCleanupOnce | None = None,
) -> dict[str, Any] | None:
    async def fail(error: str) -> dict[str, Any]:
        await cleanup_failed_spawn(
            runner,
            run_id,
            error,
            handler,
            spawn_config,
            completion_registry=completion_registry,
            cleanup_isolation=cleanup_isolation,
            task_manager=task_manager,
            child_session_id=child_session_id,
            pid=pid,
            terminal_id=terminal_id,
            cleanup_once=cleanup_once,
            # The caller passes the attempt's own terminal from its spawn result.
            attempt_terminal_known=True,
        )
        return {
            "success": False,
            "error": error,
            "run_id": run_id,
            "child_session_id": child_session_id,
        }

    try:
        start_skipped = await asyncio.to_thread(runner.run_storage.start, run_id) is None
    except Exception as exc:
        logger.warning("Failed to mark agent run %s as running: %s", run_id, type(exc).__name__)
        return await fail(f"Failed to mark agent run {run_id} as running: {exc}")

    if not start_skipped:
        return None
    try:
        current = await asyncio.to_thread(runner.run_storage.get, run_id)
    except Exception as exc:
        logger.warning(
            "Failed to read agent run %s after start conflict: %s", run_id, type(exc).__name__
        )
        return await fail(f"Failed to read agent run {run_id} after start conflict: {exc}")
    if current is not None and current.status == "running":
        return None
    return await fail("Agent run was no longer pending after spawn")


def _delete_child_session(
    runner: Any,
    run_storage: Any,
    run_id: str,
    child_session_id: str | None,
) -> None:
    if child_session_id is None:
        return
    session_storage = getattr(getattr(runner, "child_session_manager", None), "_storage", None)
    if session_storage is None:
        return
    # Failures reach the delete_child_session step, which logs them with context.
    db = getattr(run_storage, "db", None) or getattr(session_storage, "db", None)
    if db is not None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE agent_runs SET child_session_id = NULL WHERE id = %s",
                (run_id,),
            )
    session_storage.delete(child_session_id)


async def _terminate_spawn_process(
    *,
    run_storage: Any | None = None,
    run_id: str | None = None,
    pid: int | None,
    expected_starttime: str | None = None,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
    terminal: Any | None,
) -> bool:
    """Terminate the spawn's terminal and process; true only when the kill is proven."""
    settled = True
    if terminal is not None:
        settled = await _kill_spawn_terminal(
            run_storage,
            run_id,
            terminal,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )
    if pid is None:
        return settled
    if expected_starttime is None or not await asyncio.to_thread(
        _pid_matches_remembered, pid, expected_starttime
    ):
        return settled
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return settled
    except Exception as exc:
        logger.warning(
            "Failed to terminate spawn pid %s: %s",
            pid,
            type(exc).__name__,
            extra={"pid": pid},
        )
    await asyncio.sleep(_SPAWN_TERM_GRACE_SECONDS)
    if await asyncio.to_thread(_pid_matches_remembered, pid, expected_starttime):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.warning(
                "Failed to kill spawn pid %s: %s",
                pid,
                type(exc).__name__,
                extra={"pid": pid},
            )
            return False
    return settled


async def _kill_spawn_terminal(
    run_storage: Any | None,
    run_id: str | None,
    terminal: Terminal,
    *,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> bool:
    """Record only what the kill proved (1.9 kill truth).

    A held in-doubt id is not terminated. A terminate that raises, or a session the
    backend still reports afterwards, is a failed kill: the row is marked by
    ``mark_kill_failed`` (``live`` becomes ``orphaned``; ``pending`` stays) and
    this returns false.
    """
    from gobby.agents.capture import backend_session_present

    proven = False
    if not in_doubt_spawns.holds(terminal.id) and terminal_runtime_registry is not None:
        try:
            runtime = terminal_runtime_registry.resolve(terminal.backend)
            await _capture_then_kill_spawn_session(run_storage, run_id, runtime, terminal)
            proven = not await backend_session_present(runtime, terminal)
        except Exception as exc:
            logger.warning(
                "Failed to terminate %s terminal %s: %s",
                terminal.backend,
                terminal.id,
                type(exc).__name__,
            )
    if terminal_manager is None:
        return proven
    if not proven:
        await asyncio.to_thread(
            terminal_manager.mark_kill_failed,
            terminal.id,
            attempt_generation=terminal.attempt_generation,
            attempt_started_at=terminal.attempt_started_at,
        )
        return False
    # Settle only the attempt that was killed; a newer attempt keeps the row.
    attempt = {
        "attempt_generation": terminal.attempt_generation,
        "attempt_started_at": terminal.attempt_started_at,
    }
    if terminal.state == "pending":
        await asyncio.to_thread(terminal_manager.fail_pending_attempt, terminal.id, **attempt)
    elif terminal.state in {"live", "orphaned"}:
        await asyncio.to_thread(terminal_manager.mark_exited_attempt, terminal.id, **attempt)
    return True


async def _capture_then_kill_spawn_session(
    run_storage: Any | None,
    run_id: str | None,
    runtime: TerminalRuntime,
    terminal: Terminal,
) -> None:
    """Capture a failed spawn into its run row, then terminate through its runtime.

    The caller terminalizes the run afterwards, so the policy's terminal step only
    re-reads the row. Without a run row, or when the policy cannot complete, terminate
    the backend resource directly.
    """
    resource_name = terminal.spawn_key or terminal.id

    async def capture() -> str:
        return (await runtime.snapshot_full(terminal)).text

    async def terminate() -> bool:
        await runtime.terminate(terminal, _SPAWN_TERM_GRACE_SECONDS)
        return True

    if run_storage is not None and run_id is not None:
        from gobby.agents.capture import capture_then_kill_async

        async def keep_run(_action: Any, _reason: str | None) -> Any:
            return await asyncio.to_thread(run_storage.get, run_id)

        try:
            termination = await capture_then_kill_async(
                storage=run_storage,
                run_id=run_id,
                session_name=resource_name,
                action="cancel",
                reason="spawn_rollback",
                session_alive=lambda: runtime.is_live(terminal),
                capture=capture,
                kill=terminate,
                terminalize=keep_run,
            )
        except Exception as exc:
            logger.warning(
                "Capture policy failed for spawn rollback of %s (%s): %s",
                run_id,
                resource_name,
                type(exc).__name__,
            )
        else:
            if termination.success:
                return
    await runtime.terminate(terminal, _SPAWN_TERM_GRACE_SECONDS)


def _string_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else None

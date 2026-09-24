"""Cleanup helpers for failed spawn attempts."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import TerminalRuntime
from gobby.utils import spawn

_SPAWN_TERM_GRACE_SECONDS = 0.2
_RUN_STARTTIMES: dict[str, str] = {}


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


async def cleanup_created_isolation(
    handler: Any,
    spawn_config: Any,
    *,
    cleanup: bool,
) -> None:
    if not cleanup:
        return
    try:
        await handler.cleanup_environment(spawn_config)
    except Exception as exc:
        logging.getLogger(__name__).warning("Spawn failure isolation cleanup failed: %s", exc)


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
) -> None:
    run_storage = getattr(runner, "run_storage", None)
    if run_storage is not None:
        await asyncio.to_thread(run_storage.record_spawn_error, run_id, error)
    run = await asyncio.to_thread(run_storage.get, run_id) if run_storage is not None else None
    if child_session_id is None:
        child_session_id = _string_attr(run, "child_session_id")
    if pid is None:
        raw_pid = getattr(run, "pid", None)
        pid = raw_pid if isinstance(raw_pid, int) else None
    if terminal_id is None:
        terminal_id = _string_attr(run, "terminal_id")
    terminal_manager = getattr(runner, "terminal_manager", None)
    terminal_runtime_registry = getattr(runner, "terminal_runtime_registry", None)
    terminal = None
    if terminal_id is not None and terminal_manager is not None:
        candidate = await asyncio.to_thread(terminal_manager.get, terminal_id)
        if isinstance(getattr(candidate, "backend", None), str):
            terminal = candidate
    await _terminate_spawn_process(
        run_storage=run_storage if run is not None else None,
        run_id=run_id,
        pid=pid,
        expected_starttime=_RUN_STARTTIMES.get(run_id),
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
        terminal=terminal,
    )
    _forget_spawn_run(run_id)
    if run_storage is not None:
        from gobby.mcp_proxy.tools.agent_cancellation import (
            terminalize_cancelled_agent_run,
        )

        await terminalize_cancelled_agent_run(
            runner=runner,
            run_id=run_id,
            terminal_reason="spawn_rollback",
            lifecycle_monitor=getattr(runner, "agent_lifecycle_monitor", None),
            completion_registry=completion_registry,
            task_manager=task_manager,
            message=error,
        )
        db = getattr(run_storage, "db", None)
        if db is not None:
            from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state

            await asyncio.to_thread(
                cleanup_agent_runtime_state,
                db,
                run_id=run_id,
                child_session_id=child_session_id,
                terminal_reason="spawn_rollback",
            )
    await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation)
    await asyncio.to_thread(_delete_child_session, runner, run_storage, run_id, child_session_id)


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
) -> dict[str, Any] | None:
    try:
        start_skipped = await asyncio.to_thread(runner.run_storage.start, run_id) is None
    except Exception as exc:
        error = f"Failed to mark agent run {run_id} as running: {exc}"
        logging.getLogger(__name__).warning(error)
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
        )
        return {
            "success": False,
            "error": error,
            "run_id": run_id,
            "child_session_id": child_session_id,
        }

    if not start_skipped:
        return None
    try:
        current = await asyncio.to_thread(runner.run_storage.get, run_id)
    except Exception as exc:
        error = f"Failed to read agent run {run_id} after start conflict: {exc}"
        logging.getLogger(__name__).warning(error)
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
        )
        return {
            "success": False,
            "error": error,
            "run_id": run_id,
            "child_session_id": child_session_id,
        }
    if current is not None and current.status == "running":
        return None

    error = "Agent run was no longer pending after spawn"
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
    )
    return {
        "success": False,
        "error": error,
        "run_id": run_id,
        "child_session_id": child_session_id,
    }


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
    try:
        db = getattr(run_storage, "db", None) or getattr(session_storage, "db", None)
        if db is not None:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE agent_runs SET child_session_id = NULL WHERE id = %s",
                    (run_id,),
                )
        session_storage.delete(child_session_id)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to delete failed spawn child session %s: %s",
            child_session_id,
            exc,
        )


async def _terminate_spawn_process(
    *,
    run_storage: Any | None = None,
    run_id: str | None = None,
    pid: int | None,
    expected_starttime: str | None = None,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
    terminal: Any | None,
) -> None:
    if terminal is not None and terminal_runtime_registry is not None:
        try:
            runtime = terminal_runtime_registry.resolve(terminal.backend)
            await _capture_then_kill_spawn_session(
                run_storage,
                run_id,
                runtime,
                terminal,
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to terminate %s terminal %s: %s",
                terminal.backend,
                terminal.id,
                exc,
            )
        if terminal_manager is not None:
            if terminal.state == "pending":
                await asyncio.to_thread(terminal_manager.fail_pending, terminal.id)
            elif terminal.state in {"live", "orphaned"}:
                await asyncio.to_thread(terminal_manager.mark_exited, terminal.id)
    if pid is not None:
        if expected_starttime is None or not await asyncio.to_thread(
            _pid_matches_remembered, pid, expected_starttime
        ):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to terminate spawn pid %s: %s",
                pid,
                exc,
                extra={"pid": pid},
            )
        await asyncio.sleep(_SPAWN_TERM_GRACE_SECONDS)
        if await asyncio.to_thread(_pid_matches_remembered, pid, expected_starttime):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Failed to kill spawn pid %s: %s",
                    pid,
                    exc,
                    extra={"pid": pid},
                )


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
            logging.getLogger(__name__).warning(
                "Capture policy failed for spawn rollback of %s (%s): %s",
                run_id,
                resource_name,
                exc,
            )
        else:
            if termination.success:
                return
    await runtime.terminate(terminal, _SPAWN_TERM_GRACE_SECONDS)


def _string_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else None

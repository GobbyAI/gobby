"""Cleanup helpers for failed spawn attempts."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from typing import Any

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
        completed = subprocess.run(
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
    tmux_session_name: str | None = None,
    tmux_socket_name: str | None = None,
    tmux_socket_path: str | None = None,
) -> None:
    run_storage = getattr(runner, "run_storage", None)
    run = run_storage.get(run_id) if run_storage is not None else None
    if child_session_id is None:
        child_session_id = _string_attr(run, "child_session_id")
    if pid is None:
        raw_pid = getattr(run, "pid", None)
        pid = raw_pid if isinstance(raw_pid, int) else None
    if tmux_session_name is None:
        tmux_session_name = _string_attr(run, "tmux_session_name")
    await _terminate_spawn_process(
        pid=pid,
        expected_starttime=_RUN_STARTTIMES.get(run_id),
        tmux_session_name=tmux_session_name,
        tmux_socket_name=tmux_socket_name,
        tmux_socket_path=tmux_socket_path,
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

            cleanup_agent_runtime_state(
                db,
                run_id=run_id,
                child_session_id=child_session_id,
                terminal_reason="spawn_rollback",
            )
    await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation)
    _delete_child_session(runner, run_storage, run_id, child_session_id)


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
) -> dict[str, Any] | None:
    try:
        start_skipped = runner.run_storage.start(run_id) is None
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
        current = runner.run_storage.get(run_id)
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
    pid: int | None,
    expected_starttime: str | None = None,
    tmux_session_name: str | None,
    tmux_socket_name: str | None,
    tmux_socket_path: str | None,
) -> None:
    if tmux_session_name:
        try:
            from gobby.agents.tmux import get_tmux_session_manager

            manager = get_tmux_session_manager()
            await manager.kill_session(tmux_session_name, missing_ok=True)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to kill tmux session %s (socket=%s path=%s): %s",
                tmux_session_name,
                tmux_socket_name,
                tmux_socket_path,
                exc,
            )
    if pid is not None:
        if expected_starttime is None or not _pid_matches_remembered(pid, expected_starttime):
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
        if _pid_matches_remembered(pid, expected_starttime):
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


def _string_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else None

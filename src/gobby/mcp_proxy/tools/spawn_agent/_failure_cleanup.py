"""Cleanup helpers for failed spawn attempts."""

from __future__ import annotations

import logging
from typing import Any


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
    child_session_id: str | None = None,
) -> None:
    run_storage = getattr(runner, "run_storage", None)
    if run_storage is not None:
        run = run_storage.get(run_id)
        if child_session_id is None:
            child_session_id = _string_attr(run, "child_session_id")
        from gobby.mcp_proxy.tools.agent_cancellation import (
            terminalize_cancelled_agent_run,
        )

        await terminalize_cancelled_agent_run(
            runner=runner,
            run_id=run_id,
            terminal_reason="spawn_rollback",
            lifecycle_monitor=getattr(handler, "agent_lifecycle_monitor", None),
            completion_registry=completion_registry,
            task_manager=getattr(handler, "task_manager", None),
            message=error,
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


def _string_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else None

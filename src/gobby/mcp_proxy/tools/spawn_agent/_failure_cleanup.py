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
    cleanup_isolation: bool,
    child_session_id: str | None = None,
) -> None:
    run_storage = getattr(runner, "run_storage", None)
    if run_storage is not None:
        child_session_id = _fail_run(run_storage, run_id, error, child_session_id)
    await cleanup_created_isolation(handler, spawn_config, cleanup=cleanup_isolation)
    _delete_child_session(runner, run_storage, run_id, child_session_id)


async def start_run_or_cleanup(
    runner: Any,
    run_id: str,
    handler: Any,
    spawn_config: Any,
    *,
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

    error = "Agent run was no longer pending after spawn"
    await cleanup_failed_spawn(
        runner,
        run_id,
        error,
        handler,
        spawn_config,
        cleanup_isolation=cleanup_isolation,
        child_session_id=child_session_id,
    )
    return {
        "success": False,
        "error": error,
        "run_id": run_id,
        "child_session_id": child_session_id,
    }


def _fail_run(
    run_storage: Any,
    run_id: str,
    error: str,
    child_session_id: str | None,
) -> str | None:
    try:
        failed_run = run_storage.fail(run_id, error=error)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to mark agent_run %s as failed: %s", run_id, exc
        )
        failed_run = None
    if failed_run is None:
        try:
            failed_run = run_storage.get(run_id)
        except Exception:
            failed_run = None
    return _string_attr(failed_run, "child_session_id") or child_session_id


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

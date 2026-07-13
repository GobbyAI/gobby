"""Best-effort dispatch mutex release helpers for task MCP tools."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager


def _release_current_agent_dispatch_mutex(
    ctx: RegistryContext,
    *,
    task_id: str,
    session_id: str,
    run_id: str | None = None,
) -> bool:
    """Release the spawn lease only for the running agent that owns this session."""
    if run_id is None:
        run_id = _current_agent_dispatch_mutex_run_id(
            ctx,
            task_id=task_id,
            session_id=session_id,
        )
    if run_id is None:
        return False
    try:
        return TaskDispatchMutexManager(ctx.task_manager.db).clear_by_run_id(run_id) > 0
    except Exception:
        return False


def _current_agent_dispatch_mutex_run_id(
    ctx: RegistryContext,
    *,
    task_id: str,
    session_id: str,
) -> str | None:
    """Return the spawn-lease run ID only when the current agent owns it."""
    db = ctx.task_manager.db
    mutexes = TaskDispatchMutexManager(db)
    try:
        mutex = mutexes.get_mutex(task_id)
    except Exception:
        return None
    if mutex is None or not isinstance(mutex.run_id, str) or not mutex.run_id:
        return None

    try:
        row = _current_agent_mutex_owner(db, mutex.run_id, task_id, session_id)
    except Exception:
        return None
    if row is None:
        return None

    try:
        if row["id"] != mutex.run_id:
            return None
    except Exception:
        return None
    return mutex.run_id


def _current_agent_mutex_owner(
    db: Any,
    run_id: str,
    task_id: str,
    session_id: str,
) -> Any | None:
    row = db.fetchone(
        """
        SELECT id
          FROM agent_runs
         WHERE id = %s
           AND child_session_id = %s
           AND task_id = %s
           AND status = 'running'
        """,
        (run_id, session_id, task_id),
    )
    if row is not None:
        return row

    return db.fetchone(
        """
        SELECT owner.id
          FROM agent_runs AS owner
          JOIN agent_runs AS child
            ON child.parent_session_id = owner.child_session_id
         WHERE owner.id = %s
           AND owner.task_id = %s
           AND owner.status = 'running'
           AND owner.agent_name = 'merge-orchestrator'
           AND child.child_session_id = %s
           AND child.status = 'running'
           AND child.agent_name = 'merge-worker'
        """,
        (run_id, task_id, session_id),
    )

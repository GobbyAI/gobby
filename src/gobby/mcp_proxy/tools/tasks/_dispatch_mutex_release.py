"""Best-effort dispatch mutex release helpers for task MCP tools."""

from __future__ import annotations

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager


def _release_current_agent_dispatch_mutex(
    ctx: RegistryContext,
    *,
    task_id: str,
    session_id: str,
) -> bool:
    """Release the spawn lease only for the running agent that owns this session."""
    db = ctx.task_manager.db
    mutexes = TaskDispatchMutexManager(db)
    try:
        mutex = mutexes.get_mutex(task_id)
    except Exception:
        return False
    if mutex is None or not isinstance(mutex.run_id, str) or not mutex.run_id:
        return False

    try:
        row = db.fetchone(
            """
            SELECT id
              FROM agent_runs
             WHERE id = ?
               AND child_session_id = ?
               AND task_id = ?
               AND status = 'running'
            """,
            (mutex.run_id, session_id, task_id),
        )
    except Exception:
        return False
    if row is None:
        return False

    try:
        if row["id"] != mutex.run_id:
            return False
    except Exception:
        return False

    try:
        return mutexes.clear_by_run_id(mutex.run_id) > 0
    except Exception:
        return False

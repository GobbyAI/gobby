"""Task progress notifications for parent sessions."""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def notify_parent_on_task_state_change(
    db: "HubDatabase",
    task_id: str,
    new_state: str,
    task_ref: str | None = None,
) -> None:
    """Fire-and-forget: broadcast task progress to parent session via WebSocket.

    Looks up active agent_run for the task, finds parent_session_id,
    broadcasts a task_progress event.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_notify(db, task_id, new_state, task_ref))
    except RuntimeError:
        pass


async def _notify(
    db: "HubDatabase",
    task_id: str,
    new_state: str,
    task_ref: str | None,
) -> None:
    try:
        row = db.fetchone(
            "SELECT id, parent_session_id FROM agent_runs "
            "WHERE task_id = ? AND status IN ('pending', 'running') "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )

        if not row or not row["parent_session_id"]:
            return

        from gobby.app_context import get_app_context

        app_ctx = get_app_context()
        if app_ctx and app_ctx.websocket_server:
            await app_ctx.websocket_server.broadcast_task_event(
                event="task_progress",
                task_id=task_id,
                state=new_state,
                ref=task_ref or task_id,
                parent_session_id=row["parent_session_id"],
                run_id=row["id"],
            )
    except Exception:
        logger.debug("Failed to notify parent on task status change", exc_info=True)

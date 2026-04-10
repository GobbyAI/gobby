"""Session routes package for Gobby HTTP server.

Provides session registration, listing, lookup, update, message/transcript,
and analytics/summary endpoints.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from gobby.servers.routes.sessions.analytics import register_analytics_routes
from gobby.servers.routes.sessions.core import (
    _compute_resumability,
    _get_commit_count,
    register_core_routes,
)
from gobby.servers.routes.sessions.lifecycle import (
    _get_session_stats,
    register_lifecycle_routes,
)
from gobby.servers.routes.sessions.messages import register_message_routes

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Re-export helpers for backward compatibility with tests
__all__ = [
    "_compute_resumability",
    "_get_commit_count",
    "_get_session_stats",
    "create_sessions_router",
]


def create_sessions_router(server: "HTTPServer") -> APIRouter:
    """
    Create sessions router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with session endpoints
    """
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])

    def _get_session_manager() -> Any:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        return server.session_manager

    async def _broadcast_session(event: str, session_id: str, **kwargs: Any) -> None:
        """Broadcast a session event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_session_event(event, session_id, **kwargs)
            except Exception as e:
                logger.warning(
                    f"Failed to broadcast session event '{event}' for session {session_id}: {e}"
                )

    register_core_routes(router, server, _get_session_manager, _broadcast_session)
    register_lifecycle_routes(router, server, _get_session_manager, _broadcast_session)
    register_message_routes(router, server, _get_session_manager)
    register_analytics_routes(router, server, _get_session_manager, _broadcast_session)

    return router

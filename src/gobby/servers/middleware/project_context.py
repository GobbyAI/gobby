"""Project context middleware for Gobby HTTP server.

Sets the project_context ContextVar from X-Gobby-Project-Id and
X-Gobby-Session-Id request headers on every request. This ensures all
routes that call get_project_context() — including session variable
endpoints, hooks, and any future routes — have project context available
for #N session reference resolution.

Previously, only the hooks route set this ContextVar via a local helper.
Any other route that needed project context (e.g.,
/api/sessions/{session_id}/variables/set) would silently get None, causing
#N resolution failures.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from gobby.utils.project_context import (
    reset_project_context,
    set_project_context,
)

logger = logging.getLogger(__name__)


async def _run_db(request: Request, func: Callable[..., Any], *args: Any) -> Any:
    """Run a synchronous lookup without blocking the request event loop."""
    server = getattr(request.app.state, "server", None)
    if server is not None:
        return await server.run_db(func, *args)
    return await asyncio.to_thread(func, *args)


class ProjectContextMiddleware(BaseHTTPMiddleware):
    """Set project context ContextVar from request headers.

    Reads X-Gobby-Session-Id and X-Gobby-Project-Id headers injected by
    the CLI hook dispatcher and stdio proxy. Resolves project context and
    sets the ContextVar before the request handler runs, then resets it
    after the response completes.

    Resolution priority:
    1. X-Gobby-Session-Id → look up session → get project from session
    2. X-Gobby-Project-Id → look up project in DB → set full context
    3. X-Gobby-Project-Id → set minimal context (id only) if DB lookup fails
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        token = await self._set_context(request)
        try:
            return await call_next(request)
        finally:
            if token is not None:
                reset_project_context(token)

    async def _set_context(self, request: Request) -> contextvars.Token[Any] | None:
        """Set project ContextVar from request headers.

        Returns a ContextVar token for reset, or None if no headers present.
        """
        # Priority 1: resolve project from session
        session_id = request.headers.get("x-gobby-session-id")
        if session_id:
            try:
                session_manager = getattr(request.app.state, "session_manager", None)
                if session_manager:
                    session = await _run_db(request, session_manager.get, session_id)
                    if session and session.project_id:
                        try:
                            from gobby.storage.projects import LocalProjectManager

                            pm = LocalProjectManager(session_manager.db)
                            project = await _run_db(request, pm.get, session.project_id)
                        except (ImportError, OSError) as e:
                            logger.debug(
                                "Failed to enrich project context for session %s: %s",
                                session_id,
                                e,
                            )
                            project = None
                        if project:
                            return set_project_context(
                                {
                                    "id": project.id,
                                    "name": project.name,
                                    "project_path": project.repo_path,
                                }
                            )
                        return set_project_context({"id": session.project_id})
            except Exception as e:
                logger.debug("Failed to set project context from session %s: %s", session_id, e)

        # Priority 2: resolve project from project_id header
        project_id = request.headers.get("x-gobby-project-id")
        if project_id:
            try:
                from gobby.storage.projects import LocalProjectManager

                session_manager = getattr(request.app.state, "session_manager", None)
                if session_manager:
                    pm = LocalProjectManager(session_manager.db)
                    project = await _run_db(request, pm.get, project_id)
                    if project:
                        return set_project_context(
                            {
                                "id": project.id,
                                "name": project.name,
                                "project_path": project.repo_path,
                            }
                        )
            except Exception as e:
                logger.debug("Failed to resolve project %s: %s", project_id, e)
            # Fallback: set minimal context with just the id
            return set_project_context({"id": project_id})

        return None

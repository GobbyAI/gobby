"""Authentication middleware for Gobby web UI.

When auth is configured (username + password set), this middleware
protects UI routes and their backing API endpoints. It does NOT protect:
- /api/auth/* (login, logout, status)
- /api/health* (health checks)
- /api/hooks/* (CLI agent hooks)
- /api/sessions/* (CLI agent session endpoints)
- /api/mcp/* (MCP protocol endpoints)
- /api/admin/health, /api/admin/status, /api/admin/metrics, /api/admin/config (read-only admin)
- /assets/* (static assets)
- WebSocket connections (agents need open access)

When auth is NOT configured, all requests pass through unchanged.
"""

import logging
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Paths that never require authentication
_PUBLIC_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/api/hooks/",
    "/api/sessions/",
    "/api/mcp/",
    "/api/mcp",
    "/api/admin/health",
    "/api/admin/status",
    "/api/admin/metrics",
    "/api/admin/config",
    "/assets/",
    "/ws/",
    "/ws",
    "/favicon.ico",
    "/logo.png",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces auth for UI routes.

    Auth is optional — when no credentials are configured, all
    requests pass through. This preserves the local-first default.
    """

    def __init__(self, app: Any, server: "HTTPServer") -> None:
        super().__init__(app)
        self.server = server

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Always allow public paths
        if path == "/" or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
            return await call_next(request)

        # Check if auth is enabled
        from gobby.servers.routes.auth import is_auth_enabled, validate_session_cookie

        if not is_auth_enabled(self.server):
            return await call_next(request)

        # Auth is enabled — validate session cookie
        if validate_session_cookie(request, self.server):
            return await call_next(request)

        # Not authenticated
        if path.startswith("/api/"):
            # API request — return 401 JSON
            return JSONResponse(
                status_code=401,
                content={"error": "Authentication required"},
            )

        # SPA route — let the frontend handle it (it will show login page)
        # We still serve index.html so the React app can render the login form
        return await call_next(request)

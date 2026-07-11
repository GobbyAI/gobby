"""Authentication middleware for Gobby web UI.

Required-mode authentication protects daemon API, MCP, and memory routes.
Only login, health, startup readiness, signature-verified webhooks, and static
browser assets are public. Other browser routes reach the SPA login shell.
"""

import logging
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from gobby.servers.responses import JSONResponse

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Prefixes that never require daemon authentication. Webhook handlers apply
# their channel/HMAC signature checks after this middleware.
_PUBLIC_PREFIXES = (
    "/api/auth/",
    "/api/comms/webhooks/",
    "/api/github/webhooks/",
    "/assets/",
)

_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/api/health",
        "/api/admin/health",
        "/api/admin/startup-progress",
        "/favicon.ico",
        "/logo.png",
    }
)

_PROTECTED_PREFIXES = (
    "/api/",
    "/mcp",
    "/memory",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce the server's configured auth mode at HTTP route boundaries."""

    def __init__(self, app: Any, server: "HTTPServer") -> None:
        super().__init__(app)
        self.server = server

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        if path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
            return await call_next(request)

        auth_service = self.server.auth_service
        if not auth_service.enabled:
            return await call_next(request)

        if auth_service.is_request_authenticated(request):
            return await call_next(request)

        if path.startswith(_PROTECTED_PREFIXES):
            return JSONResponse(
                status_code=401,
                content={
                    "error": (
                        "Authentication required. CLI clients need ~/.gobby/local_cli_token "
                        "(run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
                    )
                },
            )

        # Browser route: serve the SPA shell so React can render login.
        return await call_next(request)

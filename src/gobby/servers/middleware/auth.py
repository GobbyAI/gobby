"""Authentication middleware for Gobby web UI.

Authentication protects daemon API, MCP, and memory routes.
Only login, health, startup readiness, signature-verified webhooks, and static
browser assets are public. Other browser routes reach the SPA login shell.
"""

import logging
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from gobby.config.ui import is_loopback_bind_host
from gobby.servers.grant_auth import admission_required
from gobby.servers.lease_fence import LeaseNotHeld
from gobby.servers.responses import JSONResponse

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Prefixes that never require daemon authentication. Webhook handlers apply
# their channel/HMAC signature checks after this middleware.
_PUBLIC_PREFIXES = (
    "/api/auth",
    "/api/comms/webhooks",
    "/api/github/webhooks",
    "/assets",
)

_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/api/health",
        "/api/admin/health",
        "/api/admin/startup-progress",
        "/api/runtime/handshake/challenge",
        "/favicon.ico",
        "/logo.png",
    }
)

_PROTECTED_PREFIXES = (
    "/api/",
    "/mcp",
    "/memory",
)

_LOGIN_GUIDANCE = (
    "Authentication required. CLI clients need ~/.gobby/local_cli_token "
    "(run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
)
_MISSING_AUTH_CODES = frozenset({None, "missing_auth"})
_GRANT_REJECTION_MESSAGE = "Request rejected"


def _remote_hook_requires_auth(server: "HTTPServer", path: str) -> bool:
    if path != "/api/hooks" and not path.startswith("/api/hooks/"):
        return False

    services = getattr(server, "services", None)
    config = getattr(services, "config", None)
    bind_host = getattr(config, "bind_host", None)
    return isinstance(bind_host, str) and not is_loopback_bind_host(bind_host)


def _matches_path_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication at HTTP route boundaries."""

    def __init__(self, app: Any, server: "HTTPServer") -> None:
        super().__init__(app)
        self.server = server

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        remote_hook_requires_auth = _remote_hook_requires_auth(self.server, path)

        if not remote_hook_requires_auth and (
            path in _PUBLIC_PATHS
            or any(_matches_path_prefix(path, prefix) for prefix in _PUBLIC_PREFIXES)
        ):
            return await call_next(request)

        auth_service = self.server.auth_service
        decision = await self.server.run_db(auth_service.authenticate, request)
        if decision.allowed:
            return await self._call_next_admitted(request, call_next)

        if path.startswith(_PROTECTED_PREFIXES):
            code = decision.code
            status = decision.status_code or 401
            message = decision.message
            if not message:
                message = (
                    _LOGIN_GUIDANCE if code in _MISSING_AUTH_CODES else _GRANT_REJECTION_MESSAGE
                )
            content: dict[str, object] = {"error": message}
            if code:
                content["code"] = code
            return JSONResponse(status_code=status, content=content)

        # Browser route: serve the SPA shell so React can render login.
        return await call_next(request)

    async def _call_next_admitted(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not admission_required(request.method, request.url.path):
            return await call_next(request)
        fence = getattr(self.server.auth_service, "effect_fence", None)
        if fence is None:
            return await call_next(request)
        try:
            with fence.admit():
                hook = getattr(fence, "admit_hook", None)
                if hook is not None:
                    await hook()
                return await call_next(request)
        except LeaseNotHeld as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": exc.message,
                    "code": exc.code,
                },
            )

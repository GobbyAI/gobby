"""Authentication middleware for Gobby web UI.

Authentication protects daemon API, MCP, and memory routes.
Only login, health, startup readiness, signature-verified webhooks, and static
browser assets are public. Other browser routes reach the SPA login shell.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from gobby.hub.caller_scope import bind_decision, reset_caller
from gobby.servers.auth_service import request_path
from gobby.servers.browser_sessions import PREFIX as BROWSER_COOKIE_PREFIX
from gobby.servers.responses import JSONResponse
from gobby.servers.routes._database import require_hub_database
from gobby.utils.project_context import reset_project_context, set_project_context

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Prefixes that never require daemon authentication. Webhook handlers apply
# their channel/HMAC signature checks after this middleware.
_PUBLIC_PREFIXES = (
    "/api/auth",
    "/api/comms/webhooks",
    "/assets",
)

_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/api/health",
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
    "Authentication required. CLI clients need an API key in bootstrap.yaml "
    "and gdaemon (run 'gobby auth login'). Browsers: log in."
)
# Absent or unrecognised operator/browser credentials get login guidance; typed
# grant and capability rejections carry their own message.
_LOGIN_GUIDANCE_CODES = frozenset({None, "missing_auth", "invalid_token", "session_invalid"})
_GRANT_REJECTION_MESSAGE = "Request rejected"


def _matches_path_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication at HTTP route boundaries."""

    def __init__(self, app: Any, server: "HTTPServer") -> None:
        super().__init__(app)
        self.server = server

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request_path(request)

        if path == "/api/auth/sign-in" or path == "/api/auth/logout":
            # These routes consume an independently authorized link or discard authority.
            return await call_next(request)
        browser_cookie = request.cookies.get("gobby_session", "").startswith(BROWSER_COOKIE_PREFIX)
        if not browser_cookie and (
            path in _PUBLIC_PATHS
            or any(_matches_path_prefix(path, prefix) for prefix in _PUBLIC_PREFIXES)
        ):
            return await call_next(request)

        services = getattr(self.server, "services", None)
        if path.startswith(_PROTECTED_PREFIXES) and bool(
            getattr(services, "http_admission_closed", False)
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Daemon is shutting down",
                    "code": "shutdown_in_progress",
                },
            )

        auth_service = self.server.auth_service
        decision = await self.server.run_db(auth_service.authenticate, request)
        project_token = None
        if decision.allowed and decision.browser_session is not None:
            claims = decision.browser_session.claims
            project_id = claims.project_id
            selectors = [
                value
                for key, value in request.query_params.multi_items()
                if key in {"project_id", "projectId"}
            ]
            selectors.extend(request.headers.getlist("x-gobby-project-id"))
            selectors.extend(request.headers.getlist("x-gobby-caller-project-id"))
            project_path = path.removeprefix("/api/projects/").split("/", 1)[0]
            if path.startswith("/api/projects/") and project_path not in {"init", "register"}:
                selectors.append(project_path)
            if any(
                value != claims.session_id
                for value in request.headers.getlist("x-gobby-session-id")
            ):
                return JSONResponse(status_code=403, content={"error": "Browser session mismatch"})
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = await request.json()
                except (ValueError, UnicodeError):
                    body = None
                if isinstance(body, dict):
                    selectors.extend(
                        body[key] for key in ("project_id", "projectId") if key in body
                    )
            if any(value != project_id for value in selectors):
                return JSONResponse(status_code=403, content={"error": "Browser project mismatch"})
        if decision.allowed:
            try:
                database = require_hub_database(getattr(services, "database", None))
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"error": exc.detail},
                )
            token = bind_decision(decision, database)
            if decision.browser_session is not None:
                project_token = set_project_context(
                    {"id": decision.browser_session.claims.project_id}
                )
            try:
                return await call_next(request)
            finally:
                reset_caller(token)
                if project_token is not None:
                    reset_project_context(project_token)

        if browser_cookie and path == "/api/auth/status":
            return JSONResponse(content={"authenticated": False})

        if path.startswith(_PROTECTED_PREFIXES):
            code = decision.code
            status = decision.status_code or 401
            message = decision.message
            if not message:
                message = (
                    _LOGIN_GUIDANCE if code in _LOGIN_GUIDANCE_CODES else _GRANT_REJECTION_MESSAGE
                )
            content: dict[str, object] = {"error": message}
            if code:
                content["code"] = code
            return JSONResponse(status_code=status, content=content)

        # Browser route: serve the SPA shell so React can render login.
        return await call_next(request)

"""
Authentication routes for Gobby web UI.

Provides login/logout/status endpoints with cookie-based sessions.
Auth is optional — disabled when no username/password is configured.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from gobby.servers.responses import JSONResponse
from gobby.servers.routes._database import require_hub_database
from gobby.storage.auth import AuthStore

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

COOKIE_NAME = "gobby_session"


class LoginRequest(BaseModel):
    """Request body for POST /api/auth/login."""

    username: str
    password: str
    remember_me: bool = False


def _get_auth_store(server: "HTTPServer") -> AuthStore:
    """Get or create AuthStore instance."""
    return AuthStore(require_hub_database(server.services.database))


def create_auth_router(server: "HTTPServer") -> APIRouter:
    """Create the authentication API router."""
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login")
    async def login(req: LoginRequest) -> JSONResponse:
        """Authenticate with username/password, set session cookie."""
        if not server.auth_service.credentials_configured:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Authentication not configured"},
            )

        if not server.auth_service.verify_password(req.username, req.password):
            logger.warning(f"Failed login attempt for user: {req.username}")
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Invalid username or password"},
            )

        # Create session
        auth_store = _get_auth_store(server)
        token, expires_at = auth_store.create_session(remember_me=req.remember_me)

        response = JSONResponse(content={"ok": True})
        cookie_kwargs: dict[str, Any] = {
            "key": COOKIE_NAME,
            "value": token,
            "httponly": True,
            "samesite": "lax",
            "path": "/",
        }

        if req.remember_me:
            cookie_kwargs["max_age"] = 30 * 24 * 60 * 60  # 30 days in seconds

        response.set_cookie(**cookie_kwargs)
        logger.info(f"User '{req.username}' logged in (remember_me={req.remember_me})")
        return response

    @router.post("/logout")
    async def logout(request: Request) -> JSONResponse:
        """Clear session cookie and delete session."""
        token = request.cookies.get(COOKIE_NAME)
        if token:
            auth_store = _get_auth_store(server)
            auth_store.delete_session(token)

        response = JSONResponse(content={"ok": True})
        response.delete_cookie(key=COOKIE_NAME, path="/")
        return response

    @router.get("/status")
    async def auth_status(request: Request) -> JSONResponse:
        """Check current auth state.

        Returns whether auth is required and if the current session is valid.
        """
        auth_required = server.auth_service.enabled
        authenticated = (
            server.auth_service.is_request_authenticated(request) if auth_required else True
        )

        return JSONResponse(
            content={
                "auth_required": auth_required,
                "authenticated": authenticated,
                "credentials_configured": server.auth_service.credentials_configured,
            }
        )

    return router

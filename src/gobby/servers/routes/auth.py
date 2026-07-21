"""
Authentication routes for Gobby web UI.

Provides login/logout/status endpoints with cookie-based sessions.
Auth is optional — disabled when no username/password is configured.
"""

import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
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
_MAX_FAILED_LOGINS = 5
_FAILED_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_LOCKOUT_SECONDS = 60
_MAX_TRACKED_CLIENTS = 1024


@dataclass(frozen=True, slots=True)
class _FailedLoginState:
    attempts: int
    expires_at: float
    locked_until: float


class _LoginRateLimiter:
    """Bound failed-login tracking by client address."""

    def __init__(self) -> None:
        self._clients: OrderedDict[str, _FailedLoginState] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, client_id: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            state = self._clients.get(client_id)
            if state is None:
                return None
            if state.locked_until > now:
                self._clients.move_to_end(client_id)
                return math.ceil(state.locked_until - now)
            if state.locked_until or state.expires_at <= now:
                del self._clients[client_id]
            return None

    def record_failure(self, client_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            state = self._clients.get(client_id)
            attempts = state.attempts + 1 if state and state.expires_at > now else 1
            locked_until = now + _LOGIN_LOCKOUT_SECONDS if attempts >= _MAX_FAILED_LOGINS else 0
            self._clients[client_id] = _FailedLoginState(
                attempts=attempts,
                expires_at=now + _FAILED_LOGIN_WINDOW_SECONDS,
                locked_until=locked_until,
            )
            self._clients.move_to_end(client_id)
            while len(self._clients) > _MAX_TRACKED_CLIENTS:
                self._clients.popitem(last=False)

    def reset(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)


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
    login_rate_limiter = _LoginRateLimiter()

    @router.post("/login")
    async def login(req: LoginRequest, request: Request) -> JSONResponse:
        """Authenticate with username/password, set session cookie."""
        if not server.auth_service.credentials_configured:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Authentication not configured"},
            )

        client_id = request.client.host if request.client else "unknown"
        retry_after = login_rate_limiter.retry_after(client_id)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "Too many failed login attempts"},
                headers={"Retry-After": str(retry_after)},
            )

        if not await server.run_db(server.auth_service.verify_password, req.username, req.password):
            login_rate_limiter.record_failure(client_id)
            logger.warning("Failed login attempt", extra={"client_id": client_id})
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Invalid username or password"},
            )

        login_rate_limiter.reset(client_id)

        # Create session
        auth_store = _get_auth_store(server)
        token, expires_at = await server.run_db(
            auth_store.create_session, remember_me=req.remember_me
        )

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
        logger.info(
            "Login succeeded",
            extra={"client_id": client_id, "remember_me": req.remember_me},
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> JSONResponse:
        """Clear session cookie and delete session."""
        token = request.cookies.get(COOKIE_NAME)
        if token:
            auth_store = _get_auth_store(server)
            await server.run_db(auth_store.delete_session, token)

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
            await server.run_db(server.auth_service.is_request_authenticated, request)
            if auth_required
            else True
        )

        return JSONResponse(
            content={
                "auth_required": auth_required,
                "authenticated": authenticated,
                "credentials_configured": server.auth_service.credentials_configured,
            }
        )

    return router

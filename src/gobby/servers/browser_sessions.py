"""Process-bound, revocable browser-testing sessions; never operator credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass

from gobby.utils.local_token import AgentApiTokenClaims

PREFIX = "browser-testing."
SESSION_SECONDS = 3600


@dataclass(frozen=True)
class BrowserSession:
    user_id: str
    claims: AgentApiTokenClaims
    expires_at: float


class BrowserSessions:
    """Own signing material and bounded active sessions for this daemon process."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._active: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def create(self, user_id: str, claims: AgentApiTokenClaims) -> str:
        expires_at = time.time() + SESSION_SECONDS
        payload = {
            "kind": "browser-testing",
            "id": secrets.token_urlsafe(24),
            "user_id": user_id,
            "project_id": claims.project_id,
            "session_id": claims.session_id,
            "run_id": claims.agent_run_id or claims.managed_execution_id,
            "exp": expires_at,
        }
        body = (
            base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        with self._lock:
            self._active = {
                key: value for key, value in self._active.items() if value.expires_at > time.time()
            }
            if len(self._active) >= 1024:
                del self._active[next(iter(self._active))]
            self._active[body] = BrowserSession(user_id, claims, expires_at)
        return f"{PREFIX}{body}.{signature}"

    def get(self, token: str) -> BrowserSession | None:
        if not token.startswith(PREFIX) or len(token) > 8192:
            return None
        body, separator, signature = token[len(PREFIX) :].partition(".")
        expected = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        if not separator or not hmac.compare_digest(signature.encode(), expected.encode()):
            return None
        with self._lock:
            session = self._active.get(body)
            if session is not None and session.expires_at > time.time():
                return session
            self._active.pop(body, None)
        return None

    def revoke(self, token: str) -> None:
        body = token.removeprefix(PREFIX).partition(".")[0]
        with self._lock:
            self._active.pop(body, None)


def browser_route_allowed(method: str, path: str) -> bool:
    """Browser-testing sessions can observe the UI and revoke their own cookie."""
    if path.startswith("/api/auth/"):
        return (method, path) in {("GET", "/api/auth/status"), ("POST", "/api/auth/logout")}
    if path == "/api/users" or path.startswith("/api/users/"):
        return False
    return method in {"GET", "HEAD", "OPTIONS"}


def browser_selectors_match(body: object, project_id: str, session_id: str | None = None) -> bool:
    """Check selectors at every nesting depth before dispatching untrusted input."""
    pending = [body]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"project_id", "projectId"} and item != project_id:
                    return False
                if (
                    session_id is not None
                    and key in {"session_id", "sessionId", "conversation_id"}
                    and item != session_id
                ):
                    return False
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    return True


BROWSER_WS_MESSAGES = frozenset(
    {
        "ping",
        "heartbeat",
        "subscribe",
        "unsubscribe",
        "terminal_list",
        "terminal_attach",
        "terminal_detach",
        "terminal_focus",
        "terminal_set_viewport",
        "terminal_set_scroll_offset",
        "workspace_attach",
        "workspace_snapshot",
    }
)

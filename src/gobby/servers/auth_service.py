"""Shared authentication service for daemon HTTP and WebSocket entry points."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from starlette.requests import HTTPConnection

from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    PASSWORD_HASH_KEY,
    USERNAME_KEY,
    AuthStore,
    hash_token,
    verify_password_hash,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    local_token_path,
    verify_agent_api_token,
)

logger = logging.getLogger(__name__)

AuthMode = Literal["required", "disabled"]

_SESSION_COOKIE = "gobby_session"
_LOCAL_TOKEN_HEADER = "X-Gobby-Local-Token"
_NEVER_REFRESHED = float("-inf")


def _agent_capability_allows(request: HTTPConnection) -> bool:
    method = str(request.scope.get("method", "GET")).upper()
    path = request.url.path
    if method == "POST" and path in {
        "/api/mcp/tools/schema",
        "/api/mcp/tools/call",
        "/api/hooks/execute",
        "/api/code-index/codewiki/refresh",
    }:
        return True
    if method == "GET" and path in {
        "/api/mcp/servers",
        "/api/mcp/tools",
        "/api/mcp/status",
    }:
        return True
    parts = path.strip("/").split("/")
    if method == "GET":
        return len(parts) == 4 and parts[:2] == ["api", "mcp"] and parts[3] == "tools"
    return (
        method == "POST" and len(parts) == 5 and parts[:2] == ["api", "mcp"] and parts[3] == "tools"
    )


def _agent_identity_matches(
    request: HTTPConnection,
    claims: AgentApiTokenClaims,
) -> bool:
    headers = request.headers
    if headers.get("X-Gobby-Session-Id") != claims.session_id:
        return False
    if headers.get("X-Gobby-Project-Id") != claims.project_id:
        return False
    if request.url.path == "/api/hooks/execute":
        return True
    return headers.get("X-Gobby-Agent-Run-Id") == claims.agent_run_id


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_token_file(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Unable to read local API token file %s: %s", path, exc)
        return None
    return token or None


class AuthService:
    """Cache and verify all daemon authentication credentials."""

    MIN_REFRESH_INTERVAL = 5.0

    def __init__(
        self,
        database_getter: Callable[[], HubDatabase],
        mode: AuthMode,
        token_file: Path | None = None,
    ) -> None:
        if mode not in ("required", "disabled"):
            raise ValueError(f"Unsupported authentication mode: {mode}")

        self._database_getter = database_getter
        self._mode = mode
        self._token_file = token_file or local_token_path()
        self._lock = threading.Lock()
        self._last_refresh = _NEVER_REFRESHED
        self._token_hash: str | None = None
        self._web_username: str | None = None
        self._web_password_hash: str | None = None
        self._local_token_plaintext: str | None = None

    @property
    def enabled(self) -> bool:
        return self._mode == "required"

    def verify_bearer(self, token: str) -> bool:
        candidate_hash = hash_token(token)
        self.refresh()

        if secrets.compare_digest(candidate_hash, self._token_hash_snapshot()):
            return True

        self.refresh()
        return secrets.compare_digest(candidate_hash, self._token_hash_snapshot())

    async def verify_ws_token(self, token: str) -> str | None:
        return "local-cli" if self.verify_bearer(token) else None

    def is_request_authenticated(self, request: HTTPConnection) -> bool:
        authorization = request.headers.get("Authorization")
        if authorization is not None:
            parts = authorization.split(maxsplit=1)
            if parts and parts[0].casefold() == "bearer":
                if len(parts) != 2:
                    return False
                return self.verify_bearer(parts[1]) or self._verify_agent_request(request, parts[1])

        local_token = request.headers.get(_LOCAL_TOKEN_HEADER)
        if local_token is not None:
            return self.verify_bearer(local_token)

        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None:
            return self.validate_session(session_token)

        return False

    def _verify_agent_request(self, request: HTTPConnection, token: str) -> bool:
        operator_token = self.local_token()
        if operator_token is None:
            return False
        claims = verify_agent_api_token(token, operator_token)
        return bool(
            claims
            and _agent_capability_allows(request)
            and _agent_identity_matches(request, claims)
        )

    def validate_session(self, token: str) -> bool:
        if not token:
            return False
        return AuthStore(self._database_getter()).validate_session(token)

    def verify_password(self, username: str, password: str) -> bool:
        self.refresh()
        with self._lock:
            expected_username = self._web_username or ""
            stored_hash = self._web_password_hash

        username_matches = secrets.compare_digest(
            username.encode("utf-8"),
            expected_username.encode("utf-8"),
        )
        password_matches = verify_password_hash(password, stored_hash)
        return username_matches and password_matches

    @property
    def credentials_configured(self) -> bool:
        self.refresh()
        with self._lock:
            return bool(self._web_username and self._web_password_hash)

    def local_token(self) -> str | None:
        self.refresh()
        with self._lock:
            return self._local_token_plaintext

    def refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_refresh < self.MIN_REFRESH_INTERVAL:
                return

            config_store = ConfigStore(self._database_getter())
            token_hash = _optional_string(config_store.get(LOCAL_API_TOKEN_HASH_KEY))
            web_username = _optional_string(config_store.get(USERNAME_KEY))
            web_password_hash = _optional_string(config_store.get(PASSWORD_HASH_KEY))
            local_token_plaintext = _read_token_file(self._token_file)

            self._token_hash = token_hash
            self._web_username = web_username
            self._web_password_hash = web_password_hash
            self._local_token_plaintext = local_token_plaintext
            self._last_refresh = now

    def _token_hash_snapshot(self) -> str:
        with self._lock:
            return self._token_hash or ""

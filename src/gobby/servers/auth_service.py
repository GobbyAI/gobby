"""Shared authentication service for daemon HTTP and WebSocket entry points."""

from __future__ import annotations

import hmac
import logging
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
from gobby.utils.local_token import local_token_path

logger = logging.getLogger(__name__)

AuthMode = Literal["required", "disabled"]

_SESSION_COOKIE = "gobby_session"
_LOCAL_TOKEN_HEADER = "X-Gobby-Local-Token"
_NEVER_REFRESHED = float("-inf")


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

        if hmac.compare_digest(candidate_hash, self._token_hash_snapshot()):
            return True

        self.refresh()
        return hmac.compare_digest(candidate_hash, self._token_hash_snapshot())

    async def verify_ws_token(self, token: str) -> str | None:
        return "local-cli" if self.verify_bearer(token) else None

    def is_request_authenticated(self, request: HTTPConnection) -> bool:
        authorization = request.headers.get("Authorization")
        if authorization is not None:
            parts = authorization.split(maxsplit=1)
            if parts and parts[0].casefold() == "bearer":
                return len(parts) == 2 and self.verify_bearer(parts[1])

        local_token = request.headers.get(_LOCAL_TOKEN_HEADER)
        if local_token is not None:
            return self.verify_bearer(local_token)

        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None:
            return self.validate_session(session_token)

        return False

    def validate_session(self, token: str) -> bool:
        if not token:
            return False
        return AuthStore(self._database_getter()).validate_session(token)

    def verify_password(self, username: str, password: str) -> bool:
        self.refresh()
        with self._lock:
            expected_username = self._web_username or ""
            stored_hash = self._web_password_hash

        username_matches = hmac.compare_digest(
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

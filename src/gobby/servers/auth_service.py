"""Shared authentication service for daemon HTTP and WebSocket entry points."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from starlette.requests import Request

from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, AuthStore, hash_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import local_token_path

logger = logging.getLogger(__name__)

AuthMode = Literal["required", "disabled"]

_WEB_USERNAME_KEY = "auth.username"
_WEB_PASSWORD_HASH_KEY = "auth.password_hash"
_SESSION_COOKIE = "gobby_session"
_LOCAL_TOKEN_HEADER = "X-Gobby-Local-Token"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_NEVER_REFRESHED = float("-inf")
_INVALID_PASSWORD_DIGEST = bytes([0xFF]) * _SCRYPT_DKLEN
_EMPTY_PASSWORD_DIGEST = bytes(_SCRYPT_DKLEN)


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


def _verify_scrypt(password: str, stored_hash: str | None) -> bool:
    expected = _INVALID_PASSWORD_DIGEST
    derived = _EMPTY_PASSWORD_DIGEST
    valid_format = False

    if stored_hash is not None:
        parts = stored_hash.split("$")
        if len(parts) == 6 and parts[:4] == ["scrypt", str(_SCRYPT_N), "8", "1"]:
            try:
                salt = base64.b64decode(parts[4], validate=True)
                candidate = base64.b64decode(parts[5], validate=True)
            except (ValueError, binascii.Error):
                pass
            else:
                if salt and len(candidate) == _SCRYPT_DKLEN:
                    expected = candidate
                    derived = hashlib.scrypt(
                        password.encode("utf-8"),
                        salt=salt,
                        n=_SCRYPT_N,
                        r=_SCRYPT_R,
                        p=_SCRYPT_P,
                        dklen=_SCRYPT_DKLEN,
                    )
                    valid_format = True

    digest_matches = hmac.compare_digest(derived, expected)
    return valid_format and digest_matches


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

    def is_request_authenticated(self, request: Request) -> bool:
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
        password_matches = _verify_scrypt(password, stored_hash)
        return username_matches and password_matches

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
            web_username = _optional_string(config_store.get(_WEB_USERNAME_KEY))
            web_password_hash = _optional_string(config_store.get(_WEB_PASSWORD_HASH_KEY))
            local_token_plaintext = _read_token_file(self._token_file)

            self._token_hash = token_hash
            self._web_username = web_username
            self._web_password_hash = web_password_hash
            self._local_token_plaintext = local_token_plaintext
            self._last_refresh = now

    def _token_hash_snapshot(self) -> str:
        with self._lock:
            return self._token_hash or ""

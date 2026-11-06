"""Signed requests for a spawned seat's restart and protected-run preflight.

The seat never sends its managed bearer on these routes. It asks the daemon for a single-use
nonce, derives a key from its own capability signature, and MACs the operation, the request, the
nonce and the payload. The daemon recomputes the signature from the payload with its managed key,
so the same key is derivable on both sides and on neither side by a relay.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final

SIGNED_REQUEST_LABEL: Final = "gobby-managed-signed-request-v1"
SIGNED_PAYLOAD_HEADER: Final = "X-Gobby-Signed-Payload"
SIGNED_NONCE_HEADER: Final = "X-Gobby-Signed-Nonce"
SIGNED_MAC_HEADER: Final = "X-Gobby-Signed-Mac"
NONCE_PATH: Final = "/api/runtime/signed-request/nonce"
NONCE_TTL_SECONDS: Final = 30.0
NONCE_CAPACITY: Final = 256

RESTART_PATH: Final = "/api/admin/restart"
PROTECTED_RUNS_PATH: Final = "/api/admin/cron/protected-runs"
RESTART_OPERATION: Final = "restart"
PROTECTED_RUNS_OPERATION: Final = "protected-runs"
_SIGNED_ROUTES: Final[dict[tuple[str, str], str]] = {
    ("POST", RESTART_PATH): RESTART_OPERATION,
    ("GET", PROTECTED_RUNS_PATH): PROTECTED_RUNS_OPERATION,
}


def signed_route_operation(method: str, path: str) -> str | None:
    """Return the operation a daemon route accepts as a signed request, or None."""
    return _SIGNED_ROUTES.get((method.upper(), path))


def has_signed_headers(headers: Mapping[str, str]) -> bool:
    """Whether a request presents any part of a signed request."""
    return any(
        name in headers for name in (SIGNED_PAYLOAD_HEADER, SIGNED_NONCE_HEADER, SIGNED_MAC_HEADER)
    )


def derive_signed_request_key(signature: bytes) -> bytes:
    """Derive the signed-request key from a capability signature.

    This is a digest, not an HMAC of the signature. The public challenge answers
    HMAC(signature, nonce) for any nonce a caller picks, so an HMAC keyed by the signature
    would be handed to whoever asks.
    """
    return hashlib.sha256(SIGNED_REQUEST_LABEL.encode() + signature).digest()


def signed_request_mac(
    key: bytes,
    *,
    operation: str,
    method: str,
    path: str,
    query: Sequence[tuple[str, str]],
    body: bytes,
    nonce: str,
    encoded_payload: str,
) -> str:
    """MAC every field a relay could change: the route, the query, the body, the nonce and the payload."""
    message = json.dumps(
        [
            SIGNED_REQUEST_LABEL,
            operation,
            method.upper(),
            path,
            sorted(query),
            hashlib.sha256(body).hexdigest(),
            nonce,
            encoded_payload,
        ],
        separators=(",", ":"),
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


class NonceStore:
    """Single-use nonces, each bound to the payload it was issued for and valid for 30 seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, str]] = {}

    def issue(self, payload: str) -> str | None:
        """Issue a fresh nonce for ``payload``, or None when the store is full."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._entries) >= NONCE_CAPACITY:
                return None
            nonce = secrets.token_urlsafe(32)
            self._entries[nonce] = (now + NONCE_TTL_SECONDS, _digest(payload))
            return nonce

    def consume(self, nonce: str, payload: str) -> bool:
        """Spend ``nonce``. A nonce is single-use whether or not it matches ``payload``."""
        with self._lock:
            now = self._clock()
            entry = self._entries.pop(nonce, None)
            if entry is None:
                return False
            expires_at, digest = entry
            return now < expires_at and hmac.compare_digest(digest, _digest(payload))

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def _prune(self, now: float) -> None:
        expired = [nonce for nonce, (expires_at, _) in self._entries.items() if now >= expires_at]
        for nonce in expired:
            del self._entries[nonce]


signed_request_nonces = NonceStore()

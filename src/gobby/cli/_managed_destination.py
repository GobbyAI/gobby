"""Prove the listener at a spawned seat's daemon URL holds the signing key behind its capability.

A spawned seat's bearer must not reach a listener that merely answers at the URL it was spawned
with. Before any authenticated request, the seat asks that listener for a challenge answer. The
request carries the token's payload fields and a fresh nonce, never the bearer. The daemon answers
with an HMAC keyed by the token signature only if it recomputes that signature from its managed
key. This process checks the answer with the signature it already holds. A listener without the
key cannot produce the answer, so the bearer stays in this process.

This is the check the managed grant handshake already performs before it sends a bearer.
"""

from __future__ import annotations

import base64
import hmac
import secrets

import httpx

from gobby.utils.local_token import (
    managed_token_challenge_claims,
    managed_token_proof,
    read_managed_api_token,
)

_CHALLENGE_PATH = "/api/runtime/handshake/challenge"
_CHALLENGE_TIMEOUT_SECONDS = 3.0


def daemon_proves_managed_capability(http_port: int) -> bool:
    """Return whether the daemon on localhost:``http_port`` proves it issued this seat's capability.

    Refuses on any doubt: no readable capability, a listener that cannot answer, a non-200 answer,
    or an answer that does not match the signature this seat holds.
    """
    try:
        token = read_managed_api_token()
    except RuntimeError:
        return False
    if token is None:
        return False
    claims = managed_token_challenge_claims(token)
    if claims is None:
        return False
    nonce = secrets.token_bytes(32)
    try:
        response = httpx.post(
            f"http://localhost:{http_port}{_CHALLENGE_PATH}",
            json={
                "nonce": base64.urlsafe_b64encode(nonce).rstrip(b"=").decode(),
                "kind": "managed",
                "claims": claims,
            },
            timeout=_CHALLENGE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    proof = payload.get("proof") if isinstance(payload, dict) else None
    expected = managed_token_proof(token, nonce)
    return isinstance(proof, str) and expected is not None and hmac.compare_digest(expected, proof)

"""A spawned seat's signed request: a daemon nonce and a MAC under a key only the seat derives.

The managed bearer never leaves the process on these routes. Every failure returns None, and the
caller refuses without sending anything that carries a credential.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping

import httpx

from gobby.runtime_grants.signed_request import (
    NONCE_PATH,
    SIGNED_MAC_HEADER,
    SIGNED_NONCE_HEADER,
    SIGNED_PAYLOAD_HEADER,
    derive_signed_request_key,
    signed_request_mac,
    signed_route_operation,
)
from gobby.utils.local_token import _AGENT_TOKEN_VERSION, read_managed_api_token

_NONCE_TIMEOUT_SECONDS = 3.0


def signed_daemon_headers(
    http_port: int,
    *,
    method: str,
    path: str,
    params: Mapping[str, str] | None = None,
    body: bytes = b"",
) -> dict[str, str] | None:
    """Return the headers that admit one request on the daemon at ``http_port``, or None.

    None means no proof could be made: no readable capability, a malformed token, a listener that
    does not issue a nonce, or a network failure. The token's signature stays in this process.
    """
    operation = signed_route_operation(method, path)
    if operation is None:
        return None
    try:
        token = read_managed_api_token()
    except RuntimeError:
        return None
    if token is None:
        return None
    parts = token.split(".", maxsplit=2)
    if len(parts) != 3 or parts[0] != _AGENT_TOKEN_VERSION:
        return None
    _version, payload, signature_text = parts
    try:
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
    except ValueError:
        return None
    try:
        response = httpx.post(
            f"http://localhost:{http_port}{NONCE_PATH}",
            json={"payload": payload},
            timeout=_NONCE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return None
    nonce = _nonce_from(response)
    if nonce is None:
        return None
    query = tuple(sorted((params or {}).items()))
    mac = signed_request_mac(
        derive_signed_request_key(signature),
        operation=operation,
        method=method,
        path=path,
        query=query,
        body=body,
        nonce=nonce,
        encoded_payload=payload,
    )
    return {
        SIGNED_PAYLOAD_HEADER: payload,
        SIGNED_NONCE_HEADER: nonce,
        SIGNED_MAC_HEADER: mac,
    }


def _nonce_from(response: httpx.Response) -> str | None:
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    nonce = payload.get("nonce") if isinstance(payload, dict) else None
    return nonce if isinstance(nonce, str) and nonce else None

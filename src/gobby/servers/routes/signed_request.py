"""Public nonce route for a spawned seat's signed daemon requests.

The route issues nothing secret. A nonce is worth something only with the key a seat derives from
its own signature, and the daemon checks that key on the request it admits.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gobby.runtime_grants.signed_request import signed_request_nonces

_MAX_PAYLOAD_CHARS = 4096


class NonceRequest(BaseModel):
    payload: str = Field(min_length=1, max_length=_MAX_PAYLOAD_CHARS)


def _is_capability_payload(encoded_payload: str) -> bool:
    try:
        raw: Any = json.loads(
            base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        )
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    return isinstance(raw, dict)


def create_signed_request_router() -> APIRouter:
    router = APIRouter(prefix="/api/runtime/signed-request")

    @router.post("/nonce")
    async def issue_nonce(body: NonceRequest) -> JSONResponse:
        """Issue a single-use nonce bound to the capability payload it was requested for."""
        if not _is_capability_payload(body.payload):
            return JSONResponse(status_code=400, content={"error": "invalid capability payload"})
        nonce = signed_request_nonces.issue(body.payload)
        if nonce is None:
            return JSONResponse(
                status_code=503, content={"error": "signed request nonces are exhausted"}
            )
        return JSONResponse(content={"nonce": nonce})

    return router

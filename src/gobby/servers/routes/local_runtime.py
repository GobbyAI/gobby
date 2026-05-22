"""Local-only runtime routes for daemon-backed CLI helpers."""

from __future__ import annotations

import ipaddress
import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gobby.config.local_cli_token import read_local_cli_token

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

_LOCAL_TOKEN_HEADER = "X-Gobby-Local-Token"


def create_local_runtime_router(server: HTTPServer) -> APIRouter:
    """Create local runtime routes."""
    router = APIRouter(prefix="/api/local/runtime", tags=["local-runtime"])

    @router.post("/database-url")
    async def database_url(request: Request) -> JSONResponse:
        if not _is_loopback_client(request):
            return JSONResponse(status_code=403, content={"error": "loopback client required"})

        expected_token = read_local_cli_token()
        if not expected_token:
            return JSONResponse(status_code=503, content={"error": "local token unavailable"})

        provided_token = request.headers.get(_LOCAL_TOKEN_HEADER, "")
        if not secrets.compare_digest(provided_token, expected_token):
            return JSONResponse(status_code=401, content={"error": "invalid local token"})

        database_url = getattr(server.services.config, "database_url", None)
        if not isinstance(database_url, str) or not database_url.strip():
            return JSONResponse(status_code=503, content={"error": "database_url unavailable"})

        return JSONResponse(
            content={"database_url": database_url},
            headers={"Cache-Control": "no-store"},
        )

    return router


def _is_loopback_client(request: Request) -> bool:
    client = request.client
    if client is None or not client.host:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return client.host == "localhost"

"""Grant-presenting non-capability runtime configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from gobby.runtime_grants.handshake import HandshakeRejection, decode_grant_header
from gobby.runtime_grants.service import GrantRejection
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.configuration_effective import _machine_config_values
from gobby.servers.routes.runtime_handshake import grant_error_response

GRANT_HEADER = "X-Gobby-Runtime-Grant"


def create_runtime_config_router(server: Any) -> APIRouter:
    """Build the grant-presenting runtime config router."""
    router = APIRouter(prefix="/api/runtime", tags=["runtime"])

    @router.get("/config")
    def get_runtime_config(request: Request) -> JSONResponse:
        if not server.auth_service.is_request_authenticated(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        encoded = request.headers.get(GRANT_HEADER)
        if not encoded:
            raise HTTPException(status_code=401, detail="Runtime grant required")
        grants = getattr(server, "grant_service", None)
        if grants is None:
            raise HTTPException(status_code=503, detail="grant service unavailable")
        try:
            grant = decode_grant_header(encoded)
            grants.present(grant)
        except (GrantRejection, HandshakeRejection, ValueError) as error:
            if isinstance(error, GrantRejection | HandshakeRejection):
                return grant_error_response(error)
            raise HTTPException(status_code=400, detail="malformed grant") from error
        runtime = getattr(server.services, "config_runtime", None)
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime configuration unavailable")
        snapshot = runtime.snapshot
        return JSONResponse(
            content={
                "config_revision": snapshot.revision,
                "settings": _machine_config_values(snapshot),
            },
            headers={"Cache-Control": "no-store"},
        )

    return router

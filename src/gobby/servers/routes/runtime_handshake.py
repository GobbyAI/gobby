"""Runtime handshake and bearer-free challenge routes."""

from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from gobby.runtime_grants.handshake import HandshakeRejection, challenge_proof
from gobby.runtime_grants.service import GrantRejection
from gobby.servers.responses import JSONResponse
from gobby.utils.local_token import AgentApiTokenClaims

logger = logging.getLogger(__name__)

# 32-byte challenge nonce, urlsafe base64 with padding.
_CHALLENGE_NONCE_MAX_CHARS = 44


class ChallengeCaller(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "interactive"
    claims: dict[str, object] | None = None


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(max_length=_CHALLENGE_NONCE_MAX_CHARS)
    kind: str = "interactive"
    claims: dict[str, object] | None = None
    caller: ChallengeCaller | None = None


class HandshakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine_id: str
    project_id: str
    session_id: str | None = None


def _authorization_present(request: Request) -> bool:
    authorization = request.headers.get("Authorization")
    if authorization:
        return True
    return bool(request.headers.get("X-Gobby-Local-Token"))


def _challenge_kind(body: ChallengeRequest) -> str:
    if body.caller is not None:
        return body.caller.kind
    return body.kind


def _required_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise HandshakeRejection(f"challenge claims missing {key}", code="claims_mismatch")
    return value


def _required_int(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandshakeRejection(f"challenge claims missing {key}", code="claims_mismatch")
    return value


def _challenge_claims(body: ChallengeRequest) -> AgentApiTokenClaims | None:
    raw = body.claims
    if body.caller is not None and body.caller.claims is not None:
        raw = body.caller.claims
    if raw is None:
        return None
    agent_run_id = raw.get("agent_run_id")
    managed_execution_id = raw.get("managed_execution_id")
    return AgentApiTokenClaims(
        session_id=_required_str(raw, "session_id"),
        project_id=_required_str(raw, "project_id"),
        machine_id=_required_str(raw, "machine_id"),
        iat=_required_int(raw, "iat"),
        exp=_required_int(raw, "exp"),
        agent_run_id=agent_run_id if isinstance(agent_run_id, str) else None,
        managed_execution_id=(
            managed_execution_id if isinstance(managed_execution_id, str) else None
        ),
    )


def grant_error_response(error: GrantRejection | HandshakeRejection) -> JSONResponse:
    status = 403
    if error.code == "stale_epoch":
        status = 409
    elif error.code in {"expired", "credential_before_proof"}:
        status = 401
    return JSONResponse(
        status_code=status,
        content={"code": error.code, "error": error.code, "message": error.message},
    )


def create_runtime_handshake_router(server: Any) -> APIRouter:
    """Build the handshake and challenge router."""
    router = APIRouter(prefix="/api/runtime", tags=["runtime"])

    @router.post("/handshake/challenge")
    def challenge(request: Request, body: ChallengeRequest) -> JSONResponse:
        if _authorization_present(request):
            return grant_error_response(
                HandshakeRejection(
                    "no credential attaches before challenge proof",
                    code="credential_before_proof",
                )
            )
        try:
            nonce = base64.urlsafe_b64decode(body.nonce + "=" * (-len(body.nonce) % 4))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid nonce") from exc
        operator_token = server.auth_service.local_token()
        if operator_token is None:
            raise HTTPException(status_code=503, detail="operator token unavailable")
        try:
            proof = challenge_proof(
                nonce,
                kind=_challenge_kind(body),
                operator_token=operator_token,
                claims=_challenge_claims(body),
            )
        except HandshakeRejection as error:
            logger.warning(
                "handshake challenge rejected",
                extra={"code": error.code},
            )
            return grant_error_response(error)
        return JSONResponse(content={"proof": proof}, headers={"Cache-Control": "no-store"})

    @router.post("/handshake")
    def handshake(request: Request, body: HandshakeRequest) -> JSONResponse:
        if not server.auth_service.is_request_authenticated(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        factory = getattr(server, "handshake_factory", None)
        service = factory() if callable(factory) else getattr(server, "handshake_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="handshake service unavailable")
        try:
            claims = server.auth_service.verified_agent_claims(request)
            if claims is not None:
                grant = service.issue_for_agent(
                    claims,
                    machine_id=body.machine_id,
                    project_id=body.project_id,
                )
            else:
                session_id = body.session_id
                if session_id is None:
                    raise HandshakeRejection("session_id is required", code="claims_mismatch")
                grant = service.issue_for_operator(
                    machine_id=body.machine_id,
                    project_id=body.project_id,
                    session_id=session_id,
                )
        except HandshakeRejection as error:
            logger.warning(
                "handshake rejected",
                extra={
                    "code": error.code,
                    "machine_id": body.machine_id,
                    "project_id": body.project_id,
                },
            )
            return grant_error_response(error)
        return JSONResponse(
            content={
                "grant": grant.model_dump(mode="json"),
                "deployment_token": grant.deployment.token,
                "fencing_epoch": grant.deployment.fencing_epoch,
            },
            headers={"Cache-Control": "no-store"},
        )

    return router

"""API key issuance and management routes.

``/api/auth`` is public to the auth middleware, so the management routes admit
callers through the route-local ``require_key_principal`` dependency. Only the
password-verified bootstrap route stays public.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from gobby.servers.responses import JSONResponse
from gobby.servers.routes._database import require_hub_database
from gobby.servers.routes.auth import (
    COOKIE_NAME,
    LoginRateLimiter,
    _login_client_id,
    login_lockout_response,
)
from gobby.storage.api_keys import ApiKey, ApiKeyManager
from gobby.storage.auth import AuthStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager, MachineOwnershipConflictError
from gobby.storage.users import LocalUserManager, UserIdentityStateError
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

_LOCAL_TOKEN_HEADER = "X-Gobby-Local-Token"
_AUTH_REQUIRED = "Authentication required. Use the operator token or log in."
_NOT_FOUND = {"ok": False, "error": "API key not found"}


@dataclass(frozen=True, slots=True)
class KeyPrincipal:
    user_id: str
    machine_id: str


class KeyPrincipalRejected(Exception):
    """A management request whose credential resolves to no usable principal."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class BootstrapKeyRequest(BaseModel):
    email: str
    password: str
    machine_id: uuid.UUID
    hostname: str | None = None
    os: str | None = None
    label: str | None = None


class MintKeyRequest(BaseModel):
    label: str | None = None


def _resolve_principal(server: HTTPServer, db: HubDatabase, request: Request) -> KeyPrincipal:
    operator_token: str | None = None
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        parts = authorization.split(maxsplit=1)
        if len(parts) != 2 or parts[0].casefold() != "bearer":
            raise KeyPrincipalRejected(401, "missing_auth", _AUTH_REQUIRED)
        operator_token = parts[1]
    else:
        operator_token = request.headers.get(_LOCAL_TOKEN_HEADER)
    if operator_token is not None:
        # Managed agent capabilities are bearers too, and they never act as a user.
        if not server.auth_service.verify_bearer(operator_token):
            raise KeyPrincipalRejected(401, "invalid_token", _AUTH_REQUIRED)
        try:
            user = LocalUserManager(db).require_sole_user()
        except UserIdentityStateError as exc:
            raise KeyPrincipalRejected(403, "user_identity_state", str(exc)) from exc
        return KeyPrincipal(user.id, require_machine_id())

    session_token = request.cookies.get(COOKIE_NAME)
    if session_token is None:
        raise KeyPrincipalRejected(401, "missing_auth", _AUTH_REQUIRED)
    user_id = AuthStore(db).session_user_id(session_token)
    if user_id is None:
        raise KeyPrincipalRejected(401, "session_invalid", _AUTH_REQUIRED)
    return KeyPrincipal(user_id, require_machine_id())


def _issued_response(plaintext: str, key: ApiKey) -> JSONResponse:
    return JSONResponse(
        content={
            "key": plaintext,
            "key_id": key.id,
            "hint": key.hint,
            "user_id": key.user_id,
            "machine_id": key.machine_id,
        },
        headers={"Cache-Control": "no-store"},
    )


def _key_summary(key: ApiKey) -> dict[str, str | None]:
    return {
        "id": key.id,
        "hint": key.hint,
        "label": key.label,
        "machine_id": key.machine_id,
        "created_at": key.created_at.isoformat(),
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
    }


def create_api_keys_router(server: HTTPServer, login_rate_limiter: LoginRateLimiter) -> APIRouter:
    """Create the API key router under ``/api/auth/keys``."""
    router = APIRouter(prefix="/api/auth/keys", tags=["auth"])

    def _db() -> HubDatabase:
        return require_hub_database(server.services.database)

    async def require_key_principal(request: Request) -> KeyPrincipal:
        principal: KeyPrincipal = await server.run_db(_resolve_principal, server, _db(), request)
        return principal

    @router.post("/bootstrap")
    async def bootstrap_key(req: BootstrapKeyRequest, request: Request) -> JSONResponse:
        """Verify a password, bind the machine to its user, and mint its key."""
        client_id = _login_client_id(server, request)
        retry_after = login_rate_limiter.retry_after(client_id)
        if retry_after is not None:
            return login_lockout_response(retry_after)
        user = await server.run_db(server.auth_service.verify_password, req.email, req.password)
        if user is None:
            login_rate_limiter.record_failure(client_id)
            return JSONResponse(
                status_code=401, content={"ok": False, "error": "Invalid email or password"}
            )
        login_rate_limiter.reset(client_id)

        def _bind_and_mint() -> tuple[str, ApiKey]:
            db = _db()
            LocalMachineManager(db).upsert_seen(
                str(req.machine_id), user.id, hostname=req.hostname, os=req.os
            )
            return ApiKeyManager(db).mint(user.id, str(req.machine_id), req.label)

        try:
            plaintext, key = await server.run_db(_bind_and_mint)
        except MachineOwnershipConflictError:
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": "Machine is owned by another user",
                    "code": "machine_not_owned",
                },
            )
        return _issued_response(plaintext, key)

    @router.post("")
    async def mint_key(
        req: MintKeyRequest, principal: KeyPrincipal = Depends(require_key_principal)
    ) -> JSONResponse:
        """Mint a key for the caller's machine."""

        def _mint() -> tuple[str, ApiKey]:
            db = _db()
            machine = LocalMachineManager(db).get(principal.machine_id)
            if machine is None or machine.owner_user_id != principal.user_id:
                raise KeyPrincipalRejected(
                    403, "machine_not_owned", "This machine is not owned by the caller"
                )
            return ApiKeyManager(db).mint(principal.user_id, principal.machine_id, req.label)

        plaintext, key = await server.run_db(_mint)
        return _issued_response(plaintext, key)

    @router.get("")
    async def list_keys(principal: KeyPrincipal = Depends(require_key_principal)) -> JSONResponse:
        """List the caller's keys without hashes or secrets."""
        keys = await server.run_db(ApiKeyManager(_db()).list_for_user, principal.user_id)
        return JSONResponse(content={"keys": [_key_summary(key) for key in keys]})

    @router.delete("/{key_id}")
    async def revoke_key(
        key_id: str, principal: KeyPrincipal = Depends(require_key_principal)
    ) -> JSONResponse:
        """Revoke one of the caller's keys; any other id is not found."""
        revoked = await server.run_db(ApiKeyManager(_db()).revoke, key_id, principal.user_id)
        if not revoked:
            return JSONResponse(status_code=404, content=_NOT_FOUND)
        return JSONResponse(content={"ok": True})

    return router

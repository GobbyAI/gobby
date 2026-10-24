"""Short-lived, one-use loopback browser sign-in, beside the existing issuer.

Pending links are hashed and process-local: restart invalidates them. The exchange
returns only a redirect, never an HTML document or script containing a credential.
Normal auth_sessions own cookie validation and revocation after the exchange.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse, Response

from gobby.config.bootstrap import is_loopback_host
from gobby.servers.responses import JSONResponse
from gobby.servers.routes._database import require_hub_database
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.machines import LocalMachineManager
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)
_LINK_TTL = 120
_SESSION_SECONDS = 3600
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True)
class _Principal:
    user_id: str
    source: str
    session_id: str | None = None
    run_id: str | None = None
    key_id: str | None = None


@dataclass(frozen=True)
class _Pending:
    principal: _Principal
    origin: str
    deadline: float
    # Only managed callers need re-verification when their link is consumed.
    managed_headers: dict[str, str] | None


def _origin(request: Request) -> str | None:
    host = request.url.hostname
    if request.url.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if request.client is None or not is_loopback_host(request.client.host):
        return None
    origin = f"{request.url.scheme}://{request.url.netloc}"
    supplied = request.headers.get("origin")
    if supplied is not None and supplied != origin:
        return None
    # A relayed external peer is never an operator-local request.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded is not None and not is_loopback_host(forwarded):
        return None
    return origin


def _principal(server: HTTPServer, request: Request) -> _Principal | None:
    service = server.auth_service
    identity = service.verified_front_door_identity(request)
    if identity is not None:
        return _Principal(identity.user_id, "operator", key_id=identity.key_id)
    claims = service.verified_agent_claims(request)
    if claims is not None:
        machine = LocalMachineManager(require_hub_database(server.services.database)).get(
            claims.machine_id
        )
        if machine is not None and machine.owner_user_id is not None:
            return _Principal(
                machine.owner_user_id,
                "managed",
                session_id=claims.session_id,
                run_id=claims.agent_run_id or claims.managed_execution_id,
            )
    if service.break_glass_admits(request):
        machine_id = get_machine_id()
        if machine_id is not None:
            machine = LocalMachineManager(require_hub_database(server.services.database)).get(
                machine_id
            )
            if machine is not None and machine.owner_user_id is not None:
                return _Principal(machine.owner_user_id, "operator-local")
    return None


def _audit(event: str, principal: _Principal, auth_session_id: str | None = None) -> None:
    # Values here are verified provenance, never bearer bytes, link/session tokens or hashes.
    logger.info(
        "%s user_id=%s source=%s session_id=%s run_id=%s key_id=%s auth_session_id=%s",
        event,
        principal.user_id,
        principal.source,
        principal.session_id,
        principal.run_id,
        principal.key_id,
        auth_session_id,
    )


def create_browser_sign_in_router(server: HTTPServer) -> APIRouter:
    router = APIRouter()
    pending: dict[str, _Pending] = {}
    lock = threading.Lock()

    @router.post("/sign-in-link")
    async def mint(request: Request) -> Response:
        origin = _origin(request)
        if origin is None:
            return JSONResponse(
                status_code=403, content={"error": "Loopback origin required"}, headers=_NO_STORE
            )
        principal = await server.run_db(_principal, server, request)
        if principal is None:
            return JSONResponse(
                status_code=401,
                content={"error": "Verified operator or managed credential required"},
                headers=_NO_STORE,
            )
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        managed_headers = None
        if principal.source == "managed":
            managed_headers = {
                key: value
                for key, value in request.headers.items()
                if key
                in {
                    "authorization",
                    "x-gobby-session-id",
                    "x-gobby-project-id",
                    "x-gobby-caller-project-id",
                    "x-gobby-agent-run-id",
                    "x-gobby-managed-execution-id",
                }
            }
        now = time.monotonic()
        with lock:
            for expired in [key for key, value in pending.items() if value.deadline <= now]:
                del pending[expired]
            if len(pending) >= 1024:
                return JSONResponse(
                    status_code=429, content={"error": "Too many pending links"}, headers=_NO_STORE
                )
            pending[token_hash] = _Pending(principal, origin, now + _LINK_TTL, managed_headers)
        _audit("browser_sign_in_minted", principal)
        return JSONResponse(
            content={"url": f"{origin}/api/auth/sign-in?token={token}", "expires_in": _LINK_TTL},
            headers=_NO_STORE,
        )

    @router.get("/sign-in")
    async def consume(request: Request) -> Response:
        origin = _origin(request)
        if origin is None:
            return JSONResponse(
                status_code=403, content={"error": "Loopback origin required"}, headers=_NO_STORE
            )
        tokens = request.query_params.getlist("token")
        if len(tokens) != 1 or not 32 <= len(tokens[0]) <= 128:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired sign-in link"},
                headers=_NO_STORE,
            )
        token_hash = hashlib.sha256(tokens[0].encode()).hexdigest()
        with lock:
            link = pending.get(token_hash)
            if link is not None and link.origin == origin:
                del pending[token_hash]
            else:
                link = None
        if link is None or link.deadline <= time.monotonic():
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired sign-in link"},
                headers=_NO_STORE,
            )
        claims = None
        if link.managed_headers is not None:
            scope = dict(request.scope)
            scope.update(
                method="POST",
                path="/api/auth/sign-in-link",
                raw_path=b"/api/auth/sign-in-link",
                query_string=b"",
                headers=[
                    (key.encode(), value.encode()) for key, value in link.managed_headers.items()
                ],
            )
            if await server.run_db(_principal, server, Request(scope)) != link.principal:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Minting run is no longer authorized"},
                    headers=_NO_STORE,
                )
            claims = await server.run_db(server.auth_service.verified_agent_claims, Request(scope))
            if claims is None:
                return JSONResponse(
                    status_code=401, content={"error": "Run expired"}, headers=_NO_STORE
                )
        row = None
        if claims is not None:
            session_token = server.auth_service.browser_sessions.create(
                link.principal.user_id, claims
            )
        else:
            store = AuthStore(require_hub_database(server.services.database))
            session_token, _ = await server.run_db(
                store.create_session,
                link.principal.user_id,
                duration=timedelta(seconds=_SESSION_SECONDS),
            )
            row = await server.run_db(
                store.db.fetchone,
                "SELECT id FROM auth_sessions WHERE token_hash = %s",
                (hash_token(session_token),),
            )
        response = RedirectResponse("/", status_code=303, headers=_NO_STORE)
        response.set_cookie(
            "gobby_session",
            session_token,
            max_age=_SESSION_SECONDS,
            httponly=True,
            secure=urlsplit(origin).scheme == "https",
            samesite="strict",
            path="/",
        )
        _audit("browser_sign_in_consumed", link.principal, str(row["id"]) if row else None)
        return response

    return router

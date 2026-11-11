"""gmcp under Codex's shared app-server exchanges a thread id for its session token (#25905).

Codex's managed app-server runs every hand-started thread's gmcp under one worker, so no
CLI process names a thread and gmcp cannot find its session the way it does under its own
TUI. Each tool call's ``_meta`` carries the thread id instead. The thread's start
registered it to the live worker that ran its hooks (codex_thread_hosts). This route binds
gmcp to that thread only when gmcp's kernel-attested parent is that same live worker, by
pid and start time, running as this daemon's user, and the caller presents no managed run.

Accepted residual, by the operator's ruling: a command child of the same worker can name
a sibling hand thread's id. Hand-started threads all run unsandboxed as the operator, so
that crosses no privilege boundary; sandboxed seats run with ``--no-daemon``, off this
worker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from gobby.hooks.session_claimant import front_door_claimant
from gobby.hooks.session_credential import mint_session_credential
from gobby.servers.responses import JSONResponse
from gobby.sessions.terminal_kill import create_time_matches
from gobby.storage.codex_thread_hosts import thread_host
from gobby.utils.session_context import AGENT_RUN_ID_HEADER, SESSION_SELECTOR_HEADERS

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

CODEX_THREAD_CREDENTIAL_PATH = "/api/hooks/codex-thread-credential"
_UNBOUND = {"success": False, "error_code": "session_credential_unbound"}


def _exchange(server: HTTPServer, request: Request, thread_id: Any) -> dict[str, Any] | None:
    """The thread's credential when gmcp's parent is the worker it registered from."""
    headers = request.headers
    if headers.get(AGENT_RUN_ID_HEADER) or any(
        headers.get(name) for name in SESSION_SELECTOR_HEADERS
    ):
        return None
    if not isinstance(thread_id, str) or not thread_id:
        return None
    identity = server.auth_service.verified_front_door_identity(request)
    db = getattr(getattr(server, "services", None), "database", None)
    if identity is None or db is None:
        return None
    claimant = front_door_claimant(headers, daemon_port=server.bootstrap_config.daemon_port)
    if (
        claimant is None
        or not claimant.shared_host
        or claimant.parent_pid is None
        or claimant.create_time is None
    ):
        return None
    host = thread_host(db, machine_id=identity.machine_id, external_id=thread_id)
    if (
        host is None
        or host.pid != claimant.parent_pid
        or not create_time_matches(host.create_time, claimant.create_time)
    ):
        return None
    return mint_session_credential(
        db,
        signing_key=server.auth_service.managed_signing_key(),
        external_id=thread_id,
        source="codex",
        machine_id=identity.machine_id,
    )


def create_codex_thread_credential_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(tags=["hooks"])

    @router.post(CODEX_THREAD_CREDENTIAL_PATH)
    async def codex_thread_credential(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = None
        thread_id = body.get("thread_id") if isinstance(body, dict) else None
        try:
            credential = await asyncio.to_thread(_exchange, server, request, thread_id)
        except Exception as exc:
            logger.warning("Codex thread credential exchange failed: %s", type(exc).__name__)
            credential = None
        if credential is None:
            return JSONResponse(status_code=403, content=_UNBOUND)
        return JSONResponse(content={"success": True, **credential})

    return router

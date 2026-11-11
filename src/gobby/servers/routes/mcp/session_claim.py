"""The hook route's kernel-attested claim and its session credential (#25905).

Every machine-bearer hook is claimed before its handler runs, and the claim is settled
after the handler persists it: a start pins or registers its owner, and a hook that may
mint receives its session's own token.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Final

from fastapi import Request

from gobby.hooks import session_claimant
from gobby.hooks.session_claimant import SessionStartClaim
from gobby.hooks.session_credential import (
    SESSION_CREDENTIAL_FIELD,
    is_session_start_hook,
    settle_claim,
)
from gobby.utils.session_context import AGENT_RUN_ID_HEADER

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

PROVIDER_SESSION_KEYS: Final = ("session_id", "sessionId", "conversationId", "conversation_id")


def provider_session_id(input_data: Any) -> str | None:
    """The provider's own id for the thread a hook body names, if it names one."""
    if not isinstance(input_data, dict):
        return None
    return next(
        (
            value
            for key in PROVIDER_SESSION_KEYS
            if isinstance(value := input_data.get(key), str) and value
        ),
        None,
    )


def claim_hook(server: HTTPServer, request: Request, payload: dict[str, Any]) -> SessionStartClaim:
    """Attest a machine-bearer hook's kernel-proven claimant before its handler runs.

    The row's owner is stamped from the claim, never from the body: a proven CLI, or the
    shared worker whose seat search finds the thread's TUI. A hook with no proof has its
    body's parent pid removed, so its row never names an owner it could later mint for.
    """
    if request.headers.get(AGENT_RUN_ID_HEADER):
        return SessionStartClaim()
    identity = server.auth_service.verified_front_door_identity(request)
    input_data = payload.get("input_data")
    source = payload.get("source")
    external_id = provider_session_id(input_data)
    db = getattr(getattr(server, "services", None), "database", None)
    if (
        identity is None
        or not isinstance(input_data, dict)
        or not isinstance(source, str)
        or external_id is None
        or db is None
    ):
        return SessionStartClaim()
    claimant = session_claimant.front_door_claimant(
        request.headers, daemon_port=server.bootstrap_config.daemon_port
    )
    claim = session_claimant.claim_session_start(
        db,
        claimant=claimant,
        daemon_pid=os.getpid(),
        external_id=external_id,
        source=source,
        machine_id=identity.machine_id,
        start=is_session_start_hook(source, payload.get("hook_type")),
    )
    if claim.refusal is None:
        owner = claim.host.pid if claim.host is not None else claim.cli_pid
        context = input_data.get("terminal_context")
        context = dict(context) if isinstance(context, dict) else {}
        if owner is None:
            context.pop("parent_pid", None)
        else:
            context["parent_pid"] = owner
        input_data["terminal_context"] = context
    return claim


def attach_session_credential(
    server: HTTPServer,
    request: Request,
    payload: dict[str, Any],
    response: dict[str, Any],
    *,
    db: Any,
    claim: SessionStartClaim,
) -> dict[str, Any]:
    """Settle a proven hook's claim once its handler has persisted it, and mint if it may.

    A managed or run-bound caller never reaches this branch; a daemon replay or an
    unproven hook settles nothing and mints nothing.
    """
    if not claim.proven or request.headers.get(AGENT_RUN_ID_HEADER):
        return response
    identity = server.auth_service.verified_front_door_identity(request)
    input_data = payload.get("input_data")
    source = payload.get("source")
    external_id = provider_session_id(input_data)
    if identity is None or not isinstance(source, str) or external_id is None or db is None:
        return response
    try:
        credential = settle_claim(
            db,
            claim,
            signing_key=server.auth_service.managed_signing_key(),
            external_id=external_id,
            source=source,
            machine_id=identity.machine_id,
            start=is_session_start_hook(source, payload.get("hook_type")),
        )
    except Exception as exc:
        logger.warning("Session credential settle failed: %s", type(exc).__name__)
        return response
    if credential is None:
        return response
    return {**response, SESSION_CREDENTIAL_FIELD: credential}

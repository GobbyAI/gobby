"""Session-bound credential a machine-bearer hook hands its unspawned CLI (#25905)."""

from __future__ import annotations

import logging
from typing import Any

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.hooks.events import HookEventType
from gobby.hooks.session_claimant import SessionStartClaim
from gobby.sessions.terminal_kill import create_time_matches
from gobby.storage.codex_thread_hosts import register_thread_host
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import pin_startup
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.terminal_context import parse_terminal_context_value
from gobby.utils.local_token import issue_session_api_token

logger = logging.getLogger(__name__)

SESSION_CREDENTIAL_FIELD = "_gobby_session_credential"


def is_session_start_hook(source: object, hook_type: object) -> bool:
    """Return whether `hook_type` is `source`'s own spelling of SessionStart.

    Each provider names the hook its own way (Claude ``session-start``, Codex and Droid
    ``SessionStart``, Grok either), so the provider's capability registry decides. An
    unknown source or hook is not a start.
    """
    if not isinstance(source, str) or not isinstance(hook_type, str):
        return False
    try:
        capability = get_provider_capabilities(source).get_hook(hook_type)
    except ValueError:
        return False
    return capability is not None and capability.event_type == HookEventType.SESSION_START


def settle_claim(
    db: HubDatabase,
    claim: SessionStartClaim,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
    start: bool,
) -> dict[str, Any] | None:
    """Record what a proven hook established, then mint if it may.

    A start pins its CLI to the mark its deferred row left, first writer winning, or
    replaces a pin proven dead. It mints once a row exists. The hook that creates a
    deferred startup's row mints for the CLI pinned to it. A shared-host Codex start
    registers its thread to its worker and mints. Every mint names only a row that stores
    the proven owner, so a claimant that lost a race gets nothing.
    """
    if not claim.proven:
        return None
    if claim.host is not None:
        registered = register_thread_host(
            db,
            machine_id=machine_id,
            external_id=external_id,
            host=claim.host,
            displaced=claim.displaced_host,
        )
        if not registered:
            logger.warning("Codex thread %s is registered to another worker", external_id)
            return None
        return mint_session_credential(
            db,
            signing_key=signing_key,
            external_id=external_id,
            source=source,
            machine_id=machine_id,
        )
    pin = claim.pin
    if pin is None:
        return None
    if start:
        pin_startup(
            db,
            machine_id=machine_id,
            source=source,
            external_id=external_id,
            pin=pin,
            displaced=claim.displaced,
        )
    elif not claim.creates_row:
        return None
    return mint_session_credential(
        db,
        signing_key=signing_key,
        external_id=external_id,
        source=source,
        machine_id=machine_id,
        owner_cli_pid=pin.cli_pid,
        owner_create_time=pin.cli_create_time,
    )


def mint_session_credential(
    db: HubDatabase,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
    owner_cli_pid: int | None = None,
    owner_create_time: float | None = None,
) -> dict[str, Any] | None:
    """Mint a token bound to the live terminal session this provider thread names.

    Returns None when no live session matches or no signing key is available, so a
    refused or unknown start hands back nothing. The token names only that session.
    ``owner_cli_pid`` and ``owner_create_time`` are the CLI a claim proved: the row must
    store that process, so a concurrent claimant whose row creation lost the race gets
    nothing.
    """
    if signing_key is None:
        logger.warning("Session credential not minted: managed signing key unavailable")
        return None
    row = db.fetchone(
        """
        SELECT id, project_id, terminal_context FROM sessions
        WHERE external_id = %s AND source = %s AND machine_id = %s
          AND session_type = 'terminal' AND status = ANY(%s)
        ORDER BY created_at DESC, id
        LIMIT 1
        """,
        (external_id, source, machine_id, list(LIVE_SESSION_STATUSES)),
    )
    if row is None:
        return None
    if owner_cli_pid is not None:
        context = parse_terminal_context_value(row["terminal_context"]) or {}
        stored_time = context.get("parent_create_time")
        if context.get("parent_pid") != owner_cli_pid or (
            owner_create_time is not None
            and isinstance(stored_time, (int, float))
            and not isinstance(stored_time, bool)
            and not create_time_matches(float(stored_time), owner_create_time)
        ):
            logger.warning("Session credential not minted: the session's CLI is not the claimant")
            return None
    session_id = str(row["id"])
    project_id = str(row["project_id"])
    token = issue_session_api_token(
        signing_key,
        session_id=session_id,
        project_id=project_id,
        machine_id=machine_id,
    )
    return {"token": token, "session_id": session_id, "project_id": project_id}

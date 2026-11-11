"""Session-bound credential a machine-bearer hook hands its unspawned CLI (#25905)."""

from __future__ import annotations

import logging
from typing import Any

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.hooks.events import HookEventType
from gobby.hooks.session_claimant import SessionStartClaim, process_is_dead
from gobby.sessions.terminal_kill import create_time_matches
from gobby.storage.codex_thread_hosts import (
    ThreadHost,
    unregister_thread_host,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_startup_marks import StartupPin, pin_startup, startup_mark
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.terminal_context import parse_terminal_context_value
from gobby.utils.local_token import issue_session_api_token

logger = logging.getLogger(__name__)

SESSION_CREDENTIAL_FIELD = "_gobby_session_credential"

_LIVE_ROW = """
    SELECT s.id, s.project_id, s.terminal_context FROM sessions s
    WHERE s.external_id = %s AND s.source = %s AND s.machine_id = %s
      AND s.session_type = 'terminal' AND s.status = ANY(%s)
    ORDER BY s.created_at DESC, s.id
    LIMIT 1
"""
# The thread's live row, read only while it is registered to the given worker and no CLI
# has pinned its startup, in one statement.
_HOSTED_LIVE_ROW = """
    SELECT s.id, s.project_id, s.terminal_context FROM sessions s
    WHERE s.external_id = %s AND s.source = %s AND s.machine_id = %s
      AND s.session_type = 'terminal' AND s.status = ANY(%s)
      AND EXISTS (
        SELECT 1 FROM codex_thread_hosts h
        WHERE h.machine_id = %s AND h.external_id = %s
          AND h.host_pid = %s AND h.host_create_time = %s
      )
      AND NOT EXISTS (
        SELECT 1 FROM session_startup_marks m
        WHERE m.machine_id = %s AND m.source = %s AND m.external_id = %s
          AND m.cli_pid IS NOT NULL
      )
    ORDER BY s.created_at DESC, s.id
    LIMIT 1
"""


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
    deferred startup's row mints for the CLI pinned to it. A shared-host Codex start,
    whose claim registered its thread to its worker, mints. Every mint names only a row that stores
    the proven owner, so a claimant that lost a race gets nothing.

    The claim was decided before the hook's handler ran, so a CLI may have pinned the
    thread since. A CLI's start therefore pins before it drops any worker registration,
    and a worker re-reads the pin after it registers and withdraws while a live CLI
    holds it. Whichever order the two run in, no registration outlives the pin, and the
    worker's mint reads the row only while its registration stands and no pin does.
    """
    if not claim.proven:
        return None
    if claim.host is not None:
        return _settle_shared_host(
            db,
            claim.host,
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
        # The pin, or the live row a resume revived, now excludes worker children, so this
        # drops the hold the claim wrote (and any registration a worker child wrote first).
        unregister_thread_host(db, machine_id=machine_id, external_id=external_id)
    elif not claim.creates_row:
        return None
    return mint_session_credential(
        db,
        signing_key=signing_key,
        external_id=external_id,
        source=source,
        machine_id=machine_id,
        owner=pin,
    )


def _settle_shared_host(
    db: HubDatabase,
    host: ThreadHost,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
) -> dict[str, Any] | None:
    """Withdraw the claim's registration if a live CLI pinned the thread since, else mint.

    The claim registered the thread before the handler ran; settle writes no registration,
    so none can appear after the claim's own re-check.
    """
    key = {"machine_id": machine_id, "external_id": external_id}
    mark = startup_mark(db, source=source, **key)
    pin = None if mark is None else mark.pin
    if pin is not None and not process_is_dead(pin.cli_pid, pin.cli_create_time):
        unregister_thread_host(db, **key, host=host)
        logger.warning("Codex thread %s was pinned by a live CLI; worker withdrew", external_id)
        return None
    return mint_session_credential(
        db,
        signing_key=signing_key,
        external_id=external_id,
        source=source,
        machine_id=machine_id,
        host=host,
    )


def mint_session_credential(
    db: HubDatabase,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
    owner: StartupPin | None = None,
    host: ThreadHost | None = None,
) -> dict[str, Any] | None:
    """Mint a token bound to the live terminal session this provider thread names.

    Returns None when no live session matches or no signing key is available, so a
    refused or unknown start hands back nothing. The token names only that session.
    ``owner`` is the CLI a claim proved: the row must store that process's pid and start
    time, so a concurrent claimant whose row creation lost the race gets
    nothing. ``host`` is the shared worker a claim proved: the same read that finds the
    row requires the thread still registered to that worker and pinned to no CLI, so a
    CLI that pinned the thread after the worker's checks leaves it nothing to name.
    """
    if signing_key is None:
        logger.warning("Session credential not minted: managed signing key unavailable")
        return None
    live = (external_id, source, machine_id, list(LIVE_SESSION_STATUSES))
    if host is None:
        row = db.fetchone(_LIVE_ROW, live)
    else:
        hosted = (machine_id, external_id, host.pid, host.create_time)
        unpinned = (machine_id, source, external_id)
        row = db.fetchone(_HOSTED_LIVE_ROW, (*live, *hosted, *unpinned))
    if row is None:
        return None
    if owner is not None:
        context = parse_terminal_context_value(row["terminal_context"]) or {}
        stored_time = context.get("parent_create_time")
        if (
            context.get("parent_pid") != owner.cli_pid
            or not isinstance(stored_time, (int, float))
            or isinstance(stored_time, bool)
            or not create_time_matches(float(stored_time), owner.cli_create_time)
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

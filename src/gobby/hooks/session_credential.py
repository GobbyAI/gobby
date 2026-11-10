"""Session-bound credential a SessionStart hands its unspawned machine caller."""

from __future__ import annotations

import logging
from typing import Any

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.hooks.events import HookEventType
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import LIVE_SESSION_STATUSES
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


def mint_session_credential(
    db: HubDatabase,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
    shared_host: bool = False,
) -> dict[str, Any] | None:
    """Mint a token bound to the live terminal session this provider start names.

    Returns None when no live session matches or no signing key is available, so a
    refused or unknown start hands back nothing. The token names only that session.
    A shared-host start's credential is marked so and lists the session's own refs: its
    hook client keeps it under those refs, never under the host process every seat shares.
    """
    if signing_key is None:
        logger.warning("Session credential not minted: managed signing key unavailable")
        return None
    row = db.fetchone(
        """
        SELECT s.id, s.project_id, s.seq_num, p.name AS project_name
        FROM sessions s LEFT JOIN projects p ON p.id = s.project_id
        WHERE s.external_id = %s AND s.source = %s AND s.machine_id = %s
          AND s.session_type = 'terminal' AND s.status = ANY(%s)
        ORDER BY s.created_at DESC, s.id
        LIMIT 1
        """,
        (external_id, source, machine_id, list(LIVE_SESSION_STATUSES)),
    )
    if row is None:
        return None
    session_id = str(row["id"])
    project_id = str(row["project_id"])
    token = issue_session_api_token(
        signing_key,
        session_id=session_id,
        project_id=project_id,
        machine_id=machine_id,
    )
    credential: dict[str, Any] = {
        "token": token,
        "session_id": session_id,
        "project_id": project_id,
    }
    if shared_host:
        credential["shared_host"] = True
        credential["session_refs"] = _session_refs(
            session_id, external_id, project_id, row["seq_num"], row["project_name"]
        )
    return credential


def _session_refs(
    session_id: str,
    external_id: str,
    project_id: str,
    seq_num: object,
    project_name: object,
) -> list[str]:
    """Every spelling a seat may give its own session as a call's session_id."""
    refs = [session_id, external_id]
    if isinstance(seq_num, int):
        refs += [f"#{seq_num}", str(seq_num), f"{project_id}#{seq_num}"]
        if isinstance(project_name, str) and project_name:
            refs.append(f"{project_name}#{seq_num}")
    return list(dict.fromkeys(refs))

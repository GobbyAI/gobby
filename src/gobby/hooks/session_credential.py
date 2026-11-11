"""Session-bound credential a SessionStart hands its unspawned machine caller."""

from __future__ import annotations

import logging
from typing import Any

from gobby.adapters.capabilities import get_provider_capabilities
from gobby.hooks.events import HookEventType
from gobby.storage.hub.protocol import HubDatabase
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


def mint_session_credential(
    db: HubDatabase,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
    owner_cli_pid: int | None = None,
) -> dict[str, Any] | None:
    """Mint a token bound to the live terminal session this provider start names.

    Returns None when no live session matches or no signing key is available, so a
    refused or unknown start hands back nothing. The token names only that session.
    ``owner_cli_pid`` is the CLI pid a claim proved for a row it created: the row must
    store that pid, so a concurrent claimant whose row creation lost the race gets nothing.
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
        if context.get("parent_pid") != owner_cli_pid:
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

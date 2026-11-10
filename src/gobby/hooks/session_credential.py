"""Session-bound credential a SessionStart hands its unspawned machine caller."""

from __future__ import annotations

import logging
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.utils.local_token import issue_session_api_token

logger = logging.getLogger(__name__)

SESSION_CREDENTIAL_FIELD = "_gobby_session_credential"


def mint_session_credential(
    db: HubDatabase,
    *,
    signing_key: bytes | None,
    external_id: str,
    source: str,
    machine_id: str,
) -> dict[str, Any] | None:
    """Mint a token bound to the live terminal session this provider start names.

    Returns None when no live session matches or no signing key is available, so a
    refused or unknown start hands back nothing. The token names only that session.
    """
    if signing_key is None:
        logger.warning("Session credential not minted: managed signing key unavailable")
        return None
    row = db.fetchone(
        """
        SELECT id, project_id FROM sessions
        WHERE external_id = %s AND source = %s AND machine_id = %s
          AND session_type = 'terminal' AND status = ANY(%s)
        ORDER BY created_at DESC, id
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
    return {"token": token, "session_id": session_id, "project_id": project_id}

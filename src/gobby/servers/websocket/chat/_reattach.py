"""Resolve durable web-chat rows after a successful clear continuation."""

from __future__ import annotations

import logging

from gobby.sessions.clear_continuation import resolve_clear_successor
from gobby.storage.session_models import Session
from gobby.storage.sessions import TERMINAL_SESSION_STATUSES, SessionManager

logger = logging.getLogger(__name__)


def redirect_terminal_web_chat_candidate(
    candidate: Session,
    storage: SessionManager,
) -> Session:
    """Redirect a cleared terminal web-chat row to its live successor."""
    if candidate.session_type != "web_chat" or candidate.status not in TERMINAL_SESSION_STATUSES:
        return candidate
    successor_id = resolve_clear_successor(storage.db, candidate.id)
    if successor_id is None:
        return candidate
    successor = storage.get(successor_id)
    if successor is None:
        return candidate
    logger.info(
        "web_chat_reattach_redirected",
        extra={
            "predecessor_session_id": candidate.id,
            "successor_session_id": successor.id,
        },
    )
    return successor

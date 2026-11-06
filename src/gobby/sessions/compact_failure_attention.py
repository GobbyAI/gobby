"""Blocked attention for a compact handoff whose delivery failed, cleared once a delivery lands."""

from __future__ import annotations

import logging

from gobby.app_context import get_app_context
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

COMPACT_FAILURE_ATTENTION_REASON = "handoff_delivery_failed"


def compact_failure_attention_manager(db: HubDatabase) -> AttentionStateManager:
    container = get_app_context()
    configured = getattr(container, "attention_manager", None)
    return (
        configured
        if isinstance(configured, AttentionStateManager) and configured.db is db
        else AttentionStateManager(db)
    )


def clear_compact_failure_attention(db: HubDatabase, session_id: str) -> None:
    """Clear the blocked entry a failed compact delivery raised once a delivery lands."""
    try:
        manager = compact_failure_attention_manager(db)
        entry_id = session_attention_entry_id(session_id)
        current = manager.get(entry_id)
        if current is None or current.reason != COMPACT_FAILURE_ATTENTION_REASON:
            return
        manager.transition(
            entry_id,
            state=None,
            expected_attention_id=current.attention_id,
            expected_fingerprint=current.fingerprint,
        )
    except Exception:
        logger.warning(
            "Failed clearing compact delivery attention for session %s",
            session_id,
            exc_info=True,
        )

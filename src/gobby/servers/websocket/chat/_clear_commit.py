"""Live-wrapper rebinding for web-chat clear-session successors."""

from __future__ import annotations

import logging
from typing import Any

from gobby.servers.chat_session_base import ChatSessionProtocol

logger = logging.getLogger(__name__)


def wire_db_persist_callbacks(mixin: Any, session: ChatSessionProtocol) -> None:
    """Point mode/tool persistence callbacks at the session's current db id."""
    session_manager = getattr(mixin, "session_manager", None)
    db_session_id = getattr(session, "db_session_id", None)
    if session_manager is None or not isinstance(db_session_id, str) or not db_session_id:
        return

    def _persist_mode(mode: str) -> None:
        try:
            session_manager.update_chat_mode(db_session_id, mode)
        except Exception:
            logger.debug("Failed to persist chat_mode", exc_info=True)

    def _persist_approved_tools(tools: set[str]) -> None:
        try:
            session_manager.update_approved_tools(db_session_id, tools)
        except Exception:
            logger.debug("Failed to persist approved_tools", exc_info=True)

    session._on_mode_persist = _persist_mode
    session._on_approved_tools_persist = _persist_approved_tools


def rebind_live_clear_successor(mixin: Any, session: ChatSessionProtocol, successor: Any) -> None:
    """Point the live wrapper at the successor row without starting a backend."""
    session.db_session_id = successor.id
    session.seq_num = successor.seq_num
    session.message_index = 0
    wire_db_persist_callbacks(mixin, session)

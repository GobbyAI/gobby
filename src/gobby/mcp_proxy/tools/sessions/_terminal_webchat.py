"""Live web-chat compaction and clear helpers for terminal session tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.servers.chat_session_base import ChatSessionProtocol
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry

logger = logging.getLogger(__name__)


def _find_live_web_chat_session(
    web_chat_session_registry: WebChatSessionRegistry | None,
    *session_ids: str | None,
) -> tuple[str | None, ChatSessionProtocol | None]:
    """Return the first live web-chat session matching any of ``session_ids``."""
    if web_chat_session_registry is None:
        return None, None

    seen: set[str] = set()
    for session_id in session_ids:
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        try:
            live_session = web_chat_session_registry.find_session(session_id)[1]
        except (LookupError, KeyError, RuntimeError):
            logger.debug(
                "Failed to look up live web_chat session %s",
                session_id,
                exc_info=True,
            )
            continue
        if live_session is not None:
            return session_id, live_session
    return None, None


async def _compact_live_web_chat_fallback(
    web_chat_session_registry: WebChatSessionRegistry | None,
    *session_ids: str | None,
) -> dict[str, Any] | None:
    """Compact a live web-chat session when DB-backed session lookup is unavailable."""
    if web_chat_session_registry is None:
        return None

    seen: set[str] = set()
    for session_id in session_ids:
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        try:
            live_session = web_chat_session_registry.find_session(session_id)[1]
        except (LookupError, KeyError, RuntimeError):
            logger.debug(
                "Failed to look up live web_chat session %s for compaction fallback",
                session_id,
                exc_info=True,
            )
            continue
        if live_session is None:
            continue
        try:
            return await web_chat_session_registry.compact_session(session_id)
        except (LookupError, KeyError, RuntimeError):
            logger.warning(
                "Failed to compact live web_chat session %s via fallback",
                session_id,
                exc_info=True,
            )
            continue
    return None


async def _clear_live_web_chat_fallback(
    web_chat_session_registry: WebChatSessionRegistry | None,
    *session_ids: str | None,
    attempt_id: str,
    continuation_prompt: str,
) -> dict[str, Any] | None:
    """Clear a live web-chat session after the caller has staged the attempt."""
    if web_chat_session_registry is None:
        return None

    seen: set[str] = set()
    for session_id in session_ids:
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        try:
            live_session = web_chat_session_registry.find_session(session_id)[1]
        except (LookupError, KeyError, RuntimeError):
            logger.debug(
                "Failed to look up live web_chat session %s for clear fallback",
                session_id,
                exc_info=True,
            )
            continue
        if live_session is None:
            continue
        try:
            return await web_chat_session_registry.clear_session(
                session_id,
                attempt_id=attempt_id,
                continuation_prompt=continuation_prompt,
            )
        except (LookupError, KeyError, RuntimeError):
            logger.warning(
                "Failed to clear live web_chat session %s via fallback",
                session_id,
                exc_info=True,
            )
            continue
    return None

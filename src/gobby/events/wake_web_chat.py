"""Web-chat live wake: the registry that owns the chat session takes the wake.

A web-chat session has no terminal to write to, so the wake is a call into the
registry instead. It reads the dispatcher's preflight and registry wiring directly,
as ``wake_batch`` does for its terminal path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.events.wake import LIVE_WAKE_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from gobby.events.wake import WakeDispatcher

logger = logging.getLogger(__name__)


async def dispatch_web_chat_wake(
    dispatcher: WakeDispatcher, session_id: str, *, priority: str = "normal"
) -> dict[str, Any]:
    if dispatcher._web_chat_session_registry is None:
        return _web_chat_no_live_result(session_id)

    _session, state_failure = await dispatcher._preflight_live_side_effect(
        session_id, priority=priority
    )
    if state_failure is not None:
        return state_failure

    try:
        result = await asyncio.wait_for(
            dispatcher._web_chat_session_registry.wake_session(session_id),
            timeout=LIVE_WAKE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "web_chat wake failed for session %s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "web_chat",
            "error": str(exc),
            "error_code": "web_chat_wake_failed",
            "error_message": str(exc),
        }

    if not isinstance(result, dict):
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "web_chat",
            "error_code": "web_chat_wake_failed",
        }
    result.setdefault("session_id", session_id)
    result.setdefault("method", "web_chat")
    return result


def _web_chat_no_live_result(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "delivered": False,
        "method": "web_chat",
        "error": "no_live_web_chat_session",
        "error_code": "no_live_web_chat_session",
        "error_message": f"No live web_chat session found for {session_id}",
    }

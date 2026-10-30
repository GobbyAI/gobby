"""Resolve an agent's SDK session and deliver its wake through the resume channel."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.events.wake import WakeDispatcher

logger = logging.getLogger(__name__)


async def dispatch_sdk_wake(
    dispatcher: WakeDispatcher,
    session_id: str,
    *,
    priority: str,
    prompt: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Return None when this agent has no SDK resume route."""
    if dispatcher._sdk_resumer is None:
        return None
    sdk_session_id = await _resolve_sdk_session_id(dispatcher, session_id)
    if not sdk_session_id:
        return None
    _, state_failure = await dispatcher._preflight_live_side_effect(session_id, priority=priority)
    if state_failure is not None:
        return state_failure
    try:
        await asyncio.wait_for(
            dispatcher._sdk_resumer(sdk_session_id, f"{prompt}\n"),
            timeout=timeout_seconds,
        )
    except Exception:
        logger.warning(
            "SDK resume failed for session %s (sdk=%s)",
            session_id,
            sdk_session_id,
            exc_info=True,
        )
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "sdk",
            "error": "sdk_resume_failed",
            "error_code": "sdk_resume_failed",
            "error_message": "SDK resume failed",
        }
    await dispatcher._record_live_wake(session_id, utc_now())
    return {"session_id": session_id, "delivered": True, "method": "sdk"}


async def _resolve_sdk_session_id(dispatcher: WakeDispatcher, session_id: str) -> str | None:
    """Resolve the SDK identity from the session or its owning agent run."""
    agent_run_manager = dispatcher._agent_run_manager
    if agent_run_manager is None:
        return None
    try:

        def resolve_sdk_session_id() -> str | None:
            with agent_run_manager.db.bounded_transaction():
                session = dispatcher._session_manager.get(session_id)
                if session and getattr(session, "external_id", None):
                    return cast(str | None, session.external_id)
                return agent_run_manager.get_sdk_session_id_for_session(session_id)

        return cast(str | None, await dispatcher._run_db(resolve_sdk_session_id))
    except Exception:
        logger.debug("Could not resolve sdk_session_id for session %s", session_id, exc_info=True)
        return None

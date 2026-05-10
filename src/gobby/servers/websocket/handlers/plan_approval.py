"""Plan approval handlers for WebSocket session control.

Handles plan_approval_response and recovered plan approval after daemon restart.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.servers.websocket.db import run_db
from gobby.storage.config_store import ConfigStore

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


def _normalize_post_plan_mode(value: str | None) -> str:
    if value in {"bypass", "auto"}:
        return "bypass"
    return "normal"


def _resolve_post_plan_mode(mixin: SessionControlMixin) -> str:
    session_manager = getattr(mixin, "session_manager", None)
    db = getattr(session_manager, "db", None) if session_manager else None
    if db is None:
        return "normal"
    try:
        configured = ConfigStore(db).get("ui_settings.postPlanChatMode")
    except Exception:
        logger.debug("Failed to load postPlanChatMode from config store", exc_info=True)
        configured = None
    return _normalize_post_plan_mode(configured if isinstance(configured, str) else None)


async def _send_mode_changed(
    websocket: Any,
    *,
    conversation_id: str,
    mode: str,
    reason: str,
) -> None:
    try:
        await websocket.send(
            json.dumps(
                {
                    "type": "mode_changed",
                    "conversation_id": conversation_id,
                    "mode": mode,
                    "reason": reason,
                }
            )
        )
    except (ConnectionClosed, ConnectionClosedError):
        pass


def _clear_pending_plan_prompt(session: Any) -> None:
    if hasattr(session, "_clear_pending_plan_prompt"):
        session._clear_pending_plan_prompt()
        return
    session._pending_plan_content = None
    session._pending_plan_allowed_prompts = None


async def handle_plan_approval_response(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle plan_approval_response message from the web UI.

    Processes the user's decision on a proposed plan:
    - "approve": Exit planning into the configured post-plan execution mode
    - "request_changes": Store feedback for the next prompt injection

    Message format:
    {
        "type": "plan_approval_response",
        "conversation_id": "stable-id",
        "decision": "approve" | "request_changes",
        "feedback": "optional feedback text"
    }
    """
    conversation_id_raw: str | None = data.get("conversation_id")
    decision = data.get("decision", "")

    session = mixin._chat_sessions.get(conversation_id_raw) if conversation_id_raw else None

    # Recovery path: no in-memory session (daemon restarted)
    if session is None and conversation_id_raw:
        await handle_recovered_plan_approval(mixin, websocket, conversation_id_raw, data)
        return

    if session is None or conversation_id_raw is None:
        logger.warning(f"plan_approval_response for unknown conversation: {conversation_id_raw}")
        return
    conversation_id: str = conversation_id_raw

    if decision == "approve":
        post_plan_mode = _resolve_post_plan_mode(mixin)
        if session.has_pending_plan:
            session._pending_post_plan_mode = post_plan_mode
            session.set_chat_mode(post_plan_mode)
            _clear_pending_plan_prompt(session)
            await session.sync_sdk_permission_mode()
            session.provide_plan_decision("approve")
            logger.info(
                "Plan approved (ExitPlanMode unblocked) for conversation %s -> %s",
                conversation_id[:8],
                post_plan_mode,
            )
        else:
            session._pending_post_plan_mode = post_plan_mode
            session.set_chat_mode(post_plan_mode)
            session.approve_plan()
            _clear_pending_plan_prompt(session)
            await session.sync_sdk_permission_mode()
            await _send_mode_changed(
                websocket,
                conversation_id=conversation_id,
                mode=post_plan_mode,
                reason="plan_approved",
            )
            logger.info(
                "Plan approved (legacy) for conversation %s -> %s",
                conversation_id[:8],
                post_plan_mode,
            )
    elif decision == "request_changes":
        feedback = data.get("feedback", "")
        if feedback:
            session.set_plan_feedback(feedback)
        _clear_pending_plan_prompt(session)
        if session.has_pending_plan:
            # ExitPlanMode is blocking — deny it so agent stays in plan mode
            session.provide_plan_decision("request_changes")
            logger.info(
                f"Plan changes requested (ExitPlanMode denied) for conversation {conversation_id[:8]}",
            )
        else:
            await _send_mode_changed(
                websocket,
                conversation_id=conversation_id,
                mode="plan",
                reason="plan_changes_requested",
            )
            logger.info(f"Plan changes requested (legacy) for conversation {conversation_id[:8]}")


async def handle_recovered_plan_approval(
    mixin: SessionControlMixin, websocket: Any, conversation_id: str, data: dict[str, Any]
) -> None:
    """Handle plan approval for a session orphaned by daemon restart.

    The SDK conversation is dead. We update DB state and notify the frontend
    so it can start a new conversation with the correct mode.
    """
    decision = data.get("decision", "")
    session_manager = getattr(mixin, "session_manager", None)
    if not session_manager:
        logger.warning("Recovered plan approval: no session_manager available")
        return

    # Primary path: conversation_id is the canonical DB session ID for web chat.
    db_session = None
    try:
        db_session = await run_db(mixin, session_manager.get, conversation_id)
    except Exception as e:
        logger.debug(f"Failed to load recovered web-chat session {conversation_id}: {e}")

    # Compatibility fallback for older clients that still send external_id.
    if not db_session:
        for source in ("claude", "gemini", "qwen", "codex", "droid"):
            try:
                db_session = await run_db(
                    mixin, session_manager.find_active_by_external_id, conversation_id, source
                )
                if db_session:
                    break
            except Exception as e:
                logger.debug(f"Failed to find session for source={source}: {e}", exc_info=True)

    if not db_session:
        logger.warning(
            f"Recovered plan approval: no DB session for {conversation_id[:8]}",
        )
        return

    if decision == "approve":
        post_plan_mode = _resolve_post_plan_mode(mixin)
        try:
            await run_db(mixin, session_manager.update_chat_mode, db_session.id, post_plan_mode)
        except Exception:
            logger.debug(
                "Failed to persist recovered post-plan mode for %s",
                db_session.id,
                exc_info=True,
            )
        await _send_mode_changed(
            websocket,
            conversation_id=conversation_id,
            mode=post_plan_mode,
            reason="plan_approved",
        )
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "plan_approved_recovered",
                        "conversation_id": conversation_id,
                    }
                )
            )
        except (ConnectionClosed, ConnectionClosedError):
            pass
        logger.info(
            "Recovered plan approved for conversation %s (db=%s) -> %s",
            conversation_id[:8],
            db_session.id[:8],
            post_plan_mode,
        )

    elif decision == "request_changes":
        await _send_mode_changed(
            websocket,
            conversation_id=conversation_id,
            mode="plan",
            reason="plan_changes_requested",
        )
        logger.info(f"Recovered plan changes requested for conversation {conversation_id[:8]}")

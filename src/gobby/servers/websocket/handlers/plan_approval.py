"""Plan approval handlers for WebSocket session control.

Handles plan_approval_response, recovered plan approval after daemon restart,
and re-broadcasting pending plans to reconnecting clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


async def handle_plan_approval_response(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle plan_approval_response message from the web UI.

    Processes the user's decision on a proposed plan:
    - "approve": Unlock write tools and transition to accept_edits mode
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

    # Helper to clear pending_plan_path in DB after approval/rejection
    async def _clear_pending_plan() -> None:
        sm = getattr(mixin, "session_manager", None)
        if sm and session.db_session_id:
            try:
                await asyncio.to_thread(sm.update_pending_plan, session.db_session_id, None)
            except Exception:
                logger.debug("Failed to clear pending_plan_path", exc_info=True)

    if decision == "approve":
        if session.has_pending_plan:
            # ExitPlanMode is blocking — unblock it with the approval
            session.provide_plan_decision("approve")
            await _clear_pending_plan()
            logger.info(
                f"Plan approved (ExitPlanMode unblocked) for conversation {conversation_id[:8]}",
            )
        else:
            # Legacy path: plan approval before ExitPlanMode was called
            session.approve_plan()
            session.set_chat_mode("accept_edits")
            await session.sync_sdk_permission_mode()
            await _clear_pending_plan()
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "mode_changed",
                            "conversation_id": conversation_id,
                            "mode": "accept_edits",
                            "reason": "plan_approved",
                        }
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass
            logger.info(
                f"Plan approved (legacy) for conversation {conversation_id[:8]}, switched to accept_edits",
            )
    elif decision == "request_changes":
        feedback = data.get("feedback", "")
        if feedback:
            session.set_plan_feedback(feedback)
        if session.has_pending_plan:
            # ExitPlanMode is blocking — deny it so agent stays in plan mode
            session.provide_plan_decision("request_changes")
            await _clear_pending_plan()
            logger.info(
                f"Plan changes requested (ExitPlanMode denied) for conversation {conversation_id[:8]}",
            )
        else:
            await _clear_pending_plan()
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "mode_changed",
                            "conversation_id": conversation_id,
                            "mode": "plan",
                            "reason": "plan_changes_requested",
                        }
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass
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

    # Look up DB session by external_id (= conversation_id for web-chat)
    db_session = None
    for source in ("claude", "gemini", "codex"):
        try:
            db_session = await asyncio.to_thread(
                session_manager.find_active_by_external_id, conversation_id, source
            )
            if db_session:
                break
        except Exception as e:
            logger.debug(f"Failed to find session for source={source}: {e}", exc_info=True)

    if not db_session or not db_session.pending_plan_path:
        logger.warning(
            f"Recovered plan approval: no DB session with pending plan for {conversation_id[:8]}",
        )
        return

    plan_path = db_session.pending_plan_path

    if decision == "approve":
        await asyncio.to_thread(session_manager.update_pending_plan, db_session.id, None)
        await asyncio.to_thread(session_manager.update_chat_mode, db_session.id, "accept_edits")
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "mode_changed",
                        "conversation_id": conversation_id,
                        "mode": "accept_edits",
                        "reason": "plan_approved",
                    }
                )
            )
            await websocket.send(
                json.dumps(
                    {
                        "type": "plan_approved_recovered",
                        "conversation_id": conversation_id,
                        "plan_path": plan_path,
                    }
                )
            )
        except (ConnectionClosed, ConnectionClosedError):
            pass
        logger.info(
            f"Recovered plan approved for conversation {conversation_id[:8]} (db={db_session.id[:8]})",
        )

    elif decision == "request_changes":
        await asyncio.to_thread(session_manager.update_pending_plan, db_session.id, None)
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "mode_changed",
                        "conversation_id": conversation_id,
                        "mode": "plan",
                        "reason": "plan_changes_requested",
                    }
                )
            )
        except (ConnectionClosed, ConnectionClosedError):
            pass
        logger.info(f"Recovered plan changes requested for conversation {conversation_id[:8]}")


async def rebroadcast_pending_plans(mixin: SessionControlMixin, websocket: Any) -> None:
    """Re-broadcast plan_pending_approval for sessions orphaned by daemon restart.

    After restart, _chat_sessions is empty but DB sessions may have
    pending_plan_path set. For each, read the plan file from disk and
    send plan_pending_approval to the reconnecting client.
    """
    session_manager = getattr(mixin, "session_manager", None)
    if not session_manager:
        return

    try:
        pending = await asyncio.to_thread(session_manager.find_pending_plans)
    except Exception as e:
        logger.warning(f"Failed to query pending plans: {e}")
        return

    for db_session in pending:
        plan_path = db_session.pending_plan_path
        if not plan_path:
            continue
        try:
            content = await asyncio.to_thread(Path(plan_path).read_text, "utf-8")
        except Exception:
            logger.warning(f"Pending plan file missing: {plan_path}, clearing")
            try:
                await asyncio.to_thread(session_manager.update_pending_plan, db_session.id, None)
            except Exception:
                pass
            continue

        msg = json.dumps(
            {
                "type": "plan_pending_approval",
                "conversation_id": db_session.external_id,
                "plan_content": content,
                "recovered": True,
            }
        )
        try:
            await websocket.send(msg)
        except (ConnectionClosed, ConnectionClosedError):
            continue

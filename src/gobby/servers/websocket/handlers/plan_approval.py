"""Plan approval handlers for WebSocket session control.

Handles plan_approval_response and recovered plan approval after daemon restart.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.adapters.plan_options import get_plan_accept_option
from gobby.servers.websocket.db import run_db

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


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


async def _inject_turn(
    mixin: SessionControlMixin, websocket: Any, conversation_id: str, content: str
) -> bool:
    """Inject a synthetic user turn through the normal chat-message path.

    Managed CLIs (Codex, Droid, Gemini, Grok, Qwen) present a plan as a
    completed assistant turn -- there is no in-flight ExitPlanMode tool to
    unblock -- so auto-continue-after-approval drives the agent by posting a new
    user turn.
    """
    handler = getattr(mixin, "_handle_chat_message", None)
    if handler is None:
        logger.warning(
            "Cannot inject continuation turn for %s: no chat-message ingress",
            conversation_id[:8],
        )
        await _send_injection_failed(mixin, websocket, conversation_id, "no chat-message ingress")
        return False
    try:
        await handler(
            websocket,
            {
                "type": "chat_message",
                "conversation_id": conversation_id,
                "content": content,
            },
        )
        return True
    except Exception:
        logger.exception("Failed to inject continuation turn for %s", conversation_id[:8])
        await _send_injection_failed(
            mixin, websocket, conversation_id, "chat-message injection failed"
        )
        return False


async def _send_injection_failed(
    mixin: SessionControlMixin, websocket: Any, conversation_id: str, reason: str
) -> None:
    sender = getattr(mixin, "_send_error", None)
    message = f"Plan continuation failed for {conversation_id[:8]}: {reason}"
    try:
        if callable(sender):
            await sender(websocket, message, code="PLAN_CONTINUATION_FAILED")
            return
        await websocket.send(
            json.dumps(
                {
                    "type": "error",
                    "code": "PLAN_CONTINUATION_FAILED",
                    "message": message,
                    "conversation_id": conversation_id,
                }
            )
        )
    except (ConnectionClosed, ConnectionClosedError):
        pass


async def _auto_continue_after_approval(
    mixin: SessionControlMixin,
    websocket: Any,
    conversation_id: str,
) -> bool:
    """Start a continuation turn so an approved plan actually executes.

    The agent proceeds in the now-active execution mode (chat_mode was already
    flipped off plan). Native Claude auto-switches via the SDK and unblocks
    ExitPlanMode in-flight, so it is excluded by the caller and never reaches
    here.
    """
    content = "The plan is approved. Proceed with the implementation."
    return await _inject_turn(mixin, websocket, conversation_id, content)


async def handle_plan_approval_response(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle plan_approval_response message from the web UI.

    Processes the user's decision on a proposed plan:
    - "approve": Exit planning into the chosen execution mode (YOLO -> bypass,
      Act -> normal), carried by the resolved accept option.
    - "request_changes": Reject with an optional comment; stay in plan mode and
      store any feedback for the next prompt injection.

    Message format:
    {
        "type": "plan_approval_response",
        "conversation_id": "stable-id",
        "decision": "approve" | "request_changes",
        "option_id": "approve_yolo" | "approve_act" (approve only),
        "feedback": "optional feedback text"
    }

    When ``option_id`` resolves against the registry, the option's post-plan
    chat mode and auto-continue flag drive the response. A missing or unknown
    ``option_id`` falls back to the generic-approve default (``normal`` mode,
    auto-continue), so older clients stay compatible.
    """
    conversation_id_raw: str | None = data.get("conversation_id")
    decision = data.get("decision", "")
    option_id = data.get("option_id")

    session = mixin._chat_sessions.get(conversation_id_raw) if conversation_id_raw else None

    # Recovery path: no in-memory session (daemon restarted)
    if session is None and conversation_id_raw:
        await handle_recovered_plan_approval(mixin, websocket, conversation_id_raw, data)
        return

    if session is None or conversation_id_raw is None:
        logger.warning(f"plan_approval_response for unknown conversation: {conversation_id_raw}")
        return
    conversation_id: str = conversation_id_raw

    source = getattr(session, "provider", None)
    option = (
        get_plan_accept_option(source, option_id) if option_id and isinstance(source, str) else None
    )

    approving = decision == "approve" or (option is not None and option.decision == "approve")

    if approving:
        post_plan_mode = option.post_plan_chat_mode if option else "normal"
        should_auto_continue = option.auto_continue if option else True
        if session.has_pending_plan:
            # Capture before releasing: a tool-plan CLI (Droid ExitSpecMode)
            # parks its plan-exit tool on a blocking gate that resumes the turn
            # natively once released, so it must not also be auto-continued.
            blocking_plan = getattr(session, "has_blocking_plan_decision", False)
            session._pending_post_plan_mode = post_plan_mode
            session.set_chat_mode(post_plan_mode)
            _clear_pending_plan_prompt(session)
            await session.sync_sdk_permission_mode()
            session.provide_plan_decision("approve")
            logger.info(
                "Plan approved (ExitPlanMode unblocked, option=%s) for conversation %s -> %s",
                option.id if option else "-",
                conversation_id[:8],
                post_plan_mode,
            )
            # Managed text-plan CLIs have no in-flight plan-exit tool to unblock;
            # the plan was a completed assistant turn, so inject a continuation.
            # Native Claude (plan_auto_switch) and tool-plan CLIs that blocked on
            # the plan-decision gate (blocking_plan) resume the paused turn
            # themselves, so skip injection for them.
            if (
                not getattr(session, "plan_auto_switch", False)
                and should_auto_continue
                and not blocking_plan
            ):
                if not await _auto_continue_after_approval(mixin, websocket, conversation_id):
                    logger.warning(
                        "Plan approval continuation injection failed for conversation %s",
                        conversation_id[:8],
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
                "Plan approved (legacy, option=%s) for conversation %s -> %s",
                option.id if option else "-",
                conversation_id[:8],
                post_plan_mode,
            )
    elif decision == "request_changes":
        feedback = data.get("feedback", "")
        if feedback:
            session.set_plan_feedback(feedback)
        # Capture the blocking-gate state before _clear_pending_plan_prompt zeros
        # the content-based has_pending_plan used by managed (tool-plan) CLIs.
        blocking_plan = getattr(session, "has_blocking_plan_decision", False)
        _clear_pending_plan_prompt(session)
        if session.has_pending_plan or blocking_plan:
            # A blocking plan-exit tool is parked (native ExitPlanMode, whose
            # event-based has_pending_plan survives the clear, or a managed
            # tool-plan CLI's gate, e.g. Droid ExitSpecMode). Deny it so the
            # agent stays in plan mode; queued feedback rides the next turn.
            session.provide_plan_decision("request_changes")
            logger.info(
                f"Plan changes requested (plan-exit tool denied) for conversation {conversation_id[:8]}",
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
        for source in ("claude", "gemini", "grok", "qwen", "codex", "droid"):
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
        # The SDK session is gone, so resolve the post-plan mode from the
        # chosen accept option (YOLO -> bypass, Act -> normal); fall back to
        # "normal" for older clients that omit option_id.
        recovered_option = get_plan_accept_option(
            getattr(db_session, "source", "") or "", data.get("option_id")
        )
        post_plan_mode = recovered_option.post_plan_chat_mode if recovered_option else "normal"
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

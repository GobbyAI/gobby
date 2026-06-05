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
from gobby.storage.config_store import ConfigStore

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)

# Directives seeded for keep_planning options that arrive without typed
# feedback, so the next planning turn has concrete guidance.
_ULTRAPLAN_DIRECTIVE = (
    "Refine the plan with deeper analysis (Ultraplan): widen the investigation, "
    "stress-test assumptions and edge cases, and present a more thorough revised plan."
)
_KEEP_PLANNING_DIRECTIVE = "Keep planning: continue refining the plan before any implementation."


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


async def _inject_turn(
    mixin: SessionControlMixin, websocket: Any, conversation_id: str, content: str
) -> None:
    """Inject a synthetic user turn through the normal chat-message path.

    Managed CLIs (Codex, Droid, Gemini, Grok, Qwen) present a plan as a
    completed assistant turn -- there is no in-flight ExitPlanMode tool to
    unblock -- so both auto-continue-after-approval and managed keep-planning
    re-plans drive the agent by posting a new user turn.
    """
    handler = getattr(mixin, "_handle_chat_message", None)
    if handler is None:
        logger.warning(
            "Cannot inject continuation turn for %s: no chat-message ingress",
            conversation_id[:8],
        )
        return
    try:
        await handler(
            websocket,
            {
                "type": "chat_message",
                "conversation_id": conversation_id,
                "content": content,
            },
        )
    except Exception:
        logger.exception("Failed to inject continuation turn for %s", conversation_id[:8])


async def _auto_continue_after_approval(
    mixin: SessionControlMixin,
    websocket: Any,
    conversation_id: str,
    *,
    plan_seed: str | None = None,
) -> None:
    """Start a continuation turn so an approved plan actually executes.

    The agent proceeds in the now-active execution mode (chat_mode was already
    flipped off plan). Native Claude auto-switches via the SDK and unblocks
    ExitPlanMode in-flight, so it is excluded by the caller and never reaches
    here.

    When ``plan_seed`` is supplied (Codex "approve + clear context"), the
    conversation was reset, so the approved plan is re-seeded into the
    continuation turn -- otherwise the fresh context would have lost it.
    """
    if plan_seed:
        content = (
            "The plan below is approved. Proceed with the implementation.\n\n"
            f"<approved-plan>\n{plan_seed}\n</approved-plan>"
        )
    else:
        content = "The plan is approved. Proceed with the implementation."
    await _inject_turn(mixin, websocket, conversation_id, content)


async def _maybe_clear_context(session: Any, conversation_id: str) -> bool:
    """Reset the session's conversation context when an option requests it.

    Only Codex carries a clear-context option, and its web-chat session
    implements ``clear_context`` as a real thread rotation (archive + fresh
    thread). For any session without the capability this logs and no-ops rather
    than silently swallowing the request.
    """
    clear = getattr(session, "clear_context", None)
    if clear is None or not callable(clear):
        logger.warning(
            "Plan option requested context clear but session %s has no clear_context",
            conversation_id[:8],
        )
        return False
    try:
        return bool(await clear())
    except Exception:
        logger.exception("Failed to clear context on plan approval for %s", conversation_id[:8])
        return False


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
        "option_id": "optional per-CLI plan-accept option id",
        "feedback": "optional feedback text"
    }

    When ``option_id`` resolves against the per-CLI registry, the option's
    action primitives (post-plan mode, auto-continue, clear-context, keep-
    planning) drive the response instead of the generic post-plan default. A
    missing or unknown ``option_id`` preserves the legacy generic-approve /
    request-changes behavior, so older clients stay compatible.
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

    # A keep_planning option (e.g. Claude's Ultraplan, ACP/Codex keep-planning)
    # holds the plan unapproved and re-enters planning with a directive.
    if option is not None and option.decision == "keep_planning":
        directive = _ULTRAPLAN_DIRECTIVE if option.escalate else _KEEP_PLANNING_DIRECTIVE
        _clear_pending_plan_prompt(session)
        if session.has_pending_plan:
            # Native Claude ExitPlanMode is blocking — deny it and inject the
            # directive as feedback so the resumed turn re-plans deeper.
            session.set_plan_feedback(directive)
            session.provide_plan_decision("request_changes")
            logger.info(
                "Plan keep-planning (%s) for conversation %s",
                option.id,
                conversation_id[:8],
            )
        else:
            # Managed CLIs have no in-flight plan tool; stay in plan mode and
            # post the directive as a new turn to drive a deeper re-plan.
            await _send_mode_changed(
                websocket,
                conversation_id=conversation_id,
                mode="plan",
                reason="plan_changes_requested",
            )
            await _inject_turn(mixin, websocket, conversation_id, directive)
            logger.info(
                "Plan keep-planning (%s, managed re-plan) for conversation %s",
                option.id,
                conversation_id[:8],
            )
        return

    approving = decision == "approve" or (option is not None and option.decision == "approve")

    if approving:
        post_plan_mode = option.post_plan_chat_mode if option else _resolve_post_plan_mode(mixin)
        should_auto_continue = option.auto_continue if option else True
        wants_clear = bool(option and option.clear_context)
        # Capture the approved plan before set_chat_mode/clear wipe pending
        # state, so a cleared context can be re-seeded with it.
        plan_seed = getattr(session, "_last_plan_content", None) if wants_clear else None
        if session.has_pending_plan:
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
            if wants_clear:
                await _maybe_clear_context(session, conversation_id)
            # Managed CLIs have no in-flight ExitPlanMode to unblock; the plan
            # was a completed assistant turn. Native Claude (plan_auto_switch)
            # continues the paused turn itself, so only auto-continue managed.
            if not getattr(session, "plan_auto_switch", False) and should_auto_continue:
                await _auto_continue_after_approval(
                    mixin, websocket, conversation_id, plan_seed=plan_seed
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

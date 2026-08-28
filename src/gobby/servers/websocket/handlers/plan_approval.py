"""Plan approval handlers for WebSocket session control.

Handles plan_approval_response and recovered plan approval after daemon restart.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.adapters.plan_keystrokes import (
    DEFAULT_PLAN_KEYSTROKES,
    dispatch_plan_keystrokes,
    resolve_action_option_id,
)
from gobby.adapters.plan_options import get_plan_accept_option
from gobby.servers.websocket.db import run_db
from gobby.terminals.lookup import manager_for_terminal_context
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.adapters.plan_keystrokes import PlanKeystrokeRegistry
    from gobby.adapters.plan_options import PlanAcceptOption
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)

# A tool-plan CLI (Droid ExitSpecMode) leaves its plan turn in-flight, parked on
# the plan-decision gate. After approval that turn resumes and ends on its own;
# we wait up to this long for it to drain before injecting the continuation so
# the inject path does not cancel a still-streaming turn.
_PLAN_TURN_DRAIN_TIMEOUT_SECONDS = 120.0
_PLAN_TMUX_OPERATION_TIMEOUT_SECONDS = 10.0

# Lines of the attached CLI's tmux pane to capture for native plan-menu detection
# (Path B). The menu markers sit within the last few lines of the prompt; this is
# generous headroom around them.
_PLAN_MENU_CAPTURE_LINES = 60


async def _send_mode_changed(
    websocket: Any,
    *,
    conversation_id: str,
    mode: str,
    reason: str,
) -> None:
    try:
        await websocket.send(
            json_dumps(
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

    Managed CLIs (Codex, Droid, Grok, Qwen) present a plan as a
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
            json_dumps(
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
    option: PlanAcceptOption | None,
) -> bool:
    """Start a continuation turn so an approved plan actually executes.

    The agent proceeds in the now-active execution mode (chat_mode was already
    flipped off plan). Native Claude auto-switches via the SDK and unblocks
    ExitPlanMode in-flight, so it is excluded by the caller and never reaches
    here.
    """
    content = _approval_continuation_content(option)
    return await _inject_turn(mixin, websocket, conversation_id, content)


def _approval_continuation_content(option: PlanAcceptOption | None) -> str:
    if option is not None and option.post_plan_chat_mode == "bypass":
        return (
            "The plan is approved in YOLO mode. Proceed with the implementation "
            "without pausing for tool approvals."
        )
    return (
        "The plan is approved in Act mode. Proceed with the implementation and ask "
        "before non-exempt tool use."
    )


def _active_chat_task(mixin: SessionControlMixin, conversation_id: str) -> Any:
    """Return the in-flight chat task for a conversation, or None."""
    registry = getattr(mixin, "web_chat_session_registry", None)
    if registry is not None:
        getter = getattr(registry, "get_active_task", None)
        if callable(getter):
            try:
                return getter(conversation_id)
            except Exception:
                logger.debug(
                    "Failed to read active task for %s", conversation_id[:8], exc_info=True
                )
    tasks = getattr(mixin, "_active_chat_tasks", None)
    if isinstance(tasks, dict):
        return tasks.get(conversation_id)
    return None


async def _continue_after_active_turn(
    mixin: SessionControlMixin,
    websocket: Any,
    conversation_id: str,
    option: PlanAcceptOption | None,
) -> bool:
    """Inject the continuation only after the in-flight plan turn has drained.

    A tool-plan CLI (Droid ExitSpecMode) parks its plan turn on the
    plan-decision gate. Once approved, that turn resumes and ends on its own
    (Droid closes the spec stream). Injecting before it ends would route through
    ``_handle_chat_message`` -> ``_cancel_active_chat`` and abort the still-open
    stream (observed live as "Droid stream ended before result"). Wait for the
    turn's task to finish first, then inject; never cancel the in-flight turn.
    """
    task = _active_chat_task(mixin, conversation_id)
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_PLAN_TURN_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "In-flight plan turn for %s did not drain within %.0fs; continuing",
                conversation_id[:8],
                _PLAN_TURN_DRAIN_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "In-flight plan turn for %s ended with error before continuation",
                conversation_id[:8],
                exc_info=True,
            )
    return await _auto_continue_after_approval(mixin, websocket, conversation_id, option)


async def handle_attached_plan_approval(
    mixin: SessionControlMixin,
    websocket: Any,
    target_session_id: str,
    data: dict[str, Any],
    *,
    registry: PlanKeystrokeRegistry = DEFAULT_PLAN_KEYSTROKES,
) -> None:
    """Drive a native plan menu for an attached (proxy-terminal) CLI session.

    The caller is attached to a CLI running in a tmux pane (Path B): there is no
    in-memory ChatSession whose plan gate we can release. The plan choice is a
    native TUI menu, so approval/rejection is a keystroke sequence sent to the
    pane. The sequence is resolved from the per-CLI registry keyed by
    ``(session.source, option_id)``; ``option_id`` is a plan_options accept id for
    approve, or the request-changes sentinel for reject.

    Message format::

        {
            "type": "plan_approval_response",
            "target_session_id": "db-uuid",
            "decision": "approve" | "request_changes",
            "option_id": "approve_yolo" | "approve_act"  # approve only
        }
    """
    session_manager = getattr(mixin, "session_manager", None)
    if session_manager is None:
        await mixin._send_error(websocket, "Session manager not available")
        return

    try:
        session = await run_db(mixin, session_manager.get, target_session_id)
    except (LookupError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to look up target session %s: %s", target_session_id, exc)
        session = None
    if session is None:
        await mixin._send_error(
            websocket, f"Session not found: {target_session_id}", code="NOT_FOUND"
        )
        return
    if getattr(session, "session_type", None) != "terminal":
        await mixin._send_error(
            websocket,
            "plan_approval_response target_session_id only supports terminal sessions",
            code="UNSUPPORTED_SESSION_TYPE",
        )
        return

    ctx: dict[str, Any] = {}
    if isinstance(getattr(session, "terminal_context", None), dict):
        ctx = session.terminal_context
    tmux_pane = ctx.get("tmux_pane")
    if not tmux_pane and isinstance(getattr(session, "metadata", None), dict):
        tmux_pane = session.metadata.get("terminal_tmux_pane")
    if not isinstance(tmux_pane, str) or not tmux_pane:
        await mixin._send_error(
            websocket,
            f"Session {target_session_id} has no tmux pane for plan approval",
            code="NO_TERMINAL_TARGET",
        )
        return

    source = getattr(session, "source", None)
    decision = data.get("decision", "")
    raw_option_id = data.get("option_id")
    if decision not in {"approve", "request_changes"} or (
        decision == "approve" and not raw_option_id
    ):
        await mixin._send_error(
            websocket,
            f"Unrecognized plan decision/option for {source!r}: "
            f"decision={decision!r} option_id={raw_option_id!r}",
            code="INVALID_PLAN_DECISION",
        )
        return

    action_option_id = resolve_action_option_id(source, decision, raw_option_id)
    if action_option_id is None:
        await mixin._send_error(
            websocket,
            f"Unrecognized plan decision/option for {source!r}: "
            f"decision={decision!r} option_id={raw_option_id!r}",
            code="INVALID_PLAN_DECISION",
        )
        return

    tmux = manager_for_terminal_context(ctx)
    # Resolve against the live pane when the source either has multiple menu
    # shapes or a static-menu presence guard. A stale web-UI click must not send
    # blind digits into whatever the pane currently shows.
    pane_text = ""
    if registry.requires_pane(source):
        try:
            captured = await asyncio.wait_for(
                tmux.snapshot_lines(tmux_pane, lines=_PLAN_MENU_CAPTURE_LINES),
                timeout=_PLAN_TMUX_OPERATION_TIMEOUT_SECONDS,
            )
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Failed to capture pane %s for plan-menu detection: %s", tmux_pane, exc)
            captured = None
        pane_text = captured or ""

    sequence = registry.resolve_for_pane(source, action_option_id, pane_text)
    if sequence is None:
        await mixin._send_error(
            websocket,
            f"No native plan keystrokes registered for source={source!r} "
            f"option={action_option_id!r}",
            code="PLAN_KEYSTROKES_UNMAPPED",
        )
        return

    try:
        dispatched = await asyncio.wait_for(
            dispatch_plan_keystrokes(tmux, tmux_pane, sequence),
            timeout=_PLAN_TMUX_OPERATION_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "tmux plan keystroke dispatch failed for pane %s: %s",
            tmux_pane,
            exc,
            exc_info=True,
        )
        dispatched = False
    if not dispatched:
        await mixin._send_error(
            websocket, "Failed to send plan approval keystrokes to attached session"
        )
        return

    await websocket.send(
        json_dumps(
            {
                "type": "plan_approval_dispatched",
                "target_session_id": target_session_id,
                "decision": decision,
                "option_id": action_option_id,
                "ok": True,
            }
        )
    )
    logger.info(
        "Plan %s dispatched to attached session %s (source=%s, option=%s)",
        decision,
        target_session_id[:8],
        source,
        action_option_id,
    )


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
    # Path B: an attached proxy-terminal session (CLI in a tmux pane) has no
    # in-memory ChatSession; its plan choice is a native TUI menu driven by
    # keystrokes. Mirror the set_mode/set_agent target_session_id convention.
    raw_target_session_id = data.get("target_session_id")
    if raw_target_session_id is not None and not isinstance(raw_target_session_id, str):
        await mixin._send_error(
            websocket,
            "plan_approval_response target_session_id must be a string",
            code="INVALID_TARGET_SESSION_ID",
        )
        return
    target_session_id = raw_target_session_id
    if target_session_id:
        await handle_attached_plan_approval(mixin, websocket, target_session_id, data)
        return

    conversation_id_raw: str | None = data.get("conversation_id")
    tool_call_id = data.get("tool_call_id")
    decision = data.get("decision", "")
    option_id = data.get("option_id")

    session = mixin._chat_sessions.get(conversation_id_raw) if conversation_id_raw else None

    # Recovery path: no in-memory session (daemon restarted)
    if session is None and conversation_id_raw:
        await handle_recovered_plan_approval(mixin, websocket, conversation_id_raw, data)
        return

    if session is None or conversation_id_raw is None:
        logger.warning("plan_approval_response for unknown conversation: %s", conversation_id_raw)
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
            plan_auto_switch = bool(getattr(session, "plan_auto_switch", False))
            if plan_auto_switch and (
                not isinstance(tool_call_id, str) or not session.has_pending_plan_id(tool_call_id)
            ):
                logger.warning("Plan approval does not match a pending tool_call_id")
                return
            session._pending_post_plan_mode = post_plan_mode
            session.set_chat_mode(post_plan_mode)
            _clear_pending_plan_prompt(session)
            await session.sync_sdk_permission_mode()
            decision_target = tool_call_id if plan_auto_switch else None
            if plan_auto_switch and not session.provide_plan_decision(decision_target, "approve"):
                logger.warning("Plan approval did not match a pending plan gate: %s", tool_call_id)
                return
            if not plan_auto_switch:
                session.provide_plan_decision(None, "approve")
            logger.info(
                "Plan approved (ExitPlanMode unblocked, option=%s) for conversation %s -> %s",
                option.id if option else "-",
                conversation_id[:8],
                post_plan_mode,
            )
            # Drive execution after approval unless the CLI resumes it natively.
            # Native Claude (plan_auto_switch) keeps the ExitPlanMode turn alive
            # through the SDK and continues into execution on its own, so it is the
            # only case we skip. Every other managed CLI needs the injected
            # continuation: text-plan CLIs presented the plan as a completed
            # assistant turn, and tool-plan CLIs like Droid END their ExitSpecMode
            # turn once the plan-decision gate releases -- verified live, the turn
            # does not auto-execute -- so without the nudge the approved plan would
            # just sit idle.
            if not getattr(session, "plan_auto_switch", False) and should_auto_continue:
                # A blocking-gate CLI (Droid ExitSpecMode) still has its plan turn
                # in-flight; drain it before injecting so the inject path does not
                # cancel the open stream. Text-plan CLIs have no in-flight turn,
                # so inject immediately.
                blocking_plan = getattr(session, "has_blocking_plan_decision", False)
                if blocking_plan:
                    continued = await _continue_after_active_turn(
                        mixin, websocket, conversation_id, option
                    )
                else:
                    continued = await _auto_continue_after_approval(
                        mixin, websocket, conversation_id, option
                    )
                if not continued:
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
                "Plan approved (option=%s) for conversation %s -> %s",
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
            plan_auto_switch = bool(getattr(session, "plan_auto_switch", False))
            if plan_auto_switch and (
                not isinstance(tool_call_id, str) or not session.has_pending_plan_id(tool_call_id)
            ):
                logger.warning("Plan rejection does not match a pending tool_call_id")
                return
            decision_target = tool_call_id if plan_auto_switch else None
            if plan_auto_switch and not session.provide_plan_decision(
                decision_target, "request_changes"
            ):
                logger.warning("Plan rejection did not match a pending plan gate: %s", tool_call_id)
                return
            if not plan_auto_switch:
                session.provide_plan_decision(None, "request_changes")
            logger.info(
                "Plan changes requested (plan-exit tool denied) for conversation %s",
                conversation_id[:8],
            )
        else:
            await _send_mode_changed(
                websocket,
                conversation_id=conversation_id,
                mode="plan",
                reason="plan_changes_requested",
            )
            logger.info("Plan changes requested for conversation %s", conversation_id[:8])


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
        logger.debug("Failed to load recovered web-chat session %s: %s", conversation_id, e)

    if not db_session:
        logger.warning("Recovered plan approval: no DB session for %s", conversation_id[:8])
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
                json_dumps(
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
        logger.info("Recovered plan changes requested for conversation %s", conversation_id[:8])

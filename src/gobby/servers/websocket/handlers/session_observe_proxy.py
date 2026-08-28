"""Attach, detach, and proxy-send handlers for observed CLI sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.servers.websocket.attachments import append_attachment_paths, store_proxy_attachments
from gobby.servers.websocket.chat_attachments import (
    append_prepared_attachment_context,
    partition_attachment_items,
    prepare_message_attachments,
)
from gobby.servers.websocket.db import run_db
from gobby.servers.websocket.handlers.session_observe_support import (
    _as_str,
    _is_terminal_session,
    _load_live_session_variables,
    _session_meta_payload,
)
from gobby.sessions.context_usage import (
    effective_context_window_for_session,
    resolve_context_window_overrides,
)
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


def _observe_facade() -> Any:
    from gobby.servers.websocket.handlers import session_observe

    return session_observe


def _tmux_context_and_pane(session: Any) -> tuple[dict[str, Any], Any | None]:
    context = (
        session.terminal_context
        if hasattr(session, "terminal_context") and isinstance(session.terminal_context, dict)
        else {}
    )
    pane = context.get("tmux_pane")
    if pane:
        return context, pane

    metadata = (
        session.metadata
        if hasattr(session, "metadata") and isinstance(session.metadata, dict)
        else {}
    )
    return context, metadata.get("terminal_tmux_pane")


async def _resolve_agent_name_for_session(
    mixin: SessionControlMixin,
    session_id: str,
    workflow_name: str | None,
    agent_run_id: str | None,
) -> str | None:
    """Resolve the UI-facing agent name for an observed session."""
    if not agent_run_id:
        return None

    session_manager = getattr(mixin, "session_manager", None)
    if not session_manager:
        return workflow_name

    try:
        from gobby.storage.agents import LocalAgentRunManager

        run = await run_db(mixin, LocalAgentRunManager(session_manager.db).get, agent_run_id)
        if run and run.agent_name:
            return cast(str, run.agent_name)
    except Exception as exc:
        logger.debug("Failed to resolve agent name for session %s: %s", session_id, exc)

    return workflow_name


async def _web_origin_session_id(
    mixin: SessionControlMixin,
    session_manager: Any,
    target_session: Any,
    web_session_id: str,
) -> str:
    if session_manager is not None and web_session_id and web_session_id != "web-ui":
        attached_session = await run_db(mixin, session_manager.get, web_session_id)
        attached_id = getattr(attached_session, "id", None)
        if isinstance(attached_id, str) and attached_id:
            return attached_id

    target_id = getattr(target_session, "id", None)
    if isinstance(target_id, str) and target_id:
        return target_id
    if web_session_id and web_session_id != "web-ui":
        return web_session_id
    raise RuntimeError("target session has no id")


async def handle_attach_to_session(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Attach a WebSocket client to observe a CLI session in real-time.

    Loads recent messages from the session, auto-subscribes the client
    to session-scoped events, and returns the initial message batch.

    Message format:
    {
        "type": "attach_to_session",
        "session_id": "db-uuid-of-session"
    }
    """
    session_id = data.get("session_id")
    if not session_id:
        await mixin._send_error(websocket, "attach_to_session requires session_id")
        return

    session_manager = getattr(mixin, "session_manager", None)
    if not session_manager:
        await mixin._send_error(websocket, "Session manager not available")
        return

    # Look up session
    try:
        session = await run_db(mixin, session_manager.get, session_id)
    except Exception as e:
        logger.warning("Failed to look up session %s: %s", session_id, e)
        session = None

    if not session:
        await mixin._send_error(websocket, f"Session not found: {session_id}", code="NOT_FOUND")
        return
    if not _is_terminal_session(session):
        await mixin._send_error(
            websocket,
            "attach_to_session only supports terminal sessions",
            code="UNSUPPORTED_SESSION_TYPE",
        )
        return

    # Authorization happens at the WebSocket handshake: only authenticated
    # connections are registered in `clients` with a user_id (bearer token via
    # the /ws proxy route, or local-first accept). The connection's project
    # scope is client-declared UI state that fresh connections — phone
    # browsers, post-suspend reconnects — have not sent yet, so it must not
    # gate observation (gobby-#20062).
    client_metadata = mixin.clients.get(websocket)
    if not isinstance(client_metadata, dict) or _as_str(client_metadata.get("user_id")) is None:
        await mixin._send_error(
            websocket,
            "Not authorized to observe session",
            code="FORBIDDEN",
        )
        return

    workflow_name = _as_str(getattr(session, "workflow_name", None))
    agent_run_id = _as_str(getattr(session, "agent_run_id", None))
    agent_name = await _observe_facade()._resolve_agent_name_for_session(
        mixin,
        session_id,
        workflow_name,
        agent_run_id,
    )
    live_variables = await _load_live_session_variables(mixin, session_manager, session_id)
    db = getattr(session_manager, "db", None) or getattr(mixin, "db", None)
    context_window_overrides = resolve_context_window_overrides(
        getattr(mixin, "daemon_config", None)
    )
    context_window = await run_db(
        mixin,
        effective_context_window_for_session,
        session,
        variables=live_variables,
        db=db,
        overrides=context_window_overrides,
    )

    # Message loading via message_manager removed (session_messages table dropped)
    messages: list[dict[str, Any]] = []
    total_count = 0

    # Auto-subscribe to session-scoped events
    if not hasattr(websocket, "subscriptions") or websocket.subscriptions is None:
        websocket.subscriptions = set()
    websocket.subscriptions.add(f"session_message:session_id={session_id}")
    websocket.subscriptions.add(f"hook_event:session_id={session.external_id}")

    # Track attached session on websocket metadata
    client_metadata["attached_session_id"] = session_id

    # Send response with initial messages and session metadata
    session_meta = _session_meta_payload(
        session,
        variables=live_variables,
        agent_name=agent_name,
        workflow_name=workflow_name,
        agent_run_id=agent_run_id,
        context_window=context_window,
    )
    await websocket.send(
        json_dumps(
            {
                "type": "attach_to_session_result",
                "session_id": session_id,
                **session_meta,
                "messages": messages,
                "total_count": total_count,
            }
        )
    )
    logger.info(
        "Client attached to session %s (%s): %s messages loaded",
        session_id,
        session_meta["ref"],
        total_count,
    )


async def handle_send_to_cli_session(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Send a message from the web UI to a CLI session.

    Uses two delivery paths:
    - Idle (at prompt): tmux send-keys injects text directly
    - Mid-execution: message persists in DB; hook piggyback picks it up

    Message format:
    {
        "type": "send_to_cli_session",
        "session_id": "db-uuid-of-target-session",
        "content": "message text",
        "attachments": [{"id": "stored-attachment-id"}],
        "client_message_id": "optional-client-id"
    }
    """
    session_id = _as_str(data.get("session_id"))
    content_value = data.get("content")
    if content_value is not None and not isinstance(content_value, str):
        await mixin._send_error(websocket, "send_to_cli_session content must be a string")
        return
    content = (content_value or "").strip()
    attachments = data.get("attachments")
    attachment_items = attachments if isinstance(attachments, list) else []
    client_message_id = _as_str(data.get("client_message_id"))
    if not session_id or (not content and not attachment_items):
        await mixin._send_error(
            websocket,
            "send_to_cli_session requires session_id and content or attachments",
        )
        return

    session_manager = getattr(mixin, "session_manager", None)
    if not session_manager:
        await mixin._send_error(websocket, "Session manager not available")
        return

    # Look up the target session
    try:
        session = await run_db(mixin, session_manager.get, session_id)
    except Exception as e:
        logger.warning("Failed to look up session %s: %s", session_id, e)
        session = None

    if not session:
        await mixin._send_error(websocket, f"Session not found: {session_id}", code="NOT_FOUND")
        return
    if not _is_terminal_session(session):
        await mixin._send_error(
            websocket,
            "send_to_cli_session only supports terminal sessions",
            code="UNSUPPORTED_SESSION_TYPE",
        )
        return

    if attachment_items:
        try:
            attachment_partitions = partition_attachment_items(attachment_items)
            prepared = await prepare_message_attachments(
                mixin,
                attachment_partitions,
                target_session_id=session_id,
            )
            content = append_prepared_attachment_context(content, prepared)
            stored_paths = await store_proxy_attachments(
                session_id,
                attachment_partitions.legacy_items,
            )
            content = append_attachment_paths(content, stored_paths)
        except ValueError as exc:
            await mixin._send_error(websocket, str(exc), code="INVALID_ATTACHMENT")
            return
        except Exception:
            logger.warning(
                "Failed to process attachments for CLI session %s",
                session_id,
                exc_info=True,
            )
            await mixin._send_error(
                websocket,
                "Failed to process attachments",
                code="ATTACHMENT_ERROR",
            )
            return

    # Persist the message via InterSessionMessageManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    inter_msg_manager = vars(mixin).get("inter_session_msg_manager")
    if inter_msg_manager is None and hasattr(session_manager, "db"):
        try:
            inter_msg_manager = InterSessionMessageManager(session_manager.db)
            mixin.inter_session_msg_manager = inter_msg_manager
        except Exception as e:
            logger.warning("Failed to create InterSessionMessageManager: %s", e)

    web_session_id = (mixin.clients.get(websocket) or {}).get("attached_session_id", "web-ui")
    try:
        from_session_id = await _web_origin_session_id(
            mixin,
            session_manager,
            session,
            str(web_session_id),
        )
    except Exception as e:
        logger.warning("Failed to resolve web-origin sender session: %s", e)
        await mixin._send_error(
            websocket,
            "Failed to resolve web-origin sender session",
            code="WEB_ORIGIN_SESSION_ERROR",
        )
        return

    msg_id: str | None = None
    if inter_msg_manager:
        try:
            msg = await run_db(
                mixin,
                inter_msg_manager.create_message,
                from_session=from_session_id,
                to_session=session_id,
                content=content,
                message_type="web_chat",
            )
            msg_id = msg.id
        except Exception as e:
            logger.warning("Failed to persist inter-session message: %s", e)

    # Try tmux delivery for idle sessions
    delivered_via_tmux = False
    ctx, tmux_pane = _tmux_context_and_pane(session)

    if tmux_pane:
        try:
            tmux_manager = _observe_facade().manager_for_terminal_context(ctx)
            ok = await tmux_manager.dispatch_keys(tmux_pane, content + "\n")
            if ok:
                delivered_via_tmux = True
                # Mark as delivered
                if inter_msg_manager and msg_id:
                    try:
                        await run_db(
                            mixin,
                            inter_msg_manager.mark_delivered,
                            msg_id,
                            session_id,
                        )
                    except Exception as e:
                        logger.warning("Failed to mark message %s as delivered: %s", msg_id, e)
        except Exception as e:
            logger.warning("tmux send_keys failed for %s: %s", tmux_pane, e)

    # Respond to the client
    await websocket.send(
        json_dumps(
            {
                "type": "send_to_cli_session_result",
                "session_id": session_id,
                "delivered": delivered_via_tmux,
                "delivery_method": "tmux" if delivered_via_tmux else "hook_piggyback",
                "message_id": msg_id,
                "client_message_id": client_message_id,
            }
        )
    )
    logger.info(
        "Message sent to CLI session %s: delivered=%s",
        session_id[:8],
        "tmux" if delivered_via_tmux else "queued for hook piggyback",
    )


async def handle_detach_from_session(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Detach a WebSocket client from an observed CLI session.

    Removes session-scoped subscriptions and clears attached state.

    Message format:
    {
        "type": "detach_from_session",
        "session_id": "db-uuid-of-session"
    }
    """
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        await mixin._send_error(websocket, "detach_from_session requires session_id")
        return

    subs: set[str] = getattr(websocket, "subscriptions", set())
    # Remove all parametric subscriptions for this session
    to_remove = {s for s in subs if session_id in s}
    subs -= to_remove

    # Clear attached session metadata
    metadata = mixin.clients.get(websocket)
    if metadata:
        metadata.pop("attached_session_id", None)

    await mixin._cleanup_attached_tts(session_id)

    await websocket.send(
        json_dumps(
            {
                "type": "detach_from_session_result",
                "session_id": session_id,
            }
        )
    )
    logger.info("Client detached from session %s", session_id)

"""Session observation handlers for WebSocket session control.

Handles continue_in_chat, attach_to_session, detach_from_session,
send_to_cli_session, and the resume-blocked check.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.sessions.terminal_kill import kill_terminal_session
from gobby.sessions.transcript_archive import restore_transcript

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


def _is_terminal_session(session: Any) -> bool:
    """Return True when a session supports live attach/proxy semantics."""
    return getattr(session, "session_type", None) == "terminal"


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

        run = await asyncio.to_thread(LocalAgentRunManager(session_manager.db).get, agent_run_id)
        if run and run.agent_name:
            return run.agent_name
    except Exception as exc:
        logger.debug("Failed to resolve agent name for session %s: %s", session_id, exc)

    return workflow_name


async def handle_continue_in_chat(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle continue_in_chat message to resume a CLI session in the web chat UI.

    Attempts SDK native resume first (picks up exact conversation state).
    Falls back to history injection if no SDK session ID is available.

    If the source session has a running agent (terminal or autonomous),
    kills it first so the CLI process releases the session.

    Message format:
    {
        "type": "continue_in_chat",
        "conversation_id": "new-uuid",
        "source_session_id": "db-uuid-of-source-session",
        "project_id": "optional-override",
        "resume": true  // optional hint to prefer SDK resume
    }
    """
    source_session_id = data.get("source_session_id")
    if not source_session_id:
        await mixin._send_error(websocket, "continue_in_chat requires source_session_id")
        return

    conversation_id = data.get("conversation_id") or str(uuid4())
    project_id = data.get("project_id")
    target_provider = data.get("provider")
    target_model = data.get("model")

    # Look up source session for project_id and SDK session ID
    session_manager = getattr(mixin, "session_manager", None)
    source_session = None
    if session_manager:
        try:
            source_session = await asyncio.to_thread(session_manager.get, source_session_id)
            if source_session and not project_id:
                project_id = source_session.project_id
        except Exception as e:
            logger.warning(f"Failed to look up source session {source_session_id}: {e}")

    # --- Resume guard: reject if source session is actively in use ---
    if source_session:
        blocked_reason = await check_resume_blocked(mixin, source_session)
        if blocked_reason:
            await mixin._send_error(
                websocket,
                f"Cannot resume session: {blocked_reason}",
                code="RESUME_BLOCKED",
            )
            return

    # --- Resolve SDK session ID for native resume ---
    sdk_resume_id: str | None = None

    source_provider = getattr(source_session, "source", None) if source_session else None
    effective_provider = target_provider or source_provider
    can_sdk_resume = (
        not effective_provider or not source_provider or effective_provider == source_provider
    )

    # 1. Source session's external_id IS the SDK session ID
    #    (web chat sessions update external_id -> SDK session ID after first turn)
    if can_sdk_resume and source_session and source_session.external_id:
        sdk_resume_id = source_session.external_id

    # 2. Check agent_runs for autonomous agents with sdk_session_id
    if can_sdk_resume and not sdk_resume_id:
        agent_run_mgr = getattr(mixin, "agent_run_manager", None)
        if agent_run_mgr:
            try:
                sdk_resume_id = await asyncio.to_thread(
                    agent_run_mgr.get_sdk_session_id_for_session, source_session_id
                )
            except Exception as e:
                logger.warning(f"Failed to look up sdk_session_id: {e}")

    # 3. Kill running agent/terminal that owns this session before resuming
    if sdk_resume_id:
        killed = False
        # Check DB for active agent run on this session
        try:
            from gobby.agents.kill import kill_agent
            from gobby.storage.agents import LocalAgentRunManager

            session_manager = getattr(mixin, "session_manager", None)
            if session_manager:
                arm = LocalAgentRunManager(session_manager.db)
                run = arm.get_by_session(source_session_id)
                if run:
                    logger.info(
                        f"Killing agent {run.id} before resume",
                    )
                    await kill_agent(run, session_manager.db, close_terminal=True)
                    killed = True
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Failed to kill running agent before resume: {e}")

        # Fallback: kill plain terminal session (user's own CLI, not agent-spawned)
        if not killed and source_session:
            terminal_ctx = source_session.terminal_context
            if terminal_ctx:
                term_killed = await kill_terminal_session(terminal_ctx, source_session_id)
                if term_killed:
                    await asyncio.sleep(0.5)
                    # Mark source session as expired
                    if session_manager:
                        try:
                            await asyncio.to_thread(
                                session_manager.update_status,
                                source_session_id,
                                "expired",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to expire source session: {e}")

    # --- Restore transcript from backup if original is missing ---
    if sdk_resume_id and source_session:
        transcript_path = source_session.transcript_path
        if transcript_path and source_session.external_id:
            original_exists = await asyncio.to_thread(lambda: Path(transcript_path).is_file())
            if not original_exists:
                restored = await asyncio.to_thread(
                    restore_transcript,
                    source_session.external_id,
                    transcript_path,
                )
                if not restored:
                    logger.warning(
                        f"Transcript restore failed for {source_session_id[:8]}; falling back to history injection",
                    )
                    sdk_resume_id = None

    # If the client pre-created a web-chat row with a stale provider, correct
    # it before booting the continuation session so provider restore is
    # source-authoritative by default.
    if session_manager and effective_provider:
        try:
            target_session = await asyncio.to_thread(session_manager.get, conversation_id)
            if target_session and getattr(target_session, "session_type", None) == "web_chat":
                updates: dict[str, Any] = {}
                if getattr(target_session, "source", None) != effective_provider:
                    updates["source"] = effective_provider
                if target_model and getattr(target_session, "model", None) != target_model:
                    updates["model"] = target_model
                if updates:
                    await asyncio.to_thread(
                        session_manager.update,
                        conversation_id,
                        **updates,
                    )
        except Exception as e:
            logger.warning(
                f"Failed to normalize continuation session metadata for {conversation_id}: {e}"
            )

    # Create chat session with optional SDK resume (check dict first to avoid redundant creation)
    session = mixin._chat_sessions.get(conversation_id)
    if session is None:
        try:
            session = await mixin._create_chat_session(
                conversation_id,
                model=target_model,
                project_id=project_id,
                resume_session_id=sdk_resume_id,
                provider=effective_provider,
            )
        except Exception as e:
            logger.error(f"Failed to create continuation session: {e}")
            await mixin._send_error(websocket, f"Failed to create session: {e}")
            return

    # History injection via message_manager removed (session_messages table dropped)

    # Set parent_session_id on the DB record for lineage tracking
    if session.db_session_id and session_manager:
        try:
            await asyncio.to_thread(
                session_manager.update_parent_session_id,
                session.db_session_id,
                source_session_id,
            )
        except Exception as e:
            logger.warning(f"Failed to set parent_session_id: {e}")

    # Send confirmation
    await websocket.send(
        json.dumps(
            {
                "type": "session_continued",
                "conversation_id": conversation_id,
                "source_session_id": source_session_id,
                "db_session_id": session.db_session_id,
                "resumed": bool(sdk_resume_id),
            }
        )
    )
    resume_mode = "SDK resume" if sdk_resume_id else "history injection"
    logger.info(
        f"Session continued ({resume_mode}): {source_session_id[:8]} -> "
        f"{conversation_id[:8]} (db={session.db_session_id})"
    )


async def check_resume_blocked(mixin: SessionControlMixin, source_session: Any) -> str | None:
    """Check if a source session is blocked from being resumed.

    Returns a human-readable reason string if blocked, None if resumable.
    """
    session_id = source_session.id

    # 1. Active agent (DB check -- pending/running agent_runs)
    session_manager = getattr(mixin, "session_manager", None)
    if session_manager:
        try:
            row = session_manager.db.fetchone(
                "SELECT id FROM agent_runs "
                "WHERE parent_session_id = ? AND status IN ('pending', 'running') "
                "LIMIT 1",
                (session_id,),
            )
            if row:
                return "session has a pending or running agent"
        except Exception as e:
            logger.debug(f"Resume block check failed for {session_id}: {e}")

        # 2. Active pipeline
        try:
            row = session_manager.db.fetchone(
                "SELECT id FROM pipeline_executions "
                "WHERE session_id = ? AND status IN ('pending', 'running', 'waiting_approval') "
                "LIMIT 1",
                (session_id,),
            )
            if row:
                return "session has an active pipeline"
        except Exception as e:
            logger.debug(f"Resume block check failed for {session_id}: {e}")

    # 4. Active web chat session (in-memory)
    if session_id in {getattr(s, "db_session_id", None) for s in mixin._chat_sessions.values()}:
        return "session is active in another web chat"

    return None


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
        session = await asyncio.to_thread(session_manager.get, session_id)
    except Exception as e:
        logger.warning(f"Failed to look up session {session_id}: {e}")
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

    workflow_name = getattr(session, "workflow_name", None)
    agent_run_id = getattr(session, "agent_run_id", None)
    agent_name = await _resolve_agent_name_for_session(
        mixin,
        session_id,
        workflow_name,
        agent_run_id,
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
    metadata = mixin.clients.get(websocket)
    if metadata:
        metadata["attached_session_id"] = session_id

    # Send response with initial messages and session metadata
    ref = f"#{session.seq_num}" if getattr(session, "seq_num", None) else None
    await websocket.send(
        json.dumps(
            {
                "type": "attach_to_session_result",
                "session_id": session_id,
                "external_id": session.external_id,
                "source": getattr(session, "source", "unknown"),
                "title": getattr(session, "title", None),
                "status": getattr(session, "status", "unknown"),
                "model": getattr(session, "model", None),
                "ref": ref,
                "chat_mode": getattr(session, "chat_mode", None),
                "git_branch": getattr(session, "git_branch", None),
                "context_window": getattr(session, "context_window", None),
                "session_type": getattr(session, "session_type", None),
                "workflow_name": workflow_name,
                "agent_run_id": agent_run_id,
                "agent_name": agent_name,
                "messages": messages,
                "total_count": total_count,
            }
        )
    )
    logger.info(f"Client attached to session {session_id} ({ref}): {total_count} messages loaded")


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
        "content": "message text"
    }
    """
    session_id = data.get("session_id")
    content = data.get("content", "").strip()
    if not session_id or not content:
        await mixin._send_error(websocket, "send_to_cli_session requires session_id and content")
        return

    session_manager = getattr(mixin, "session_manager", None)
    if not session_manager:
        await mixin._send_error(websocket, "Session manager not available")
        return

    # Look up the target session
    try:
        session = await asyncio.to_thread(session_manager.get, session_id)
    except Exception as e:
        logger.warning(f"Failed to look up session {session_id}: {e}")
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

    # Persist the message via InterSessionMessageManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    inter_msg_manager: InterSessionMessageManager | None = None
    if session_manager and hasattr(session_manager, "db"):
        try:
            inter_msg_manager = InterSessionMessageManager(session_manager.db)
        except Exception as e:
            logger.warning(f"Failed to create InterSessionMessageManager: {e}")

    web_session_id = (mixin.clients.get(websocket) or {}).get("attached_session_id", "web-ui")

    msg_id: str | None = None
    if inter_msg_manager:
        try:
            msg = await asyncio.to_thread(
                inter_msg_manager.create_message,
                from_session=f"web:{web_session_id}",
                to_session=session_id,
                content=content,
                message_type="web_chat",
            )
            msg_id = msg.id
        except Exception as e:
            logger.warning(f"Failed to persist inter-session message: {e}")

    # Try tmux delivery for idle sessions
    delivered_via_tmux = False
    tmux_pane = None
    if hasattr(session, "terminal_context") and session.terminal_context:
        ctx = session.terminal_context if isinstance(session.terminal_context, dict) else {}
        tmux_pane = ctx.get("tmux_pane")

    if not tmux_pane and hasattr(session, "metadata") and session.metadata:
        meta = session.metadata if isinstance(session.metadata, dict) else {}
        tmux_pane = meta.get("terminal_tmux_pane")

    if tmux_pane:
        try:
            from gobby.agents.tmux import get_tmux_session_manager

            tmux_manager = get_tmux_session_manager()
            ok = await tmux_manager.send_keys(tmux_pane, content + "\n")
            if ok:
                delivered_via_tmux = True
                # Mark as delivered
                if inter_msg_manager and msg_id:
                    try:
                        await asyncio.to_thread(inter_msg_manager.mark_delivered, msg_id)
                    except Exception as e:
                        logger.warning(f"Failed to mark message {msg_id} as delivered: {e}")
        except Exception as e:
            logger.warning(f"tmux send_keys failed for {tmux_pane}: {e}")

    # Respond to the client
    await websocket.send(
        json.dumps(
            {
                "type": "send_to_cli_session_result",
                "session_id": session_id,
                "delivered": delivered_via_tmux,
                "delivery_method": "tmux" if delivered_via_tmux else "hook_piggyback",
                "message_id": msg_id,
            }
        )
    )
    logger.info(
        f"Message sent to CLI session {session_id[:8]}: "
        f"delivered={'tmux' if delivered_via_tmux else 'queued for hook piggyback'}"
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
    if not session_id:
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

    await websocket.send(
        json.dumps(
            {
                "type": "detach_from_session_result",
                "session_id": session_id,
            }
        )
    )
    logger.info(f"Client detached from session {session_id}")

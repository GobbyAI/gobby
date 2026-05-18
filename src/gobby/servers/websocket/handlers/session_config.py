"""Session configuration handlers for WebSocket session control.

Handles set_mode, set_project, set_worktree, set_agent, and set_provider
message types.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from gobby.servers.websocket.db import run_db
from gobby.sessions.tmux_context import get_tmux_manager_for_context

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


async def _set_attached_session_mode(
    mixin: SessionControlMixin,
    websocket: Any,
    target_session_id: str,
    mode: str,
) -> None:
    """Persist chat_mode for a session the caller is attached to but does not own.

    Used when the web chat client drives a tmux/CLI session's mode via
    set_mode with a target_session_id. The session has no in-memory ChatSession
    on this server; we update the storage row and session_variables, then
    rely on the next attach_to_session_result reload to confirm.
    """
    session_manager = getattr(mixin, "session_manager", None)
    if session_manager is None:
        await mixin._send_error(websocket, "Session manager not available")
        return

    try:
        session = await run_db(mixin, session_manager.get, target_session_id)
    except (LookupError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to look up target session %s: %s", target_session_id, exc)
        await mixin._send_error(
            websocket,
            f"Session not found: {target_session_id}",
            code="NOT_FOUND",
        )
        return
    if session is None:
        await mixin._send_error(
            websocket,
            f"Session not found: {target_session_id}",
            code="NOT_FOUND",
        )
        return

    if getattr(session, "chat_mode", None) == mode:
        logger.debug(
            "Chat mode unchanged ('%s') for attached session %s",
            mode,
            target_session_id[:8],
        )
        return

    try:
        await run_db(mixin, session_manager.update_chat_mode, target_session_id, mode)
    except ValueError as exc:
        await mixin._send_error(websocket, str(exc))
        return

    try:
        from gobby.workflows.observers import compute_mode_level
        from gobby.workflows.state_manager import SessionVariableManager

        db = getattr(session_manager, "db", None) or getattr(mixin, "db", None)
        if db is not None:
            svm = SessionVariableManager(db)
            svm.merge_variables(
                target_session_id,
                {"chat_mode": mode, "mode_level": compute_mode_level(mode)},
            )
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "Failed to sync mode_level for attached session %s: %s",
            target_session_id[:8],
            exc,
        )

    logger.info(
        "Chat mode set to '%s' for attached session %s",
        mode,
        target_session_id[:8],
    )


async def _validate_persona_agent(
    mixin: SessionControlMixin,
    websocket: Any,
    session_manager: Any,
    agent_name: str,
    existing_row: Any | None,
) -> bool:
    if agent_name == "default":
        return True
    try:
        from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

        agent_body = await run_db(
            mixin,
            resolve_agent,
            agent_name,
            session_manager.db,
            getattr(existing_row, "source", None),
            getattr(existing_row, "project_id", None),
        )
    except (AgentResolutionError, RuntimeError, TypeError, ValueError) as e:
        logger.warning("Failed to resolve persona candidate '%s': %s", agent_name, e)
        agent_body = None

    if agent_body is None:
        await mixin._send_error(websocket, f"Unknown agent definition '{agent_name}'")
        return False
    supports_surface = getattr(agent_body, "supports_surface", None)
    if not callable(supports_surface):
        await mixin._send_error(
            websocket,
            f"Agent definition '{agent_name}' is invalid: missing supports_surface",
        )
        return False
    try:
        persona_supported = bool(supports_surface("persona"))
    except (RuntimeError, TypeError, ValueError):
        logger.warning(
            "Agent definition '%s' failed persona surface validation",
            agent_name,
            exc_info=True,
        )
        await mixin._send_error(
            websocket,
            f"Agent definition '{agent_name}' failed persona surface validation",
        )
        return False
    if not persona_supported:
        await mixin._send_error(
            websocket,
            f"Agent definition '{agent_name}' is not persona-capable",
        )
        return False
    return True


async def _set_attached_session_agent(
    mixin: SessionControlMixin,
    websocket: Any,
    target_session_id: str,
    agent_name: str,
) -> None:
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
            websocket,
            f"Session not found: {target_session_id}",
            code="NOT_FOUND",
        )
        return
    if getattr(session, "session_type", None) != "terminal":
        await mixin._send_error(
            websocket,
            "set_agent target_session_id only supports terminal sessions",
            code="UNSUPPORTED_SESSION_TYPE",
        )
        return

    if not await _validate_persona_agent(mixin, websocket, session_manager, agent_name, session):
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
            f"Session {target_session_id} has no tmux pane for persona switching",
            code="NO_TERMINAL_TARGET",
        )
        return

    try:
        ok = await get_tmux_manager_for_context(ctx).send_keys(
            tmux_pane,
            f"/gobby persona {agent_name}\n",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "tmux persona send_keys failed for pane %s: %s",
            tmux_pane,
            exc,
            exc_info=True,
        )
        ok = False
    if not ok:
        await mixin._send_error(websocket, "Failed to send persona command to attached session")
        return

    await websocket.send(
        json.dumps(
            {
                "type": "agent_changed",
                "target_session_id": target_session_id,
                "agent_name": agent_name,
            }
        )
    )
    logger.info("Persona switched for attached session %s: %s", target_session_id[:8], agent_name)


async def handle_set_mode(mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]) -> None:
    """Handle set_mode message to change chat mode for a conversation.

    Message format:
    {
        "type": "set_mode",
        "mode": "normal" | "accept_edits" | "bypass" | "plan",
        "conversation_id": "stable-id",
        "target_session_id": "db-uuid"  # optional: drives an attached
                                        # tmux/CLI session instead of the
                                        # caller's chat session
    }
    """
    conversation_id: str | None = data.get("conversation_id")
    target_session_id: str | None = data.get("target_session_id")
    mode: str = str(data.get("mode", "bypass"))
    valid_modes = {"normal", "accept_edits", "bypass", "plan"}
    if mode not in valid_modes:
        await mixin._send_error(websocket, f"Invalid mode: {mode}. Must be one of {valid_modes}")
        return
    if mode == "accept_edits":
        mode = "normal"

    if target_session_id:
        await _set_attached_session_mode(mixin, websocket, target_session_id, mode)
        return

    # Track which conversation this client is in (for scoped broadcasts)
    if conversation_id:
        client_info = mixin.clients.get(websocket)
        if client_info is not None:
            client_info["conversation_id"] = conversation_id

    session = mixin._chat_sessions.get(conversation_id) if conversation_id else None
    if session is not None and conversation_id:
        if getattr(session, "chat_mode", None) == mode:
            logger.debug(
                "Chat mode unchanged ('%s') for conversation %s", mode, conversation_id[:8]
            )
            return
        session.set_chat_mode(mode)
        # Sync SDK permission mode so the agent gets a structured mode signal
        await session.sync_sdk_permission_mode()
        # If user toggles away from plan while ExitPlanMode is blocking,
        # cancel the pending approval to unblock the streaming loop.
        if mode != "plan" and session.has_pending_plan:
            session.provide_plan_decision("request_changes")
        # Sync mode_level to session variables
        db_sid = getattr(session, "db_session_id", None)
        if db_sid:
            try:
                from gobby.workflows.observers import compute_mode_level
                from gobby.workflows.state_manager import SessionVariableManager

                sm = getattr(mixin, "session_manager", None)
                db = getattr(sm, "db", None) if sm else None
                if db is None:
                    db = getattr(mixin, "db", None)
                if db is None:
                    logger.warning("No database instance available for session variable sync")
                    return
                svm = SessionVariableManager(db)
                svm.merge_variables(
                    db_sid,
                    {"chat_mode": mode, "mode_level": compute_mode_level(mode)},
                )
            except Exception as e:
                logger.warning(f"Failed to sync mode_level on mode change: {e}")
        logger.info(f"Chat mode set to '{mode}' for conversation {conversation_id[:8]}")
    elif conversation_id:
        # Store mode for when session is created
        mixin._pending_modes[conversation_id] = mode
        logger.debug(f"Chat mode '{mode}' queued for future conversation {conversation_id[:8]}")


async def handle_set_project(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle set_project message to switch the project for a conversation.

    Stops the existing CLI subprocess so the next message creates a fresh
    session with the correct CWD and project context while keeping the
    conversation_id stable.

    Message format:
    {
        "type": "set_project",
        "project_id": "uuid-or-_personal",
        "conversation_id": "stable-id"
    }
    """
    conversation_id = data.get("conversation_id")
    new_project_id = data.get("project_id")

    if not conversation_id or not new_project_id:
        await mixin._send_error(websocket, "set_project requires conversation_id and project_id")
        return

    session = mixin._chat_sessions.get(conversation_id)
    old_project_id = getattr(session, "project_id", None) if session else None

    if session and old_project_id == new_project_id:
        logger.debug("Project unchanged for conversation %s", conversation_id[:8])
        return

    if session:
        await mixin._cancel_active_chat(conversation_id)
        if session.db_session_id:
            session_manager = getattr(mixin, "session_manager", None)
            if session_manager:
                try:
                    await run_db(
                        mixin,
                        session_manager.update,
                        session.db_session_id,
                        status="paused",
                        project_id=new_project_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on project switch: {e}")
        await session.stop()
        registry = getattr(mixin, "web_chat_session_registry", None)
        if registry is not None:
            registry.unregister(conversation_id)
        else:
            mixin._chat_sessions.pop(conversation_id, None)

    # Store project for next session creation (works whether or not session existed)
    mixin._pending_projects[conversation_id] = new_project_id

    await websocket.send(
        json.dumps(
            {
                "type": "project_switched",
                "conversation_id": conversation_id,
                "old_project_id": old_project_id,
                "new_project_id": new_project_id,
            }
        )
    )
    logger.info(
        f"Project switched for conversation {conversation_id[:8]}: "
        f"{old_project_id} -> {new_project_id}"
    )


async def handle_set_worktree(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle set_worktree message to switch the worktree for a conversation.

    Stops the existing CLI subprocess so the next message creates a fresh
    session with the worktree's CWD while keeping the conversation_id stable.

    Message format:
    {
        "type": "set_worktree",
        "conversation_id": "stable-id",
        "worktree_path": "/absolute/path/to/worktree",
        "worktree_id": "optional-db-uuid"
    }
    """
    from gobby.servers.websocket.chat._session import _resolve_git_branch

    conversation_id = data.get("conversation_id")
    worktree_path = data.get("worktree_path")
    worktree_id = data.get("worktree_id")

    if not conversation_id:
        await mixin._send_error(websocket, "set_worktree requires conversation_id")
        return

    # Resolve worktree_path from DB if only worktree_id provided
    if not worktree_path and worktree_id:
        session_manager = getattr(mixin, "session_manager", None)
        if session_manager:
            try:
                from gobby.storage.worktrees import LocalWorktreeManager

                wm = LocalWorktreeManager(session_manager.db)
                wt = wm.get(worktree_id)
                if wt:
                    worktree_path = wt.worktree_path
            except Exception as e:
                logger.warning(f"Failed to resolve worktree {worktree_id}: {e}")

    if not worktree_path:
        await mixin._send_error(websocket, "set_worktree requires worktree_path or worktree_id")
        return

    if not os.path.isdir(worktree_path):
        await mixin._send_error(websocket, f"Worktree path does not exist: {worktree_path}")
        return

    # Tear down existing session (same pattern as set_project)
    session = mixin._chat_sessions.get(conversation_id)
    if session:
        await mixin._cancel_active_chat(conversation_id)
        if session.db_session_id:
            session_manager = getattr(mixin, "session_manager", None)
            if session_manager:
                try:
                    await run_db(
                        mixin,
                        session_manager.update,
                        session.db_session_id,
                        status="paused",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on worktree switch: {e}")
        await session.stop()
        registry = getattr(mixin, "web_chat_session_registry", None)
        if registry is not None:
            registry.unregister(conversation_id)
        else:
            mixin._chat_sessions.pop(conversation_id, None)

    # Store worktree path for next session creation
    mixin._pending_worktree_paths[conversation_id] = worktree_path

    # Resolve the branch name for the new worktree
    new_branch, _ = await _resolve_git_branch(worktree_path)

    await websocket.send(
        json.dumps(
            {
                "type": "worktree_switched",
                "conversation_id": conversation_id,
                "new_branch": new_branch,
                "worktree_path": worktree_path,
            }
        )
    )
    logger.info(
        f"Worktree switched for conversation {conversation_id[:8]}: "
        f"branch={new_branch}, path={worktree_path}"
    )


async def handle_set_agent(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle set_agent message to switch the active agent for a conversation.

    Stops the existing CLI subprocess so the next message creates a fresh
    session with the new agent context while keeping the conversation_id stable.

    Message format:
    {
        "type": "set_agent",
        "conversation_id": "stable-id",
        "agent_name": "agent-definition-name"
    }
    """
    raw_conversation_id = data.get("conversation_id")
    target_session_id = data.get("target_session_id")
    raw_agent_name = data.get("agent_name")

    if not isinstance(raw_agent_name, str) or not raw_agent_name:
        await mixin._send_error(
            websocket,
            "set_agent requires conversation_id or target_session_id and agent_name",
        )
        return
    agent_name = raw_agent_name

    if target_session_id:
        await _set_attached_session_agent(mixin, websocket, str(target_session_id), agent_name)
        return

    if not isinstance(raw_conversation_id, str) or not raw_conversation_id:
        await mixin._send_error(
            websocket,
            "set_agent requires a valid conversation_id and agent_name",
        )
        return
    conversation_id = raw_conversation_id

    session_manager = getattr(mixin, "session_manager", None)
    if session_manager and agent_name != "default":
        try:
            existing_row = await run_db(mixin, session_manager.get, conversation_id)
        except (LookupError, RuntimeError, ValueError) as exc:
            logger.debug(
                "Failed to look up existing session %s for agent validation: %s",
                conversation_id,
                exc,
            )
            existing_row = None
        if not await _validate_persona_agent(
            mixin, websocket, session_manager, agent_name, existing_row
        ):
            return

    # Tear down existing session (same pattern as set_worktree)
    session = mixin._chat_sessions.get(conversation_id)
    current_agent_name = getattr(session, "_pending_agent_name", None) if session else None
    if session and current_agent_name == agent_name:
        logger.debug("Agent unchanged for conversation %s", conversation_id[:8])
        return
    if session:
        await mixin._cancel_active_chat(conversation_id)
        if session.db_session_id:
            if session_manager:
                try:
                    await run_db(
                        mixin,
                        session_manager.update,
                        session.db_session_id,
                        status="paused",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on agent switch: {e}")
        await session.stop()
        registry = getattr(mixin, "web_chat_session_registry", None)
        if registry is not None:
            registry.unregister(conversation_id)
        else:
            mixin._chat_sessions.pop(conversation_id, None)

    # Store agent name for next session creation
    mixin._pending_agents[conversation_id] = agent_name

    await websocket.send(
        json.dumps(
            {
                "type": "agent_changed",
                "conversation_id": conversation_id,
                "agent_name": agent_name,
            }
        )
    )
    logger.info(f"Agent switched for conversation {conversation_id[:8]}: {agent_name}")


async def handle_set_provider(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle set_provider message to switch the provider for a conversation."""
    conversation_id = data.get("conversation_id")
    provider = data.get("provider")
    valid_providers = {"claude", "gemini", "qwen", "codex", "droid"}

    if not conversation_id or not provider:
        await mixin._send_error(websocket, "set_provider requires conversation_id and provider")
        return
    if provider not in valid_providers:
        await mixin._send_error(
            websocket,
            f"Invalid provider: {provider}. Must be one of {sorted(valid_providers)}",
        )
        return

    session = mixin._chat_sessions.get(conversation_id)
    old_provider = getattr(session, "provider", None) if session else None

    if session and old_provider == provider:
        logger.debug("Provider unchanged for conversation %s", conversation_id[:8])
        return

    if session:
        await mixin._cancel_active_chat(conversation_id)
        if session.db_session_id:
            session_manager = getattr(mixin, "session_manager", None)
            if session_manager:
                try:
                    await run_db(
                        mixin,
                        session_manager.update,
                        session.db_session_id,
                        source=provider,
                        status="paused",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on provider switch: {e}")
        await session.stop()
        registry = getattr(mixin, "web_chat_session_registry", None)
        if registry is not None:
            registry.unregister(conversation_id)
        else:
            mixin._chat_sessions.pop(conversation_id, None)

    mixin._pending_providers[conversation_id] = provider

    await websocket.send(
        json.dumps(
            {
                "type": "provider_switched",
                "conversation_id": conversation_id,
                "old_provider": old_provider,
                "provider": provider,
            }
        )
    )
    logger.info(
        f"Provider switched for conversation {conversation_id[:8]}: "
        f"{old_provider or '(new)'} -> {provider}"
    )

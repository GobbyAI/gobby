"""Session configuration handlers for WebSocket session control.

Handles set_mode, set_project, set_worktree, set_agent, and set_provider
message types.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


async def handle_set_mode(mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]) -> None:
    """Handle set_mode message to change chat mode for a conversation.

    Message format:
    {
        "type": "set_mode",
        "mode": "normal" | "accept_edits" | "bypass" | "plan",
        "conversation_id": "stable-id"
    }
    """
    conversation_id: str | None = data.get("conversation_id")
    mode: str = str(data.get("mode", "bypass"))
    valid_modes = {"normal", "accept_edits", "bypass", "plan"}
    if mode not in valid_modes:
        await mixin._send_error(websocket, f"Invalid mode: {mode}. Must be one of {valid_modes}")
        return
    if mode == "accept_edits":
        mode = "normal"

    # Track which conversation this client is in (for scoped broadcasts)
    if conversation_id:
        client_info = mixin.clients.get(websocket)
        if client_info is not None:
            client_info["conversation_id"] = conversation_id

    session = mixin._chat_sessions.get(conversation_id) if conversation_id else None
    if session is not None and conversation_id:
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
                    await asyncio.to_thread(
                        session_manager.update,
                        session.db_session_id,
                        status="paused",
                        project_id=new_project_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on project switch: {e}")
        await session.stop()
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
                    await asyncio.to_thread(
                        session_manager.update,
                        session.db_session_id,
                        status="paused",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on worktree switch: {e}")
        await session.stop()
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
    conversation_id = data.get("conversation_id")
    agent_name = data.get("agent_name")

    if not conversation_id or not agent_name:
        await mixin._send_error(websocket, "set_agent requires conversation_id and agent_name")
        return

    session_manager = getattr(mixin, "session_manager", None)
    if session_manager and agent_name != "default":
        try:
            existing_row = await asyncio.to_thread(session_manager.get, conversation_id)
        except Exception:
            existing_row = None
        try:
            from gobby.workflows.agent_resolver import resolve_agent

            agent_body = await asyncio.to_thread(
                resolve_agent,
                agent_name,
                session_manager.db,
                getattr(existing_row, "source", None),
                getattr(existing_row, "project_id", None),
            )
        except Exception as e:
            logger.warning("Failed to resolve persona candidate '%s': %s", agent_name, e)
            agent_body = None

        if agent_body is None:
            await mixin._send_error(websocket, f"Unknown agent definition '{agent_name}'")
            return
        if not agent_body.supports_surface("persona"):
            await mixin._send_error(
                websocket,
                f"Agent definition '{agent_name}' is not persona-capable",
            )
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
                    await asyncio.to_thread(
                        session_manager.update,
                        session.db_session_id,
                        status="paused",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on agent switch: {e}")
        await session.stop()
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
    valid_providers = {"claude", "gemini", "qwen", "codex"}

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
                    await asyncio.to_thread(
                        session_manager.update,
                        session.db_session_id,
                        source=provider,
                        status="paused",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session on provider switch: {e}")
        await session.stop()
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

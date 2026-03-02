"""WebSocket session control handlers.

SessionControlMixin provides session lifecycle operations: stop, clear,
delete, mode changes, project switching, plan approval, continue-in-chat,
and idle session cleanup. Extracted from chat.py to reduce file size.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.models import (
    CLEANUP_INTERVAL_SECONDS,
    IDLE_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


async def _kill_terminal_session(terminal_ctx: dict[str, Any], session_id: str) -> bool:
    """Kill a plain terminal CLI session using its terminal context.

    Tries tmux pane kill first (cleanest — kills just that pane), then
    falls back to PID-based SIGTERM.

    Args:
        terminal_ctx: Session's terminal_context dict (tmux_pane, parent_pid, etc.)
        session_id: Session ID for logging.

    Returns:
        True if any kill method succeeded.
    """
    # 1. Try tmux pane kill (sends SIGHUP to process in pane)
    tmux_pane = terminal_ctx.get("tmux_pane")
    if tmux_pane:
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "kill-pane",
                "-t",
                str(tmux_pane),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0:
                logger.info(
                    "Killed terminal session %s via tmux pane %s",
                    session_id[:8],
                    tmux_pane,
                )
                return True
            else:
                logger.debug(
                    "tmux kill-pane failed for %s: %s",
                    tmux_pane,
                    stderr.decode().strip() if stderr else "unknown",
                )
        except TimeoutError:
            logger.warning("tmux kill-pane timed out for pane %s", tmux_pane)
        except FileNotFoundError:
            logger.debug("tmux not available, skipping pane kill")
        except Exception as e:
            logger.warning("tmux kill-pane error for %s: %s", tmux_pane, e)

    # 2. Fallback: PID-based kill
    parent_pid = terminal_ctx.get("parent_pid")
    if parent_pid:
        try:
            pid = int(parent_pid)
            os.kill(pid, signal.SIGTERM)
            logger.info(
                "Killed terminal session %s via SIGTERM to PID %d",
                session_id[:8],
                pid,
            )
            return True
        except ProcessLookupError:
            logger.debug("PID %s already dead for session %s", parent_pid, session_id[:8])
        except (ValueError, OSError) as e:
            logger.warning("PID kill failed for session %s: %s", session_id[:8], e)

    logger.debug(
        "No kill method available for session %s (no tmux_pane or parent_pid)",
        session_id[:8],
    )
    return False


class SessionControlMixin:
    """Mixin providing session control handlers for WebSocketServer.

    Requires on the host class:
    - ``self.clients: dict[Any, dict[str, Any]]``
    - ``self._chat_sessions: dict[str, ChatSession]``
    - ``self._active_chat_tasks: dict[str, asyncio.Task[None]]``
    - ``self._pending_modes: dict[str, str]``
    - ``self._cancel_active_chat(...)`` (from ChatMixin)
    - ``self._send_error(...)`` (from HandlerMixin)
    - ``self._create_chat_session(...)`` (from ChatMixin)
    """

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSession]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]

    # Provided by ChatMixin / HandlerMixin – declared for type checking only.
    if TYPE_CHECKING:

        async def _cancel_active_chat(self, conversation_id: str) -> None: ...

        async def _fire_session_end(self, conversation_id: str) -> None: ...

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def _create_chat_session(
            self,
            conversation_id: str,
            model: str | None = None,
            project_id: str | None = None,
            resume_session_id: str | None = None,
        ) -> ChatSession: ...

    async def _handle_stop_chat(self, websocket: Any, data: dict[str, Any] | None = None) -> None:
        """
        Handle stop_chat message to cancel the active chat stream.

        Message format:
        {
            "type": "stop_chat",
            "conversation_id": "optional-id"
        }
        """
        conversation_id = (data or {}).get("conversation_id")

        if conversation_id:
            await self._cancel_active_chat(conversation_id)
        else:
            # Legacy: stop all active chats (backwards compatibility)
            for conv_id in list(self._active_chat_tasks.keys()):
                await self._cancel_active_chat(conv_id)

    async def _handle_plan_approval_response(self, websocket: Any, data: dict[str, Any]) -> None:
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

        session = self._chat_sessions.get(conversation_id_raw) if conversation_id_raw else None
        if session is None or conversation_id_raw is None:
            logger.warning(
                "plan_approval_response for unknown conversation: %s", conversation_id_raw
            )
            return
        conversation_id: str = conversation_id_raw

        if decision == "approve":
            if session.has_pending_plan:
                # ExitPlanMode is blocking — unblock it with the approval
                session.provide_plan_decision("approve")
                logger.info(
                    "Plan approved (ExitPlanMode unblocked) for conversation %s",
                    conversation_id[:8],
                )
            else:
                # Legacy path: plan approval before ExitPlanMode was called
                session.approve_plan()
                session.set_chat_mode("accept_edits")
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
                    "Plan approved (legacy) for conversation %s, switched to accept_edits",
                    conversation_id[:8],
                )
        elif decision == "request_changes":
            feedback = data.get("feedback", "")
            if feedback:
                session.set_plan_feedback(feedback)
            if session.has_pending_plan:
                # ExitPlanMode is blocking — deny it so agent stays in plan mode
                session.provide_plan_decision("request_changes")
                logger.info(
                    "Plan changes requested (ExitPlanMode denied) for conversation %s",
                    conversation_id[:8],
                )
            else:
                logger.info("Plan changes requested for conversation %s", conversation_id[:8])

    async def _handle_continue_in_chat(self, websocket: Any, data: dict[str, Any]) -> None:
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
            await self._send_error(websocket, "continue_in_chat requires source_session_id")
            return

        conversation_id = data.get("conversation_id") or str(uuid4())
        project_id = data.get("project_id")

        # Look up source session for project_id and SDK session ID
        session_manager = getattr(self, "session_manager", None)
        source_session = None
        if session_manager:
            try:
                source_session = await asyncio.to_thread(session_manager.get, source_session_id)
                if source_session and not project_id:
                    project_id = source_session.project_id
            except Exception as e:
                logger.warning(f"Failed to look up source session {source_session_id}: {e}")

        # --- Resolve SDK session ID for native resume ---
        sdk_resume_id: str | None = None

        # 1. Source session's external_id IS the SDK session ID
        #    (web chat sessions update external_id → SDK session ID after first turn)
        if source_session and source_session.external_id:
            sdk_resume_id = source_session.external_id

        # 2. Check agent_runs for autonomous agents with sdk_session_id
        if not sdk_resume_id:
            agent_run_mgr = getattr(self, "agent_run_manager", None)
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
            # Try agent registry first (Gobby-spawned agents)
            try:
                from gobby.agents.registry import get_running_agent_registry

                registry = get_running_agent_registry()
                running = registry.get_by_session(source_session_id)
                if running:
                    logger.info(
                        "Killing agent %s (mode=%s) before resume",
                        running.run_id,
                        running.mode,
                    )
                    await registry.kill(running.run_id, close_terminal=True)
                    killed = True
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"Failed to kill running agent before resume: {e}")

            # Fallback: kill plain terminal session (user's own CLI, not agent-spawned)
            if not killed and source_session:
                terminal_ctx = source_session.terminal_context
                if terminal_ctx:
                    term_killed = await _kill_terminal_session(terminal_ctx, source_session_id)
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

        # Create chat session with optional SDK resume
        try:
            session = await self._create_chat_session(
                conversation_id,
                project_id=project_id,
                resume_session_id=sdk_resume_id,
            )
        except Exception as e:
            logger.error(f"Failed to create continuation session: {e}")
            await self._send_error(websocket, f"Failed to create session: {e}")
            return

        # Fall back to history injection if no SDK resume available
        if not sdk_resume_id:
            message_manager = getattr(self, "message_manager", None)
            if message_manager:
                try:
                    max_idx = await message_manager.get_max_message_index(source_session_id)
                    if max_idx >= 0:
                        session._message_manager_source_session_id = source_session_id
                        session._needs_history_injection = True
                        session._message_manager = message_manager
                        logger.info(
                            "Cross-session history injection enabled for continuation",
                            extra={
                                "source": source_session_id[:8],
                                "target": conversation_id[:8],
                                "max_idx": max_idx,
                            },
                        )
                except Exception as e:
                    logger.warning(f"Failed to set up history injection: {e}")

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

    async def _handle_set_mode(self, websocket: Any, data: dict[str, Any]) -> None:
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
            await self._send_error(websocket, f"Invalid mode: {mode}. Must be one of {valid_modes}")
            return

        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session is not None and conversation_id:
            session.set_chat_mode(mode)
            # If user toggles away from plan while ExitPlanMode is blocking,
            # cancel the pending approval to unblock the streaming loop.
            if mode != "plan" and session.has_pending_plan:
                session.provide_plan_decision("request_changes")
            # Sync mode_level to workflow state
            workflow_handler = getattr(self, "workflow_handler", None)
            db_sid = getattr(session, "db_session_id", None)
            if workflow_handler and db_sid:
                try:
                    from gobby.workflows.observers import compute_mode_level

                    sm = workflow_handler.engine.state_manager
                    sm.merge_variables(
                        db_sid,
                        {"chat_mode": mode, "mode_level": compute_mode_level(mode)},
                    )
                except Exception as e:
                    logger.warning(f"Failed to sync mode_level on mode change: {e}")
            logger.info(f"Chat mode set to '{mode}' for conversation {conversation_id[:8]}")
        elif conversation_id:
            # Store mode for when session is created
            self._pending_modes[conversation_id] = mode
            logger.debug(f"Chat mode '{mode}' queued for future conversation {conversation_id[:8]}")

    async def _handle_set_project(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle set_project message to switch the project for a conversation.

        Stops the existing CLI subprocess so the next message creates a fresh
        session with the correct CWD and project context. Conversation history
        is preserved via database-backed history injection.

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
            await self._send_error(websocket, "set_project requires conversation_id and project_id")
            return

        session = self._chat_sessions.get(conversation_id)
        old_project_id = getattr(session, "project_id", None) if session else None

        if session:
            await self._cancel_active_chat(conversation_id)
            if session.db_session_id:
                session_manager = getattr(self, "session_manager", None)
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
            self._chat_sessions.pop(conversation_id, None)

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

    async def _handle_set_worktree(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle set_worktree message to switch the worktree for a conversation.

        Stops the existing CLI subprocess so the next message creates a fresh
        session with the worktree's CWD. Conversation history is preserved via
        database-backed history injection.

        Message format:
        {
            "type": "set_worktree",
            "conversation_id": "stable-id",
            "worktree_path": "/absolute/path/to/worktree",
            "worktree_id": "optional-db-uuid"
        }
        """
        import os

        from gobby.servers.websocket.chat import _resolve_git_branch

        conversation_id = data.get("conversation_id")
        worktree_path = data.get("worktree_path")
        worktree_id = data.get("worktree_id")

        if not conversation_id:
            await self._send_error(websocket, "set_worktree requires conversation_id")
            return

        # Resolve worktree_path from DB if only worktree_id provided
        if not worktree_path and worktree_id:
            session_manager = getattr(self, "session_manager", None)
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
            await self._send_error(websocket, "set_worktree requires worktree_path or worktree_id")
            return

        if not os.path.isdir(worktree_path):
            await self._send_error(websocket, f"Worktree path does not exist: {worktree_path}")
            return

        # Tear down existing session (same pattern as set_project)
        session = self._chat_sessions.get(conversation_id)
        if session:
            await self._cancel_active_chat(conversation_id)
            if session.db_session_id:
                session_manager = getattr(self, "session_manager", None)
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
            self._chat_sessions.pop(conversation_id, None)

        # Store worktree path for next session creation
        self._pending_worktree_paths[conversation_id] = worktree_path

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

    async def _handle_set_agent(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle set_agent message to switch the active agent for a conversation.

        Stops the existing CLI subprocess so the next message creates a fresh
        session with the new agent context. Conversation history is preserved
        via database-backed history injection.

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
            await self._send_error(websocket, "set_agent requires conversation_id and agent_name")
            return

        # Tear down existing session (same pattern as set_worktree)
        session = self._chat_sessions.get(conversation_id)
        if session:
            await self._cancel_active_chat(conversation_id)
            if session.db_session_id:
                session_manager = getattr(self, "session_manager", None)
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
            self._chat_sessions.pop(conversation_id, None)

        # Store agent name for next session creation
        self._pending_agents[conversation_id] = agent_name

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

    async def _handle_clear_chat(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle clear_chat message: stop session, mark completed, notify frontend.

        Message format:
        {
            "type": "clear_chat",
            "conversation_id": "stable-id"
        }
        """
        conversation_id = data.get("conversation_id")
        if not conversation_id:
            return

        session = self._chat_sessions.get(conversation_id)
        if not session:
            # No active session — just acknowledge
            await websocket.send(
                json.dumps({"type": "chat_cleared", "conversation_id": conversation_id})
            )
            return

        # Mark session as completed in database
        if session.db_session_id:
            session_manager = getattr(self, "session_manager", None)
            if session_manager:
                try:
                    await asyncio.to_thread(
                        session_manager.update, session.db_session_id, status="completed"
                    )
                except Exception as e:
                    logger.warning(f"Failed to update session status on clear: {e}")

        # Fire SESSION_END before teardown
        await self._fire_session_end(conversation_id)

        # Stop the old ChatSession
        await self._cancel_active_chat(conversation_id)
        await session.stop()
        self._chat_sessions.pop(conversation_id, None)

        # Notify frontend
        await websocket.send(
            json.dumps({"type": "chat_cleared", "conversation_id": conversation_id})
        )
        logger.info(f"Chat cleared for conversation {conversation_id[:8]}")

    async def _handle_delete_chat(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle delete_chat message: stop session, delete from DB, notify frontend.

        Message format:
        {
            "type": "delete_chat",
            "conversation_id": "stable-id"
        }
        """
        conversation_id = data.get("conversation_id")
        if not conversation_id:
            return

        session = self._chat_sessions.get(conversation_id)
        db_session_id = getattr(session, "db_session_id", None) if session else None

        # Fall back to session_id from the message (for historical sessions not in memory)
        if not db_session_id:
            db_session_id = data.get("session_id")

        # Stop the ChatSession if active
        if session:
            await self._fire_session_end(conversation_id)
            await self._cancel_active_chat(conversation_id)
            await session.stop()
            self._chat_sessions.pop(conversation_id, None)

        # Soft-delete: mark as handoff_ready (preserves messages for handoff context;
        # hard delete fails due to FK constraints from agent_runs, tasks, etc.)
        if db_session_id:
            session_manager = getattr(self, "session_manager", None)
            try:
                if session_manager:
                    await asyncio.to_thread(
                        session_manager.update, db_session_id, status="handoff_ready"
                    )
            except Exception as e:
                logger.warning(f"Failed to soft-delete session from DB: {e}")

        # Notify frontend
        await websocket.send(
            json.dumps({"type": "chat_deleted", "conversation_id": conversation_id})
        )
        logger.info(f"Chat deleted for conversation {conversation_id[:8]}")

    async def _handle_attach_to_session(self, websocket: Any, data: dict[str, Any]) -> None:
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
            await self._send_error(websocket, "attach_to_session requires session_id")
            return

        session_manager = getattr(self, "session_manager", None)
        if not session_manager:
            await self._send_error(websocket, "Session manager not available")
            return

        # Look up session
        try:
            session = await asyncio.to_thread(session_manager.get, session_id)
        except Exception as e:
            logger.warning(f"Failed to look up session {session_id}: {e}")
            session = None

        if not session:
            await self._send_error(websocket, f"Session not found: {session_id}", code="NOT_FOUND")
            return

        # Load recent messages
        message_manager = getattr(self, "message_manager", None)
        messages: list[dict[str, Any]] = []
        total_count = 0
        if message_manager:
            try:
                raw_messages = await message_manager.get_messages(session_id, limit=100)
                messages = [
                    {
                        "id": m.get("id", ""),
                        "role": m.get("role", ""),
                        "content": m.get("content", ""),
                        "content_type": m.get("content_type"),
                        "tool_name": m.get("tool_name"),
                        "tool_input": m.get("tool_input"),
                        "tool_result": m.get("tool_result"),
                        "timestamp": m.get("timestamp", ""),
                        "message_index": m.get("message_index"),
                    }
                    for m in raw_messages
                ]
                total_count = len(messages)
            except Exception as e:
                logger.warning(f"Failed to load messages for session {session_id}: {e}")

        # Auto-subscribe to session-scoped events
        if not hasattr(websocket, "subscriptions") or websocket.subscriptions is None:
            websocket.subscriptions = set()
        websocket.subscriptions.add(f"session_message:session_id={session_id}")
        websocket.subscriptions.add(f"hook_event:session_id={session.external_id}")

        # Track attached session on websocket metadata
        metadata = self.clients.get(websocket)
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
                    "messages": messages,
                    "total_count": total_count,
                }
            )
        )
        logger.info(
            f"Client attached to session {session_id} ({ref}): {total_count} messages loaded"
        )

    async def _handle_send_to_cli_session(self, websocket: Any, data: dict[str, Any]) -> None:
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
            await self._send_error(websocket, "send_to_cli_session requires session_id and content")
            return

        session_manager = getattr(self, "session_manager", None)
        if not session_manager:
            await self._send_error(websocket, "Session manager not available")
            return

        # Look up the target session
        try:
            session = await asyncio.to_thread(session_manager.get, session_id)
        except Exception as e:
            logger.warning(f"Failed to look up session {session_id}: {e}")
            session = None

        if not session:
            await self._send_error(websocket, f"Session not found: {session_id}", code="NOT_FOUND")
            return

        # Persist the message via InterSessionMessageManager
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        inter_msg_manager: InterSessionMessageManager | None = None
        if session_manager and hasattr(session_manager, "db"):
            try:
                inter_msg_manager = InterSessionMessageManager(session_manager.db)
            except Exception as e:
                logger.warning(f"Failed to create InterSessionMessageManager: {e}")

        web_session_id = (self.clients.get(websocket) or {}).get("attached_session_id", "web-ui")

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
                        except Exception:
                            pass
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

    async def _handle_detach_from_session(self, websocket: Any, data: dict[str, Any]) -> None:
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
            await self._send_error(websocket, "detach_from_session requires session_id")
            return

        subs: set[str] = getattr(websocket, "subscriptions", set())
        # Remove all parametric subscriptions for this session
        to_remove = {s for s in subs if session_id in s}
        subs -= to_remove

        # Clear attached session metadata
        metadata = self.clients.get(websocket)
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

    async def _cleanup_idle_sessions(self) -> None:
        """Periodically disconnect chat sessions that have been idle too long."""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                now = datetime.now(UTC)
                stale_ids = [
                    conv_id
                    for conv_id, session in self._chat_sessions.items()
                    if (now - session.last_activity).total_seconds() > IDLE_TIMEOUT_SECONDS
                ]
                for conv_id in stale_ids:
                    # Fire SESSION_END before popping (needs session in dict for lookup)
                    await self._fire_session_end(conv_id)
                    session = self._chat_sessions.pop(conv_id)
                    await self._cancel_active_chat(conv_id)
                    # Mark as paused in database before stopping
                    if session.db_session_id:
                        session_manager = getattr(self, "session_manager", None)
                        if session_manager:
                            try:
                                await asyncio.to_thread(
                                    session_manager.update, session.db_session_id, status="paused"
                                )
                            except Exception as e:
                                logger.warning(f"Failed to update session status: {e}")
                    await session.stop()
                    logger.debug(f"Cleaned up idle chat session {conv_id}")
                if stale_ids:
                    logger.info(f"Cleaned up {len(stale_ids)} idle chat session(s)")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in idle session cleanup")

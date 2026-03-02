"""Chat session lifecycle mixin."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.hooks.events import HookEvent, HookEventType
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat._lifecycle import _inject_agent_skills
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)

_CANCEL_YIELD_DELAY = 0.1


async def _resolve_git_branch(project_path: str | None) -> tuple[str | None, str | None]:
    """Resolve the current git branch for a project directory.

    Returns (branch_name, worktree_path). branch_name is None for detached HEAD
    or non-git directories.
    """
    if not project_path:
        return None, None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--show-current",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        branch = stdout.decode().strip() or None
        # For detached HEAD, show short SHA instead of nothing
        if not branch:
            proc2 = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--short",
                "HEAD",
                cwd=project_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
            short_sha = stdout2.decode().strip()
            if short_sha:
                branch = f"detached:{short_sha}"
        return branch, project_path
    except Exception as e:
        logger.debug("Failed to resolve git branch: %s", e)
        return None, None


class ChatSessionMixin:
    """Session management methods for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSession]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]

    if TYPE_CHECKING:

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def broadcast_session_event(
            self,
            event: str,
            session_id: str,
            **kwargs: Any,
        ) -> None: ...

        async def _fire_lifecycle(
            self,
            conversation_id: str,
            event_type: HookEventType,
            data: dict[str, Any],
        ) -> dict[str, Any] | None: ...

        def _inject_pending_messages(
            self,
            db_session_id: str,
            event_type: HookEventType,
        ) -> str | None: ...

        async def _evaluate_blocking_webhooks(
            self,
            event: HookEvent,
        ) -> dict[str, Any] | None: ...

    async def _cancel_active_chat(self, conversation_id: str) -> None:
        """Cancel any active chat streaming task for a conversation.

        Attempts a graceful interrupt first so the SDK can clean up its
        internal task group, then force-cancels if the task is still running.
        After the task is cancelled, drains any stale response events from
        the SDK to prevent the off-by-one bug where the next query's
        ``receive_response()`` returns leftover events from the interrupted
        turn.
        """
        session = self._chat_sessions.get(conversation_id)
        if session:
            try:
                await asyncio.wait_for(session.interrupt(), timeout=0.5)
            except Exception as e:
                logger.debug("Interrupt failed: %s", e)

        active_task = self._active_chat_tasks.pop(conversation_id, None)
        if active_task and not active_task.done():
            active_task.cancel()
            try:
                await active_task
            except asyncio.CancelledError:
                pass
            # Let the SDK settle after interrupt+cancellation.
            # Without this pause, an immediate query() can get an empty
            # response because the SDK hasn't finished its internal cleanup.
            await asyncio.sleep(_CANCEL_YIELD_DELAY)

        # Drain any stale response events buffered in the SDK.
        # Without this, receive_response() on the *next* query returns
        # leftover events from this interrupted turn (off-by-one bug).
        if session:
            await session.drain_pending_response()

    async def _create_chat_session(
        self,
        conversation_id: str,
        model: str | None = None,
        project_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> ChatSession:
        """Create and bootstrap a new ChatSession with lifecycle hooks wired."""
        session = ChatSession(conversation_id=conversation_id)
        if resume_session_id:
            session.resume_session_id = resume_session_id

        # Wire lifecycle callbacks before start() so hooks are registered with the SDK
        session._on_before_agent = lambda data: self._fire_lifecycle(
            conversation_id, HookEventType.BEFORE_AGENT, data
        )
        session._on_pre_tool = lambda data: self._fire_lifecycle(
            conversation_id, HookEventType.BEFORE_TOOL, data
        )
        session._on_post_tool = lambda data: self._fire_lifecycle(
            conversation_id, HookEventType.AFTER_TOOL, data
        )
        session._on_pre_compact = lambda data: self._fire_lifecycle(
            conversation_id, HookEventType.PRE_COMPACT, data
        )
        session._on_stop = lambda data: self._fire_lifecycle(
            conversation_id, HookEventType.STOP, data
        )

        # Wire mode-change callback so agent-initiated plan mode transitions
        # (EnterPlanMode/ExitPlanMode) are broadcast to all connected clients
        async def _notify_mode_changed(mode: str, reason: str) -> None:
            msg = json.dumps(
                {
                    "type": "mode_changed",
                    "conversation_id": conversation_id,
                    "mode": mode,
                    "reason": reason,
                }
            )
            for ws in list(self.clients.keys()):
                try:
                    await ws.send(msg)
                except (ConnectionClosed, ConnectionClosedError):
                    pass

        session._on_mode_changed = _notify_mode_changed

        # Wire plan-ready callback so ExitPlanMode sends plan content to frontend
        async def _notify_plan_ready(content: str | None, input_data: dict[str, Any]) -> None:
            msg = json.dumps(
                {
                    "type": "plan_pending_approval",
                    "conversation_id": conversation_id,
                    "plan_content": content,
                    "allowed_prompts": input_data.get("allowedPrompts"),
                }
            )
            for ws in list(self.clients.keys()):
                try:
                    await ws.send(msg)
                except (ConnectionClosed, ConnectionClosedError):
                    pass

        session._on_plan_ready = _notify_plan_ready

        # Wire tool approval config if available
        daemon_cfg = getattr(self, "daemon_config", None)
        if daemon_cfg is not None:
            tool_approval_cfg = getattr(daemon_cfg, "tool_approval", None)
            if tool_approval_cfg is not None and tool_approval_cfg.enabled:
                session._tool_approval_config = tool_approval_cfg

        # Apply daemon config default chat mode (lowest priority — overridden below)
        if daemon_cfg is not None:
            chat_cfg = getattr(daemon_cfg, "chat", None)
            if chat_cfg is not None:
                session.chat_mode = chat_cfg.default_mode

        # Set project context on session BEFORE start() so env vars and CWD
        # are correctly configured for the CLI subprocess.
        effective_pid = project_id or PERSONAL_PROJECT_ID
        session.project_id = effective_pid

        # Register in database BEFORE start() so that db_session_id is available
        # for the CLI subprocess env vars (GOBBY_SESSION_ID) during start().
        session_manager = getattr(self, "session_manager", None)
        _is_new_registration = False
        if session_manager:
            try:
                db_session = await asyncio.to_thread(
                    session_manager.register,
                    external_id=conversation_id,
                    machine_id=get_machine_id(),
                    source="claude_sdk_web_chat",
                    project_id=project_id or PERSONAL_PROJECT_ID,
                )
                session.db_session_id = db_session.id
                session.seq_num = db_session.seq_num
                _is_new_registration = True
                logger.info(
                    f"Registered web-chat session {db_session.id} "
                    f"(conv={conversation_id[:8]}, project={project_id or PERSONAL_PROJECT_ID})"
                )
            except Exception as e:
                logger.warning(f"Failed to register web-chat session in DB: {e}")

        # Override chat mode with DB-persisted value (for returning sessions only —
        # new registrations just have the column default which would clobber daemon config)
        if session_manager and session.db_session_id and not _is_new_registration:
            try:
                db_session = await asyncio.to_thread(session_manager.get, session.db_session_id)
                if db_session and db_session.chat_mode:
                    session.chat_mode = db_session.chat_mode
            except Exception as e:
                logger.debug("Failed to get DB session: %s", e)

        # Override with pending mode (highest priority — user toggled before session existed)
        pending_modes = getattr(self, "_pending_modes", {})
        pending_mode = pending_modes.pop(conversation_id, None)
        if pending_mode:
            session.chat_mode = pending_mode

        # Wire DB persistence callback for chat_mode changes
        if session_manager and session.db_session_id:
            _db_sid = session.db_session_id
            _sm = session_manager

            def _persist_mode(mode: str) -> None:
                try:
                    _sm.update_chat_mode(_db_sid, mode)
                except Exception:
                    logger.debug("Failed to persist chat_mode", exc_info=True)

            session._on_mode_persist = _persist_mode

        # Persist pending_mode to DB now that the callback is wired
        if pending_mode and session._on_mode_persist:
            try:
                session._on_mode_persist(pending_mode)
            except Exception:
                logger.debug("Failed to persist pending chat_mode", exc_info=True)

        # Look up repo_path from DB so the subprocess CWD matches the selected project
        if session_manager and not session.project_path:
            try:
                from gobby.storage.projects import LocalProjectManager

                pm = LocalProjectManager(session_manager.db)
                project = pm.get(effective_pid)
                if project and project.repo_path:
                    session.project_path = project.repo_path
            except Exception as e:
                logger.warning(f"Failed to look up project repo_path: {e}")

        # Override project_path with pending worktree path (from set_worktree)
        pending_wt = getattr(self, "_pending_worktree_paths", {})
        wt_override = pending_wt.pop(conversation_id, None)
        if wt_override:
            session.project_path = wt_override

        # Pop pending agent override (from set_agent WS message) BEFORE start()
        # so we can resolve agent preamble and use it as the system prompt.
        pending_agents = getattr(self, "_pending_agents", {})
        pending_agent = pending_agents.pop(conversation_id, None)
        if pending_agent:
            session._pending_agent_name = pending_agent

        # Resolve agent + skills as system prompt (agent definition is single source of truth)
        # Always resolve an agent — pending_agent if user selected one, otherwise default-web-chat
        agent_name = pending_agent or "default-web-chat"
        if session_manager:
            try:
                from gobby.workflows.agent_resolver import resolve_agent

                agent_body = await asyncio.to_thread(
                    resolve_agent,
                    agent_name,
                    session_manager.db,
                    cli_source="claude_sdk_web_chat",
                    project_id=project_id or PERSONAL_PROJECT_ID,
                )
                if agent_body:
                    context_parts: list[str] = []
                    preamble = agent_body.build_prompt_preamble()
                    if preamble:
                        context_parts.append(preamble)
                    # Audience-aware skill injection (canvas, etc.)
                    skills_text = await asyncio.to_thread(
                        _inject_agent_skills,
                        agent_body,
                        session_manager.db,
                        project_id or PERSONAL_PROJECT_ID,
                    )
                    if skills_text:
                        context_parts.append(skills_text)
                    if context_parts:
                        session.system_prompt_override = "\n\n".join(context_parts)
            except Exception as e:
                logger.warning(f"Failed to resolve agent '{agent_name}': {e}")

        await session.start(model=model)
        self._chat_sessions[conversation_id] = session

        # Detect returning sessions and set up history injection
        message_manager = getattr(self, "message_manager", None)
        if message_manager and session.db_session_id:
            try:
                max_idx = await message_manager.get_max_message_index(session.db_session_id)
                if max_idx >= 0:
                    session.message_index = max_idx + 1
                    session._needs_history_injection = True
                    session._message_manager = message_manager
                    logger.info(
                        "Returning session detected; history injection enabled",
                        extra={"max_idx": max_idx, "conversation_id": conversation_id[:8]},
                    )
            except Exception as e:
                logger.warning(
                    "Failed to check message history",
                    extra={"conversation_id": conversation_id[:8]},
                    exc_info=e,
                )

        # Fire SESSION_START (informational, fire-and-forget)
        start_data: dict[str, Any] = {}
        if pending_agent:
            start_data["agent_name_override"] = pending_agent
        asyncio.create_task(
            self._fire_lifecycle(conversation_id, HookEventType.SESSION_START, start_data)
        )

        # Broadcast authoritative mode to frontend so it can override local storage
        mode_msg = json.dumps(
            {
                "type": "mode_changed",
                "conversation_id": conversation_id,
                "mode": session.chat_mode,
                "reason": "session_restored",
            }
        )
        for ws in list(self.clients.keys()):
            try:
                await ws.send(mode_msg)
            except (ConnectionClosed, ConnectionClosedError):
                pass

        return session

    async def _fire_session_end(self, conversation_id: str) -> None:
        """Fire SESSION_END event for a chat session (best-effort).

        Called before session cleanup in clear, delete, idle cleanup, and
        server shutdown paths to maintain parity with CLI adapters.
        """
        try:
            await self._fire_lifecycle(conversation_id, HookEventType.SESSION_END, {})
        except Exception:
            logger.debug("SESSION_END fire failed for %s", conversation_id[:8], exc_info=True)

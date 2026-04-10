"""Chat session lifecycle mixin."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.hooks.events import HookEvent, HookEventType
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat._lifecycle import _inject_agent_skills
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)

_CANCEL_YIELD_DELAY = 0.1


def _get_runtime_external_id(session: ChatSessionProtocol) -> str | None:
    """Return the provider-native session/thread id discovered during start()."""
    sdk_session_id = getattr(session, "sdk_session_id", None)
    if isinstance(sdk_session_id, str) and sdk_session_id:
        return sdk_session_id

    thread_id = getattr(session, "_thread_id", None)
    if isinstance(thread_id, str) and thread_id:
        return thread_id

    return None


def _get_runtime_transcript_path(session: ChatSessionProtocol) -> str | None:
    """Return the live transcript path discovered during start(), if available."""
    transcript_path = getattr(session, "transcript_path", None)
    if isinstance(transcript_path, str) and transcript_path:
        return transcript_path

    private_path = getattr(session, "_transcript_path", None)
    if isinstance(private_path, str) and private_path:
        return private_path

    return None


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
        logger.debug(f"Failed to resolve git branch: {e}")
        return None, None


class ChatSessionMixin:
    """Session management methods for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]
    _pending_projects: dict[str, str]
    _pending_providers: dict[str, str]
    _session_create_locks: dict[str, asyncio.Lock]

    def _get_session_create_lock(self, conversation_id: str) -> asyncio.Lock:
        """Get or create a per-conversation lock for session creation."""
        if not hasattr(self, "_session_create_locks"):
            self._session_create_locks = {}
        lock = self._session_create_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_create_locks[conversation_id] = lock
        return lock

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
                logger.debug(f"Interrupt failed: {e}")

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

        # Cancel any active TTS pipeline for this conversation.
        # This ensures barge-in (new message, stop button, etc.) also
        # stops audio synthesis, not just the LLM stream.
        if hasattr(self, "_cancel_tts"):
            try:
                await self._cancel_tts(conversation_id)
            except Exception:
                logger.debug("TTS cancel during chat interrupt failed", exc_info=True)

    async def _create_chat_session(
        self,
        conversation_id: str,
        model: str | None = None,
        project_id: str | None = None,
        resume_session_id: str | None = None,
        provider: str | None = None,
    ) -> ChatSessionProtocol:
        """Create and bootstrap a new ChatSession with lifecycle hooks wired.

        Uses a per-conversation lock to prevent duplicate session creation
        when concurrent handlers (chat_message + continue_in_chat) race.
        """
        lock = self._get_session_create_lock(conversation_id)
        async with lock:
            # Double-check: another coroutine may have created it while we waited
            existing = self._chat_sessions.get(conversation_id)
            if existing is not None:
                return existing
            return await self._create_chat_session_inner(
                conversation_id, model, project_id, resume_session_id, provider
            )

    async def _create_chat_session_inner(
        self,
        conversation_id: str,
        model: str | None = None,
        project_id: str | None = None,
        resume_session_id: str | None = None,
        provider: str | None = None,
    ) -> ChatSessionProtocol:
        """Inner implementation — must be called under the per-conversation lock from _session_create_locks."""
        # Early agent resolution to determine provider (Codex vs Claude SDK)
        pending_agents = getattr(self, "_pending_agents", {})
        pending_agent = pending_agents.pop(conversation_id, None)
        agent_name = pending_agent or "default-web-chat"
        agent_body = None
        session_manager = getattr(self, "session_manager", None)
        if session_manager:
            try:
                from gobby.workflows.agent_resolver import resolve_agent

                agent_body = await asyncio.to_thread(
                    resolve_agent,
                    agent_name,
                    session_manager.db,
                    cli_source="claude",
                    project_id=project_id or PERSONAL_PROJECT_ID,
                )
            except Exception as e:
                logger.warning(f"Failed to resolve agent '{agent_name}' for provider check: {e}")

        # Provider precedence: queued UI override > explicit message provider
        # > agent definition > resumed session > default.
        pending_providers = getattr(self, "_pending_providers", {})
        pending_provider = pending_providers.pop(conversation_id, None)
        effective_provider = pending_provider or provider
        if not effective_provider and agent_body:
            effective_provider = getattr(agent_body, "provider", None)

        # Route to appropriate session implementation
        # Claude uses SDK (no interactive subprocess protocol).
        # Gemini uses ACP (gemini --acp, JSON-RPC over stdio).
        # Codex uses app-server (codex app-server, JSON-RPC over stdio).
        match effective_provider:
            case "gemini":
                from gobby.servers.gemini_cli_chat_session import GeminiCLIChatSession

                session: ChatSessionProtocol = GeminiCLIChatSession(
                    conversation_id=conversation_id, _model=model
                )
            case "codex":
                from gobby.servers.codex_cli_chat_session import CodexCLIChatSession

                session = CodexCLIChatSession(conversation_id=conversation_id, model=model)
            case _:
                # Claude + Auto + unknown: SDK path
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
        # (EnterPlanMode/ExitPlanMode) are broadcast to conversation clients only
        async def _notify_mode_changed(mode: str, reason: str) -> None:
            msg = json.dumps(
                {
                    "type": "mode_changed",
                    "conversation_id": conversation_id,
                    "mode": mode,
                    "reason": reason,
                }
            )
            for ws, meta in list(self.clients.items()):
                # Only send to clients in this conversation (or untracked clients for compat)
                cid = meta.get("conversation_id") if meta else None
                if cid is not None and cid != conversation_id:
                    continue
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
            for ws, meta in list(self.clients.items()):
                cid = meta.get("conversation_id") if meta else None
                if cid is not None and cid != conversation_id:
                    continue
                try:
                    await ws.send(msg)
                except (ConnectionClosed, ConnectionClosedError):
                    pass

        session._on_plan_ready = _notify_plan_ready

        # Wire config from daemon
        daemon_cfg = getattr(self, "daemon_config", None)
        if daemon_cfg is not None:
            session._config = daemon_cfg
            tool_approval_cfg = getattr(daemon_cfg, "tool_approval", None)
            if tool_approval_cfg is not None and tool_approval_cfg.enabled:
                session._tool_approval_config = tool_approval_cfg
            ctx_overrides = getattr(daemon_cfg, "context_window_overrides", None)
            if ctx_overrides:
                session._context_window_overrides = ctx_overrides

        # Apply daemon config default chat mode (lowest priority — overridden below)
        if daemon_cfg is not None:
            chat_cfg = getattr(daemon_cfg, "chat", None)
            if chat_cfg is not None:
                session.chat_mode = chat_cfg.default_mode

        # Set project context on session BEFORE start() so env vars and CWD
        # are correctly configured for the CLI subprocess.
        # Precedence: explicit message project_id > pending from set_project > fallback
        pending_projects = getattr(self, "_pending_projects", {})
        pending_project = pending_projects.pop(conversation_id, None)
        effective_pid = project_id or pending_project or PERSONAL_PROJECT_ID
        session.project_id = effective_pid

        # Register in database BEFORE start() so that db_session_id is available
        # for the CLI subprocess env vars (GOBBY_SESSION_ID) during start().
        session_manager = getattr(self, "session_manager", None)
        if session_manager:
            try:
                db_session = await asyncio.to_thread(
                    session_manager.register,
                    external_id=conversation_id,
                    machine_id=get_machine_id(),
                    source=effective_provider or "claude",
                    project_id=project_id or PERSONAL_PROJECT_ID,
                    session_type="web_chat",
                )
                session.db_session_id = db_session.id
                session.seq_num = db_session.seq_num
                session._session_manager_ref = session_manager

                # Auto-resume for returning sessions: if the DB row has prior
                # usage (meaning at least one turn completed) and no explicit
                # resume was requested, enable SDK resume.  After re-keying,
                # conversation_id IS the SDK session ID the transcript lives under.
                if not resume_session_id and db_session.usage_output_tokens > 0:
                    session.resume_session_id = conversation_id
                    logger.info(
                        f"Auto-resume enabled for returning session {db_session.id} "
                        f"(output_tokens={db_session.usage_output_tokens}, "
                        f"sdk_id={conversation_id[:8]})"
                    )

                # Restore persisted state from DB (safe for both new and returning
                # sessions — new rows have defaults that won't clobber anything).
                if db_session.chat_mode and db_session.chat_mode != "plan":
                    session.chat_mode = db_session.chat_mode
                if db_session.usage_output_tokens:
                    session._accumulated_output_tokens = db_session.usage_output_tokens
                if db_session.approved_tools_json:
                    try:
                        session._approved_tools = set(json.loads(db_session.approved_tools_json))
                    except (ValueError, TypeError):
                        logger.debug("Malformed approved_tools_json, ignoring")
                logger.info(
                    f"Registered web-chat session {db_session.id} "
                    f"(conv={conversation_id[:8]}, project={project_id or PERSONAL_PROJECT_ID})"
                )
            except Exception as e:
                logger.warning(f"Failed to register web-chat session in DB: {e}")

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

            def _persist_approved_tools(tools: set[str]) -> None:
                try:
                    _sm.update_approved_tools(_db_sid, tools)
                except Exception:
                    logger.debug("Failed to persist approved_tools", exc_info=True)

            session._on_approved_tools_persist = _persist_approved_tools

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

        # Agent was already resolved at the top of the method for provider detection.
        # Use the cached agent_body to build the system prompt.
        if pending_agent:
            session._pending_agent_name = pending_agent

        if agent_body and session_manager:
            try:
                cli_source = "claude"
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
                    cli_source,
                )
                if skills_text:
                    context_parts.append(skills_text)
                if context_parts:
                    session.system_prompt_override = "\n\n".join(context_parts)
            except Exception as e:
                logger.warning(f"Failed to build agent system prompt for '{agent_name}': {e}")

        try:
            await session.start(model=model)
        except Exception:
            if session.resume_session_id:
                logger.warning(
                    f"SDK resume failed for {session.resume_session_id[:8]}, starting fresh"
                )
                session.resume_session_id = None
                await session.start(model=model)
            else:
                raise

        if session_manager and session.db_session_id:
            update_kwargs: dict[str, str] = {}
            runtime_external_id = _get_runtime_external_id(session)
            if runtime_external_id and runtime_external_id != conversation_id:
                update_kwargs["external_id"] = runtime_external_id

            runtime_transcript_path = _get_runtime_transcript_path(session)
            if runtime_transcript_path:
                update_kwargs["transcript_path"] = runtime_transcript_path

            if update_kwargs:
                try:
                    await asyncio.to_thread(
                        session_manager.update,
                        session.db_session_id,
                        **update_kwargs,
                    )
                except Exception:
                    logger.debug(
                        "Failed to persist runtime session metadata for web-chat session",
                        exc_info=True,
                    )

        if session_manager and session.db_session_id and session.model:
            try:
                await asyncio.to_thread(
                    session_manager.update_model,
                    session.db_session_id,
                    session.model,
                )
            except Exception:
                logger.debug("Failed to persist selected model for web-chat session", exc_info=True)

        self._chat_sessions[conversation_id] = session

        # History injection via message_manager removed (session_messages table dropped)

        # Fire SESSION_START (informational, fire-and-forget)
        start_data: dict[str, Any] = {}
        if pending_agent:
            start_data["agent_name_override"] = pending_agent

        def _log_session_start_error(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.warning(f"SESSION_START lifecycle hook failed: {exc}")

        t = asyncio.create_task(
            self._fire_lifecycle(conversation_id, HookEventType.SESSION_START, start_data)
        )
        t.add_done_callback(_log_session_start_error)

        # Broadcast authoritative mode to frontend so it can override local storage.
        # Skip if the mode came from a pending client set_mode — echoing it back
        # triggers a set_mode → mode_changed → set_mode feedback loop.
        if not pending_mode:
            mode_msg = json.dumps(
                {
                    "type": "mode_changed",
                    "conversation_id": conversation_id,
                    "mode": session.chat_mode,
                    "reason": "session_restored",
                }
            )
            for ws, meta in list(self.clients.items()):
                cid = meta.get("conversation_id") if meta else None
                if cid is not None and cid != conversation_id:
                    continue
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
            logger.debug(f"SESSION_END fire failed for {conversation_id[:8]}", exc_info=True)

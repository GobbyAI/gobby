"""Chat session lifecycle mixin."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.agents.sandbox import (
    web_chat_policy_mismatch_message,
    web_chat_sandbox_config,
    web_chat_sandbox_policy_hash,
)
from gobby.hooks.events import HookEvent, HookEventType
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.tool_approvals import normalize_approved_tool_keys
from gobby.servers.websocket.chat._lifecycle import _inject_agent_skills
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)

_CANCEL_YIELD_DELAY = 0.1


def _normalize_runtime_chat_mode(mode: str | None) -> str | None:
    if mode == "accept_edits":
        return "normal"
    return mode


def _normalize_web_chat_provider(provider: Any) -> str | None:
    """Normalize provider identifiers persisted on web-chat sessions."""
    if not isinstance(provider, str):
        return None

    normalized = provider.strip().lower()
    if normalized in {"", "inherit"}:
        return None
    if normalized in {"claude", "gemini", "qwen", "codex", "droid"}:
        return normalized
    return None


def _build_agent_identity_preamble(agent_body: Any) -> str | None:
    """Build the non-duplicated identity preamble for web-chat sessions.

    Gemini web chat sends instructions and skill manifests through the first
    BEFORE_AGENT lifecycle hook, so its session bootstrap should only carry
    stable identity fields. Other providers can still use the full prompt
    preamble plus skill manifests.
    """
    parts: list[str] = []
    if getattr(agent_body, "role", None):
        parts.append(f"## Role\n{agent_body.role}")
    if getattr(agent_body, "goal", None):
        parts.append(f"## Goal\n{agent_body.goal}")
    if getattr(agent_body, "personality", None):
        parts.append(f"## Personality\n{agent_body.personality}")
    return "\n\n".join(parts) if parts else None


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


def _is_bootstrap_external_id(external_id: str | None) -> bool:
    """Return True when external_id is still a temporary web-chat bootstrap value."""
    return bool(external_id and external_id.startswith("web-chat-bootstrap:"))


def _has_meaningful_web_chat_history(session: Any) -> bool:
    """Return True when a web-chat row already has meaningful runtime history."""
    return bool(
        getattr(session, "message_count", 0)
        or getattr(session, "turn_count", 0)
        or getattr(session, "usage_output_tokens", 0)
    )


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
    _pending_inject_contexts: dict[str, str]
    _session_create_locks: dict[str, asyncio.Lock]
    web_chat_session_registry: Any

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
        active_task = self._active_chat_tasks.pop(conversation_id, None)
        if session:
            if active_task and not active_task.done():
                try:
                    await asyncio.wait_for(session.interrupt(), timeout=0.5)
                except Exception as e:
                    logger.debug(f"Interrupt failed: {e}")

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
        reasoning_effort: str | None = None,
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
                if reasoning_effort is not None:
                    existing.reasoning_effort = reasoning_effort
                return existing
            return await self._create_chat_session_inner(
                conversation_id,
                model,
                project_id,
                resume_session_id,
                provider,
                reasoning_effort,
            )

    async def _create_chat_session_inner(
        self,
        conversation_id: str,
        model: str | None = None,
        project_id: str | None = None,
        resume_session_id: str | None = None,
        provider: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatSessionProtocol:
        """Inner implementation — must be called under the per-conversation lock from _session_create_locks."""
        session_key = conversation_id
        session_manager = getattr(self, "session_manager", None)
        existing_db_session = None
        existing_terminal_resume = False
        if session_manager:
            try:
                candidate = await asyncio.to_thread(session_manager.get, session_key)
                if candidate:
                    candidate_session_type = getattr(candidate, "session_type", None)
                    if candidate_session_type == "web_chat":
                        existing_db_session = candidate
                    elif candidate_session_type == "terminal" and resume_session_id:
                        existing_db_session = candidate
                        existing_terminal_resume = True
            except Exception as e:
                logger.debug(f"Failed to resolve existing chat session {session_key}: {e}")

        # Resolve any queued persona selection early so session bootstrap can
        # prepare persona-facing prompt context without treating the definition
        # as an autonomous-agent runtime config.
        pending_projects = getattr(self, "_pending_projects", {})
        pending_project = pending_projects.get(session_key)
        effective_project_for_agent = (
            project_id
            or pending_project
            or getattr(existing_db_session, "project_id", None)
            or PERSONAL_PROJECT_ID
        )

        daemon_cfg = getattr(self, "daemon_config", None)
        pending_providers = getattr(self, "_pending_providers", {})
        pending_provider = _normalize_web_chat_provider(pending_providers.pop(session_key, None))
        effective_provider = pending_provider
        if not effective_provider and existing_db_session:
            effective_provider = _normalize_web_chat_provider(
                getattr(existing_db_session, "source", None)
            )
        if not effective_provider:
            effective_provider = _normalize_web_chat_provider(provider)
        if not effective_provider and daemon_cfg is not None:
            chat_cfg = getattr(daemon_cfg, "chat", None)
            effective_provider = _normalize_web_chat_provider(getattr(chat_cfg, "provider", None))

        pending_agents = getattr(self, "_pending_agents", {})
        pending_agent = pending_agents.pop(session_key, None)
        agent_name = pending_agent or "default"
        agent_body = None
        persona_selected = False
        if session_manager:
            try:
                from gobby.workflows.agent_resolver import resolve_agent

                agent_body = await asyncio.to_thread(
                    resolve_agent,
                    agent_name,
                    session_manager.db,
                    cli_source=effective_provider or "claude",
                    project_id=effective_project_for_agent,
                )
                persona_selected = bool(
                    pending_agent
                    and pending_agent != "default"
                    and agent_body is not None
                    and agent_body.supports_surface("persona")
                )
            except Exception as e:
                logger.warning(f"Failed to resolve agent '{agent_name}' for session bootstrap: {e}")

        # Provider precedence: queued UI override > existing DB session source
        # > explicit message provider > default.
        #
        # Durable web-chat rows are the authoritative provider binding for an
        # existing conversation. A stale frontend provider selection should not
        # silently re-route a restored session onto a different backend.
        runtime_manager = getattr(self, "web_chat_runtime_manager", None)
        if runtime_manager is not None:
            current_web_chat_sandbox = runtime_manager.sandbox_config
            current_web_chat_policy_hash = runtime_manager.sandbox_policy_hash
        else:
            current_web_chat_sandbox = web_chat_sandbox_config(daemon_cfg)
            current_web_chat_policy_hash = web_chat_sandbox_policy_hash(daemon_cfg)
        current_web_chat_sandbox_enabled = bool(current_web_chat_sandbox.enabled)
        provider_name = effective_provider or "claude"
        session: ChatSessionProtocol
        if runtime_manager is not None:
            session = runtime_manager.create_session(
                provider=provider_name,
                conversation_id=conversation_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        else:
            if provider_name != "claude":
                raise RuntimeError(
                    f"Web chat provider '{provider_name}' requires the managed runtime backend"
                )
            session = ChatSession(conversation_id=conversation_id, provider=provider_name)
            session.reasoning_effort = reasoning_effort

        if reasoning_effort is not None:
            session.reasoning_effort = reasoning_effort

        if resume_session_id:
            session.resume_session_id = resume_session_id

        # Wire lifecycle callbacks before start() so hooks are registered with the SDK
        session._on_before_agent = lambda data: self._fire_lifecycle(
            session_key, HookEventType.BEFORE_AGENT, data
        )
        session._on_pre_tool = lambda data: self._fire_lifecycle(
            session_key, HookEventType.BEFORE_TOOL, data
        )
        session._on_post_tool = lambda data: self._fire_lifecycle(
            session_key, HookEventType.AFTER_TOOL, data
        )
        session._on_pre_compact = lambda data: self._fire_lifecycle(
            session_key, HookEventType.PRE_COMPACT, data
        )
        session._on_stop = lambda data: self._fire_lifecycle(session_key, HookEventType.STOP, data)

        # Wire mode-change callback so agent-initiated plan mode transitions
        # (EnterPlanMode/ExitPlanMode) are broadcast to conversation clients only
        async def _notify_mode_changed(mode: str, reason: str) -> None:
            msg = json.dumps(
                {
                    "type": "mode_changed",
                    "conversation_id": session_key,
                    "mode": mode,
                    "reason": reason,
                }
            )
            for ws, meta in list(self.clients.items()):
                # Only send to clients in this conversation (or untracked clients for compat)
                cid = meta.get("conversation_id") if meta else None
                if cid is not None and cid != session_key:
                    continue
                try:
                    await ws.send(msg)
                except (ConnectionClosed, ConnectionClosedError):
                    pass

        session._on_mode_changed = _notify_mode_changed

        # Wire plan-ready callback so ExitPlanMode sends plan content to frontend
        async def _notify_plan_ready(content: str | None, input_data: dict[str, Any]) -> None:
            session._pending_plan_content = content
            allowed_prompts = input_data.get("allowedPrompts")
            session._pending_plan_allowed_prompts = (
                list(allowed_prompts)
                if isinstance(allowed_prompts, list)
                and all(isinstance(prompt, str) for prompt in allowed_prompts)
                else None
            )
            msg = json.dumps(
                {
                    "type": "plan_pending_approval",
                    "conversation_id": session_key,
                    "plan_content": content,
                    "allowed_prompts": session._pending_plan_allowed_prompts,
                }
            )
            for ws, meta in list(self.clients.items()):
                cid = meta.get("conversation_id") if meta else None
                if cid is not None and cid != session_key:
                    continue
                try:
                    await ws.send(msg)
                except (ConnectionClosed, ConnectionClosedError):
                    pass

        session._on_plan_ready = _notify_plan_ready

        # Wire config from daemon
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
                session.chat_mode = _normalize_runtime_chat_mode(chat_cfg.default_mode) or "plan"

        # Set project context on session BEFORE start() so env vars and CWD
        # are correctly configured for the CLI subprocess.
        # Precedence: explicit message project_id > pending from set_project > fallback
        pending_project = pending_projects.pop(session_key, None)
        effective_pid = (
            project_id
            or pending_project
            or getattr(existing_db_session, "project_id", None)
            or PERSONAL_PROJECT_ID
        )
        session.project_id = effective_pid

        if existing_terminal_resume and existing_db_session and session_manager:
            try:
                normalized_session = await asyncio.to_thread(
                    session_manager.update,
                    existing_db_session.id,
                    source=provider_name,
                    model=model,
                    project_id=effective_pid,
                    session_type="web_chat",
                    status="active",
                    terminal_context={},
                    sandbox_enabled=current_web_chat_sandbox_enabled,
                    sandbox_policy_hash=current_web_chat_policy_hash,
                )
                if normalized_session is not None:
                    existing_db_session = normalized_session
                logger.info(
                    "Converted resumed terminal session %s into durable web-chat row",
                    existing_db_session.id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to normalize resumed terminal session %s for web chat: %s",
                    existing_db_session.id,
                    e,
                )

        if existing_db_session and getattr(existing_db_session, "session_type", None) == "web_chat":
            mismatch_reason = None
            stored_policy_hash = getattr(existing_db_session, "sandbox_policy_hash", None)
            if (
                isinstance(stored_policy_hash, str)
                and stored_policy_hash
                and stored_policy_hash != current_web_chat_policy_hash
            ):
                mismatch_reason = web_chat_policy_mismatch_message()

            stored_sandbox_enabled = getattr(existing_db_session, "sandbox_enabled", None)
            if (
                mismatch_reason is None
                and isinstance(stored_sandbox_enabled, bool)
                and stored_sandbox_enabled != current_web_chat_sandbox_enabled
            ):
                mismatch_reason = web_chat_policy_mismatch_message()

            if mismatch_reason and runtime_manager is not None:
                runtime_mismatch_reason = runtime_manager.policy_mismatch_reason(
                    existing_db_session
                )
                if isinstance(runtime_mismatch_reason, str) and runtime_mismatch_reason:
                    mismatch_reason = runtime_mismatch_reason

            if mismatch_reason:
                if _has_meaningful_web_chat_history(existing_db_session):
                    raise RuntimeError(mismatch_reason)
                if session_manager:
                    try:
                        migrated = await asyncio.to_thread(
                            session_manager.update,
                            existing_db_session.id,
                            sandbox_enabled=current_web_chat_sandbox_enabled,
                            sandbox_policy_hash=current_web_chat_policy_hash,
                        )
                        if migrated is not None:
                            existing_db_session = migrated
                    except Exception:
                        logger.debug(
                            "Failed to migrate empty web-chat session to current sandbox policy",
                            exc_info=True,
                        )

        # Bind to the durable web-chat DB row if one already exists. Terminal
        # resumes also reuse their existing row and normalize it to web_chat
        # in place so resume cannot mint a duplicate web-chat identity.
        if existing_db_session:
            session.db_session_id = existing_db_session.id
            session.seq_num = existing_db_session.seq_num
            session._session_manager_ref = session_manager

            if (
                not resume_session_id
                and existing_db_session.usage_output_tokens > 0
                and existing_db_session.external_id
                and not _is_bootstrap_external_id(existing_db_session.external_id)
                and existing_db_session.source == provider_name
            ):
                session.resume_session_id = existing_db_session.external_id

            runtime_mode = _normalize_runtime_chat_mode(existing_db_session.chat_mode)
            if runtime_mode and runtime_mode != "plan":
                session.chat_mode = runtime_mode
            if existing_db_session.usage_output_tokens:
                session._accumulated_output_tokens = existing_db_session.usage_output_tokens
                if existing_db_session.approved_tools_json:
                    try:
                        session._approved_tools = normalize_approved_tool_keys(
                            json.loads(existing_db_session.approved_tools_json)
                        )
                    except (ValueError, TypeError):
                        logger.debug("Malformed approved_tools_json, ignoring")

            logger.info(
                f"Hydrated web-chat session {existing_db_session.id} "
                f"(source={existing_db_session.source}, project={effective_pid})"
            )
        elif session_manager:
            try:
                db_session = await asyncio.to_thread(
                    session_manager.register,
                    external_id=session_key,
                    machine_id=get_machine_id(),
                    source=effective_provider or "claude",
                    project_id=effective_pid,
                    session_type="web_chat",
                    sandbox_enabled=current_web_chat_sandbox_enabled,
                    sandbox_policy_hash=current_web_chat_policy_hash,
                )
                session.db_session_id = db_session.id
                session.seq_num = db_session.seq_num
                session._session_manager_ref = session_manager

                # Compatibility path for callers that still lazily create web
                # chats without pre-creating the DB row first.
                resume_identity = getattr(db_session, "external_id", None)
                if not isinstance(resume_identity, str) or not resume_identity:
                    resume_identity = session_key
                if (
                    not resume_session_id
                    and db_session.usage_output_tokens > 0
                    and not _is_bootstrap_external_id(resume_identity)
                ):
                    session.resume_session_id = resume_identity
                    logger.info(
                        f"Auto-resume enabled for returning session {db_session.id} "
                        f"(output_tokens={db_session.usage_output_tokens}, "
                        f"sdk_id={resume_identity[:8]})"
                    )

                # Restore persisted state from DB (safe for both new and returning
                # sessions — new rows have defaults that won't clobber anything).
                runtime_mode = _normalize_runtime_chat_mode(db_session.chat_mode)
                if runtime_mode and runtime_mode != "plan":
                    session.chat_mode = runtime_mode
                if db_session.usage_output_tokens:
                    session._accumulated_output_tokens = db_session.usage_output_tokens
                if db_session.approved_tools_json:
                    try:
                        session._approved_tools = normalize_approved_tool_keys(
                            json.loads(db_session.approved_tools_json)
                        )
                    except (ValueError, TypeError):
                        logger.debug("Malformed approved_tools_json, ignoring")
                logger.info(
                    f"Registered web-chat session {db_session.id} "
                    f"(key={session_key[:8]}, project={effective_pid})"
                )
            except Exception as e:
                logger.warning(f"Failed to register web-chat session in DB: {e}")

        # Override with pending mode (highest priority — user toggled before session existed)
        pending_modes = getattr(self, "_pending_modes", {})
        pending_mode = pending_modes.pop(session_key, None)
        if pending_mode:
            session.chat_mode = _normalize_runtime_chat_mode(pending_mode) or pending_mode

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
        wt_override = pending_wt.pop(session_key, None)
        if wt_override:
            session.project_path = wt_override

        # Persona / agent prompt bootstrap.
        session._pending_agent_name = agent_name

        if agent_body and session_manager:
            try:
                cli_source = effective_provider or "claude"
                if persona_selected:
                    from gobby.mcp_proxy.tools.apply_persona import build_session_persona_context

                    persona_context, _ = await asyncio.to_thread(
                        build_session_persona_context,
                        agent_body,
                        session_manager.db,
                        cli_source=cli_source,
                        identity_only=True,
                    )
                    if persona_context:
                        session.system_prompt_override = persona_context
                else:
                    context_parts: list[str] = []
                    if effective_provider in {"gemini", "qwen"}:
                        preamble = _build_agent_identity_preamble(agent_body)
                    else:
                        preamble = agent_body.build_prompt_preamble()
                    if preamble:
                        context_parts.append(preamble)
                    # Gemini/Qwen web chat defer instructions + skill manifests to BEFORE_AGENT
                    # so the first prompt does not duplicate context blocks.
                    if effective_provider not in {"gemini", "qwen"}:
                        skills_text = await asyncio.to_thread(
                            _inject_agent_skills,
                            agent_body,
                            session_manager.db,
                            effective_pid,
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
            if runtime_external_id and runtime_external_id != session_key:
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

        if persona_selected and session_manager and session.db_session_id:
            try:
                from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

                await apply_persona_impl(
                    agent=agent_name,
                    db=session_manager.db,
                    session_id=session.db_session_id,
                    cli_source=provider_name,
                )
            except Exception as e:
                logger.warning(
                    "Failed to apply persona '%s' to session %s: %s", agent_name, session_key, e
                )

        registry = getattr(self, "web_chat_session_registry", None)
        if registry is not None:
            registry.register(session_key, session)
        else:
            self._chat_sessions[session_key] = session

        # Fire SESSION_START (informational, fire-and-forget)
        start_data: dict[str, Any] = {}
        if persona_selected:
            start_data["skip_default_agent_activation"] = True
        elif pending_agent:
            start_data["agent_name_override"] = pending_agent

        def _log_session_start_error(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.warning(f"SESSION_START lifecycle hook failed: {exc}")

        t = asyncio.create_task(
            self._fire_lifecycle(session_key, HookEventType.SESSION_START, start_data)
        )
        t.add_done_callback(_log_session_start_error)

        # Broadcast authoritative mode to frontend so it can override local storage.
        # Skip if the mode came from a pending client set_mode — echoing it back
        # triggers a set_mode → mode_changed → set_mode feedback loop.
        if not pending_mode:
            mode_msg = json.dumps(
                {
                    "type": "mode_changed",
                    "conversation_id": session_key,
                    "mode": session.chat_mode,
                    "reason": "session_restored",
                }
            )
            for ws, meta in list(self.clients.items()):
                cid = meta.get("conversation_id") if meta else None
                if cid is not None and cid != session_key:
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

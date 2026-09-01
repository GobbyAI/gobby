"""Chat session lifecycle mixin."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.sandbox import (
    web_chat_policy_mismatch_message,
    web_chat_sandbox_policy_hash,
)
from gobby.hooks.event_handlers._session_start.claims import preserve_task_claim_state
from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.hook_types import SessionEndReason
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.tool_approvals import normalize_approved_tool_keys
from gobby.servers.websocket.db import run_db
from gobby.sessions.clear_continuation import commit_web_chat_clear_successor
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.machine_id import get_machine_id
from gobby.workflows.state_manager import SessionVariableManager

from ._clear_commit import rebind_live_clear_successor
from ._reattach import redirect_terminal_web_chat_candidate
from ._session_binding import (
    _first_configured_chat_binding,
    _normalize_runtime_chat_mode,
    _normalize_web_chat_provider,
    _resolve_web_chat_reasoning,
)
from ._session_launch import (
    SessionLaunchContext,
    bind_session_lifecycle,
    snapshot_from_session,
    start_hydrated_session,
)
from ._session_runtime import (
    _has_meaningful_web_chat_history,
    _is_bootstrap_external_id,
)

logger = logging.getLogger(__name__)

_CANCEL_YIELD_DELAY = 0.1


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
    _pending_config_updated_at: dict[str, datetime]
    _pending_inject_contexts: dict[str, str]
    config_runtime: Any | None
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
            *,
            pending_message_ids: list[str] | None = None,
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
                    logger.debug("Interrupt failed: %s", e)

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
                    existing_provider = (
                        _normalize_web_chat_provider(getattr(existing, "provider", None))
                        or "claude"
                    )
                    existing_model = getattr(existing, "model", None)
                    if not isinstance(existing_model, str):
                        existing_model = None
                    existing.reasoning_effort = _resolve_web_chat_reasoning(
                        existing_provider,
                        existing_model,
                        reasoning_effort,
                    )
                return existing
            return await self._create_chat_session_inner(
                conversation_id,
                model,
                project_id,
                resume_session_id,
                provider,
                reasoning_effort,
            )

    def resolve_chat_binding(
        self,
        conversation_id: str,
        *,
        provider: str | None,
        model: str | None,
    ) -> tuple[str, str | None]:
        """Resolve the provider/model serving an existing or future chat session."""
        configured_binding = _first_configured_chat_binding(getattr(self, "daemon_config", None))
        existing = self._chat_sessions.get(conversation_id)
        if existing is not None:
            existing_provider = _normalize_web_chat_provider(getattr(existing, "provider", None))
            existing_model = getattr(existing, "model", None)
            if not isinstance(existing_model, str) or not existing_model:
                existing_model = None
            if (
                existing_model is None
                and configured_binding is not None
                and configured_binding[0] == existing_provider
            ):
                existing_model = configured_binding[1]
            return existing_provider or "claude", existing_model

        effective_provider = _normalize_web_chat_provider(provider)
        effective_model = model.strip() if isinstance(model, str) and model.strip() else None
        if effective_provider is None and configured_binding is not None:
            effective_provider = configured_binding[0]
            if effective_model is None:
                effective_model = configured_binding[1]
        return effective_provider or "claude", effective_model

    async def commit_clear_successor(
        self,
        *,
        conversation_id: str,
        session: ChatSessionProtocol,
        predecessor_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Expire the predecessor and rebind the live wrapper to a force-new successor."""
        session_manager = getattr(self, "session_manager", None)
        db = getattr(session_manager, "db", None)
        if db is None:
            return {"ok": False, "reason": "session manager is not bound"}
        try:
            successor = await run_db(
                self,
                commit_web_chat_clear_successor,
                db,
                predecessor_id,
                attempt_id=attempt_id,
            )
        except Exception:
            logger.exception(
                "Web-chat clear commit failed for predecessor %s",
                predecessor_id,
            )
            return {"ok": False, "reason": "clear successor commit failed"}
        if successor is None:
            return {"ok": False, "reason": "clear successor commit returned no row"}
        # Fan out while the live wrapper still resolves to the predecessor: the
        # SESSION_END handler reads db_session_id from the wrapper.
        await self._fire_session_end(conversation_id, reason=SessionEndReason.CLEAR)
        await rebind_live_clear_successor(self, session, successor)
        self._transfer_clear_claims(predecessor_id, successor.id)
        return {
            "ok": True,
            "successor_id": successor.id,
            "predecessor_id": predecessor_id,
            "seq_num": successor.seq_num,
        }

    def _transfer_clear_claims(self, predecessor_id: str, successor_id: str) -> None:
        handler = getattr(self, "event_handlers", None)
        session_manager = getattr(self, "session_manager", None)
        db = getattr(session_manager, "db", None)
        if handler is None or session_manager is None or db is None:
            return
        sv_mgr = SessionVariableManager(db)
        predecessor_vars = sv_mgr.get_variables(predecessor_id)
        preserve_task_claim_state(
            handler,
            sv_mgr,
            successor_id,
            predecessor_id,
            predecessor_vars,
        )
        predecessor = session_manager.get(predecessor_id)
        if predecessor is not None:
            from gobby.sessions.title_lifecycle import apply_clear_successor_title

            apply_clear_successor_title(session_manager, successor_id, predecessor)

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
                candidate = await run_db(self, session_manager.get, session_key)
                if candidate:
                    candidate = await run_db(
                        self,
                        redirect_terminal_web_chat_candidate,
                        candidate,
                        session_manager,
                    )
                    candidate_session_type = getattr(candidate, "session_type", None)
                    if candidate_session_type == "web_chat":
                        existing_db_session = candidate
                    elif candidate_session_type == "terminal" and resume_session_id:
                        existing_db_session = candidate
                        existing_terminal_resume = True
            except Exception as e:
                logger.debug("Failed to resolve existing chat session %s: %s", session_key, e)

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
        effective_model = model
        effective_reasoning_effort = reasoning_effort
        if not effective_provider and existing_db_session:
            effective_provider = _normalize_web_chat_provider(
                getattr(existing_db_session, "source", None)
            )
        if not effective_provider:
            effective_provider = _normalize_web_chat_provider(provider)
        if not effective_provider and daemon_cfg is not None:
            configured_binding = _first_configured_chat_binding(daemon_cfg)
            if configured_binding is not None:
                effective_provider, candidate_model, candidate_reasoning = configured_binding
                if effective_model is None:
                    effective_model = candidate_model
                if effective_reasoning_effort is None:
                    effective_reasoning_effort = candidate_reasoning

        pending_agents = getattr(self, "_pending_agents", {})
        pending_agent = pending_agents.pop(session_key, None)
        agent_name = pending_agent or "default"
        agent_body = None
        persona_selected = False
        persona_surface_error: str | None = None
        if session_manager:
            try:
                from gobby.workflows.agent_resolver import resolve_agent

                agent_body = await run_db(
                    self,
                    resolve_agent,
                    agent_name,
                    session_manager.db,
                    cli_source=effective_provider or "claude",
                    project_id=effective_project_for_agent,
                )
                if pending_agent and pending_agent != "default" and agent_body is not None:
                    try:
                        persona_selected = bool(agent_body.supports_surface("persona"))
                        if not persona_selected:
                            persona_surface_error = (
                                f"Agent definition '{agent_name}' does not support "
                                "the 'persona' surface"
                            )
                    except Exception:
                        logger.warning(
                            "Agent '%s' failed persona surface validation during session bootstrap",
                            agent_name,
                            exc_info=True,
                        )
            except Exception as e:
                logger.warning(
                    "Failed to resolve agent '%s' for session bootstrap: %s", agent_name, e
                )
        if persona_surface_error:
            raise ValueError(persona_surface_error)

        # Provider precedence: queued UI override > existing DB session source
        # > explicit message provider > default.
        #
        # Durable web-chat rows are the authoritative provider binding for an
        # existing conversation. A stale frontend provider selection should not
        # silently re-route a restored session onto a different backend.
        runtime_manager = getattr(self, "web_chat_runtime_manager", None)
        provider_name = effective_provider or "claude"
        effective_reasoning_effort = _resolve_web_chat_reasoning(
            provider_name,
            effective_model,
            effective_reasoning_effort,
        )
        session: ChatSessionProtocol
        if runtime_manager is not None:
            created = runtime_manager.create_session(
                provider=provider_name,
                conversation_id=conversation_id,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
            )
            session = await created if inspect.isawaitable(created) else created
            current_web_chat_policy_hash = (
                session.sandbox_policy_hash or runtime_manager.sandbox_policy_hash
            )
        else:
            if provider_name != "claude":
                raise RuntimeError(
                    f"Web chat provider '{provider_name}' requires the managed runtime backend"
                )
            session = ChatSession(conversation_id=conversation_id, provider=provider_name)
            current_web_chat_policy_hash = web_chat_sandbox_policy_hash(daemon_cfg)
            session.sandbox_policy_hash = current_web_chat_policy_hash
        session._config_runtime_ref = self.config_runtime

        if effective_reasoning_effort is not None:
            session.reasoning_effort = effective_reasoning_effort

        if resume_session_id:
            session.resume_session_id = resume_session_id

        bind_session_lifecycle(self, session, session_key)

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
        project_context_changed = bool(
            existing_db_session
            and project_id
            and getattr(existing_db_session, "project_id", None) != effective_pid
        )
        session.project_id = effective_pid

        if existing_terminal_resume and existing_db_session and session_manager:
            normalized_session = await run_db(
                self,
                session_manager.continue_terminal_session_as_web_chat,
                existing_db_session.id,
                source=provider_name,
                model=model,
                project_id=effective_pid,
                sandbox_policy_hash=current_web_chat_policy_hash,
            )
            if normalized_session is None:
                raise RuntimeError(
                    f"Terminal session {existing_db_session.id} is ineligible for web continuation"
                )
            existing_db_session = normalized_session
            logger.info(
                "Converted resumed terminal session %s into durable web-chat row",
                existing_db_session.id,
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
                        migrated = await run_db(
                            self,
                            session_manager.update,
                            existing_db_session.id,
                            sandbox_enabled=False,
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
                and not project_context_changed
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
                session.set_accumulated_output_tokens(existing_db_session.usage_output_tokens)
                if existing_db_session.approved_tools_json:
                    try:
                        session._approved_tools = normalize_approved_tool_keys(
                            json.loads(existing_db_session.approved_tools_json)
                        )
                    except (ValueError, TypeError):
                        logger.debug("Malformed approved_tools_json, ignoring")

            logger.info(
                "Hydrated web-chat session %s (source=%s, project=%s)",
                existing_db_session.id,
                existing_db_session.source,
                effective_pid,
            )
        elif session_manager:
            try:
                db_session = await run_db(
                    self,
                    session_manager.register,
                    external_id=session_key,
                    machine_id=get_machine_id(),
                    source=effective_provider or "claude",
                    project_id=effective_pid,
                    session_type="web_chat",
                    sandbox_enabled=False,
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
                        "Auto-resume enabled for returning session %s (output_tokens=%s, sdk_id=%s)",
                        db_session.id,
                        db_session.usage_output_tokens,
                        resume_identity[:8],
                    )

                # Restore persisted state from DB (safe for both new and returning
                # sessions — new rows have defaults that won't clobber anything).
                runtime_mode = _normalize_runtime_chat_mode(db_session.chat_mode)
                if runtime_mode and runtime_mode != "plan":
                    session.chat_mode = runtime_mode
                if db_session.usage_output_tokens:
                    session.set_accumulated_output_tokens(db_session.usage_output_tokens)
                if db_session.approved_tools_json:
                    try:
                        session._approved_tools = normalize_approved_tool_keys(
                            json.loads(db_session.approved_tools_json)
                        )
                    except (ValueError, TypeError):
                        logger.debug("Malformed approved_tools_json, ignoring")
                logger.info(
                    "Registered web-chat session %s (key=%s, project=%s)",
                    db_session.id,
                    session_key[:8],
                    effective_pid,
                )
            except Exception as e:
                logger.warning("Failed to register web-chat session in DB: %s", e)

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

        # Look up the session-machine checkout so the subprocess CWD matches.
        if session_manager and not session.project_path:
            try:
                from gobby.servers.websocket.chat._session_checkout import (
                    resolve_chat_session_project_path,
                )

                session_machine_id = getattr(existing_db_session, "machine_id", None)
                checkout_root = resolve_chat_session_project_path(
                    session_manager.db, effective_pid, session_machine_id
                )
                if checkout_root:
                    session.project_path = checkout_root
            except Exception as e:
                logger.warning("Failed to look up project checkout: %s", e)

        # Override project_path with pending worktree path (from set_worktree)
        pending_wt = getattr(self, "_pending_worktree_paths", {})
        wt_override = pending_wt.pop(session_key, None)
        if wt_override:
            session.project_path = wt_override
        getattr(self, "_pending_config_updated_at", {}).pop(session_key, None)

        # Persona / agent prompt bootstrap.
        session._pending_agent_name = agent_name

        if agent_body and session_manager:
            try:
                cli_source = effective_provider or "claude"
                if persona_selected:
                    from gobby.mcp_proxy.tools.apply_persona import build_session_persona_context

                    persona_context, _ = await run_db(
                        self,
                        build_session_persona_context,
                        agent_body,
                        session_manager.db,
                        cli_source=cli_source,
                    )
                    if persona_context:
                        session.system_prompt_override = persona_context
            except Exception as e:
                logger.warning("Failed to build agent system prompt for '%s': %s", agent_name, e)

        launch_context = SessionLaunchContext(
            sandbox=snapshot_from_session(session, current_web_chat_policy_hash),
            workspace_path=session.project_path or ".",
        )
        return await start_hydrated_session(
            self,
            session,
            launch_context,
            session_key=session_key,
            effective_model=effective_model,
            persona_selected=persona_selected,
            pending_agent=pending_agent,
            pending_mode=pending_mode,
            agent_name=agent_name,
            provider_name=provider_name,
            session_manager=session_manager,
            existing_db_session=existing_db_session,
            project_context_changed=project_context_changed,
            effective_pid=effective_pid,
        )

    async def _fire_session_end(
        self,
        conversation_id: str,
        *,
        reason: SessionEndReason | None = None,
    ) -> None:
        """Fire SESSION_END event for a chat session (best-effort).

        Called before session cleanup in clear, delete, idle cleanup, and
        server shutdown paths to maintain parity with CLI adapters.
        """
        try:
            data = {"reason": reason.value} if reason is not None else {}
            await self._fire_lifecycle(conversation_id, HookEventType.SESSION_END, data)
        except Exception:
            logger.debug("SESSION_END fire failed for %s", conversation_id[:8], exc_info=True)

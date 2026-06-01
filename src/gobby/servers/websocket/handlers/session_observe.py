"""Session observation handlers for WebSocket session control.

Handles continue_in_chat, attach_to_session, detach_from_session,
send_to_cli_session, and the resume-blocked check.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard, cast
from uuid import uuid4

from gobby.agents.sandbox import web_chat_sandbox_config, web_chat_sandbox_policy_hash
from gobby.servers.websocket.attachments import append_attachment_paths, store_proxy_attachments
from gobby.servers.websocket.chat_attachments import (
    append_prepared_attachment_context,
    partition_attachment_items,
    prepare_message_attachments,
)
from gobby.servers.websocket.db import run_db
from gobby.sessions.context_usage import effective_context_window_for_session
from gobby.sessions.terminal_kill import kill_terminal_session
from gobby.sessions.tmux_context import get_tmux_manager_for_context
from gobby.sessions.transcript_archive import restore_transcript

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)

_POST_KILL_SETTLE_SECONDS = 0.5
_VALID_FALLBACK_CONTEXTS = {"auto", "summary", "digest", "none"}


def _is_terminal_session(session: Any) -> bool:
    """Return True when a session supports live attach/proxy semantics."""
    return getattr(session, "session_type", None) == "terminal"


def _as_str(value: Any) -> str | None:
    """Return a JSON-safe string or None."""
    return value if isinstance(value, str) else None


def _normalize_optional_markdown(value: str | None) -> str | None:
    """Return original markdown text unless it is missing or whitespace-only."""
    if not value or not value.strip():
        return None
    return value


def _as_int(value: Any, default: int | None = None) -> int | None:
    """Return a JSON-safe int or the provided default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError:
            return default
    return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Return a JSON-safe float or the provided default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return float(stripped)
        except ValueError:
            return default
    return default


def _mode_from_level(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return {0: "plan", 1: "normal", 2: "bypass"}.get(value)
    return None


def _variable_value[T](
    variables: dict[str, Any],
    predicate: Callable[[Any], TypeGuard[T]],
    *names: str,
) -> T | None:
    for name in names:
        value = variables.get(name)
        if predicate(value):
            return value
    return None


def _is_nonempty_str(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _variable_str(variables: dict[str, Any], *names: str) -> str | None:
    return _variable_value(variables, _is_nonempty_str, *names)


def _read_session_variables(db: Any, session_id: str) -> dict[str, Any]:
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db).get_variables(session_id)
    return variables if isinstance(variables, dict) else {}


async def _load_live_session_variables(
    mixin: SessionControlMixin,
    session_manager: Any,
    session_id: str,
) -> dict[str, Any]:
    db = getattr(session_manager, "db", None) or getattr(mixin, "db", None)
    if db is None:
        return {}
    try:
        variables = await run_db(mixin, _read_session_variables, db, session_id)
        return variables if isinstance(variables, dict) else {}
    except Exception as exc:
        logger.debug("Failed to read live session variables for %s: %s", session_id, exc)
        return {}


def _has_terminal_liveness(terminal_context: Any) -> bool:
    """Return True when terminal metadata indicates a live tmux-backed session."""
    if not isinstance(terminal_context, dict):
        return False

    tmux_pane = terminal_context.get("tmux_pane")
    if isinstance(tmux_pane, str) and tmux_pane:
        return True

    parent_pid = terminal_context.get("parent_pid")
    if isinstance(parent_pid, int) and not isinstance(parent_pid, bool):
        return parent_pid > 0
    if isinstance(parent_pid, str):
        try:
            return int(parent_pid) > 0
        except ValueError:
            return False

    return False


def _can_proxy_attach_session(session: Any) -> bool:
    """Return True when a terminal session is eligible for interactive proxy attach."""
    if getattr(session, "session_type", None) != "terminal":
        return False

    explicit = getattr(session, "can_proxy_attach", None)
    if isinstance(explicit, bool):
        return explicit

    if getattr(session, "status", None) == "active":
        return True

    return _has_terminal_liveness(getattr(session, "terminal_context", None))


def _session_meta_payload(
    session: Any,
    *,
    variables: dict[str, Any],
    agent_name: str | None,
    workflow_name: str | None,
    agent_run_id: str | None,
    context_window: int | None = None,
) -> dict[str, Any]:
    seq_num = _as_int(getattr(session, "seq_num", None))
    live_chat_mode = (
        _variable_str(variables, "chat_mode")
        or _mode_from_level(variables.get("mode_level"))
        or _as_str(getattr(session, "chat_mode", None))
    )
    live_reasoning_effort = _variable_str(
        variables,
        "reasoning_effort",
        "_effective_reasoning_effort",
        "_requested_reasoning_effort",
    ) or _as_str(getattr(session, "reasoning_effort", None))
    live_model = _variable_str(
        variables,
        "model",
        "model_id",
        "modelId",
    ) or _as_str(getattr(session, "model", None))
    live_context_window = context_window

    return {
        "external_id": _as_str(getattr(session, "external_id", None)),
        "source": _as_str(getattr(session, "source", None)) or "unknown",
        "title": _as_str(getattr(session, "title", None)),
        "status": _as_str(getattr(session, "status", None)) or "unknown",
        "model": live_model,
        "reasoning_effort": live_reasoning_effort,
        "ref": f"#{seq_num}" if seq_num is not None else None,
        "chat_mode": live_chat_mode,
        "git_branch": _as_str(getattr(session, "git_branch", None)),
        "context_window": live_context_window,
        "session_type": _as_str(getattr(session, "session_type", None)),
        "can_proxy_attach": _can_proxy_attach_session(session),
        "workflow_name": workflow_name,
        "agent_run_id": agent_run_id,
        "agent_name": agent_name,
        "context_used_tokens": _as_int(getattr(session, "context_used_tokens", None)),
        "context_usage_ratio": _as_float(getattr(session, "context_usage_ratio", None)),
        "context_usage_source": _as_str(getattr(session, "context_usage_source", None)),
        "context_usage_confidence": _as_str(
            getattr(session, "context_usage_confidence", None),
        ),
        "last_prompt_input_tokens": _as_int(
            getattr(session, "last_prompt_input_tokens", None),
        ),
        "last_prompt_uncached_input_tokens": _as_int(
            getattr(session, "last_prompt_uncached_input_tokens", None),
        ),
        "last_prompt_cache_read_tokens": _as_int(
            getattr(session, "last_prompt_cache_read_tokens", None),
        ),
        "last_prompt_cache_creation_tokens": _as_int(
            getattr(session, "last_prompt_cache_creation_tokens", None),
        ),
        "last_completion_output_tokens": _as_int(
            getattr(session, "last_completion_output_tokens", None),
        ),
        "usage_input_tokens": _as_int(getattr(session, "usage_input_tokens", 0), 0),
        "usage_output_tokens": _as_int(getattr(session, "usage_output_tokens", 0), 0),
        "usage_cache_read_tokens": _as_int(getattr(session, "usage_cache_read_tokens", 0), 0),
        "usage_cache_creation_tokens": _as_int(
            getattr(session, "usage_cache_creation_tokens", 0),
            0,
        ),
    }


async def _release_source_session(
    mixin: SessionControlMixin,
    source_session_id: str,
    source_session: Any,
) -> None:
    """Stop the active terminal/agent runtime before resuming in web chat."""
    killed = False
    session_manager = getattr(mixin, "session_manager", None)

    try:
        from gobby.agents.kill import kill_agent
        from gobby.storage.agents import LocalAgentRunManager

        if session_manager:
            arm = LocalAgentRunManager(session_manager.db)
            run = arm.get_by_session(source_session_id)
            if run:
                logger.info("Killing agent %s before resume", run.id)
                await kill_agent(run, session_manager.db, close_terminal=True)
                killed = True
                await asyncio.sleep(_POST_KILL_SETTLE_SECONDS)
    except Exception as exc:
        logger.warning("Failed to kill running agent before resume: %s", exc)

    if killed:
        return

    terminal_ctx = getattr(source_session, "terminal_context", None)
    if not terminal_ctx:
        return

    try:
        term_killed = await kill_terminal_session(terminal_ctx, source_session_id)
    except Exception as exc:
        logger.warning("Failed to kill terminal session before resume: %s", exc)
        return

    if term_killed:
        await asyncio.sleep(_POST_KILL_SETTLE_SECONDS)


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


def _resolve_requested_fallback_context(data: dict[str, Any]) -> str:
    """Return the requested fallback context mode, defaulting to auto."""
    requested = _as_str(data.get("fallback_context")) or _as_str(data.get("fallbackContext"))
    if requested in _VALID_FALLBACK_CONTEXTS:
        return requested
    return "auto"


def _resolve_fallback_inject_context(source_session: Any, requested_mode: str) -> str | None:
    """Choose hidden resume context based on the requested fallback mode."""
    if requested_mode == "none" or not source_session:
        return None

    summary_markdown = _normalize_optional_markdown(
        _as_str(getattr(source_session, "summary_markdown", None))
    )
    digest_markdown = _normalize_optional_markdown(
        _as_str(getattr(source_session, "digest_markdown", None))
    )

    if requested_mode == "summary":
        return summary_markdown or digest_markdown
    if requested_mode == "digest":
        return digest_markdown

    return summary_markdown or digest_markdown


async def handle_continue_in_chat(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle continue_in_chat message to resume a CLI session in the web chat UI.

    Attempts SDK native resume first (picks up exact conversation state).
    Falls back to hidden summary/digest context injection when native resume
    is unavailable and fallback context is configured.

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

    requested_conversation_id = data.get("conversation_id") or str(uuid4())
    conversation_id = requested_conversation_id
    project_id = data.get("project_id")
    target_provider = data.get("provider")
    target_model = data.get("model")
    target_reasoning_effort = _as_str(data.get("reasoning_effort"))
    target_chat_mode = _as_str(data.get("chat_mode"))
    requested_fallback_context = _resolve_requested_fallback_context(data)

    # Look up source session for project_id and SDK session ID
    session_manager = getattr(mixin, "session_manager", None)
    source_session = None
    if session_manager:
        try:
            source_session = await run_db(mixin, session_manager.get, source_session_id)
            if source_session and not project_id:
                project_id = source_session.project_id
        except Exception as e:
            logger.warning(f"Failed to look up source session {source_session_id}: {e}")

    resume_in_place = bool(source_session and _is_terminal_session(source_session))
    if resume_in_place:
        # Resuming a tmux session preserves the same durable session identity.
        conversation_id = source_session_id

    runtime_manager = getattr(mixin, "web_chat_runtime_manager", None)
    daemon_config = getattr(mixin, "daemon_config", None)
    if runtime_manager is not None:
        current_web_chat_sandbox_enabled = bool(runtime_manager.sandbox_config.enabled)
        current_web_chat_policy_hash = runtime_manager.sandbox_policy_hash
    else:
        current_web_chat_sandbox_enabled = bool(web_chat_sandbox_config(daemon_config).enabled)
        current_web_chat_policy_hash = web_chat_sandbox_policy_hash(daemon_config)

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

    source_provider = _as_str(getattr(source_session, "source", None)) if source_session else None
    effective_provider = target_provider or source_provider
    source_title = _as_str(getattr(source_session, "title", None)) if source_session else None
    fallback_source_session = source_session
    source_chat_mode = (
        _as_str(getattr(source_session, "chat_mode", None)) if source_session else None
    )
    effective_chat_mode = target_chat_mode or source_chat_mode
    source_model = _as_str(getattr(source_session, "model", None)) if source_session else None
    effective_model = target_model or source_model
    can_sdk_resume = (
        not effective_provider or not source_provider or effective_provider == source_provider
    )
    resume_notice: str | None = None

    if (
        can_sdk_resume
        and source_session
        and getattr(source_session, "session_type", None) == "web_chat"
        and runtime_manager is not None
    ):
        resume_notice = runtime_manager.policy_mismatch_reason(source_session)
        if resume_notice:
            can_sdk_resume = False

    # 1. Source session's external_id IS the SDK session ID
    #    (web chat sessions update external_id -> SDK session ID after first turn)
    if can_sdk_resume and source_session and source_session.external_id:
        sdk_resume_id = source_session.external_id

    # 2. Check agent_runs for autonomous agents with sdk_session_id
    if can_sdk_resume and not sdk_resume_id:
        agent_run_mgr = getattr(mixin, "agent_run_manager", None)
        if agent_run_mgr:
            try:
                sdk_resume_id = await run_db(
                    mixin, agent_run_mgr.get_sdk_session_id_for_session, source_session_id
                )
            except Exception as e:
                logger.warning(f"Failed to look up sdk_session_id: {e}")

    # 3. Kill the terminal/agent runtime that currently owns the session so the
    #    resumed web chat can take over the same durable session identity.
    if resume_in_place and source_session:
        await _release_source_session(mixin, source_session_id, source_session)

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
                        f"Transcript restore failed for {source_session_id[:8]}; falling back to hidden context injection",
                    )
                    sdk_resume_id = None

    if session_manager and source_session and resume_in_place:
        try:
            source_session = await run_db(
                mixin,
                session_manager.update,
                source_session_id,
                source=effective_provider,
                model=effective_model,
                chat_mode=effective_chat_mode,
                session_type="web_chat",
                status="active",
                title=source_title,
                terminal_context={},
                project_id=project_id,
                sandbox_enabled=current_web_chat_sandbox_enabled,
                sandbox_policy_hash=current_web_chat_policy_hash,
            )
        except Exception as e:
            logger.error(
                "Failed to convert resumed session %s to web_chat: %s", source_session_id, e
            )
            await mixin._send_error(websocket, f"Failed to resume session: {e}")
            return
    # If the client pre-created a web-chat row with a stale provider, correct
    # it before booting the continuation session so provider restore is
    # source-authoritative by default.
    elif session_manager and effective_provider:
        try:
            target_session = await run_db(mixin, session_manager.get, conversation_id)
            if target_session and getattr(target_session, "session_type", None) == "web_chat":
                if (
                    getattr(target_session, "source", None) != effective_provider
                    or (
                        effective_model
                        and getattr(target_session, "model", None) != effective_model
                    )
                    or (source_title and getattr(target_session, "title", None) != source_title)
                    or (
                        effective_chat_mode
                        and getattr(target_session, "chat_mode", None) != effective_chat_mode
                    )
                ):
                    await run_db(
                        mixin,
                        session_manager.update,
                        conversation_id,
                        source=effective_provider,
                        model=effective_model,
                        title=source_title,
                        chat_mode=effective_chat_mode,
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
                model=effective_model,
                project_id=project_id,
                resume_session_id=sdk_resume_id,
                provider=effective_provider,
                reasoning_effort=target_reasoning_effort,
            )
            if (
                session_manager
                and session.db_session_id
                and not resume_in_place
                and (source_title or effective_chat_mode or effective_model)
            ):
                session_updates: dict[str, Any] = {}
                if source_title:
                    session_updates["title"] = source_title
                if effective_chat_mode:
                    session_updates["chat_mode"] = effective_chat_mode
                if effective_model:
                    session_updates["model"] = effective_model
                if session_updates:
                    await run_db(
                        mixin,
                        session_manager.update,
                        session.db_session_id,
                        **session_updates,
                    )
        except Exception as e:
            logger.error(f"Failed to create continuation session: {e}")
            await mixin._send_error(websocket, f"Failed to create session: {e}")
            return
    elif target_reasoning_effort is not None:
        session.reasoning_effort = target_reasoning_effort

    if effective_chat_mode:
        session.chat_mode = effective_chat_mode

    pending_inject_contexts = getattr(mixin, "_pending_inject_contexts", {})
    pending_inject_contexts.pop(requested_conversation_id, None)
    pending_inject_contexts.pop(conversation_id, None)

    pending_inject_context: str | None = None
    if not sdk_resume_id:
        pending_inject_context = _resolve_fallback_inject_context(
            fallback_source_session,
            requested_fallback_context,
        )
        if pending_inject_context:
            pending_inject_contexts[conversation_id] = pending_inject_context

    # Set parent_session_id on the DB record for lineage tracking
    if session.db_session_id and session_manager and session.db_session_id != source_session_id:
        try:
            await run_db(
                mixin,
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
                "ref": f"#{session.seq_num}" if session.seq_num is not None else None,
                "title": source_title,
                "source": effective_provider,
                "model": effective_model,
                "chat_mode": effective_chat_mode,
                "reasoning_effort": target_reasoning_effort,
                "session_type": "web_chat",
                "status": "active",
                "resume_notice": resume_notice,
            }
        )
    )
    if sdk_resume_id:
        resume_mode = "SDK resume"
    elif pending_inject_context:
        resume_mode = "hidden context injection"
    else:
        resume_mode = "fresh web chat"
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
                "WHERE parent_session_id = %s AND status IN ('pending', 'running') "
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
                "WHERE session_id = %s AND status IN ('pending', 'running', 'waiting_approval') "
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
        session = await run_db(mixin, session_manager.get, session_id)
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

    workflow_name = _as_str(getattr(session, "workflow_name", None))
    agent_run_id = _as_str(getattr(session, "agent_run_id", None))
    agent_name = await _resolve_agent_name_for_session(
        mixin,
        session_id,
        workflow_name,
        agent_run_id,
    )
    live_variables = await _load_live_session_variables(mixin, session_manager, session_id)
    db = getattr(session_manager, "db", None) or getattr(mixin, "db", None)
    context_window = effective_context_window_for_session(
        session,
        variables=live_variables,
        db=db,
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
    session_meta = _session_meta_payload(
        session,
        variables=live_variables,
        agent_name=agent_name,
        workflow_name=workflow_name,
        agent_run_id=agent_run_id,
        context_window=context_window,
    )
    await websocket.send(
        json.dumps(
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
        f"Client attached to session {session_id} ({session_meta['ref']}): "
        f"{total_count} messages loaded"
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
            msg = await run_db(
                mixin,
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
    ctx: dict[str, Any] | None = None
    tmux_pane = None
    if hasattr(session, "terminal_context") and session.terminal_context:
        ctx = session.terminal_context if isinstance(session.terminal_context, dict) else {}
        tmux_pane = ctx.get("tmux_pane")

    if not tmux_pane and hasattr(session, "metadata") and session.metadata:
        meta = session.metadata if isinstance(session.metadata, dict) else {}
        tmux_pane = meta.get("terminal_tmux_pane")

    if tmux_pane:
        try:
            tmux_manager = get_tmux_manager_for_context(ctx)
            ok = await tmux_manager.send_keys(tmux_pane, content + "\n")
            if ok:
                delivered_via_tmux = True
                # Mark as delivered
                if inter_msg_manager and msg_id:
                    try:
                        await run_db(mixin, inter_msg_manager.mark_delivered, msg_id)
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
                "client_message_id": client_message_id,
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

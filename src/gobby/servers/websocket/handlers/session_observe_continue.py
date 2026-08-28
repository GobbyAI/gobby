"""Continuation and resume handlers for websocket session observation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.agents import kill as agent_kill
from gobby.agents.sandbox import web_chat_sandbox_policy_hash
from gobby.agents.terminal_delivery import (
    deliver_existing_terminal_run_in_scope,
    shielded_terminal_delivery,
)
from gobby.servers.websocket.db import run_db
from gobby.servers.websocket.handlers.session_observe_support import (
    _as_str,
    _is_terminal_session,
    _resolve_fallback_inject_context,
    _resolve_requested_fallback_context,
)
from gobby.sessions.transcript_archive import restore_transcript
from gobby.storage import agents as agent_storage
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)

_POST_KILL_SETTLE_SECONDS = 0.5


def _observe_facade() -> Any:
    from gobby.servers.websocket.handlers import session_observe

    return session_observe


async def _release_source_session(
    mixin: SessionControlMixin,
    source_session_id: str,
    source_session: Any,
) -> None:
    """Stop the active terminal/agent runtime before resuming in web chat."""
    killed = False
    session_manager = getattr(mixin, "session_manager", None)

    if session_manager:
        try:
            arm = agent_storage.LocalAgentRunManager(session_manager.db)
            run = arm.get_by_session(source_session_id)
            if run:
                logger.info("Killing agent %s before resume", run.id)

                async def run_storage(func: Any, *args: Any, **kwargs: Any) -> Any:
                    return await run_db(mixin, func, *args, **kwargs)

                async def kill_and_deliver() -> bool:
                    try:
                        await agent_kill.kill_agent(
                            run,
                            session_manager.db,
                            close_terminal=True,
                            terminal_services=getattr(mixin, "terminal_services", None),
                        )
                        await run_storage(
                            arm.cancel,
                            run.id,
                            terminal_reason="user_cancelled",
                        )
                    finally:
                        await deliver_existing_terminal_run_in_scope(
                            db=session_manager.db,
                            agent_run_manager=arm,
                            completion_registry=mixin.completion_registry,
                            run_id=run.id,
                            run_db=run_storage,
                        )
                    return True

                delivery_result = await shielded_terminal_delivery(run.id, kill_and_deliver)
                if delivery_result is None:
                    raise RuntimeError("terminal delivery admission is closed")
                killed = True
                await asyncio.sleep(_POST_KILL_SETTLE_SECONDS)
        except Exception as exc:
            raise RuntimeError(f"failed to kill running agent: {exc}") from exc

    if killed:
        return

    terminal_ctx = getattr(source_session, "terminal_context", None)
    if not terminal_ctx:
        return

    try:
        term_killed = await _observe_facade().kill_terminal_session(
            terminal_ctx,
            source_session_id,
        )
    except Exception as exc:
        raise RuntimeError(f"failed to kill terminal session: {exc}") from exc

    if term_killed:
        await asyncio.sleep(_POST_KILL_SETTLE_SECONDS)
    else:
        raise RuntimeError("terminal session was not killed")


async def _resolved_session_title(
    mixin: SessionControlMixin,
    session_manager: Any,
    session_id: str | None,
) -> str | None:
    if session_manager is None or session_id is None:
        return None
    persisted_session = await run_db(mixin, session_manager.get, session_id)
    return _as_str(getattr(persisted_session, "title", None))


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
    if not isinstance(source_session_id, str) or not source_session_id:
        await mixin._send_error(websocket, "continue_in_chat requires source_session_id")
        return

    requested_conversation_id = data.get("conversation_id")
    if requested_conversation_id is not None and not isinstance(requested_conversation_id, str):
        await mixin._send_error(websocket, "continue_in_chat conversation_id must be a string")
        return
    requested_conversation_id = requested_conversation_id or str(uuid4())
    conversation_id = requested_conversation_id
    requested_project_id = _as_str(data.get("project_id"))
    target_provider = _as_str(data.get("provider"))
    target_model = _as_str(data.get("model"))
    target_reasoning_effort = _as_str(data.get("reasoning_effort"))
    target_chat_mode = _as_str(data.get("chat_mode"))
    requested_fallback_context = _resolve_requested_fallback_context(data)

    # Look up source session for project_id and SDK session ID
    session_manager = getattr(mixin, "session_manager", None)
    source_session = None
    if not session_manager:
        await mixin._send_error(websocket, "Session manager not available")
        return
    try:
        source_session = await run_db(mixin, session_manager.get, source_session_id)
    except Exception as e:
        logger.warning("Failed to look up source session %s: %s", source_session_id, e)
        await mixin._send_error(websocket, "Source session lookup failed")
        return
    if not source_session:
        await mixin._send_error(
            websocket,
            f"Source session not found: {source_session_id}",
            code="NOT_FOUND",
        )
        return
    project_id = _as_str(getattr(source_session, "project_id", None)) or requested_project_id

    resume_in_place = bool(source_session and _is_terminal_session(source_session))
    if resume_in_place:
        # Resuming a tmux session preserves the same durable session identity.
        conversation_id = source_session_id

    runtime_manager = getattr(mixin, "web_chat_runtime_manager", None)
    if runtime_manager is not None:
        current_web_chat_policy_hash = runtime_manager.sandbox_policy_hash
    else:
        current_web_chat_policy_hash = web_chat_sandbox_policy_hash(
            getattr(mixin, "daemon_config", None)
        )

    # --- Resume guard: reject if source session is actively in use ---
    if source_session:
        blocked_reason = await _observe_facade().check_resume_blocked(mixin, source_session)
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
    source_title_source = (
        _as_str(getattr(source_session, "title_source", None)) if source_session else None
    )
    manual_source_title = source_title if source_title_source == "manual" else None
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
                logger.warning("Failed to look up sdk_session_id: %s", e)

    # 3. Kill the terminal/agent runtime that currently owns the session so the
    #    resumed web chat can take over the same durable session identity.
    if resume_in_place and source_session:
        try:
            await _release_source_session(mixin, source_session_id, source_session)
        except RuntimeError as exc:
            logger.error("Failed to release source session %s: %s", source_session_id, exc)
            await mixin._send_error(websocket, f"Failed to release source session: {exc}")
            return

    # --- Restore transcript from backup if original is missing ---
    if sdk_resume_id and source_session:
        transcript_path = source_session.transcript_path
        if transcript_path and source_session.external_id:
            original_exists = await asyncio.to_thread(Path(transcript_path).is_file)
            if not original_exists:
                restored = await asyncio.to_thread(
                    restore_transcript,
                    source_session.external_id,
                    transcript_path,
                )
                if not restored:
                    logger.warning(
                        "Transcript restore failed for %s; falling back to hidden context injection",
                        source_session_id[:8],
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
                terminal_context={},
                project_id=project_id,
                sandbox_enabled=False,
                sandbox_policy_hash=current_web_chat_policy_hash,
            )
        except Exception as e:
            logger.error(
                "Failed to convert resumed session %s to web_chat: %s", source_session_id, e
            )
            await mixin._send_error(websocket, "Failed to resume session")
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
                    or (
                        manual_source_title
                        and getattr(target_session, "title", None) != manual_source_title
                    )
                    or (
                        effective_chat_mode
                        and getattr(target_session, "chat_mode", None) != effective_chat_mode
                    )
                ):
                    target_updates: dict[str, Any] = {
                        "source": effective_provider,
                        "model": effective_model,
                        "chat_mode": effective_chat_mode,
                    }
                    if manual_source_title:
                        target_updates["title"] = manual_source_title
                        target_updates["title_source"] = "manual"
                    await run_db(
                        mixin,
                        session_manager.update,
                        conversation_id,
                        **target_updates,
                    )
        except Exception as e:
            logger.warning(
                "Failed to normalize continuation session metadata for %s: %s",
                conversation_id,
                e,
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
                and (manual_source_title or effective_chat_mode or effective_model)
            ):
                session_updates: dict[str, Any] = {}
                if manual_source_title:
                    session_updates["title"] = manual_source_title
                    session_updates["title_source"] = "manual"
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
            logger.error("Failed to create continuation session: %s", e)
            await mixin._send_error(websocket, "Failed to create session")
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
            logger.warning("Failed to set parent_session_id: %s", e)

    continued_title = (
        source_title
        if resume_in_place
        else await _resolved_session_title(mixin, session_manager, session.db_session_id)
    )

    # Send confirmation
    await websocket.send(
        json_dumps(
            {
                "type": "session_continued",
                "conversation_id": conversation_id,
                "source_session_id": source_session_id,
                "db_session_id": session.db_session_id,
                "resumed": bool(sdk_resume_id),
                "ref": f"#{session.seq_num}" if session.seq_num is not None else None,
                "title": continued_title,
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
        "Session continued (%s): %s -> %s (db=%s)",
        resume_mode,
        source_session_id[:8],
        conversation_id[:8],
        session.db_session_id,
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
            row = await run_db(
                mixin,
                session_manager.db.fetchone,
                "SELECT id FROM agent_runs "
                "WHERE parent_session_id = %s AND status IN ('pending', 'running') "
                "LIMIT 1",
                (session_id,),
            )
            if row:
                return "session has a pending or running agent"
        except Exception as e:
            logger.debug("Resume block check failed for %s: %s", session_id, e)

        # 2. Active pipeline
        try:
            row = await run_db(
                mixin,
                session_manager.db.fetchone,
                "SELECT id FROM pipeline_executions "
                "WHERE session_id = %s AND status IN ('pending', 'running', 'waiting_approval') "
                "LIMIT 1",
                (session_id,),
            )
            if row:
                return "session has an active pipeline"
        except Exception as e:
            logger.debug("Resume block check failed for %s: %s", session_id, e)

    # 4. Active web chat session (in-memory)
    if session_id in {getattr(s, "db_session_id", None) for s in mixin._chat_sessions.values()}:
        return "session is active in another web chat"

    return None

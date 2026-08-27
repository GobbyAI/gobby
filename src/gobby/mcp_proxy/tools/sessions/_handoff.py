"""Handoff tools for session management.

This module contains MCP tools for setting and retrieving handoff context.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.wait_tools import clamp_wait_tool_timeout
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.config.sessions import SessionSummaryConfig
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager


def _is_bound_clear_successor(
    session_manager: SessionManager,
    caller_session_id: str | None,
    parent_session: Any,
) -> bool:
    """Return whether the caller is the bound child of ``parent_session``."""
    if not isinstance(caller_session_id, str):
        return False
    parent_id = getattr(parent_session, "id", None)
    if not isinstance(parent_id, str):
        return False
    caller = session_manager.get(caller_session_id)
    if caller is None or getattr(caller, "parent_session_id", None) != parent_id:
        return False
    parent_project_id = getattr(parent_session, "project_id", None)
    caller_project_id = getattr(caller, "project_id", None)
    if (
        isinstance(parent_project_id, str)
        and isinstance(caller_project_id, str)
        and parent_project_id != caller_project_id
    ):
        return False
    return True


def register_handoff_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager | None,
    llm_service_resolver: Callable[[], Any | None] | None = None,
    transcript_processor: Any | None = None,
    session_summary_config_resolver: Callable[[], SessionSummaryConfig | None] | None = None,
    inter_session_message_manager: InterSessionMessageManager | None = None,
) -> None:
    """
    Register handoff tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: SessionManager instance for session operations
        llm_service_resolver: per-call resolver for the current LLM service (optional)
        transcript_processor: Transcript processor for parsing transcripts (optional)
        session_summary_config_resolver: current session summary config resolver
        inter_session_message_manager: For sending P2P messages between sessions (optional)
    """
    from gobby.utils.session_context import resolve_session_ref

    def _resolve_session_id(ref: str) -> str:
        if session_manager is None:
            raise ValueError("Session manager not available")
        return resolve_session_ref(session_manager, ref)

    def _send_to_peer(from_session_id: str, to_session_ref: str, content: str) -> dict[str, Any]:
        """Send handoff content to a peer session via P2P message."""
        if inter_session_message_manager is None:
            return {"success": False, "error": "Inter-session message manager not available"}
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        try:
            resolved_to = _resolve_session_id(to_session_ref)
            to_session_obj = session_manager.get(resolved_to)
            if not to_session_obj:
                return {"success": False, "error": f"Target session {to_session_ref} not found"}

            # Validate same project
            from_session_obj = session_manager.get(from_session_id)
            if from_session_obj and to_session_obj:
                from_proj = getattr(from_session_obj, "project_id", None)
                to_proj = getattr(to_session_obj, "project_id", None)
                if from_proj and to_proj and from_proj != to_proj:
                    return {"success": False, "error": "Sessions belong to different projects"}

            msg = inter_session_message_manager.create_message(
                from_session=from_session_id,
                to_session=resolved_to,
                content=content,
                message_type="handoff",
            )
            return {"success": True, "message_id": msg.id, "to_session": resolved_to}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="set_handoff_context",
        description=(
            "Set handoff context for a session. Two modes:\n"
            "1. Agent-authored (fast): Pass `content` directly — writes to summary_markdown, "
            "sets handoff_ready.\n"
            "2. Automated fallback: Omit `content` — uses TranscriptAnalyzer and/or LLM.\n"
            "Optionally sends context to a peer session via `to_session`.\n\n"
            "Args:\n"
            "    session_id: Session to update. Accepts #N, N, UUID, or prefix; "
            "defaults to the current session."
        ),
    )
    async def set_handoff_context(
        session_id: str | None = None,
        content: str | None = None,
        to_session: str | None = None,
        notes: str | None = None,
        write_file: bool = False,
        output_path: str = ".gobby/session_summaries/",
        set_handoff_ready: bool = True,
    ) -> dict[str, Any]:
        """
        Set handoff context for a session.

        Args:
            session_id: Session reference; defaults to the current session
            content: Agent-authored handoff content (fast path, skips transcript analysis)
            to_session: Target session to send handoff context to via P2P message
            notes: Additional notes to include in handoff
            write_file: Also write to file (default: False). DB is always written.
            output_path: Directory for file output (default: .gobby/session_summaries/)
            set_handoff_ready: Set session status to handoff_ready (default: True)

        Returns:
            Success status, markdown lengths, and context summary
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = session_id or get_current_session_id()
        if not session_id:
            return {"success": False, "error": "No session context available"}

        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        # Resolve session reference
        try:
            resolved_id = _resolve_session_id(session_id)
            session = session_manager.get(resolved_id)
        except ValueError as e:
            return {"success": False, "error": str(e), "session_id": session_id}

        if not session:
            return {"success": False, "error": "No session found", "session_id": session_id}

        # --- Agent-authored fast path ---
        if content is not None:
            session_manager.update_summary(session.id, summary_markdown=content)
            update_last_turn = getattr(session_manager, "update_last_turn_markdown", None)
            if callable(update_last_turn):
                update_last_turn(session.id, content)

            if set_handoff_ready:
                session_manager.update_status(session.id, "handoff_ready")

            result: dict[str, Any] = {
                "success": True,
                "session_id": session.id,
                "mode": "agent_authored",
                "summary_length": len(content),
            }

            if to_session:
                result["send_result"] = _send_to_peer(session.id, to_session, content)

            return result

        # --- Automated fallback — delegate to shared function ---
        from gobby.sessions.summarize import generate_session_summaries

        summary_result = await generate_session_summaries(
            session_id=session.id,
            session_manager=session_manager,
            llm_service=llm_service_resolver() if llm_service_resolver is not None else None,
            session_summary_config=(
                session_summary_config_resolver()
                if session_summary_config_resolver is not None
                else None
            ),
            db=getattr(session_manager, "db", None),
            write_file=write_file,
            output_path=output_path,
            set_handoff_ready=set_handoff_ready,
        )

        if not summary_result.get("success"):
            return summary_result

        # Add mode marker for MCP response
        summary_result["mode"] = "automated"
        if notes:
            summary_result["notes"] = notes

        # Send to peer if requested
        if to_session:
            # Prefer full summary, fall back to compact
            session_after = session_manager.get(session.id)
            send_content = ""
            if session_after:
                send_content = session_after.summary_markdown or ""
            if send_content:
                summary_result["send_result"] = _send_to_peer(session.id, to_session, send_content)
            else:
                summary_result["send_result"] = {"success": False, "reason": "no_content"}

        return summary_result

    @registry.tool(
        name="get_handoff_context",
        description=(
            "Get handoff context from a session. Finds sessions by ID, project/source, "
            "or most recent handoff_ready.\n"
            "Accepts #N, N, UUID, or prefix for session_id and link_child_session_id."
        ),
    )
    def get_handoff_context(
        session_id: str | None = None,
        project_id: str | None = None,
        source: str | None = None,
        link_child_session_id: str | None = None,
    ) -> dict[str, Any]:
        """
                Retrieve handoff context from a session.

                Args:
                    session_id: Session reference - supports #N, N (seq_num), UUID, or prefix (optional)
                    project_id: Project ID to find parent session in when no caller project context exists
        source: Filter by CLI source - claude, grok, qwen, codex, droid, agy (optional)
                    link_child_session_id: Session to link as child - supports #N, N, UUID, or prefix (optional)

                Returns:
                    Handoff context markdown and session metadata
        """
        from gobby.utils.machine_id import get_machine_id

        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        parent_session = None
        project_ctx = get_project_context()
        project_ctx_id = project_ctx.get("id") if project_ctx else None
        caller_project_id = project_ctx_id if isinstance(project_ctx_id, str) else project_id

        # Option 1: Direct session_id lookup with resolution
        if session_id:
            try:
                resolved_id = _resolve_session_id(session_id)
                parent_session = session_manager.get(resolved_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}
            if not parent_session:
                return {
                    "success": False,
                    "found": False,
                    "message": "No handoff-ready session found",
                    "filters": {
                        "session_id": session_id,
                        "project_id": caller_project_id,
                        "source": source,
                    },
                }

        # Option 2: Find parent by project_id and source
        if not parent_session and (project_id or source) and caller_project_id:
            machine_id = get_machine_id()
            if machine_id:
                parent_session = session_manager.find_parent(
                    machine_id=machine_id,
                    project_id=caller_project_id,
                    source=source,
                    status="handoff_ready",
                )

        # Option 3: Find most recent handoff_ready session scoped to caller project.
        if not parent_session and not session_id and caller_project_id:
            machine_id = get_machine_id()
            if machine_id:
                parent_session = session_manager.find_parent(
                    machine_id=machine_id,
                    project_id=caller_project_id,
                    source=source,
                    status="handoff_ready",
                )

        if not parent_session:
            return {
                "success": False,
                "found": False,
                "message": "No handoff-ready session found",
                "filters": {
                    "session_id": session_id,
                    "project_id": caller_project_id,
                    "source": source,
                },
            }

        # In-place compaction reactivates the same row, so a session may read
        # its own pre-compaction summary after it is active again. A bound
        # clear_self successor may also read its direct predecessor after that
        # row expires. Every other target still requires handoff_ready.
        caller_session_id = get_current_session_id()
        is_self_read = isinstance(caller_session_id, str) and caller_session_id == getattr(
            parent_session, "id", None
        )
        is_bound_successor = _is_bound_clear_successor(
            session_manager, caller_session_id, parent_session
        )
        if (
            getattr(parent_session, "status", None) != "handoff_ready"
            and not is_self_read
            and not is_bound_successor
        ):
            return {
                "success": False,
                "found": False,
                "message": "No handoff-ready session found",
                "filters": {
                    "session_id": session_id,
                    "project_id": caller_project_id,
                    "source": source,
                },
            }

        parent_project_id = getattr(parent_session, "project_id", None)
        if (
            isinstance(parent_project_id, str)
            and isinstance(caller_project_id, str)
            and parent_project_id != caller_project_id
        ):
            return {
                "success": False,
                "found": False,
                "message": "No handoff-ready session found",
                "filters": {
                    "session_id": session_id,
                    "project_id": caller_project_id,
                    "source": source,
                },
            }

        from gobby.sessions.summary_refresh import handoff_context

        context, context_type, stale = handoff_context(parent_session)

        if not context:
            return {
                "success": False,
                "found": True,
                "session_id": parent_session.id,
                "has_context": False,
                "message": "Session found but has no handoff context",
            }

        # Optionally link child session (resolve if using #N format)
        resolved_child_id = None
        if link_child_session_id:
            try:
                resolved_child_id = _resolve_session_id(link_child_session_id)
                child_session = session_manager.get(resolved_child_id)
                child_project_id = getattr(child_session, "project_id", None)
                if (
                    not child_session
                    or child_project_id is None
                    or parent_project_id is None
                    or (
                        isinstance(child_project_id, str)
                        and isinstance(parent_project_id, str)
                        and child_project_id != parent_project_id
                    )
                ):
                    return {
                        "success": False,
                        "found": True,
                        "session_id": parent_session.id,
                        "has_context": True,
                        "error": "Child session belongs to a different project",
                        "context": context,
                    }
                session_manager.update_parent_session_id(resolved_child_id, parent_session.id)
            except ValueError as e:
                return {
                    "success": False,
                    "found": True,
                    "session_id": parent_session.id,
                    "has_context": True,
                    "error": f"Failed to resolve child session '{link_child_session_id}': {e}",
                    "context": context,
                }

        result = {
            "success": True,
            "found": True,
            "session_id": parent_session.id,
            "has_context": True,
            "context": context,
            "context_type": context_type,
            "parent_title": getattr(parent_session, "title", None),
            "parent_status": parent_session.status,
            "linked_child": resolved_child_id or link_child_session_id,
        }
        if stale:
            result["stale"] = True
        return result

    @registry.tool(
        name="wait_for_summary",
        description=(
            "Wait for a specific session's summary_markdown to become available. "
            "Accepts #N, N, UUID, or prefix for session_id."
        ),
    )
    async def wait_for_summary(
        session_id: str,
        timeout_seconds: float = 60,
        poll_interval_seconds: float = 1,
    ) -> dict[str, Any]:
        """Poll until summary_markdown is present for a resolved session reference."""
        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        try:
            resolved_id = _resolve_session_id(session_id)
        except ValueError as e:
            return {
                "success": False,
                "completed": False,
                "found": False,
                "session_id": session_id,
                "error": str(e),
            }

        timeout = clamp_wait_tool_timeout(
            "wait_for_summary",
            timeout_seconds,
            default=60.0,
        )
        try:
            poll_interval = max(0.1, float(poll_interval_seconds))
        except (TypeError, ValueError):
            poll_interval = 1.0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            session = await asyncio.to_thread(session_manager.get, resolved_id)
            if session is None:
                return {
                    "success": False,
                    "completed": False,
                    "found": False,
                    "session_id": resolved_id,
                    "error": f"Session {session_id} not found",
                }

            from gobby.sessions.summary_refresh import handoff_context

            summary = getattr(session, "summary_markdown", None)
            if isinstance(summary, str) and summary.strip():
                context, context_type, stale = handoff_context(session)
                result: dict[str, Any] = {
                    "success": True,
                    "completed": True,
                    "session_id": resolved_id,
                    "has_context": True,
                    "context": context,
                    "context_type": context_type,
                }
                if stale:
                    result["stale"] = True
                return result

            remaining = deadline - loop.time()
            if remaining <= 0:
                return {
                    "success": True,
                    "completed": False,
                    "session_id": resolved_id,
                    "timeout_seconds": timeout,
                }
            await asyncio.sleep(min(poll_interval, remaining))

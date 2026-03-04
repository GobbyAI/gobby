"""Handoff tools for session management.

This module contains MCP tools for setting and retrieving handoff context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import LocalSessionManager


def register_handoff_tools(
    registry: InternalToolRegistry,
    session_manager: LocalSessionManager,
    llm_service: Any | None = None,
    transcript_processor: Any | None = None,
    inter_session_message_manager: InterSessionMessageManager | None = None,
) -> None:
    """
    Register handoff tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: LocalSessionManager instance for session operations
        llm_service: LLM service for generating full summaries (optional)
        transcript_processor: Transcript processor for parsing transcripts (optional)
        inter_session_message_manager: For sending P2P messages between sessions (optional)
    """
    from gobby.utils.project_context import get_project_context

    def _resolve_session_id(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        return session_manager.resolve_session_reference(ref, project_id)

    def _send_to_peer(from_session_id: str, to_session_ref: str, content: str) -> dict[str, Any]:
        """Send handoff content to a peer session via P2P message."""
        if inter_session_message_manager is None:
            return {"success": False, "error": "Inter-session message manager not available"}

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
            "    session_id: (REQUIRED) Your session ID. Accepts #N, N, UUID, or prefix."
        ),
    )
    async def set_handoff_context(
        session_id: str,
        content: str | None = None,
        to_session: str | None = None,
        notes: str | None = None,
        compact: bool = False,
        full: bool = False,
        write_file: bool = False,
        output_path: str = ".gobby/session_summaries/",
        set_handoff_ready: bool = True,
    ) -> dict[str, Any]:
        """
        Set handoff context for a session.

        Args:
            session_id: Session reference - supports #N, N (seq_num), UUID, or prefix (REQUIRED)
            content: Agent-authored handoff content (fast path, skips transcript analysis)
            to_session: Target session to send handoff context to via P2P message
            notes: Additional notes to include in handoff
            compact: Generate compact summary only (TranscriptAnalyzer)
            full: Generate full LLM summary only
            write_file: Also write to file (default: False). DB is always written.
            output_path: Directory for file output (default: .gobby/session_summaries/)
            set_handoff_ready: Set session status to handoff_ready (default: True)

        Returns:
            Success status, markdown lengths, and context summary
        """
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
            llm_service=llm_service,
            db=getattr(session_manager, "db", None),
            write_file=write_file,
            output_path=output_path,
            set_handoff_ready=set_handoff_ready,
            compact_only=compact and not full,
            full_only=full and not compact,
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
                send_content = (
                    session_after.summary_markdown or session_after.compact_markdown or ""
                )
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
            project_id: Project ID to find parent session in (optional)
            source: Filter by CLI source - claude, gemini, codex, cursor, windsurf, copilot (optional)
            link_child_session_id: Session to link as child - supports #N, N, UUID, or prefix (optional)

        Returns:
            Handoff context markdown and session metadata
        """
        from gobby.utils.machine_id import get_machine_id

        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        parent_session = None

        # Option 1: Direct session_id lookup with resolution
        if session_id:
            try:
                resolved_id = _resolve_session_id(session_id)
                parent_session = session_manager.get(resolved_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        # Option 2: Find parent by project_id and source
        if not parent_session and project_id:
            machine_id = get_machine_id()
            if machine_id:
                parent_session = session_manager.find_parent(
                    machine_id=machine_id,
                    project_id=project_id,
                    source=source,
                    status="handoff_ready",
                )

        # Option 3: Find most recent handoff_ready session
        if not parent_session:
            sessions = session_manager.list(status="handoff_ready", limit=1)
            parent_session = sessions[0] if sessions else None

        if not parent_session:
            return {
                "success": False,
                "found": False,
                "message": "No handoff-ready session found",
                "filters": {
                    "session_id": session_id,
                    "project_id": project_id,
                    "source": source,
                },
            }

        # Get handoff context (prefer summary_markdown, fall back to compact_markdown)
        context = parent_session.summary_markdown or parent_session.compact_markdown

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

        return {
            "success": True,
            "found": True,
            "session_id": parent_session.id,
            "has_context": True,
            "context": context,
            "context_type": (
                "summary_markdown" if parent_session.summary_markdown else "compact_markdown"
            ),
            "parent_title": parent_session.title,
            "parent_status": parent_session.status,
            "linked_child": resolved_child_id or link_child_session_id,
        }

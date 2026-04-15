"""Session CRUD tools for session management.

This module contains MCP tools for:
- Getting session details (get_session)
- Getting current session (get_current_session)
- Listing sessions (list_sessions)
- Session statistics (session_stats)
- Usage breakdown by source and model (get_usage_breakdown)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import LocalSessionManager


def register_crud_tools(
    registry: InternalToolRegistry,
    session_manager: LocalSessionManager,
) -> None:
    """
    Register session CRUD tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: LocalSessionManager instance for session operations
    """

    @registry.tool(
        name="get_session",
        description="Get session details by ID. Accepts #N (project-scoped ref), UUID, or prefix. Use the session_id from your injected context.",
    )
    def get_session(session_id: str) -> dict[str, Any]:
        """
        Get session details by session reference.

        Your session_id is injected into your context at session start.
        Look for 'Session Ref: #N' or 'session_id: xxx' in your system reminders.

        Args:
            session_id: Session reference - supports #N (project-scoped), UUID, or prefix

        Returns:
            Session dict with all fields, or error if not found
        """
        from gobby.utils.project_context import get_project_context

        # Support #N format, UUID, and prefix matching
        if session_manager is None:
            return {"error": "Session manager not available"}

        # Get project_id for project-scoped resolution
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        # Try to resolve session reference (#N, UUID, or prefix)
        try:
            resolved_id = session_manager.resolve_session_reference(session_id, project_id)
            session = session_manager.get(resolved_id)
        except ValueError:
            session = None

        if not session:
            return {"error": f"Session {session_id} not found", "found": False}

        return {
            "found": True,
            **session.to_dict(),
        }

    @registry.tool(
        name="get_current_session",
        description="""Get YOUR current session ID - the CORRECT way to look up your session.

Use this when session_id wasn't in your injected context. Pass your external_id
(from transcript path or GOBBY_SESSION_ID env) and source (claude, gemini, qwen, codex).

DO NOT use list_sessions to find your session - it won't work with multiple active sessions.""",
    )
    def get_current_session(
        external_id: str,
        source: str,
    ) -> dict[str, Any]:
        """
        Look up your internal session_id from external_id and source.

        The agent passes external_id (from injected context or GOBBY_SESSION_ID env var)
        and source (claude, gemini, qwen, codex). project_id and machine_id are
        auto-resolved from config files.

        Args:
            external_id: Your CLI's session ID (from context or GOBBY_SESSION_ID env)
            source: CLI source - "claude", "gemini", "qwen", or "codex"

        Returns:
            session_id: Internal Gobby session ID (use for parent_session_id, etc.)
            Plus basic session metadata
        """
        from gobby.utils.machine_id import get_machine_id
        from gobby.utils.project_context import get_project_context

        if session_manager is None:
            return {"error": "Session manager not available"}

        # Auto-resolve context
        machine_id = get_machine_id()
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        if not machine_id:
            return {"error": "Could not determine machine_id"}
        if not project_id:
            return {"error": "Could not determine project_id (not in a gobby project?)"}

        # Use find_by_external_id with full composite key (safe lookup)
        session = session_manager.find_by_external_id(
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            source=source,
        )

        if not session:
            return {
                "found": False,
                "error": "Session not found",
                "lookup": {
                    "external_id": external_id,
                    "source": source,
                    "project_id": project_id,
                },
            }

        return {
            "found": True,
            "session_id": session.id,
            "project_id": session.project_id,
            "status": session.status,
            "agent_run_id": session.agent_run_id,
        }

    @registry.tool(
        name="list_sessions",
        description="""List sessions with optional filtering.

WARNING: Do NOT use this to find your own session_id!
- `list_sessions(status="active", limit=1)` will NOT reliably return YOUR session
- Multiple sessions can be active simultaneously (parallel agents, multiple terminals)
- Use `get_current_session(external_id, source)` instead - it uses your unique session key

This tool is for browsing/listing sessions, not for self-identification.""",
    )
    def list_sessions(
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        List sessions with filters.

        Args:
            project_id: Filter by project ID
            status: Filter by status (active, paused, expired, archived, handoff_ready)
            source: Filter by CLI source (claude, gemini, qwen, codex)
            limit: Max results (default 20)

        Returns:
            List of sessions and count
        """
        if session_manager is None:
            return {"error": "Session manager not available"}

        sessions = session_manager.list(
            project_id=project_id,
            status=status,
            source=source,
            limit=limit,
        )

        total = session_manager.count(
            project_id=project_id,
            status=status,
            source=source,
        )

        # Detect likely misuse pattern: trying to find own session
        if status == "active" and limit == 1:
            return {
                "warning": (
                    "list_sessions(status='active', limit=1) will NOT reliably get YOUR session_id! "
                    "Multiple sessions can be active simultaneously. "
                    "Use get_current_session(external_id='<your-external-id>', source='claude') instead."
                ),
                "hint": "Your external_id is in your transcript path: /path/to/<external_id>.jsonl",
                "sessions": [s.to_brief() for s in sessions],
                "count": len(sessions),
                "total": total,
                "limit": limit,
                "filters": {
                    "project_id": project_id,
                    "status": status,
                    "source": source,
                },
            }

        return {
            "sessions": [s.to_brief() for s in sessions],
            "count": len(sessions),
            "total": total,
            "limit": limit,
            "filters": {
                "project_id": project_id,
                "status": status,
                "source": source,
            },
        }

    @registry.tool(
        name="session_stats",
        description="Get session statistics for a project.",
    )
    def session_stats(project_id: str | None = None) -> dict[str, Any]:
        """
        Get session statistics.

        Args:
            project_id: Filter by project ID (optional)

        Returns:
            Statistics including total, by_status, by_source
        """
        if session_manager is None:
            return {"error": "Session manager not available"}

        total = session_manager.count(project_id=project_id)
        by_status = session_manager.count_by_status()

        # Count by source
        by_source: dict[str, int] = {}
        for src in ["claude", "gemini", "qwen", "codex"]:
            count = session_manager.count(project_id=project_id, source=src)
            if count > 0:
                by_source[src] = count

        return {
            "total": total,
            "by_status": by_status,
            "by_source": by_source,
            "project_id": project_id,
        }

    @registry.tool(
        name="get_usage_breakdown",
        description="Get token usage breakdown by source (CLI adapter) and model over a time period.",
    )
    def get_usage_breakdown(
        days: int = 1,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get token usage aggregated by source and model.

        Args:
            days: Number of days to look back (default: 1 = today, max 365)
            project_id: Filter by project ID (optional)

        Returns:
            Usage summary with token totals, usage_by_model, and usage_by_source
        """
        from gobby.sessions.token_tracker import SessionTokenTracker

        if session_manager is None:
            return {"error": "Session manager not available"}

        days = max(1, min(days, 365))
        tracker = SessionTokenTracker(session_storage=session_manager)
        return tracker.get_usage_summary(days=days, project_id=project_id)

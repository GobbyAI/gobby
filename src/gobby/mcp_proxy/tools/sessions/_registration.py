"""Session registration tools for hookless clients.

Provides MCP tools for clients that don't use Gobby's hook system
(e.g., Agent SDK apps, Antigravity agents) to register their sessions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import LocalSessionManager


def register_registration_tools(
    registry: InternalToolRegistry,
    session_manager: LocalSessionManager,
) -> None:
    """
    Register session registration tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: LocalSessionManager instance for session operations
    """

    @registry.tool(
        name="register_session",
        description="""Register a session with Gobby. For hookless clients (Agent SDK, Antigravity, etc.)
that don't trigger SessionStart hooks.

Idempotent: calling with the same external_id + source + machine_id + project_id
returns the existing session instead of creating a duplicate.

machine_id and project_id are auto-resolved from the local environment if omitted.""",
    )
    def register_session(
        external_id: str,
        source: str,
        machine_id: str | None = None,
        project_id: str | None = None,
        title: str | None = None,
        git_branch: str | None = None,
        parent_session_id: str | None = None,
        agent_depth: int = 0,
    ) -> dict[str, Any]:
        """
        Register a session for a hookless client.

        Args:
            external_id: Client's session ID (required)
            source: CLI type - claude, gemini, codex, cursor, windsurf, copilot, agent-sdk, etc. (required)
            machine_id: Machine identifier (auto-resolved if omitted)
            project_id: Project ID (auto-resolved from .gobby/project.json if omitted)
            title: Session title
            git_branch: Git branch name
            parent_session_id: Parent session ID for handoff chains
            agent_depth: Depth in agent hierarchy (0 = root session)

        Returns:
            Session details including session_id, session_ref (#N), and status
        """
        from gobby.utils.machine_id import get_machine_id
        from gobby.utils.project_context import get_project_context

        if session_manager is None:
            return {"error": "Session manager not available"}

        # Auto-resolve machine_id
        resolved_machine_id = machine_id
        if not resolved_machine_id:
            resolved_machine_id = get_machine_id()
        if not resolved_machine_id:
            return {"error": "Could not determine machine_id — pass it explicitly"}

        # Auto-resolve project_id
        resolved_project_id = project_id
        if not resolved_project_id:
            project_ctx = get_project_context()
            resolved_project_id = project_ctx.get("id") if project_ctx else None
        if not resolved_project_id:
            return {
                "error": "Could not determine project_id — run 'gobby init' or pass it explicitly"
            }

        try:
            session = session_manager.register(
                external_id=external_id,
                machine_id=resolved_machine_id,
                source=source,
                project_id=resolved_project_id,
                title=title,
                git_branch=git_branch,
                parent_session_id=parent_session_id,
                agent_depth=agent_depth,
            )

            return {
                "session_id": session.id,
                "session_ref": f"#{session.seq_num}",
                "external_id": session.external_id,
                "status": session.status,
                "source": session.source,
                "project_id": session.project_id,
            }
        except Exception as e:
            return {"error": f"Failed to register session: {e}"}

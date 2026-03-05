"""Factory function for creating the session messages tool registry.

Orchestrates the creation of all session tool sub-registries and merges them
into a unified registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._actions import register_action_tools
from gobby.mcp_proxy.tools.sessions._commits import register_commits_tools
from gobby.mcp_proxy.tools.sessions._crud import register_crud_tools
from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools
from gobby.mcp_proxy.tools.sessions._messages import register_message_tools
from gobby.mcp_proxy.tools.sessions._registration import register_registration_tools

if TYPE_CHECKING:
    from gobby.storage.session_messages import LocalSessionMessageManager
    from gobby.storage.sessions import LocalSessionManager

__all__ = ["create_session_messages_registry"]


def create_session_messages_registry(
    message_manager: LocalSessionMessageManager | None = None,
    session_manager: LocalSessionManager | None = None,
    llm_service: Any | None = None,
    transcript_processor: Any | None = None,
    config: Any | None = None,
    db: Any | None = None,
    worktree_manager: Any | None = None,
    inter_session_message_manager: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a sessions tool registry with session and message tools.

    Args:
        message_manager: LocalSessionMessageManager instance for message operations
        session_manager: LocalSessionManager instance for session CRUD
        llm_service: LLM service for handoff generation (optional)
        transcript_processor: Transcript processor for handoff generation (optional)
        config: DaemonConfig for settings (optional)
        db: Database for dependency injection (optional)
        worktree_manager: Worktree manager for context enrichment (optional)

    Returns:
        InternalToolRegistry with all session tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-sessions",
        description="Session management and message querying - CRUD, retrieval, search",
    )

    # --- Message Tools ---
    # Only register if message_manager is available
    if message_manager is not None:
        register_message_tools(registry, message_manager, session_manager)

    # --- Handoff Tools ---
    # Only register if session_manager is available
    if session_manager is not None:
        register_handoff_tools(
            registry,
            session_manager,
            inter_session_message_manager=inter_session_message_manager,
        )

    # --- Session CRUD Tools ---
    # Only register if session_manager is available
    if session_manager is not None:
        register_crud_tools(registry, session_manager)

    # --- Registration Tools (for hookless clients) ---
    if session_manager is not None:
        register_registration_tools(registry, session_manager)

    # --- Commits Tools ---
    # Only register if session_manager is available
    if session_manager is not None:
        register_commits_tools(registry, session_manager, db=db)

    # --- Action Tools (workflow action wrappers) ---
    # Only register if session_manager is available
    if session_manager is not None:
        register_action_tools(
            registry,
            session_manager=session_manager,
            llm_service=llm_service,
            transcript_processor=transcript_processor,
            config=config,
            db=db,
            worktree_manager=worktree_manager,
        )

    return registry

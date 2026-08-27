"""Factory function for creating the session messages tool registry.

Orchestrates the creation of all session tool sub-registries and merges them
into a unified registry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._actions import register_action_tools
from gobby.mcp_proxy.tools.sessions._commits import register_commits_tools
from gobby.mcp_proxy.tools.sessions._crud import register_crud_tools
from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools
from gobby.mcp_proxy.tools.sessions._messages import register_message_tools
from gobby.mcp_proxy.tools.sessions._registration import register_registration_tools
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.mcp_proxy.tools.sessions._transcripts import register_transcript_tools

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.sessions import SessionManager

__all__ = ["create_session_messages_registry"]


def create_session_messages_registry(
    session_manager: SessionManager | None = None,
    llm_service_resolver: Callable[[], Any | None] | None = None,
    memory_manager_resolver: Callable[[], Any | None] | None = None,
    transcript_processor: Any | None = None,
    startup_config: DaemonConfig | None = None,
    config_resolver: Callable[[], DaemonConfig | None] | None = None,
    db: Any | None = None,
    worktree_manager: Any | None = None,
    inter_session_message_manager: Any | None = None,
    transcript_reader: TranscriptReader | None = None,
    web_chat_session_registry: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a sessions tool registry with session and message tools.

    Args:
        session_manager: SessionManager instance for session CRUD
        llm_service_resolver: per-call resolver for the current LLM service (optional)
        memory_manager_resolver: per-call resolver for the current memory manager (optional)
        transcript_processor: Transcript processor for handoff generation (optional)
        startup_config: DaemonConfig fallback before runtime readiness
        config_resolver: per-operation current DaemonConfig resolver
        db: Database for dependency injection (optional)
        worktree_manager: Worktree manager for context enrichment (optional)
        transcript_reader: TranscriptReader for JSONL + gzip fallback reads (optional)
        web_chat_session_registry: Live web-chat registry for compact_self (optional)

    Returns:
        InternalToolRegistry with all session tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-sessions",
        description="Session management and message querying - CRUD, retrieval, search",
    )

    def _config() -> DaemonConfig | None:
        config = config_resolver() if config_resolver is not None else None
        return config if config is not None else startup_config

    initial_config = _config()
    session_summary_config = getattr(initial_config, "session_summary", None)
    compact_handoff_config = getattr(initial_config, "compact_handoff", None)

    # --- Message Tools ---
    # Register if transcript_reader or session_manager is available
    if transcript_reader is not None or session_manager is not None:
        register_message_tools(registry, session_manager, transcript_reader)

    # --- Handoff Tools ---
    # Only register if session_manager is available
    if session_manager is not None:
        register_handoff_tools(
            registry,
            session_manager,
            llm_service_resolver=llm_service_resolver,
            transcript_processor=transcript_processor,
            session_summary_config_resolver=lambda: (
                getattr(config, "session_summary", None)
                if (config := _config()) is not None
                else None
            ),
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
            llm_service_resolver=llm_service_resolver,
            transcript_processor=transcript_processor,
            db=db,
            worktree_manager=worktree_manager,
        )

    # --- Transcript Archive Tools ---
    if session_manager is not None:
        register_transcript_tools(registry, session_manager)

    # --- Terminal Interaction Tools (send_keys, capture_output) ---
    if session_manager is not None and db is not None:
        register_terminal_tools(
            registry,
            session_manager,
            db,
            llm_service_resolver=llm_service_resolver,
            memory_manager_resolver=memory_manager_resolver,
            session_summary_config=session_summary_config,
            compact_handoff_config=compact_handoff_config,
            config_resolver=_config,
            web_chat_session_registry=web_chat_session_registry,
        )

    return registry

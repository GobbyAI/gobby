"""Factory function for creating the session messages tool registry.

Orchestrates the creation of all session tool sub-registries and merges them
into a unified registry.
"""

from __future__ import annotations

import warnings
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
from gobby.mcp_proxy.tools.sessions._verification import register_verification_tools

if TYPE_CHECKING:
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.sessions import SessionManager

__all__ = ["create_session_messages_registry"]


def create_session_messages_registry(
    session_manager: SessionManager | None = None,
    llm_service: Any | None = None,
    transcript_processor: Any | None = None,
    config: Any | None = None,
    db: Any | None = None,
    worktree_manager: Any | None = None,
    inter_session_message_manager: Any | None = None,
    transcript_reader: TranscriptReader | None = None,
    web_chat_session_registry: Any | None = None,
    # Deprecated: kept for backwards-compat callers, ignored
    message_manager: object | None = None,
) -> InternalToolRegistry:
    """
    Create a sessions tool registry with session and message tools.

    Args:
        session_manager: SessionManager instance for session CRUD
        llm_service: LLM service for handoff generation (optional)
        transcript_processor: Transcript processor for handoff generation (optional)
        config: DaemonConfig for settings (optional)
        db: Database for dependency injection (optional)
        worktree_manager: Worktree manager for context enrichment (optional)
        transcript_reader: TranscriptReader for JSONL + gzip fallback reads (optional)
        web_chat_session_registry: Live web-chat registry for compact_self (optional)
        message_manager: Deprecated, ignored. Kept for backwards compatibility.

    Returns:
        InternalToolRegistry with all session tools registered
    """
    if message_manager is not None:
        warnings.warn(
            "message_manager is deprecated and ignored",
            DeprecationWarning,
            stacklevel=2,
        )
    registry = InternalToolRegistry(
        name="gobby-sessions",
        description="Session management and message querying - CRUD, retrieval, search",
    )

    # --- Message Tools ---
    # Register if transcript_reader or session_manager is available
    if transcript_reader is not None or session_manager is not None:
        register_message_tools(registry, None, session_manager, transcript_reader)

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

    # --- Verification Evidence Tools ---
    if session_manager is not None and db is not None:
        register_verification_tools(registry, session_manager, db)

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

    # --- Transcript Archive Tools ---
    if session_manager is not None:
        register_transcript_tools(registry, session_manager)

    # --- Terminal Interaction Tools (send_keys, capture_output) ---
    if session_manager is not None and db is not None:
        register_terminal_tools(
            registry,
            session_manager,
            db,
            llm_service=llm_service,
            web_chat_session_registry=web_chat_session_registry,
        )

    return registry

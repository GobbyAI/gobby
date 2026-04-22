"""Hooks-facing protocols for session access.

These protocols describe the subset of SessionManager behavior that the hooks
stack relies on. The runtime implementation is the canonical
``gobby.storage.sessions.SessionManager``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class HookSessionManager(Protocol):
    """Protocol for session operations used by the hooks subsystem."""

    db: DatabaseProtocol

    def get(self, session_id: str) -> Session | None: ...

    def list(
        self,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        exclude_subagents: bool = False,
    ) -> list[Session]: ...

    def update(self, session_id: str, **kwargs: Any) -> Session | None: ...

    def update_status(self, session_id: str, status: str) -> Session | None: ...

    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> Session | None: ...

    def update_session_status(self, session_id: str, status: str) -> bool: ...

    def register_session(
        self,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str | None,
        parent_session_id: str | None = None,
        transcript_path: str | None = None,
        title: str | None = None,
        git_branch: str | None = None,
        project_path: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        agent_depth: int = 0,
        sandbox_enabled: bool | None = None,
    ) -> str: ...

    def get_session_id(self, external_id: str, source: str) -> str | None: ...

    def lookup_session_id(
        self,
        external_id: str,
        source: str,
        machine_id: str,
        project_id: str | None,
    ) -> str | None: ...

    def recover_session(
        self,
        external_id: str,
        source: str,
        machine_id: str,
        project_id: str | None,
        session_type: str | None = None,
    ) -> Session | None: ...

    def cache_session_mapping(self, external_id: str, source: str, session_id: str) -> None: ...

    def mark_session_expired(self, session_id: str) -> bool: ...

    def backfill_terminal_context(
        self,
        session_id: str,
        terminal_context: dict[str, Any] | None,
    ) -> tuple[Session | None, bool]: ...

    def reset_transcript_processed(self, session_id: str) -> Session | None: ...

    def update_usage(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
        context_window: int | None = None,
        model: str | None = None,
    ) -> bool: ...

    def mark_had_edits(self, session_id: str) -> Session | None: ...

    def find_parent(
        self,
        machine_id: str,
        project_id: str,
        source: str | None = None,
        status: str = "handoff_ready",
        max_age_minutes: int = 10,
    ) -> Session | None: ...

    def resolve_session_reference(self, ref: str, project_id: str | None = None) -> str: ...

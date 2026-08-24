"""Hooks-facing protocols for session access.

These protocols describe the subset of SessionManager behavior that the hooks
stack relies on. The runtime implementation is the canonical
``gobby.storage.sessions.SessionManager``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from gobby.sessions.contested_expiry import ContestedExpiryCause
from gobby.storage.session_models import Session
from gobby.storage.sessions._update_sentinel import UNSET, UnsetType

if TYPE_CHECKING:
    from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
    from gobby.storage.hub.protocol import HubDatabase


class HookSessionManager(Protocol):
    """Protocol for session operations used by the hooks subsystem."""

    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def find_by_external_id(
        self,
        external_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = "terminal",
    ) -> Session | None: ...

    def list(
        self,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        exclude_subagents: bool = False,
        machine_id: str | None = None,
    ) -> list[Session]: ...

    def update(self, session_id: str, **kwargs: Any) -> Session | None: ...

    def update_title(
        self,
        session_id: str,
        title: str,
        *,
        title_source: str | None = "manual",
    ) -> Session | None: ...

    def update_status(self, session_id: str, status: str) -> Session | None: ...

    def update_status_if_non_terminal(self, session_id: str, status: str) -> Session | None: ...

    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> Session | None: ...

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        activity_confirmed: bool = False,
    ) -> bool: ...

    def revive_expired_terminal_session(self, session_id: str) -> Session | None: ...

    def register_session(
        self,
        external_id: str,
        machine_id: str | None,
        source: str,
        project_id: str | None,
        parent_session_id: str | None | UnsetType = UNSET,
        transcript_path: str | None = None,
        title: str | None = None,
        git_branch: str | None = None,
        project_path: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        agent_depth: int = 0,
        sandbox_enabled: bool | None = None,
    ) -> str: ...

    def get_session_id(
        self,
        external_id: str,
        source: str,
        project_id: str | None = None,
        session_type: str = "terminal",
    ) -> str | None: ...

    def lookup_session_id(
        self,
        external_id: str,
        source: str,
        project_id: str | None,
        session_type: str = "terminal",
    ) -> str | None: ...

    def recover_session(
        self,
        external_id: str,
        source: str,
        project_id: str | None,
        session_type: str | None = "terminal",
    ) -> Session | None: ...

    def cache_session_mapping(
        self,
        external_id: str,
        source: str,
        session_id: str,
        project_id: str | None = None,
        session_type: str = "terminal",
    ) -> None: ...

    def mark_session_expired(self, session_id: str, *, cause: ContestedExpiryCause) -> bool: ...

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

    def update_context_usage(
        self,
        session_id: str,
        snapshot: ContextUsageSnapshot,
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

"""Protocols used by session summary storage helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class SummaryUpdateHost(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def persist_summary_state(
        self,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        source_digest_turn_count: int | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        summary_path: str | None = None,
    ) -> Session | None: ...

    def persist_wiki_state(
        self,
        session_id: str,
        *,
        wiki_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        digest_turn_count: int | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        wiki_path: str | None = None,
    ) -> Session | None: ...

    def record_wiki_synthesis_failure(
        self,
        session_id: str,
        reason: str,
        error: str | None = None,
    ) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

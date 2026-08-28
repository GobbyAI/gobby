"""Protocols used by session summary storage helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from gobby.storage.session_models import Session

from ._update_sentinel import UNSET, UnsetType

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class SummaryUpdateHost(Protocol):
    db: HubDatabase
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def persist_summary_state(
        self,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        summary_path: str | None | UnsetType = UNSET,
    ) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _run_title_change_side_effects(self, updated: Session, title: str) -> None: ...

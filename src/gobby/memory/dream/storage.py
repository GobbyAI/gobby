"""Composed storage façade for memory dream state."""

from __future__ import annotations

from gobby.memory.dream.storage_actions import _DreamActionMixin
from gobby.memory.dream.storage_journal import _DreamJournalMixin
from gobby.memory.dream.storage_runs import _DreamRunMixin
from gobby.storage.hub.protocol import HubDatabase

__all__ = ["MemoryDreamStore"]


class MemoryDreamStore(
    _DreamRunMixin,
    _DreamJournalMixin,
    _DreamActionMixin,
):
    """Store memory dream runs and exact mutation snapshots."""

    def __init__(self, db: HubDatabase, *, report_project_id: str | None = None) -> None:
        self.db = db
        self.report_project_id = report_project_id

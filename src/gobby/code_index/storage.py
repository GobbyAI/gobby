"""Public facade for hub-backed code index storage."""

from __future__ import annotations

from gobby.code_index._storage.content import CodeIndexContentStorageMixin
from gobby.code_index._storage.files import CodeIndexFileStorageMixin
from gobby.code_index._storage.graph_fallbacks import CodeIndexGraphFallbackStorageMixin
from gobby.code_index._storage.projection_cleanup import (
    CodeIndexProjectionCleanupStorageMixin,
)
from gobby.code_index._storage.projects import CodeIndexProjectStorageMixin
from gobby.code_index._storage.prune_dirty import CodeIndexPruneStorageMixin
from gobby.code_index._storage.relations import CodeIndexRelationStorageMixin
from gobby.code_index._storage.summaries import CodeIndexSummaryStorageMixin
from gobby.code_index._storage.symbols import CodeIndexSymbolStorageMixin
from gobby.storage.hub.protocol import HubDatabase


class CodeIndexStorage(
    CodeIndexSymbolStorageMixin,
    CodeIndexFileStorageMixin,
    CodeIndexRelationStorageMixin,
    CodeIndexProjectStorageMixin,
    CodeIndexProjectionCleanupStorageMixin,
    CodeIndexPruneStorageMixin,
    CodeIndexSummaryStorageMixin,
    CodeIndexContentStorageMixin,
    CodeIndexGraphFallbackStorageMixin,
):
    """Storage for code symbols, indexed files, and projects in the runtime hub."""

    def __init__(self, db: HubDatabase) -> None:
        """Initialize storage against the runtime hub seam."""
        self.db: HubDatabase = db


__all__ = ["CodeIndexStorage"]

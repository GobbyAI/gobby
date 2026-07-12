"""Structured lifecycle status for the memory vector collection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VectorStoreStatus:
    """Track dimension recovery until existing memories are re-embedded."""

    collection: str
    configured_dimension: int
    state: str = "uninitialized"
    rebuild_required: bool = False
    dimension_recovery: dict[str, Any] | None = None

    def mark_ready(self) -> None:
        if not self.rebuild_required:
            self.state = "ready"

    def mark_dimension_recreated(self, previous_dimension: int) -> None:
        self.state = "recreated_pending_rebuild"
        self.rebuild_required = True
        self.dimension_recovery = {
            "action": "recreated",
            "previous_dimension": previous_dimension,
            "configured_dimension": self.configured_dimension,
        }
        logger.warning(
            "Recreated Qdrant collection '%s' after embedding dimension change %s->%s; "
            "existing memories require re-embedding",
            self.collection,
            previous_dimension,
            self.configured_dimension,
        )

    def mark_rebuild_complete(self) -> None:
        self.state = "ready"
        self.rebuild_required = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "collection": self.collection,
            "configured_dimension": self.configured_dimension,
            "rebuild_required": self.rebuild_required,
            "dimension_recovery": (
                dict(self.dimension_recovery) if self.dimension_recovery is not None else None
            ),
        }

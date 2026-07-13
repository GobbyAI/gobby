"""Typed state for safe vector collection rebuilds."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RebuildCollectionPlan:
    """Describe the collection populated and activated by a rebuild."""

    target_name: str
    target_is_empty: bool
    active_target: str | None = None
    active_is_alias: bool = False

    @property
    def requires_swap(self) -> bool:
        return self.active_target is not None

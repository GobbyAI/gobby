"""Typed invocation options for memory dream runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.memory.dream.related import RetrievalScope
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope


@dataclass(frozen=True)
class DreamRunOptions:
    dry_run: bool = True
    skip_consolidation: bool = False
    memory_type: str | None = None
    project_id: str | None = None
    global_only: bool = False
    include_global: bool | None = None
    full_sweep: bool = False

    def __post_init__(self) -> None:
        if self.global_only and self.project_id is not None:
            raise ValueError("global_only and project_id are mutually exclusive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "skip_consolidation": self.skip_consolidation,
            "memory_type": self.memory_type,
            "project_id": self.project_id,
            "global_only": self.global_only,
            "include_global": self.include_global,
            "full_sweep": self.full_sweep,
        }

    def memory_scope(self, *, include_global: bool) -> MemoryScope:
        """Resolve transport options to the typed storage scope contract."""
        if self.global_only:
            return MemoryScope.global_only()
        if self.project_id is None:
            return ALL_MEMORIES
        if include_global:
            return MemoryScope.project_visible(self.project_id)
        return MemoryScope.project_only(self.project_id)

    def retrieval_scope(self, *, include_global: bool) -> RetrievalScope | None:
        """Resolve options to project-isolated evidence retrieval scope."""
        if self.global_only:
            return RetrievalScope.global_only()
        if self.project_id is None:
            return None
        if include_global:
            return RetrievalScope.project_and_global(self.project_id)
        return RetrievalScope.project_only(self.project_id)

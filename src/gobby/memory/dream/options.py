"""Typed invocation options for memory dream runs."""

from __future__ import annotations

from collections.abc import Mapping
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


def normalize_dream_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize stored or requested run options for admission comparison.

    Accepts both option shapes persisted today: ``DreamRunOptions.to_dict()``
    (all seven fields) and the aggregate all-due dict (``aggregate: true``
    with only the four shared flags). Missing fields take their
    ``DreamRunOptions`` defaults except ``dry_run``, which admission callers
    always persist explicitly.
    """
    return {
        "dry_run": bool(options.get("dry_run", False)),
        "skip_consolidation": bool(options.get("skip_consolidation", False)),
        "memory_type": options.get("memory_type"),
        "project_id": options.get("project_id"),
        "global_only": bool(options.get("global_only", False)),
        "include_global": options.get("include_global"),
        "full_sweep": bool(options.get("full_sweep", False)),
    }


def dream_scope_key(options: Mapping[str, Any]) -> str:
    """Admission scope key derived from run options.

    ``memory_dream_runs.project_id`` is NULL for both global-only and all-due
    runs (unlike ``memories``, where global scope is ``is_global = true`` with
    a non-null owning project), so the scope key comes from the options:
    ``global`` for global-only runs, ``all`` for all-due aggregate runs, and
    ``project:<id>`` for project-scoped runs.
    """
    normalized = normalize_dream_options(options)
    if normalized["global_only"]:
        return "global"
    if normalized["project_id"] is None:
        return "all"
    return f"project:{normalized['project_id']}"

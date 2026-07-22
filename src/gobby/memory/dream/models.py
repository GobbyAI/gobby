"""Shared models for memory dream planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from gobby.utils.datetime import normalize_datetime_model

DreamActionName = Literal["keep", "delete", "refresh", "merge", "supersede", "review", "promote"]


@normalize_datetime_model(required=("created_at",))
@dataclass(frozen=True)
class RelatedMemoryEvidence:
    """A newer memory related to a dream candidate."""

    id: str
    memory_type: str
    created_at: datetime
    newer_by_days: float
    content: str
    matched_via: str

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return full JSON-safe evidence for the planner prompt."""
        return {
            "id": self.id,
            "memory_type": self.memory_type,
            "created_at": self.created_at,
            "newer_by_days": self.newer_by_days,
            "content": self.content,
            "matched_via": self.matched_via,
        }


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=("last_accessed_at",),
)
@dataclass(frozen=True)
class DreamCandidate:
    """A memory selected for dream review."""

    id: str
    content: str
    memory_type: str
    project_id: str
    is_global: bool
    source_type: str | None
    source_session_id: str | None
    tags: list[str]
    age_days: float
    access_count: int
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    dream_due_version: int = 0
    reasons: list[str] = field(default_factory=list)
    related: tuple[RelatedMemoryEvidence, ...] = ()

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return compact JSON-safe context for the planner prompt."""
        prompt: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type,
            "project_id": self.project_id,
            "source_type": self.source_type,
            "source_session_id": self.source_session_id,
            "tags": self.tags,
            "age_days": round(self.age_days, 1),
            "access_count": self.access_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "dream_due_version": self.dream_due_version,
            "reasons": self.reasons,
        }
        if self.related:
            prompt["related_newer_memories"] = [item.to_prompt_dict() for item in self.related]
        return prompt


@dataclass(frozen=True)
class DuplicateGroup:
    """A deterministic exact-duplicate memory group."""

    memory_ids: list[str]
    canonical_content: str
    reason: str

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "memory_ids": self.memory_ids,
            "canonical_content": self.canonical_content,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DreamAction:
    """One validated dream plan action."""

    action: DreamActionName
    memory_id: str | None = None
    memory_ids: list[str] = field(default_factory=list)
    content: str | None = None
    target_id: str | None = None
    memory_type: str | None = None
    tags: list[str] | None = None
    reason: str = ""
    confidence: float = 0.0

    def affected_ids(self) -> set[str]:
        ids = set(self.memory_ids)
        if self.memory_id:
            ids.add(self.memory_id)
        return ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "memory_id": self.memory_id,
            "memory_ids": self.memory_ids,
            "content": self.content,
            "target_id": self.target_id,
            "memory_type": self.memory_type,
            "tags": self.tags,
            "reason": self.reason,
            "confidence": self.confidence,
        }

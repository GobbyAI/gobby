import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

# Stable namespace for deterministic memory UUIDs (uuid5)
MEMORY_UUID_NAMESPACE = uuid.UUID("a3b2c1d0-1234-5678-9abc-def012345678")

Visibility = Literal["active", "hidden", "all"]
"""Three-state memory visibility filter: visible rows, dream-hidden rows, or both."""


def visibility_predicate(visibility: Visibility, *, column: str = "deleted_at") -> str:
    """Return a bare SQL predicate enforcing the visibility filter.

    ``"active"`` -> visible rows only, ``"hidden"`` -> dream-hidden rows only,
    ``"all"`` -> no filter (empty string). Raises ``ValueError`` on an unknown
    value so bad input fails loudly at the storage boundary rather than silently
    leaking hidden rows.
    """
    if visibility == "active":
        return f"{column} IS NULL"
    if visibility == "hidden":
        return f"{column} IS NOT NULL"
    if visibility == "all":
        return ""
    raise ValueError(f"Invalid visibility: {visibility!r}")


@dataclass
class MemoryCrossRef:
    """A link between two related memories with a similarity score."""

    source_id: str
    target_id: str
    similarity: float
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MemoryCrossRef":
        return cls(
            source_id=row["source_id"],
            target_id=row["target_id"],
            similarity=row["similarity"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "similarity": self.similarity,
            "created_at": self.created_at,
        }


@dataclass
class Memory:
    id: str
    memory_type: Literal["fact", "preference", "pattern", "context"]
    content: str
    created_at: str
    updated_at: str
    project_id: str | None = None
    source_type: Literal["user", "agent"] = "agent"
    source_session_id: str | None = None
    access_count: int = 0
    last_accessed_at: str | None = None
    tags: list[str] | None = None
    deleted_at: str | None = None  # NULL = visible; non-NULL = dream-hidden (recoverable)
    dream_action: Literal["review", "delete"] | None = None  # why dream hid the row
    last_dreamed_at: str | None = None  # cooldown cursor for the nightly active sweep
    similarity: float | None = None  # Set at search time, not persisted
    search_via: str | None = None  # Set at search time, not persisted
    ranking_score: float | None = None  # Hybrid retrieval rank, not persisted
    raw_semantic_score: float | None = None  # Raw Qdrant score, not persisted
    temporal_decay_factor: float | None = None  # Search-time decay, not persisted
    ranking_mode: str | None = None  # Search-time scoring mode, not persisted

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Memory":
        tags_json = row["tags"]
        tags = json.loads(tags_json) if tags_json else []

        raw_source_type = row["source_type"]
        source_type = cast(
            Literal["user", "agent"],
            raw_source_type if raw_source_type in ("user", "agent") else "agent",
        )

        return cls(
            id=row["id"],
            memory_type=row["memory_type"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            project_id=row["project_id"],
            source_type=source_type,
            source_session_id=row["source_session_id"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            tags=tags,
            deleted_at=row.get("deleted_at"),
            dream_action=row.get("dream_action"),
            last_dreamed_at=row.get("last_dreamed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "memory_type": self.memory_type,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "project_id": self.project_id,
            "source_type": self.source_type,
            "source_session_id": self.source_session_id,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "tags": self.tags,
            "deleted_at": self.deleted_at,
            "dream_action": self.dream_action,
            "last_dreamed_at": self.last_dreamed_at,
        }
        if self.similarity is not None:
            data["similarity"] = self.similarity
        if self.search_via is not None:
            data["search_via"] = self.search_via
        if self.ranking_score is not None:
            data["ranking_score"] = self.ranking_score
        if self.raw_semantic_score is not None:
            data["raw_semantic_score"] = self.raw_semantic_score
        if self.temporal_decay_factor is not None:
            data["temporal_decay_factor"] = self.temporal_decay_factor
        if self.ranking_mode is not None:
            data["ranking_mode"] = self.ranking_mode
        return data

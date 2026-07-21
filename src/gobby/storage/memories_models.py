import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.datetime import normalize_datetime_model

# Stable namespace for deterministic memory UUIDs (uuid5)
MEMORY_UUID_NAMESPACE = uuid.UUID("a3b2c1d0-1234-5678-9abc-def012345678")

Visibility = Literal["active", "hidden", "all"]
_ALLOWED_VISIBILITY_COLUMNS = frozenset({"deleted_at", "memories.deleted_at", "m.deleted_at"})
"""Three-state memory visibility filter: visible rows, dream-hidden rows, or both."""


def visibility_predicate(visibility: Visibility, *, column: str = "deleted_at") -> str:
    """Return a bare SQL predicate enforcing the visibility filter.

    ``"active"`` -> visible rows only, ``"hidden"`` -> dream-hidden rows only,
    ``"all"`` -> no filter (empty string). Raises ``ValueError`` on an unknown
    value so bad input fails loudly at the storage boundary rather than silently
    leaking hidden rows.
    """
    if column not in _ALLOWED_VISIBILITY_COLUMNS:
        raise ValueError(f"Invalid visibility column: {column!r}")
    if visibility == "active":
        return f"{column} IS NULL"
    if visibility == "hidden":
        return f"{column} IS NOT NULL"
    if visibility == "all":
        return ""
    raise ValueError(f"Invalid visibility: {visibility!r}")


@normalize_datetime_model(required=("created_at",))
@dataclass
class MemoryCrossRef:
    """A link between two related memories with a similarity score."""

    source_id: str
    target_id: str
    similarity: float
    created_at: datetime

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


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "last_accessed_at",
        "deleted_at",
        "last_dreamed_at",
    ),
)
@dataclass
class Memory:
    id: str
    memory_type: Literal["fact", "preference", "pattern", "context"]
    content: str
    created_at: datetime
    updated_at: datetime
    project_id: str = PERSONAL_PROJECT_ID
    is_global: bool = False
    source_type: Literal["user", "agent"] = "agent"
    source_session_id: str | None = None
    access_count: int = 0
    last_accessed_at: datetime | None = None
    graph_processed: bool = True
    graph_attempts: int = 0
    graph_status: Literal["pending", "completed", "failed"] = "completed"
    vector_needs_reindex: bool = False
    tags: list[str] | None = None
    deleted_at: datetime | None = None  # NULL = visible; non-NULL = dream-hidden (recoverable)
    dream_action: Literal["review", "delete"] | None = None  # why dream hid the row
    last_dreamed_at: datetime | None = None  # cooldown cursor for the nightly active sweep
    similarity: float | None = None  # Set at search time, not persisted
    search_via: str | None = None  # Set at search time, not persisted
    ranking_score: float | None = None  # Hybrid retrieval rank, not persisted
    raw_semantic_score: float | None = None  # Raw Qdrant score, not persisted
    temporal_decay_factor: float | None = None  # Search-time decay, not persisted
    ranking_mode: str | None = None  # Search-time scoring mode, not persisted

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Memory":
        tags_json = row["tags"]
        if isinstance(tags_json, str):
            tags = json.loads(tags_json) if tags_json else []
        elif isinstance(tags_json, list):
            tags = tags_json
        else:
            tags = []

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
            is_global=bool(row.get("is_global", False)),
            source_type=source_type,
            source_session_id=row["source_session_id"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            graph_processed=bool(row.get("graph_processed", True)),
            graph_attempts=int(row.get("graph_attempts", 0)),
            graph_status=cast(
                Literal["pending", "completed", "failed"],
                row.get("graph_status", "completed"),
            ),
            vector_needs_reindex=bool(row.get("vector_needs_reindex", False)),
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
            "is_global": self.is_global,
            "source_type": self.source_type,
            "source_session_id": self.source_session_id,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "graph_processed": self.graph_processed,
            "graph_attempts": self.graph_attempts,
            "graph_status": self.graph_status,
            "vector_needs_reindex": self.vector_needs_reindex,
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

"""
Persistence configuration module.

Contains storage and sync-related Pydantic config models:
- MemoryConfig: Memory system settings (injection, decay, search)
- MemorySyncConfig: Memory file sync settings (debounce, export path)

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryConfig",
    "MemoryBackupConfig",
]


class MemoryConfig(BaseModel):
    """Memory system configuration."""

    model_config = {"extra": "ignore"}

    enabled: bool = Field(
        default=True,
        description="Enable persistent memory system",
    )
    backend: str = Field(
        default="local",
        description=(
            "Storage backend for memories. Options: "
            "'local' (default, direct SQLite via LocalMemoryManager), "
            "'null' (no persistence, for testing)"
        ),
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="Embedding model for semantic search",
    )
    qdrant_path: str | None = Field(
        default=None,
        description=(
            "Directory path for embedded Qdrant storage (on-disk, zero Docker). "
            "Mutually exclusive with qdrant_url. "
            "Default set by runner to ~/.gobby/services/qdrant/"
        ),
    )
    qdrant_url: str | None = Field(
        default=None,
        description=(
            "URL for remote Qdrant server. "
            "Mutually exclusive with qdrant_path. "
            "Example: 'http://localhost:6333'"
        ),
    )
    qdrant_api_key: str | None = Field(
        default=None,
        description=(
            "API key for remote Qdrant server. "
            "Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )
    neo4j_url: str | None = Field(
        default="http://localhost:8474",
        description=(
            "Neo4j HTTP API URL for knowledge graph visualization. "
            "Uses port 8474 (mapped from 7474) to avoid conflicts. "
            "Set automatically by 'gobby install neo4j'."
        ),
    )
    neo4j_auth: str | None = Field(
        default="neo4j:gobbyneo4j",
        description=(
            "Neo4j authentication in 'user:password' format. "
            "Default matches docker-compose.neo4j.yml fallback password. "
            "Set automatically during 'gobby install neo4j'. "
            "Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )
    neo4j_database: str = Field(
        default="neo4j",
        description="Neo4j database name",
    )
    auto_crossref: bool = Field(
        default=False,
        description="Automatically create cross-references between similar memories",
    )
    crossref_threshold: float = Field(
        default=0.3,
        description="Minimum similarity score to create a cross-reference (0.0-1.0)",
    )
    crossref_max_links: int = Field(
        default=5,
        description="Maximum number of cross-references to create per memory",
    )
    access_debounce_seconds: int = Field(
        default=60,
        description="Minimum seconds between access stat updates for the same memory",
    )
    neo4j_graph_search: bool = Field(
        default=True,
        description="Enable graph-augmented search (Neo4j entity vector search merged with Qdrant via RRF)",
    )
    neo4j_graph_min_score: float = Field(
        default=0.5,
        description="Minimum entity vector similarity score for graph search (0.0-1.0)",
    )
    neo4j_rrf_k: int = Field(
        default=60,
        description="RRF constant for merging Qdrant and graph results (higher = more uniform weighting)",
    )

    @field_validator("crossref_threshold")
    @classmethod
    def validate_probability(cls, v: float) -> float:
        """Validate value is between 0.0 and 1.0."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("Value must be between 0.0 and 1.0")
        return v

    @field_validator("crossref_max_links")
    @classmethod
    def validate_positive_links(cls, v: int) -> int:
        """Validate crossref_max_links is positive."""
        if v < 1:
            raise ValueError("crossref_max_links must be at least 1")
        return v

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        """Validate backend is a supported storage option."""
        # Accept "sqlite" as backwards-compat alias for "local"
        if v == "sqlite":
            return "local"
        valid_backends = {"local", "null"}
        if v not in valid_backends:
            raise ValueError(f"Invalid backend '{v}'. Must be one of: {sorted(valid_backends)}")
        return v

    @model_validator(mode="after")
    def validate_qdrant_exclusivity(self) -> "MemoryConfig":
        """Validate qdrant_path and qdrant_url are mutually exclusive."""
        if self.qdrant_path and self.qdrant_url:
            raise ValueError(
                "qdrant_path and qdrant_url are mutually exclusive. "
                "Use qdrant_path for embedded mode or qdrant_url for remote mode."
            )
        return self


class MemoryBackupConfig(BaseModel):
    """Memory backup configuration (filesystem export).

    Note: This was previously named MemorySyncConfig.
    Memories are stored in the database via MemoryBackendProtocol; this config
    controls the JSONL backup file export (for disaster recovery/migration).
    """

    enabled: bool = Field(
        default=True,
        description="Enable memory synchronization to filesystem",
    )
    export_debounce: float = Field(
        default=5.0,
        description="Seconds to wait before exporting after a change",
    )
    export_path: Path = Field(
        default=Path(".gobby/memories.jsonl"),
        description="Path to the memories export file (relative to project root or absolute)",
    )

    @field_validator("export_debounce")
    @classmethod
    def validate_positive(cls, v: float) -> float:
        """Validate value is non-negative."""
        if v < 0:
            raise ValueError("Value must be non-negative")
        return v

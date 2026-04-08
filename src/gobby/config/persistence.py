"""
Persistence configuration module.

Contains storage and sync-related Pydantic config models:
- DatabasesConfig: Shared database connections (Qdrant, Neo4j)
- EmbeddingsConfig: Embedding model settings (shared by memory, tools, code index)
- MemoryConfig: Memory-specific behavior (crossrefs, decay, search)
- MemoryBackupConfig: Memory file sync settings (debounce, export path)

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

__all__ = [
    "DatabasesConfig",
    "EmbeddingsConfig",
    "MemoryConfig",
    "MemoryBackupConfig",
    "Neo4jConfig",
    "QdrantConfig",
]


# ---------------------------------------------------------------------------
# Database connection configs (shared infrastructure)
# ---------------------------------------------------------------------------


class QdrantConfig(BaseModel):
    """Qdrant vector database connection configuration."""

    model_config = {"extra": "ignore"}

    url: str | None = Field(
        default="http://localhost:6333",
        description="URL for Qdrant server (Docker-managed).",
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "API key for Qdrant server (optional for local access). "
            "Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )
    port: int = Field(
        default=6333,
        description="HTTP port for Qdrant server",
    )
    collection_prefix: str = Field(
        default="code_symbols_",
        description="Qdrant collection name prefix for code symbol embeddings",
    )


class Neo4jConfig(BaseModel):
    """Neo4j graph database connection configuration."""

    model_config = {"extra": "ignore"}

    url: str | None = Field(
        default="http://localhost:8474",
        description=(
            "Neo4j HTTP API URL. "
            "Uses port 8474 (mapped from 7474) to avoid conflicts. "
            "Set automatically by 'gobby install'."
        ),
    )
    auth: str | None = Field(
        default=None,
        description=(
            "Neo4j authentication in 'user:password' format. "
            "Must be provided when Neo4j is enabled. "
            "Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )
    database: str = Field(
        default="neo4j",
        description="Neo4j database name",
    )
    graph_search: bool = Field(
        default=True,
        description="Enable graph-augmented search (entity vector search merged via RRF)",
    )
    graph_min_score: float = Field(
        default=0.5,
        description="Minimum entity vector similarity score for graph search (0.0-1.0)",
    )
    rrf_k: int = Field(
        default=60,
        description="RRF constant for merging Qdrant and graph results (higher = more uniform weighting)",
    )

    @field_validator("graph_min_score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        """Validate score is between 0.0 and 1.0."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("Value must be between 0.0 and 1.0")
        return v


class DatabasesConfig(BaseModel):
    """Shared database connection configuration for Qdrant and Neo4j."""

    model_config = {"extra": "ignore"}

    qdrant: QdrantConfig = Field(
        default_factory=QdrantConfig,
        description="Qdrant vector database connection",
    )
    neo4j: Neo4jConfig = Field(
        default_factory=Neo4jConfig,
        description="Neo4j graph database connection",
    )


# ---------------------------------------------------------------------------
# Embeddings config (shared by memory, tools, code index)
# ---------------------------------------------------------------------------


class EmbeddingsConfig(BaseModel):
    """Embedding model configuration — single source of truth for all subsystems.

    Used by: memory, code index, search (UnifiedSearcher, SkillSearch),
    MCP proxy (SemanticToolSearch). If a vision/multimodal model is needed
    in the future, add a nested ``vision: EmbeddingsConfig`` field; the
    current flat fields remain the text embedding defaults.
    """

    model_config = {"extra": "ignore"}

    model: str = Field(
        default="nomic-embed-text",
        description="Embedding model for semantic search",
    )
    dim: int = Field(
        default=768,
        description=(
            "Dimensionality of embedding vectors. Must match the model's output: "
            "768 for nomic-embed-text (default), 1536 for text-embedding-3-small, 1024 for BGE-M3."
        ),
    )
    api_base: str | None = Field(
        default=None,
        description=(
            "API base URL for the embedding endpoint. "
            "Use for local models (e.g., 'http://localhost:11434/v1' for Ollama). "
            "When None, uses the provider's default endpoint."
        ),
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "Explicit API key for the embedding endpoint. "
            "Overrides auto-resolved key from secrets/env. "
            "Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )

    @field_validator("dim")
    @classmethod
    def validate_dim(cls, v: int) -> int:
        """Validate dim is positive."""
        if v < 1:
            raise ValueError("dim must be at least 1")
        return v


# ---------------------------------------------------------------------------
# Memory-specific behavior config
# ---------------------------------------------------------------------------


class MemoryConfig(BaseModel):
    """Memory system configuration.

    Database connections (Qdrant, Neo4j) live in DatabasesConfig.
    Embedding model settings live in EmbeddingsConfig.
    This config only contains memory-specific behavior settings.
    """

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
    kg_provider: str = Field(
        default="claude",
        description="LLM provider for knowledge graph entity/relationship extraction",
    )
    kg_model: str = Field(
        default="haiku",
        description="Model for KG extraction (cheap/fast recommended)",
    )
    code_link_min_score: float = Field(
        default=0.82,
        description="Minimum cosine similarity for RELATES_TO_CODE edges between memory entities and code symbols",
    )
    temporal_decay_half_life_days: float = Field(
        default=30.0,
        description=(
            "Half-life in days for temporal decay scoring in memory search. "
            "Memories lose half their score boost after this many days since last update. "
            "Set to 0 to disable temporal decay."
        ),
    )
    min_recall_score: float = Field(
        default=0.6,
        description=(
            "Minimum similarity score for memory recall (0.0-1.0). "
            "Memories below this threshold are excluded from search results. "
            "Applies to cosine similarity after source boost and temporal decay."
        ),
    )

    @field_validator("crossref_threshold", "code_link_min_score", "min_recall_score")
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

    @field_validator("temporal_decay_half_life_days")
    @classmethod
    def validate_half_life(cls, v: float) -> float:
        """Validate temporal_decay_half_life_days is non-negative."""
        if v < 0:
            raise ValueError("temporal_decay_half_life_days must be >= 0")
        return v

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        """Validate backend is a supported storage option."""
        if v == "sqlite":
            return "local"
        valid_backends = {"local", "null"}
        if v not in valid_backends:
            raise ValueError(f"Invalid backend '{v}'. Must be one of: {sorted(valid_backends)}")
        return v


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

"""
Persistence configuration module.

Contains storage and sync-related Pydantic config models:
- DatabasesConfig: Shared database connections (Qdrant, FalkorDB)
- EmbeddingsConfig: Embedding model settings (shared by memory, tools, code index)
- MemoryConfig: Memory-specific behavior (crossrefs, decay, search)
- MemoryBackupConfig: Memory file sync settings (debounce, export path)

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from gobby.config.feature_base import FeatureDefaultConfig, FeatureProfile

logger = logging.getLogger(__name__)

__all__ = [
    "DatabasesConfig",
    "EmbeddingsConfig",
    "MemoryKnowledgeGraphConfig",
    "MemoryDreamConfig",
    "MemoryConfig",
    "MemoryBackupConfig",
    "FalkorConfig",
    "QdrantConfig",
    "is_falkordb_enabled",
    "validate_falkordb_password",
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


def validate_falkordb_password(value: str) -> str:
    """Reject FalkorDB passwords whose Docker boundary cannot round-trip."""
    if not value:
        raise ValueError("FalkorDB password must not be empty")
    if any(ch.isspace() for ch in value):
        raise ValueError("FalkorDB password must not contain whitespace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("FalkorDB password must not contain ASCII control characters")
    if any(ord(ch) > 0x7E for ch in value):
        raise ValueError(
            "FalkorDB password must use printable ASCII only (Docker round-trip constraint)"
        )
    return value


def _validate_optional_falkordb_password(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_falkordb_password(value)


class FalkorConfig(BaseModel):
    """FalkorDB graph database connection configuration."""

    model_config = {"extra": "forbid"}

    host: str = Field(
        default="127.0.0.1",
        description="FalkorDB host. Docker default: 127.0.0.1 (port-mapped).",
    )
    port: int = Field(
        default=16379,
        description=(
            "FalkorDB port. Docker host-side: 16379 (remapped from container 6379 to "
            "avoid system Redis conflicts). 0.4.0 supports Docker only — see 3.1's mode decision."
        ),
    )
    password: str | None = Field(
        default=None,
        description=(
            "FalkorDB password (Redis AUTH). `databases.falkordb.password` resolves to "
            "secret-store name `falkordb_password` to avoid colliding with the web login "
            "`auth.password` secret. Must be provided when "
            "FalkorDB is enabled. Supports ${ENV_VAR} pattern for env var expansion at load time."
        ),
    )
    graph_name: str = Field(
        default="gobby_kg",
        description=(
            "FalkorDB graph key. Memory KG uses 'gobby_kg', code graph uses 'gobby_code'. "
            "Set per consumer; this is the default for the memory KG client."
        ),
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

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str | None) -> str | None:
        return _validate_optional_falkordb_password(value)

    @field_validator("graph_min_score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Value must be between 0.0 and 1.0")
        return v


class DatabasesConfig(BaseModel):
    """Shared database connection configuration for Qdrant and FalkorDB."""

    model_config = {"extra": "ignore"}

    qdrant: QdrantConfig = Field(
        default_factory=QdrantConfig,
        description="Qdrant vector database connection",
    )
    falkordb: FalkorConfig = Field(
        default_factory=FalkorConfig,
        description="FalkorDB graph database connection",
    )


def is_falkordb_enabled(databases: DatabasesConfig) -> bool:
    """Whether the FalkorDB knowledge-graph backend is active.

    Activation signal: the installer wrote `databases.falkordb.password`
    into config_store and `load_config(config_store=..., secret_resolver=...)`
    successfully resolved it. Default `FalkorConfig.password = None` so the
    truthy check distinguishes installed-and-resolved from unconfigured.

    Pass a `DatabasesConfig` instance (e.g. `runner.config.databases`), NOT the
    top-level config — `config` has no top-level `falkordb` attribute.
    """
    return bool(databases.falkordb.password)


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
            "Installers store this as an encrypted $secret: reference in config_store."
        ),
    )
    query_prefix: str | None = Field(
        default=None,
        description="Optional text prefix prepended to semantic-search queries before embedding.",
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


class MemoryKnowledgeGraphConfig(FeatureDefaultConfig):
    """LLM configuration for memory knowledge-graph extraction."""


class MemoryDreamConfig(FeatureDefaultConfig):
    """Configuration for scheduled memory dream maintenance."""

    enabled: bool = Field(
        default=True,
        description="Enable scheduled memory dream maintenance",
    )
    schedule_cron: str = Field(
        default="0 3 * * *",
        description="Cron expression for the system memory dream job",
    )
    prompt_path: str = Field(
        default="memory/dream",
        description="Prompt template path for memory dream planning",
    )
    max_tokens: int = Field(
        default=8192,
        description="Maximum tokens for memory dream planner responses",
    )
    scan_limit: int = Field(
        default=500,
        description=(
            "Deprecated and ignored. The sweep now pages every active memory "
            "via page_size; retained so existing configs still load."
        ),
    )
    planner_batch_size: int = Field(
        default=25,
        description="Maximum stale candidates sent to the planner in one LLM call",
    )
    planner_max_concurrency: int = Field(
        default=3,
        description="Maximum concurrent planner LLM calls per dream run",
    )
    max_scan_rows: int = Field(
        default=5000,
        description=(
            "Deprecated and ignored. The streaming page-and-apply loop is bounded "
            "by page_size and the redream cooldown, not a global scan cap."
        ),
    )
    candidate_page_timeout_seconds: float = Field(
        default=10.0,
        description="Maximum seconds to wait for one stale-candidate memory page",
    )
    stale_age_days: int = Field(
        default=30,
        description=(
            "Deprecated and ignored. Dream now reviews every active memory once "
            "per cooldown window instead of gating on row age."
        ),
    )
    page_size: int = Field(
        default=200,
        description="Active memories hydrated per page in the streaming dream sweep",
    )
    redream_after_hours: int = Field(
        default=20,
        description=(
            "Cooldown in hours before a dreamed memory is eligible again; makes the "
            "nightly sweep idempotent across same-day re-runs"
        ),
    )
    purge_delete_after_days: int = Field(
        default=30,
        description="Grace days before soft-hidden delete-action memories are hard-purged",
    )
    purge_review_after_days: int = Field(
        default=90,
        description="Grace days before soft-hidden review-action memories are hard-purged",
    )
    run_retention_days: int = Field(
        default=30,
        description="Days to retain memory_dream_runs/_snapshots history before pruning",
    )
    min_action_confidence: float = Field(
        default=0.72,
        description="Minimum confidence required for mutating dream actions",
    )
    min_delete_confidence: float = Field(
        default=0.85,
        description="Minimum confidence required for delete actions",
    )
    min_rescope_confidence: float = Field(
        default=0.85,
        description="Minimum confidence required for scope-changing dream actions",
    )
    include_global_memories: bool = Field(
        default=True,
        description="Include global memories when a project-scoped dream runs",
    )
    reconcile_after_apply: bool = Field(
        default=True,
        description="Reconcile secondary memory stores after applying mutations",
    )
    reconcile_after_revert: bool = Field(
        default=True,
        description="Reconcile secondary memory stores after reverting mutations",
    )
    profile: FeatureProfile = Field(
        default=FeatureProfile.MID,
        description="Provider-agnostic capability profile for memory dream planning",
    )

    @field_validator(
        "scan_limit",
        "planner_batch_size",
        "planner_max_concurrency",
        "max_scan_rows",
        "stale_age_days",
        "max_tokens",
        "page_size",
        "redream_after_hours",
        "purge_delete_after_days",
        "purge_review_after_days",
        "run_retention_days",
    )
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        """Validate positive integer settings."""
        if v < 1:
            raise ValueError("value must be at least 1")
        return v

    @field_validator("candidate_page_timeout_seconds")
    @classmethod
    def validate_positive_float(cls, v: float) -> float:
        """Validate positive float settings."""
        if v <= 0.0:
            raise ValueError("value must be greater than 0")
        return v

    @field_validator("min_action_confidence", "min_delete_confidence", "min_rescope_confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        """Validate confidence thresholds."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


class MemoryConfig(BaseModel):
    """Memory system configuration.

    Database connections (Qdrant, FalkorDB) live in DatabasesConfig.
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
            "'local' (default, PostgreSQL hub via LocalMemoryManager), "
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
    kg: MemoryKnowledgeGraphConfig = Field(
        default_factory=MemoryKnowledgeGraphConfig,
        description="LLM feature routing configuration for knowledge graph extraction",
    )
    dream: MemoryDreamConfig = Field(
        default_factory=MemoryDreamConfig,
        description="Memory dream maintenance configuration",
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
    graph_edge_weighting: bool = Field(
        default=False,
        description=(
            "Enable weighted entity->entity edges in the memory knowledge graph. "
            "When on, typed relations carry a cosine-similarity weight (and a "
            "reinforcement count as metadata). Default off; adopt per recall benchmark."
        ),
    )
    materialize_cooccurrence: bool = Field(
        default=False,
        description=(
            "Materialize derived CO_OCCURS support edges between entities that share "
            "a memory, densifying the traversable entity layer. Default off."
        ),
    )
    graph_edge_decay: bool = Field(
        default=False,
        description=(
            "Apply edge-recency decay during knowledge-graph traversal candidate "
            "selection (reshapes which neighbors survive the traversal cap). Default off."
        ),
    )
    edge_half_life_days: float = Field(
        default=30.0,
        description=(
            "Half-life in days for knowledge-graph edge-recency decay "
            "(graph_edge_decay). Set to 0 to disable edge decay."
        ),
    )
    cluster_recall_expansion: bool = Field(
        default=False,
        description=(
            "Enable read-path recall expansion through stored _Entity.cluster_id labels. "
            "Default off; labels are written only by the offline reclustering tool."
        ),
    )
    cluster_expansion_per_entity: int = Field(
        default=3,
        description=(
            "Maximum co-clustered non-seed entities to add per seed or traversed entity "
            "when cluster_recall_expansion is enabled."
        ),
    )
    cluster_min_cluster_size: int = Field(
        default=5,
        description=(
            "HDBSCAN min_cluster_size for offline entity embedding reclustering. "
            "Daemon-global static default; per-project tuning is intentionally separate."
        ),
    )
    cluster_min_samples: int | None = Field(
        default=2,
        description=(
            "HDBSCAN min_samples for offline entity embedding reclustering. "
            "None delegates to HDBSCAN's min_cluster_size behavior."
        ),
    )
    recall_signal_logging: bool = Field(
        default=False,
        description=(
            "Append observational recall/search ranking signal events to a dedicated "
            "JSONL file (default ~/.gobby/logs/recall_signal.jsonl). Purely "
            "observational; search, recall, and injection behavior are unchanged."
        ),
    )
    recall_signal_log_path: str | None = Field(
        default=None,
        description=(
            "Override path for the recall-signal JSONL log. None resolves to "
            "~/.gobby/logs/recall_signal.jsonl. '~' is expanded."
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

    @field_validator("edge_half_life_days")
    @classmethod
    def validate_edge_half_life(cls, v: float) -> float:
        """Validate edge_half_life_days is non-negative."""
        if v < 0:
            raise ValueError("edge_half_life_days must be >= 0")
        return v

    @field_validator("cluster_expansion_per_entity")
    @classmethod
    def validate_cluster_expansion(cls, v: int) -> int:
        """Validate cluster expansion fanout is non-negative."""
        if v < 0:
            raise ValueError("cluster_expansion_per_entity must be >= 0")
        return v

    @field_validator("cluster_min_cluster_size")
    @classmethod
    def validate_cluster_min_cluster_size(cls, v: int) -> int:
        """Validate HDBSCAN min_cluster_size."""
        if v < 2:
            raise ValueError("cluster_min_cluster_size must be >= 2")
        return v

    @field_validator("cluster_min_samples")
    @classmethod
    def validate_cluster_min_samples(cls, v: int | None) -> int | None:
        """Validate HDBSCAN min_samples while preserving explicit None."""
        if v is not None and v < 1:
            raise ValueError("cluster_min_samples must be None or >= 1")
        return v

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        """Validate backend is a supported storage option."""
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

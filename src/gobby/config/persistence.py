"""
Persistence configuration module.

Contains storage and sync-related Pydantic config models:
- DatabasesConfig: Shared database connections (Qdrant, FalkorDB)
- EmbeddingsConfig: Embedding model settings (shared by memory, tools, code index)
- MemoryConfig: Memory-specific behavior (crossrefs, decay, search)
- MemoryBackupConfig: Memory JSONL backup settings

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from gobby.config.feature_base import FeatureDefaultConfig, FeatureProfile
from gobby.config.url_validation import validate_optional_endpoint_url

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
        default=None,
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

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return validate_optional_endpoint_url(value, field_name="url")


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
            "secret-store name `falkordb_password` for stable disambiguation. Must be provided when "
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

    published_host: str | None = Field(
        default=None,
        description="DNS host clients use to reach shared datastores",
    )
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

    The resolved active projection supplies a non-empty FalkorDB password when
    the backend is configured. ``FalkorConfig.password`` otherwise defaults to
    ``None``.
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
            "Installers store this as an encrypted secret-store reference in config_store."
        ),
    )
    query_prefix: str | None = Field(
        default=None,
        description="Optional text prefix prepended to semantic-search queries before embedding.",
    )
    catalog_key: str | None = Field(
        default=None,
        description=(
            "Quant-qualified stable identity from the embedding catalog "
            "(e.g. 'qwen3-8b-q8'). Decoupled from the provider model ID. "
            "When set, gcode and diagnostics resolve model properties from this key."
        ),
    )

    @field_validator("dim")
    @classmethod
    def validate_dim(cls, v: int) -> int:
        """Validate dim is positive."""
        if v < 1:
            raise ValueError("dim must be at least 1")
        return v

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str | None) -> str | None:
        return validate_optional_endpoint_url(value, field_name="api_base")


# ---------------------------------------------------------------------------
# Memory-specific behavior config
# ---------------------------------------------------------------------------


class MemoryKnowledgeGraphConfig(FeatureDefaultConfig):
    """LLM configuration for memory knowledge-graph extraction."""

    max_rebuild_concurrency: int = Field(
        default=2,
        ge=1,
        description="Maximum concurrent add_to_graph calls during KG rebuild jobs.",
    )


class MemoryDreamConfig(FeatureDefaultConfig):
    """Configuration for scheduled memory dream maintenance."""

    enabled: bool = Field(
        default=True,
        description="Enable scheduled memory dream maintenance",
    )
    schedule_cron: str = Field(
        default="0 2 * * *",
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
    planner_batch_size: int = Field(
        default=25,
        description="Maximum stale candidates sent to the planner in one LLM call",
    )
    planner_batch_max_chars: int = Field(
        default=100_000,
        ge=10_000,
        description="Soft maximum rendered candidate characters per planner LLM call",
    )
    related_evidence_enabled: bool = Field(
        default=True,
        description="Attach related memories as evidence during dream planning",
    )
    related_evidence_top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum related memories attached to each dream candidate",
    )
    related_evidence_fetch_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum retrieval hits fetched before related-evidence ranking",
    )
    write_supersession_mark_due_enabled: bool = Field(
        default=True,
        description="Mark older related memories due after superseding writes",
    )
    max_runtime_seconds: int = Field(
        default=14400,
        description=(
            "Seconds from coordinator start during which new dream work units "
            "may be admitted; the final admitted unit may still finish under "
            "its own work-unit timeout"
        ),
    )
    work_unit_timeout_seconds: float = Field(
        default=1500.0,
        description="Maximum seconds for one dream work unit, selection through apply",
    )
    evidence_channel_timeout_seconds: float = Field(
        default=30.0,
        description=(
            "Total budget in seconds for one required-evidence channel attempt, "
            "including connection-pool admission"
        ),
    )
    evidence_retry_attempts: int = Field(
        default=3,
        description=(
            "Attempts per required evidence channel before the work unit stops "
            "as a dependency failure"
        ),
    )
    evidence_phase_timeout_seconds: float = Field(
        default=210.0,
        description=(
            "Ceiling in seconds for one work unit's whole evidence phase across "
            "channels and retries"
        ),
    )
    dry_run_max_candidates: int = Field(
        default=1000,
        description="Maximum candidates evaluated by one dry-run preview",
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
        "planner_batch_size",
        "planner_batch_max_chars",
        "max_tokens",
        "max_runtime_seconds",
        "evidence_retry_attempts",
        "dry_run_max_candidates",
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

    @field_validator(
        "work_unit_timeout_seconds",
        "evidence_channel_timeout_seconds",
        "evidence_phase_timeout_seconds",
    )
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
    graph_related_expansion_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "End-to-end deadline in seconds for related-memory graph expansion during "
            "search. Changes take effect after daemon restart."
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
    recall_signal_log_max_mb: int = Field(
        default=50,
        ge=1,
        description=(
            "Rotate the recall-signal JSONL when the live file reaches this many "
            "megabytes: '.1' shifts to '.2' and the live file becomes '.1', "
            "bounding retained size to roughly three times the cap (#18196)."
        ),
    )
    recall_signal_hub: bool = Field(
        default=False,
        description=(
            "Mirror recall-signal events into the Postgres hub tables "
            "(recall_signal_requests/recall_signal_hits) and record durable "
            "injection outcomes (recall_injection_outcomes) at delivery time. "
            "Purely observational; independent of recall_signal_logging (#17196)."
        ),
    )
    digest_shadow_usefulness: bool = Field(
        default=False,
        description=(
            "Judge the complete returned recall candidate set during the digest pass "
            "and persist label_source='digest_shadow' evidence. Requires "
            "recall_signal_hub so every judged request and candidate is durable."
        ),
    )

    use_fitted_recall_constants: bool = Field(
        default=False,
        description=(
            "Apply the pooled fitted recall ranking constants from a shipped "
            "#17198 gate decision record as the daemon-global defaults. The "
            "static constants remain the one-flag rollback floor: a missing, "
            "malformed, or non-shipping (reject) record keeps static behavior "
            "even when enabled (#17200)."
        ),
    )
    fitted_recall_decision_path: str | None = Field(
        default=None,
        description=(
            "Override path for the recall refit gate decision record JSON. None "
            "resolves to ~/.gobby/recall_refit_decision.json. '~' is expanded."
        ),
    )
    recall_drift_monitor_enabled: bool = Field(
        default=True,
        description=(
            "Run the periodic recall-quality drift monitor (#17201). It replays "
            "recent labeled recall signals under the effective constants and "
            "alarms when live pairwise accuracy regresses beyond "
            "recall_drift_accuracy_drop below the recorded holdout baseline."
        ),
    )
    recall_drift_interval_hours: float = Field(
        default=24.0,
        description="Hours between recall-drift monitor checks in the daemon.",
    )
    recall_drift_accuracy_drop: float = Field(
        default=0.05,
        description=(
            "Alarm threshold: live pairwise accuracy this far below the recorded "
            "holdout baseline accuracy raises the drift alarm."
        ),
    )

    @model_validator(mode="after")
    def validate_digest_shadow_signal_hub(self) -> "MemoryConfig":
        """Require the durable signal hub whenever shadow judging is enabled."""
        if self.digest_shadow_usefulness and not self.recall_signal_hub:
            raise ValueError("digest_shadow_usefulness requires recall_signal_hub=true")
        return self

    @field_validator("recall_drift_interval_hours")
    @classmethod
    def validate_drift_positive(cls, v: float) -> float:
        """Validate the drift-monitor cadence is positive."""
        if v <= 0:
            raise ValueError("Value must be > 0")
        return v

    @field_validator("recall_drift_accuracy_drop")
    @classmethod
    def validate_drift_accuracy_drop(cls, v: float) -> float:
        """Validate the drift alarm threshold is a meaningful accuracy delta."""
        if not (0.0 < v < 1.0):
            raise ValueError("recall_drift_accuracy_drop must be in (0.0, 1.0)")
        return v

    @field_validator("crossref_threshold", "code_link_min_score")
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
    """Memory JSONL backup configuration for disaster recovery and migration."""

    enabled: bool = Field(
        default=True,
        description="Enable memory JSONL backup and restore tools",
    )
    backup_path: Path = Field(
        default=Path(".gobby/memories.jsonl"),
        description="Path to the memories backup file (relative to project root or absolute)",
    )

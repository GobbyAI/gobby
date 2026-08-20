"""Code index configuration."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.config.feature_base import FeatureDefaultConfig


class CodeIndexSymbolSummaryConfig(FeatureDefaultConfig):
    """Configuration for LLM-generated code-index symbol summaries."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-generated symbol summaries",
    )
    batch_size: int = Field(
        default=20,
        ge=1,
        description="Max symbols to summarize per maintenance pass",
    )
    max_concurrency: int = Field(
        default=2,
        ge=1,
        description="Maximum concurrent symbol summary LLM calls",
    )
    max_tokens: int = Field(
        default=100,
        ge=1,
        description="Maximum tokens for each symbol summary generation",
    )


class CodeIndexConfig(BaseModel):
    """Configuration for native AST-based code indexing."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Enable code indexing via tree-sitter AST parsing",
    )
    maintenance_interval_seconds: int = Field(
        default=3600,
        ge=1,
        description="Lightweight background reindex interval in seconds",
    )
    maintenance_index_timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="Timeout for each lightweight maintenance gcode index command",
    )
    nightly_repair_enabled: bool = Field(
        default=True,
        description="Enable nightly code-index repair and graph reconciliation",
    )
    nightly_repair_cron: str = Field(
        default="0 2 * * *",
        description="Cron expression for nightly code-index repair",
    )
    nightly_repair_timezone: str | None = Field(
        default=None,
        description="Timezone for nightly code-index repair; UTC when unset",
    )
    nightly_repair_timeout_seconds: int = Field(
        default=8 * 60 * 60,
        ge=1,
        description="Timeout for each nightly gcode repair command",
    )
    nightly_repair_concurrency: int = Field(
        default=1,
        ge=1,
        description="Maximum concurrent nightly repair commands",
    )
    maintenance_log_file: str = Field(
        default="~/.gobby/logs/code-index-maintenance.log",
        description="Dedicated rotating log file for code-index prune and nightly maintenance",
    )
    content_retention_days: int = Field(
        default=30,
        ge=1,
        description="Days to retain unreferenced indexed content and recent Git history",
    )
    missing_root_purge_observations: int = Field(
        default=3,
        ge=1,
        description=(
            "Consecutive maintenance passes that must observe a missing project root "
            "before purging its index"
        ),
    )
    embedding_enabled: bool = Field(
        default=True,
        description="Enable Qdrant vector embeddings for semantic search",
    )
    graph_enabled: bool = Field(
        default=True,
        description="Enable FalkorDB call/import graph",
    )
    symbol_summary: CodeIndexSymbolSummaryConfig = Field(
        default_factory=CodeIndexSymbolSummaryConfig,
        description="LLM-generated symbol summary configuration",
    )
    sync_worker_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Sync worker poll interval in seconds",
    )
    sync_worker_projection_timeout_seconds: float = Field(
        default=300.0,
        ge=1,
        description="Timeout for each per-file graph/vector projection sync command",
    )
    sync_worker_batch_size: int = Field(
        default=50,
        ge=1,
        description="Max files to sync per poll iteration",
    )
    sync_worker_breaker_failure_threshold: int = Field(
        default=5,
        ge=1,
        description="Consecutive vector-sync transport failures before the breaker opens",
    )
    sync_worker_breaker_backoff_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Initial vector-sync pause when the breaker opens (doubles per failed probe)",
    )
    sync_worker_breaker_max_backoff_seconds: float = Field(
        default=900.0,
        gt=0,
        description="Maximum vector-sync breaker backoff",
    )

    @field_validator("nightly_repair_cron")
    @classmethod
    def validate_nightly_repair_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError("nightly_repair_cron must be a valid cron expression")
        return value

    @field_validator("nightly_repair_timezone")
    @classmethod
    def validate_nightly_repair_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("nightly_repair_timezone must be a valid IANA timezone") from exc
        return value

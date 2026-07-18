"""Code index configuration."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby.config.feature_base import FeatureDefaultConfig

_LEGACY_SUMMARY_KEYS = {
    "summary_enabled": "enabled",
    "summary_batch_size": "batch_size",
    "summary_profile": "profile",
    "summary_candidates": "candidates",
    "summary_max_concurrency": "max_concurrency",
}


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
    auto_index_on_commit: bool = Field(
        default=True,
        description="Auto-reindex changed files on git commit",
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
    nightly_full_reindex_enabled: bool = Field(
        default=True,
        description="Enable nightly full code-index reindex with projection sync",
    )
    nightly_full_reindex_cron: str = Field(
        default="0 2 * * *",
        description="Cron expression for nightly full code-index reindex",
    )
    nightly_full_reindex_timezone: str | None = Field(
        default=None,
        description="Timezone for nightly full code-index reindex; UTC when unset",
    )
    nightly_full_reindex_timeout_seconds: int = Field(
        default=8 * 60 * 60,
        ge=1,
        description="Timeout for each nightly full gcode reindex command",
    )
    nightly_full_reindex_concurrency: int = Field(
        default=1,
        ge=1,
        description="Maximum concurrent nightly full reindex commands",
    )
    maintenance_log_file: str = Field(
        default="~/.gobby/logs/code-index-maintenance.log",
        description="Dedicated rotating log file for code-index prune and nightly maintenance",
    )
    missing_root_purge_observations: int = Field(
        default=3,
        ge=1,
        description=(
            "Consecutive maintenance passes that must observe a missing project root "
            "before purging its index"
        ),
    )
    max_file_size_bytes: int = Field(
        default=1_000_000,
        ge=1,
        description="Skip files larger than this",
    )
    exclude_patterns: list[str] = Field(
        default=[
            "node_modules",
            ".vite",
            ".git",
            "__pycache__",
            ".mypy_cache",
            ".ruff_cache",
            ".pytest_cache",
            ".tox",
            ".eggs",
            "vendor",
            "build",
            "dist",
            ".venv",
        ],
        description="Glob patterns to exclude from indexing",
    )
    embedding_enabled: bool = Field(
        default=True,
        description="Enable Qdrant vector embeddings for semantic search",
    )
    graph_enabled: bool = Field(
        default=True,
        description="Enable FalkorDB call/import graph",
    )
    qdrant_collection_prefix: str = Field(
        default="code_symbols_",
        description="Qdrant collection name prefix",
    )
    languages: list[str] = Field(
        default=[
            "python",
            "javascript",
            "typescript",
            "go",
            "rust",
            "java",
            "php",
            "dart",
            "csharp",
            "c",
            "cpp",
            "elixir",
            "ruby",
            "markdown",
            "yaml",
            "json",
        ],
        description="Languages to index",
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
    content_extensions: list[str] = Field(
        default=[
            ".html",
            ".css",
            ".scss",
            ".less",
            ".toml",
            ".cfg",
            ".ini",
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".sql",
            ".graphql",
            ".proto",
            ".txt",
            ".rst",
            ".csv",
            ".gitignore",
            ".editorconfig",
        ],
        description="Additional file extensions to index for content search only (no AST parsing)",
    )

    @model_validator(mode="before")
    @classmethod
    def drop_removed_keys(cls, data: object) -> object:
        """Drop removed keys and migrate legacy flat symbol-summary keys."""
        if isinstance(data, dict):
            updated = dict(data)
            updated.pop("sync_worker_vector_batch_size", None)
            if any(key in updated for key in _LEGACY_SUMMARY_KEYS):
                existing_summary = updated.get("symbol_summary")
                symbol_summary = (
                    dict(existing_summary) if isinstance(existing_summary, dict) else {}
                )
                for old_key, new_key in _LEGACY_SUMMARY_KEYS.items():
                    if old_key in updated and new_key not in symbol_summary:
                        symbol_summary[new_key] = updated.pop(old_key)
                    else:
                        # Drop the legacy flat key once an explicit nested value wins.
                        updated.pop(old_key, None)
                updated["symbol_summary"] = symbol_summary
            return updated
        return data

    @field_validator("nightly_full_reindex_cron")
    @classmethod
    def validate_nightly_full_reindex_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError("nightly_full_reindex_cron must be a valid cron expression")
        return value

    @field_validator("nightly_full_reindex_timezone")
    @classmethod
    def validate_nightly_full_reindex_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("nightly_full_reindex_timezone must be a valid IANA timezone") from exc
        return value

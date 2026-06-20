"""Code index configuration."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    auto_index_on_commit: bool = Field(
        default=True,
        description="Auto-reindex changed files on git commit",
    )
    maintenance_interval_seconds: int = Field(
        default=300,
        description="Background reindex interval in seconds",
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
        description="Sync worker poll interval in seconds",
    )
    sync_worker_batch_size: int = Field(
        default=50,
        description="Max files to sync per poll iteration",
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
    def migrate_legacy_keys(cls, data: object) -> object:
        """Normalize legacy daemon-owned code-index config keys."""
        if isinstance(data, dict):
            updated = dict(data)
            updated.pop("sync_worker_vector_batch_size", None)
            legacy_summary = {
                "enabled": updated.pop("summary_enabled", None),
                "batch_size": updated.pop("summary_batch_size", None),
                "profile": updated.pop("summary_profile", None),
                "candidates": updated.pop("summary_candidates", None),
                "max_concurrency": updated.pop("summary_max_concurrency", None),
                "max_tokens": updated.pop("summary_max_tokens", None),
            }
            migrated = {key: value for key, value in legacy_summary.items() if value is not None}
            if migrated:
                symbol_summary = updated.get("symbol_summary")
                if isinstance(symbol_summary, dict):
                    updated["symbol_summary"] = {**migrated, **symbol_summary}
                else:
                    updated["symbol_summary"] = migrated
            return updated
        return data

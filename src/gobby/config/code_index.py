"""Code index configuration."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gobby.config.feature_base import FeatureProfile, default_candidates_for_profile


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
    summary_enabled: bool = Field(
        default=True,
        description="Enable LLM-generated symbol summaries",
    )
    summary_batch_size: int = Field(
        default=20,
        description="Max symbols to summarize per maintenance pass",
    )
    summary_profile: FeatureProfile = Field(
        default=FeatureProfile.LOW,
        description="Capability profile for symbol summary generation",
    )
    summary_candidates: list[str] = Field(
        default_factory=list,
        description="Ordered provider/model candidates for symbol summary generation",
    )
    summary_max_concurrency: int = Field(
        default=2,
        ge=1,
        description="Maximum concurrent symbol summary LLM calls",
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
    def drop_deprecated_vector_batch_size(cls, data: object) -> object:
        """Ignore old daemon-owned vector batching config after gcode cutover."""
        if isinstance(data, dict) and "sync_worker_vector_batch_size" in data:
            updated = dict(data)
            updated.pop("sync_worker_vector_batch_size", None)
            return updated
        return data

    @model_validator(mode="after")
    def populate_summary_candidates(self) -> "CodeIndexConfig":
        """Fill default summary candidates from the summary profile."""
        if not self.summary_candidates:
            self.summary_candidates = list(default_candidates_for_profile(self.summary_profile))
        invalid = [candidate for candidate in self.summary_candidates if "/" not in candidate]
        if invalid:
            joined = ", ".join(repr(candidate) for candidate in invalid)
            raise ValueError(f"summary_candidates must use provider/model format: {joined}")
        return self

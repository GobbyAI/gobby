"""Code index configuration."""

from pydantic import BaseModel, Field


class CodeIndexConfig(BaseModel):
    """Configuration for native AST-based code indexing."""

    enabled: bool = Field(
        default=True,
        description="Enable code indexing via tree-sitter AST parsing",
    )
    auto_index_on_session_start: bool = Field(
        default=True,
        description="Auto-index project on session start",
    )
    auto_index_on_commit: bool = Field(
        default=True,
        description="Auto-reindex changed files on git commit",
    )
    maintenance_interval_seconds: int = Field(
        default=300,
        description="Background reindex interval in seconds",
    )
    max_file_size_bytes: int = Field(
        default=1_000_000,
        description="Skip files larger than this",
    )
    exclude_patterns: list[str] = Field(
        default=[
            "node_modules",
            ".git",
            "__pycache__",
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
        description="Enable Neo4j call/import graph",
    )
    summary_enabled: bool = Field(
        default=True,
        description="Enable AI-generated symbol summaries",
    )
    summary_provider: str = Field(
        default="claude",
        description="LLM provider for symbol summaries",
    )
    summary_model: str = Field(
        default="haiku",
        description="Model for symbol summaries (fast/cheap)",
    )
    summary_batch_size: int = Field(
        default=50,
        description="Number of symbols to summarize per batch",
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
        ],
        description="Languages to index",
    )

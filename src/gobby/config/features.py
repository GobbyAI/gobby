"""
Feature configuration module.

Contains MCP proxy and tool feature Pydantic config models:
- ToolSummarizerConfig: Tool description summarization settings
- RecommendToolsConfig: Tool recommendation settings
- ImportMCPServerConfig: MCP server import settings
- MetricsConfig: Metrics endpoint settings
- ProjectVerificationConfig: Project verification command settings
- TaskDescriptionConfig: LLM-based task description generation settings

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ChatConfig",
    "ReviewConfig",
    "ToolSummarizerConfig",
    "RecommendToolsConfig",
    "ImportMCPServerConfig",
    "MemoryDedupDecisionConfig",
    "MemoryEntityExtractionConfig",
    "MemoryExtractionConfig",
    "MemoryFactExtractionConfig",
    "MergeResolutionConfig",
    "MetricsConfig",
    "OutputCompressionConfig",
    "ProjectVerificationConfig",
    "HookStageConfig",
    "HooksConfig",
    "SkillDescriptionConfig",
    "TaskDescriptionConfig",
]


class ToolSummarizerConfig(BaseModel):
    """Tool description summarization configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based tool description summarization",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for summarization",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for summarization (fast/cheap recommended)",
    )

    prompt_path: str | None = Field(
        default=None,
        description="Path to custom tool summary prompt template (e.g., 'features/tool_summary')",
    )

    system_prompt_path: str | None = Field(
        default=None,
        description="Path to custom tool summary system prompt (e.g., 'features/tool_summary_system')",
    )

    server_description_prompt_path: str | None = Field(
        default=None,
        description="Path to custom server description prompt (e.g., 'features/server_description')",
    )

    server_description_system_prompt_path: str | None = Field(
        default=None,
        description="Path to custom server description system prompt (e.g., 'features/server_description_system')",
    )


class TaskDescriptionConfig(BaseModel):
    """Task description generation configuration.

    Controls LLM-based description generation for tasks created from specs.
    Used when structured extraction yields minimal results.
    """

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based task description generation",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for description generation",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for description generation (fast/cheap recommended)",
    )
    min_structured_length: int = Field(
        default=50,
        description="Minimum length of structured extraction before LLM fallback triggers",
    )

    prompt_path: str | None = Field(
        default=None,
        description="Path to custom task description prompt (e.g., 'features/task_description')",
    )

    system_prompt_path: str | None = Field(
        default=None,
        description="Path to custom task description system prompt (e.g., 'features/task_description_system')",
    )

    @field_validator("min_structured_length")
    @classmethod
    def validate_min_structured_length(cls, v: int) -> int:
        """Validate min_structured_length is non-negative."""
        if v < 0:
            raise ValueError("min_structured_length must be non-negative")
        return v


class MemoryExtractionConfig(BaseModel):
    """Configuration for session memory extraction LLM calls."""

    provider: str = Field(
        default="claude",
        description="LLM provider to use for memory extraction",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for memory extraction (fast/cheap recommended)",
    )


class MergeResolutionConfig(BaseModel):
    """Configuration for merge conflict resolution LLM calls."""

    provider: str = Field(
        default="claude",
        description="LLM provider to use for merge resolution",
    )
    model: str = Field(
        default="sonnet",
        description="Model to use for merge resolution",
    )


class SkillDescriptionConfig(BaseModel):
    """Configuration for skill description synthesis LLM calls."""

    provider: str = Field(
        default="claude",
        description="LLM provider to use for skill description synthesis",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for skill description synthesis (fast/cheap recommended)",
    )


class MemoryFactExtractionConfig(BaseModel):
    """Configuration for memory fact extraction LLM calls."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based fact extraction from memory content",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for fact extraction",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for fact extraction (fast/cheap recommended)",
    )
    prompt_path: str | None = Field(
        default=None,
        description="Path to custom fact extraction prompt (e.g., 'memory/fact_extraction')",
    )


class MemoryDedupDecisionConfig(BaseModel):
    """Configuration for memory deduplication decision LLM calls."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based dedup decision for memories",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for dedup decisions",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for dedup decisions (fast/cheap recommended)",
    )
    prompt_path: str | None = Field(
        default=None,
        description="Path to custom dedup decision prompt (e.g., 'memory/dedup_decision')",
    )


class MemoryEntityExtractionConfig(BaseModel):
    """Configuration for memory entity extraction LLM calls."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based entity extraction from memories",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for entity extraction",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for entity extraction (fast/cheap recommended)",
    )
    prompt_path: str | None = Field(
        default=None,
        description="Path to custom entity extraction prompt (e.g., 'memory/entity_extraction')",
    )


class RecommendToolsConfig(BaseModel):
    """Tool recommendation configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable tool recommendation MCP tool",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for tool recommendations",
    )
    model: str = Field(
        default="sonnet",
        description="Model to use for tool recommendations",
    )

    prompt_path: str | None = Field(
        default=None,
        description="Path to custom recommend tools system prompt (e.g., 'features/recommend_tools')",
    )

    hybrid_rerank_prompt_path: str | None = Field(
        default=None,
        description="Path to custom hybrid re-rank prompt (e.g., 'features/recommend_tools_hybrid')",
    )

    llm_prompt_path: str | None = Field(
        default=None,
        description="Path to custom LLM recommendation prompt (e.g., 'features/recommend_tools_llm')",
    )


class ImportMCPServerConfig(BaseModel):
    """MCP server import configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable MCP server import tool",
    )
    provider: str = Field(
        default="claude",
        description="LLM provider to use for config extraction",
    )
    model: str = Field(
        default="haiku",
        description="Model to use for config extraction",
    )

    prompt_path: str | None = Field(
        default=None,
        description="Path to custom import MCP system prompt (e.g., 'features/import_mcp')",
    )

    github_fetch_prompt_path: str | None = Field(
        default=None,
        description="Path to custom GitHub fetch prompt (e.g., 'features/import_mcp_github')",
    )

    search_fetch_prompt_path: str | None = Field(
        default=None,
        description="Path to custom search fetch prompt (e.g., 'features/import_mcp_search')",
    )


class MetricsConfig(BaseModel):
    """Configuration for metrics and status endpoints."""

    list_limit: int = Field(
        default=10000,
        description="Maximum items to fetch when counting sessions/tasks for metrics. "
        "Set higher for large installs to avoid underreporting. "
        "Use 0 for unbounded (uses COUNT queries instead of list).",
    )
    daily_budget_usd: float = Field(
        default=50.0,
        ge=0.0,
        description="Daily budget limit in USD. Set to 0 for unlimited.",
    )

    @field_validator("list_limit")
    @classmethod
    def validate_list_limit(cls, v: int) -> int:
        """Validate list_limit is non-negative."""
        if v < 0:
            raise ValueError("list_limit must be non-negative")
        return v


class ProjectVerificationConfig(BaseModel):
    """Project verification commands configuration.

    Stores project-specific commands for running tests, type checking, linting, etc.
    Used by task expansion to generate precise validation criteria with actual commands.
    Also used by git hooks to run verification commands at pre-commit, pre-push, etc.
    """

    unit_tests: str | None = Field(
        default=None,
        description="Command to run unit tests (e.g., 'uv run pytest tests/ -v')",
    )
    type_check: str | None = Field(
        default=None,
        description="Command to run type checking (e.g., 'uv run mypy src/')",
    )
    lint: str | None = Field(
        default=None,
        description="Command to run linting (e.g., 'uv run ruff check src/')",
    )
    format: str | None = Field(
        default=None,
        description="Command to check formatting (e.g., 'uv run ruff format --check src/')",
    )
    integration: str | None = Field(
        default=None,
        description="Command to run integration tests",
    )
    security: str | None = Field(
        default=None,
        description="Command to run security scanning (e.g., 'bandit -r src/')",
    )
    code_review: str | None = Field(
        default=None,
        description="Command to run AI/automated code review (e.g., 'coderabbit review --ci')",
    )
    custom: dict[str, str] = Field(
        default_factory=dict,
        description="Custom verification commands (name -> command)",
    )

    # Standard field names for lookup
    _standard_fields: tuple[str, ...] = (
        "unit_tests",
        "type_check",
        "lint",
        "format",
        "integration",
        "security",
        "code_review",
    )

    def get_command(self, name: str) -> str | None:
        """Get a command by name, checking both standard and custom fields.

        Args:
            name: Command name (e.g., 'lint', 'unit_tests', or custom name)

        Returns:
            The command string if found, None otherwise
        """
        # Check standard fields first
        if name in self._standard_fields:
            return getattr(self, name, None)
        # Check custom commands
        return self.custom.get(name)

    def all_commands(self) -> dict[str, str]:
        """Return all defined commands as a dict.

        Returns:
            Dict mapping command names to command strings (only non-None values)
        """
        result: dict[str, str] = {}
        for field in self._standard_fields:
            if cmd := getattr(self, field, None):
                result[field] = cmd
        result.update(self.custom)
        return result


class OutputCompressionConfig(BaseModel):
    """Output compression configuration for token optimization."""

    enabled: bool = Field(
        default=False,
        description="Enable output compression (opt-in). When enabled, verbose tool "
        "output is compressed before reaching the LLM context window.",
    )
    min_output_length: int = Field(
        default=1000,
        ge=0,
        description="Minimum output length (chars) before compression triggers",
    )
    max_compressed_lines: int = Field(
        default=100,
        ge=0,
        description="Target maximum lines after compression",
    )
    excluded_commands: list[str] = Field(
        default_factory=list,
        description="Regex patterns for commands that should never be compressed",
    )

    @field_validator("excluded_commands")
    @classmethod
    def _validate_excluded_commands(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern {pattern!r}: {e}") from e
        return v

    track_savings: bool = Field(
        default=True,
        description="Track token savings via metrics",
    )


class ReviewConfig(BaseModel):
    """Code review configuration."""

    model: str = Field(default="opus", description="Model for code review")
    provider: str = Field(default="claude", description="Provider for code review")


class ChatConfig(BaseModel):
    """Chat mode configuration."""

    model: str = Field(
        default="opus",
        description="Default model for chat sessions",
    )
    default_mode: str = Field(
        default="plan",
        description="Default chat mode for new sessions. One of: normal, accept_edits, bypass, plan.",
    )

    @field_validator("default_mode")
    @classmethod
    def validate_default_mode(cls, v: str) -> str:
        """Validate default_mode is a known chat mode."""
        valid = {"normal", "accept_edits", "bypass", "plan"}
        if v not in valid:
            raise ValueError(f"default_mode must be one of {valid}, got '{v}'")
        return v


class HookStageConfig(BaseModel):
    """Configuration for a single git hook stage."""

    run: list[str] = Field(
        default_factory=list,
        description="List of verification command names to run (e.g., ['lint', 'format'])",
    )
    fail_fast: bool = Field(
        default=True,
        description="Stop on first failure (exit 1) vs run all and report",
    )
    timeout: int = Field(
        default=300,
        description="Timeout in seconds for each command",
    )
    enabled: bool = Field(
        default=True,
        description="Whether this hook stage is active",
    )


class HooksConfig(BaseModel):
    """Git hooks configuration for verification commands.

    Maps git hook stages to verification commands defined in ProjectVerificationConfig.
    """

    pre_commit: HookStageConfig = Field(
        default_factory=HookStageConfig,
        alias="pre-commit",
        description="Pre-commit hook configuration",
    )
    pre_push: HookStageConfig = Field(
        default_factory=HookStageConfig,
        alias="pre-push",
        description="Pre-push hook configuration",
    )
    pre_merge: HookStageConfig = Field(
        default_factory=HookStageConfig,
        alias="pre-merge",
        description="Pre-merge hook configuration (runs before merge commits)",
    )

    model_config = ConfigDict(populate_by_name=True)

    def get_stage(self, stage: str) -> HookStageConfig:
        """Get configuration for a hook stage.

        Args:
            stage: Hook stage name (e.g., 'pre-commit', 'pre-push', 'pre-merge')

        Returns:
            HookStageConfig for the stage
        """
        # Normalize stage name (pre-commit -> pre_commit)
        attr_name = stage.replace("-", "_")
        return getattr(self, attr_name, HookStageConfig())

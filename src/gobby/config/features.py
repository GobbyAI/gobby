"""
Feature configuration module.

Contains MCP proxy and tool feature Pydantic config models:
- ToolSummarizerConfig: Tool description summarization settings
- RecommendToolsConfig: Tool recommendation settings
- ImportMCPServerConfig: MCP server import settings
- MetricsConfig: Metrics endpoint settings
- ProjectVerificationConfig: Project verification command settings

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.config.feature_base import FeatureDefaultConfig, ModelTier

__all__ = [
    "ChatConfig",
    "KnowledgeGraphQueueConfig",
    "ToolSummarizerConfig",
    "RecommendToolsConfig",
    "ImportMCPServerConfig",
    "MergeResolutionConfig",
    "MetricsConfig",
    "ProjectVerificationConfig",
    "HookStageConfig",
    "HooksConfig",
    "SkillDescriptionConfig",
]


class ToolSummarizerConfig(FeatureDefaultConfig):
    """Tool description summarization configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable LLM-based tool description summarization",
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


class MergeResolutionConfig(FeatureDefaultConfig):
    """Configuration for merge conflict resolution LLM calls.

    `model` is the primary model choice (e.g. "sonnet"). `tier` (ModelTier) is
    only consulted as a fallback selector when the primary/local provider fails
    — see LLMService.call_feature() for the fallback path. `model` is
    authoritative for normal operation.
    """

    model: str = Field(
        default="sonnet",
        description="Primary model for merge resolution. Authoritative under normal operation.",
    )
    tier: ModelTier = Field(
        default=ModelTier.MID,
        description="Fallback complexity tier — determines fallback model when the primary/local provider fails",
    )


class SkillDescriptionConfig(FeatureDefaultConfig):
    """Configuration for skill description synthesis LLM calls."""


class KnowledgeGraphQueueConfig(BaseModel):
    """Configuration for background KG processing queue."""

    interval_minutes: int = Field(
        default=30,
        ge=1,
        description="How often to process the KG queue (minutes)",
    )
    batch_size: int = Field(
        default=20,
        ge=1,
        description="Max memories to process per KG queue cycle",
    )


class RecommendToolsConfig(FeatureDefaultConfig):
    """Tool recommendation configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable tool recommendation MCP tool",
    )
    model: str = Field(
        default="sonnet",
        description="Model to use for tool recommendations",
    )
    tier: ModelTier = Field(
        default=ModelTier.MID,
        description="Complexity tier — determines fallback model when local provider fails",
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


class ImportMCPServerConfig(FeatureDefaultConfig):
    """MCP server import configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable MCP server import tool",
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

    track_savings: bool = Field(
        default=True,
        description="Track token savings via metrics",
    )


class ChatConfig(FeatureDefaultConfig):
    """Chat mode configuration."""

    model: str = Field(
        default="opus",
        description="Default model for chat sessions",
    )
    tier: ModelTier = Field(
        default=ModelTier.HIGH,
        description="Complexity tier — determines fallback model when local provider fails",
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

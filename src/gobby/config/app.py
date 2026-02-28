"""
Configuration management for Gobby daemon.

Runtime config: DB config_store + Pydantic defaults.
Pre-DB bootstrap: ~/.gobby/bootstrap.yaml (5 settings).
YAML export: export_config_to_yaml() for backup/migration.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# Internal imports for DaemonConfig fields - NOT re-exported
from gobby.config.cron import CronConfig
from gobby.config.extensions import HookExtensionsConfig
from gobby.config.features import (
    ChatConfig,
    ImportMCPServerConfig,
    MemoryDedupDecisionConfig,
    MemoryEntityExtractionConfig,
    MemoryExtractionConfig,
    MemoryFactExtractionConfig,
    MergeResolutionConfig,
    MetricsConfig,
    ProjectVerificationConfig,
    RecommendToolsConfig,
    ReviewConfig,
    SkillDescriptionConfig,
    TaskDescriptionConfig,
    ToolSummarizerConfig,
)
from gobby.config.llm_providers import LLMProvidersConfig
from gobby.config.logging import LoggingSettings
from gobby.config.persistence import MemoryBackupConfig, MemoryConfig
from gobby.config.servers import MCPClientProxyConfig, WebSocketSettings
from gobby.config.sessions import (
    ChatHistoryConfig,
    ContextInjectionConfig,
    DigestConfig,
    MessageTrackingConfig,
    SessionLifecycleConfig,
    SessionSummaryConfig,
    SessionTitleConfig,
)
from gobby.config.skills import SkillsConfig
from gobby.config.tasks import CompactHandoffConfig, GobbyTasksConfig, WorkflowConfig
from gobby.config.tmux import TmuxConfig
from gobby.config.voice import VoiceConfig
from gobby.config.watchdog import WatchdogConfig
from gobby.search.models import SearchConfig


class ToolApprovalPolicy(BaseModel):
    """A single tool approval policy matching server/tool glob patterns."""

    server_pattern: str = Field(default="*", description="Glob pattern for server name")
    tool_pattern: str = Field(default="*", description="Glob pattern for tool name")
    policy: Literal["auto", "approve_once", "always_ask"] = Field(
        default="always_ask",
        description="Approval policy: 'auto', 'approve_once', or 'always_ask'",
    )


class ToolApprovalConfig(BaseModel):
    """Configuration for tool approval UI in web chat."""

    enabled: bool = Field(default=False, description="Enable tool approval prompts")
    default_policy: Literal["auto", "approve_once", "always_ask"] = Field(
        default="auto",
        description="Default policy: 'auto' (no prompts), 'approve_once', or 'always_ask'",
    )
    policies: list[ToolApprovalPolicy] = Field(
        default_factory=list,
        description="Per-tool approval policies (server/tool glob patterns)",
    )


class AuthConfig(BaseModel):
    """Basic authentication for the web UI.

    Leave username and password empty to disable auth (default).
    Once both are set, the UI requires login. Password is encrypted
    via Fernet in the secrets table.
    """

    username: str = Field(
        default="",
        description="Username for web UI login. Leave empty to disable auth.",
    )
    password: str = Field(
        default="",
        description="Password for web UI login (encrypted in secrets table).",
    )
    session_secret: str = Field(
        default="",
        description="HMAC signing key for session cookies (auto-generated on first login).",
        json_schema_extra={"ui_hidden": True},
    )


class UIConfig(BaseModel):
    """Configuration for the web UI."""

    enabled: bool = Field(default=False, description="Enable web UI serving")
    mode: str = Field(default="production", description="'production' or 'dev'")
    port: int = Field(default=60889, description="Dev server port (dev mode only)")
    host: str = Field(default="localhost", description="Dev server host (dev mode only)")
    web_dir: str | None = Field(
        default=None, description="Path to web/ dir (auto-detected if None)"
    )
    memory_graph_limit: int = Field(
        default=5000,
        ge=50,
        le=5000,
        description="Default display limit for the 2D memory graph (nodes)",
    )
    knowledge_graph_limit: int = Field(
        default=5000,
        ge=50,
        le=5000,
        description="Default display limit for the 3D knowledge graph (entities)",
    )

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number is in valid range."""
        if not (1024 <= v <= 65535):
            raise ValueError("Port must be between 1024 and 65535")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("production", "dev"):
            raise ValueError("UI mode must be 'production' or 'dev'")
        return v


__all__ = [
    # Local definitions only - no re-exports
    "DaemonConfig",
    "deep_merge",
    "expand_env_vars",
    "load_yaml",
    "apply_cli_overrides",
    "export_config_to_yaml",
    "load_config",
    "save_config",  # deprecated alias for export_config_to_yaml
]

logger = logging.getLogger(__name__)

# Pattern for environment variable substitution:
# ${VAR} - simple substitution
# ${VAR:-default} - with default value if VAR is unset or empty
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Pattern for secret references (secrets-store-only, no env fallback):
# $secret:NAME - resolved from encrypted secrets store
SECRET_REF_PATTERN = re.compile(r"\$secret:([A-Za-z_][A-Za-z0-9_]*)")


def expand_env_vars(
    content: str,
    secret_resolver: Callable[[str], str | None] | None = None,
) -> str:
    """
    Expand variable references in configuration content.

    Supports three syntaxes:
    - $secret:NAME — resolved exclusively from encrypted secrets store
    - ${VAR} — secrets store first (if resolver provided), then env var
    - ${VAR:-default} — same as above, with fallback default

    Args:
        content: Configuration file content as string
        secret_resolver: Optional callable that takes a variable name and returns
            the decrypted secret value, or None if not found.

    Returns:
        Content with variables expanded
    """

    # Pass 1: Resolve $secret:NAME references (secrets-store-only)
    if secret_resolver is not None:

        def replace_secret(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                value = secret_resolver(name)
                if value is not None:
                    return value
            except Exception as e:
                logger.debug(f"Secret resolver failed for '$secret:{name}': {e}")
            logger.warning(
                f"Unresolved secret '$secret:{name}' in config — not found in secrets store"
            )
            return match.group(0)

        content = SECRET_REF_PATTERN.sub(replace_secret, content)

    # Pass 2: Resolve ${VAR} references (secrets first, then env vars)
    def replace_env(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)  # None if no default specified

        # 1. Try secret store first
        if secret_resolver is not None:
            try:
                secret_value = secret_resolver(var_name)
                if secret_value is not None and secret_value != "":
                    return secret_value
            except Exception as e:
                logger.debug(f"Secret resolver failed for '{var_name}': {e}")

        # 2. Try environment variable
        env_value = os.environ.get(var_name)
        if env_value is not None and env_value != "":
            return env_value

        # 3. Use default if provided
        if default_value is not None:
            return default_value

        # 4. Unresolved — warn and leave unchanged
        logger.warning(
            f"Unresolved variable '${{{var_name}}}' in config "
            f"— not found in secrets store or environment"
        )
        return match.group(0)

    return ENV_VAR_PATTERN.sub(replace_env, content)


class DaemonConfig(BaseModel):
    """
    Main configuration for Gobby daemon.

    Configuration is loaded with the following priority:
    1. CLI arguments (highest)
    2. DB config_store (runtime settings)
    3. Pydantic defaults (lowest)

    Pre-DB bootstrap settings (daemon_port, bind_host, database_path,
    websocket_port, ui_port) are read from ~/.gobby/bootstrap.yaml.

    Note: machine_id is stored separately in ~/.gobby/machine_id
    """

    model_config = {"populate_by_name": True}

    # Daemon settings
    daemon_port: int = Field(
        default=60887,
        description="Port for daemon to listen on",
    )
    bind_host: str = Field(
        default="localhost",
        description="Host/IP to bind servers to. Use 'localhost' for local-only access, "
        "'0.0.0.0' for all interfaces, or a specific IP (e.g., Tailscale IP) for restricted access.",
    )
    daemon_health_check_interval: float = Field(
        default=10.0,
        description="Daemon health check interval in seconds",
    )
    test_mode: bool = Field(
        default=False,
        description="Run daemon in test mode (enables test endpoints)",
    )

    # Local storage
    database_path: str = Field(
        default="~/.gobby/gobby-hub.db",
        description="Path to hub database for cross-project queries.",
    )
    # Sub-configs
    websocket: WebSocketSettings = Field(
        default_factory=WebSocketSettings,
        description="WebSocket server configuration",
    )
    logging: LoggingSettings = Field(
        default_factory=LoggingSettings,
        description="Logging configuration",
    )
    session_summary: SessionSummaryConfig = Field(
        default_factory=SessionSummaryConfig,
        description="Session summary generation configuration",
    )
    compact_handoff: CompactHandoffConfig = Field(
        default_factory=CompactHandoffConfig,
        description="Compact handoff context configuration",
    )
    context_injection: ContextInjectionConfig = Field(
        default_factory=ContextInjectionConfig,
        description="Context injection configuration for subagent spawning",
    )
    mcp_client_proxy: MCPClientProxyConfig = Field(
        default_factory=MCPClientProxyConfig,
        description="MCP client proxy configuration",
    )
    gobby_tasks: GobbyTasksConfig = Field(
        default_factory=GobbyTasksConfig,
        alias="gobby-tasks",
        serialization_alias="gobby-tasks",
        description="gobby-tasks internal MCP server configuration",
    )

    # Multi-provider LLM configuration
    llm_providers: LLMProvidersConfig = Field(
        default_factory=LLMProvidersConfig,
        description="Multi-provider LLM configuration",
    )
    digest: DigestConfig = Field(
        default_factory=DigestConfig,
        description="Rolling digest and title generation configuration",
    )
    recommend_tools: RecommendToolsConfig = Field(
        default_factory=RecommendToolsConfig,
        description="Tool recommendation configuration",
    )
    tool_summarizer: ToolSummarizerConfig = Field(
        default_factory=ToolSummarizerConfig,
        description="Tool description summarization configuration",
    )
    task_description: TaskDescriptionConfig = Field(
        default_factory=TaskDescriptionConfig,
        description="LLM-based task description generation configuration",
    )
    import_mcp_server: ImportMCPServerConfig = Field(
        default_factory=ImportMCPServerConfig,
        description="MCP server import configuration",
    )
    memory_fact_extraction: MemoryFactExtractionConfig = Field(
        default_factory=MemoryFactExtractionConfig,
        description="Memory fact extraction LLM configuration",
    )
    memory_dedup_decision: MemoryDedupDecisionConfig = Field(
        default_factory=MemoryDedupDecisionConfig,
        description="Memory dedup decision LLM configuration",
    )
    memory_entity_extraction: MemoryEntityExtractionConfig = Field(
        default_factory=MemoryEntityExtractionConfig,
        description="Memory entity extraction LLM configuration",
    )
    hook_extensions: HookExtensionsConfig = Field(
        default_factory=HookExtensionsConfig,
        description="Hook extensions configuration",
    )
    workflow: WorkflowConfig = Field(
        default_factory=WorkflowConfig,
        description="Workflow engine configuration",
    )
    memory: MemoryConfig = Field(
        default_factory=MemoryConfig,
        description="Memory system configuration",
    )
    memory_sync: MemoryBackupConfig = Field(
        default_factory=MemoryBackupConfig,
        description="Memory synchronization configuration",
    )
    skills: SkillsConfig = Field(
        default_factory=SkillsConfig,
        description="Skills injection configuration",
    )
    chat_history: ChatHistoryConfig = Field(
        default_factory=ChatHistoryConfig,
        description="Chat history injection limits for session recreation",
    )
    message_tracking: MessageTrackingConfig = Field(
        default_factory=MessageTrackingConfig,
        description="Session message tracking configuration",
    )
    session_lifecycle: SessionLifecycleConfig = Field(
        default_factory=SessionLifecycleConfig,
        description="Session lifecycle management configuration",
    )
    metrics: MetricsConfig = Field(
        default_factory=MetricsConfig,
        description="Metrics and status endpoint configuration",
    )
    verification_defaults: ProjectVerificationConfig = Field(
        default_factory=ProjectVerificationConfig,
        description="Default verification commands for projects without auto-detected config",
    )
    search: SearchConfig = Field(
        default_factory=SearchConfig,
        description="Unified search configuration with embedding fallback",
    )
    watchdog: WatchdogConfig = Field(
        default_factory=WatchdogConfig,
        description="Daemon watchdog process configuration",
    )
    ui: UIConfig = Field(
        default_factory=UIConfig,
        description="Web UI configuration",
    )
    auth: AuthConfig = Field(
        default_factory=AuthConfig,
        description="Web UI authentication configuration",
    )
    tmux: TmuxConfig = Field(
        default_factory=TmuxConfig,
        description="Tmux agent spawning configuration",
    )
    cron: CronConfig = Field(
        default_factory=CronConfig,
        description="Cron scheduler configuration",
    )
    voice: VoiceConfig = Field(
        default_factory=VoiceConfig,
        description="Voice chat configuration (STT + TTS)",
    )
    tool_approval: ToolApprovalConfig = Field(
        default_factory=ToolApprovalConfig,
        description="Tool approval UI configuration for web chat",
    )
    chat: ChatConfig = Field(
        default_factory=ChatConfig,
        description="Chat mode configuration (default mode for new sessions)",
    )
    review: ReviewConfig = Field(
        default_factory=ReviewConfig,
        description="Code review configuration",
    )
    session_title: SessionTitleConfig = Field(
        default_factory=SessionTitleConfig,
        description="Session title synthesis LLM configuration",
    )
    memory_extraction: MemoryExtractionConfig = Field(
        default_factory=MemoryExtractionConfig,
        description="Session memory extraction LLM configuration",
    )
    merge_resolution: MergeResolutionConfig = Field(
        default_factory=MergeResolutionConfig,
        description="Merge conflict resolution LLM configuration",
    )
    skill_description: SkillDescriptionConfig = Field(
        default_factory=SkillDescriptionConfig,
        description="Skill description synthesis LLM configuration",
    )

    def get_recommend_tools_config(self) -> RecommendToolsConfig:
        """Get recommend_tools configuration."""
        return self.recommend_tools

    def get_tool_summarizer_config(self) -> ToolSummarizerConfig:
        """Get tool_summarizer configuration."""
        return self.tool_summarizer

    def get_task_description_config(self) -> TaskDescriptionConfig:
        """Get task_description configuration."""
        return self.task_description

    def get_import_mcp_server_config(self) -> ImportMCPServerConfig:
        """Get import_mcp_server configuration."""
        return self.import_mcp_server

    def get_mcp_client_proxy_config(self) -> MCPClientProxyConfig:
        """Get MCP client proxy configuration."""
        return self.mcp_client_proxy

    def get_memory_config(self) -> MemoryConfig:
        """Get memory configuration."""
        return self.memory

    def get_memory_sync_config(self) -> MemoryBackupConfig:
        """Get memory sync configuration."""
        return self.memory_sync

    def get_skills_config(self) -> SkillsConfig:
        """Get skills configuration."""
        return self.skills

    def get_gobby_tasks_config(self) -> GobbyTasksConfig:
        """Get gobby-tasks configuration."""
        return self.gobby_tasks

    def get_metrics_config(self) -> MetricsConfig:
        """Get metrics configuration."""
        return self.metrics

    def get_verification_defaults(self) -> ProjectVerificationConfig:
        """Get default verification commands configuration."""
        return self.verification_defaults

    def get_search_config(self) -> SearchConfig:
        """Get search configuration."""
        return self.search

    @field_validator("daemon_port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number is in valid range."""
        if not (1024 <= v <= 65535):
            raise ValueError("Port must be between 1024 and 65535")
        return v

    @field_validator("daemon_health_check_interval")
    @classmethod
    def validate_health_check_interval(cls, v: float) -> float:
        """Validate health check interval is in valid range."""
        if not (1.0 <= v <= 300.0):
            raise ValueError("daemon_health_check_interval must be between 1.0 and 300.0 seconds")
        return v


def load_yaml(
    config_file: str,
    secret_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """
    Load YAML or JSON configuration file.

    Args:
        config_file: Path to YAML or JSON configuration file
        secret_resolver: Optional callable for resolving secrets (checked before env vars)

    Returns:
        Dictionary with parsed YAML/JSON content

    Raises:
        ValueError: If YAML/JSON is invalid or file format is wrong
    """
    config_path = Path(config_file).expanduser()

    if not config_path.exists():
        return {}

    # Validate file extension matches format
    file_ext = config_path.suffix.lower()
    if file_ext not in [".yaml", ".yml", ".json"]:
        raise ValueError(
            f"Config file must have .yaml, .yml, or .json extension, got: {file_ext}\n"
            f"File: {config_path}"
        )

    import json

    try:
        with open(config_path) as f:
            content = f.read()

        # Expand variables (secrets first if resolver provided, then env vars)
        content = expand_env_vars(content, secret_resolver=secret_resolver)

        # Handle JSON files
        if file_ext == ".json":
            return json.loads(content) if content.strip() else {}

        # Handle YAML files
        data = yaml.safe_load(content)
        return data if data is not None else {}

    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config file: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config file: {e}") from e


def apply_cli_overrides(
    config_dict: dict[str, Any],
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply CLI argument overrides to config dictionary.

    Args:
        config_dict: Configuration dictionary
        cli_overrides: Dictionary of CLI overrides

    Returns:
        Configuration dictionary with CLI overrides applied
    """
    if cli_overrides is None:
        return config_dict

    # Apply overrides at top level
    for key, value in cli_overrides.items():
        if "." in key:
            # Handle nested keys like "logging.level"
            parts = key.split(".")
            current = config_dict
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value
        else:
            config_dict[key] = value

    return config_dict


def deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> None:
    """Deep-merge updates into base dict (in-place)."""
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value


def _resolve_config_values(
    d: dict[str, Any],
    secret_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Walk a config dict and resolve $secret:NAME / ${VAR} patterns in string values."""
    result: dict[str, Any] = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _resolve_config_values(value, secret_resolver)
        elif isinstance(value, list):
            result[key] = [
                expand_env_vars(item, secret_resolver=secret_resolver)
                if isinstance(item, str)
                else _resolve_config_values(item, secret_resolver)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = expand_env_vars(value, secret_resolver=secret_resolver)
        else:
            result[key] = value
    return result


def load_config(
    config_file: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    create_default: bool = False,
    secret_resolver: Callable[[str], str | None] | None = None,
    config_store: Any | None = None,
) -> DaemonConfig:
    """
    Load configuration with hierarchy: CLI > DB > bootstrap > Pydantic defaults.

    When config_store is provided (Phase 2), config is loaded from the database.
    Otherwise reads bootstrap.yaml for the 5 pre-DB settings (Phase 1).

    Args:
        config_file: Path hint for locating bootstrap.yaml (default: ~/.gobby/)
        cli_overrides: Dictionary of CLI argument overrides
        create_default: Unused (kept for API compatibility)
        secret_resolver: Optional callable for resolving secrets (checked before env vars)
        config_store: Optional ConfigStore instance for DB-first resolution

    Returns:
        Validated DaemonConfig instance

    Raises:
        ValueError: If configuration is invalid or required fields are missing
    """
    if config_store is not None:
        # Phase 2: bootstrap → config file → DB → Pydantic defaults.
        # Each layer overrides the previous one.
        from gobby.config.bootstrap import load_bootstrap
        from gobby.storage.config_store import unflatten_config

        # Layer 1: bootstrap values (ports, db_path, bind_host)
        bootstrap = load_bootstrap(config_file)
        config_dict: dict[str, Any] = bootstrap.to_config_dict()

        # Layer 2: config file values (non-bootstrap settings like test_mode,
        # memory, logging, etc.). Only read if config_file points to a full
        # config YAML (not bootstrap.yaml itself).
        if config_file:
            config_path = Path(config_file)
            if config_path.exists() and config_path.name != "bootstrap.yaml":
                try:
                    with open(config_path) as f:
                        file_dict = yaml.safe_load(f)
                    if isinstance(file_dict, dict):
                        deep_merge(config_dict, file_dict)
                except Exception as e:
                    logger.warning(f"Failed to read config file {config_path}: {e}")

        # Layer 3: DB values (runtime overrides via config_store)
        flat_db = config_store.get_all()
        if flat_db:
            db_dict = unflatten_config(flat_db)
            # Resolve $secret:NAME and ${VAR} patterns in DB values
            if secret_resolver is not None or any(
                isinstance(v, str) and ("$secret:" in v or "${" in v) for v in flat_db.values()
            ):
                db_dict = _resolve_config_values(db_dict, secret_resolver)
            # Deep merge: DB values override config file and bootstrap
            deep_merge(config_dict, db_dict)
    else:
        # Phase 1: bootstrap.yaml for pre-DB settings (database_path, ports)
        from gobby.config.bootstrap import load_bootstrap

        bootstrap = load_bootstrap(config_file)
        config_dict = bootstrap.to_config_dict()

    # Apply CLI argument overrides
    config_dict = apply_cli_overrides(config_dict, cli_overrides)

    # SAFETY SWITCH: Protect production resources during tests
    # If GOBBY_TEST_PROTECT is set, force safe paths from environment
    if os.environ.get("GOBBY_TEST_PROTECT") == "1":
        # Override database path
        if safe_db := os.environ.get("GOBBY_DATABASE_PATH"):
            config_dict["database_path"] = safe_db

        # Override logging paths
        logging_config = config_dict.setdefault("logging", {})
        if safe_client := os.environ.get("GOBBY_LOGGING_CLIENT"):
            logging_config["client"] = safe_client
        if safe_error := os.environ.get("GOBBY_LOGGING_CLIENT_ERROR"):
            logging_config["client_error"] = safe_error
        if safe_mcp_server := os.environ.get("GOBBY_LOGGING_MCP_SERVER"):
            logging_config["mcp_server"] = safe_mcp_server
        if safe_mcp_client := os.environ.get("GOBBY_LOGGING_MCP_CLIENT"):
            logging_config["mcp_client"] = safe_mcp_client

    # Validate and create config object
    try:
        config = DaemonConfig(**config_dict)
        return config
    except Exception as e:
        source = "database" if config_store is not None else "bootstrap.yaml"
        raise ValueError(
            f"Configuration validation failed: {e}\n"
            f"Please check your configuration source: {source}"
        ) from e


def export_config_to_yaml(config: DaemonConfig, config_file: str | None = None) -> None:
    """
    Export configuration to YAML file (for backup/migration).

    This is NOT used at runtime — runtime config comes from DB + Pydantic defaults.
    Use this for export/import workflows or one-time migration snapshots.

    Args:
        config: DaemonConfig instance to export
        config_file: Path to YAML export file (default: ~/.gobby/config.yaml)

    Raises:
        OSError: If file operations fail
        RuntimeError: If called with production path during tests (GOBBY_TEST_PROTECT=1)
    """
    if config_file is None:
        config_file = "~/.gobby/config.yaml"

    config_path = Path(config_file).expanduser()

    # Block writes to production config during tests
    if os.environ.get("GOBBY_TEST_PROTECT") == "1":
        real_gobby_home = Path("~/.gobby").expanduser().resolve()
        try:
            if config_path.resolve().is_relative_to(real_gobby_home):
                raise RuntimeError(
                    f"export_config_to_yaml() would write to production path "
                    f"{config_path} during tests."
                )
        except (ValueError, OSError):
            pass

    # Ensure directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert config to dict, excluding None values to keep file clean
    # mode="json" ensures Path objects are converted to strings for YAML serialization
    config_dict = config.model_dump(mode="json", exclude_none=True)

    # Write to YAML file
    with open(config_path, "w") as f:
        yaml.safe_dump(config_dict, f, default_flow_style=False, sort_keys=False)

    # Set restrictive permissions (owner read/write only)
    config_path.chmod(0o600)


def save_config(config: DaemonConfig, config_file: str | None = None) -> None:
    """Deprecated: use export_config_to_yaml() instead."""
    logger.warning("save_config() is deprecated — use export_config_to_yaml()")
    export_config_to_yaml(config, config_file)

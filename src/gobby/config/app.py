"""
Configuration management for Gobby daemon.

Runtime config: DB config_store + Pydantic defaults.
Pre-DB bootstrap: ~/.gobby/bootstrap.yaml.
YAML export: export_config_to_yaml() for backup/migration.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from gobby.config._loading import (
    _drop_legacy_embedding_config_store_keys,
    _drop_removed_config_store_keys,
    _migrate_code_index_symbol_summary_config_store_keys,
    _migrate_default_ui_mode_config_store_row,
    _migrate_legacy_config,
    _reject_removed_file_config_sections,
    _resolve_config_values,
    _restore_bootstrap_pre_database_settings,
    apply_cli_overrides,
    deep_merge,
    expand_env_vars,
    export_config_to_yaml,
    load_yaml,
)

# Internal imports for DaemonConfig fields - NOT re-exported
from gobby.config.ai import AIConfig
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.config.code_index import CodeIndexConfig
from gobby.config.communications import CommunicationsConfig
from gobby.config.cron import CronConfig
from gobby.config.daemon_sandbox import DaemonOwnedSandboxConfig
from gobby.config.embedding_keys import storage_embedding_config_entries_to_runtime
from gobby.config.extensions import HookExtensionsConfig
from gobby.config.feature_base import iter_feature_default_configs, validate_feature_candidates
from gobby.config.feature_candidate_defaults import delete_stale_default_feature_candidate_rows
from gobby.config.features import (
    ChatConfig,
    ImportMCPServerConfig,
    KnowledgeGraphQueueConfig,
    MergeResolutionConfig,
    MetricsConfig,
    ProjectVerificationConfig,
    ProjectVerificationSynthesisConfig,
    RecommendToolsConfig,
    SkillDescriptionConfig,
    ToolSummarizerConfig,
)
from gobby.config.indexing import IndexingConfig
from gobby.config.logging import LoggingSettings, common_log_parent
from gobby.config.persistence import (
    DatabasesConfig,
    EmbeddingsConfig,
    MemoryBackupConfig,
    MemoryConfig,
)
from gobby.config.pipelines import PipelineConfig
from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.config.servers import MCPClientProxyConfig, WebSocketSettings
from gobby.config.sessions import (
    ChatHistoryConfig,
    ContextInjectionConfig,
    DigestConfig,
    MemoryRecallConfig,
    MemoryUsefulnessConfig,
    MessageTrackingConfig,
    SessionLifecycleConfig,
    SessionSummaryConfig,
)
from gobby.config.skills import SkillsConfig
from gobby.config.system_loops import SystemLoopsConfig
from gobby.config.tasks import CompactHandoffConfig, GobbyTasksConfig, WorkflowConfig
from gobby.config.tmux import TmuxConfig
from gobby.config.ui import (
    AuthConfig,
    ToolApprovalConfig,
    UIConfig,
    is_loopback_bind_host,
)
from gobby.config.ui import (
    ToolApprovalPolicy as ToolApprovalPolicy,
)
from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    default_validation_detection_config,
)
from gobby.config.voice import VoiceConfig
from gobby.config.wiki import WikiConfig
from gobby.search.models import SearchConfig
from gobby.telemetry.config import TelemetrySettings

__all__ = [
    "CompactHandoffConfig",
    "DaemonConfig",
    "GobbyTasksConfig",
    "WorkflowConfig",
    "deep_merge",
    "expand_env_vars",
    "load_yaml",
    "apply_cli_overrides",
    "export_config_to_yaml",
    "load_config",
]

logger = logging.getLogger(__name__)

_TEST_LOG_PATH_ENV_VARS = (
    "GOBBY_LOGGING_CLIENT",
    "GOBBY_LOGGING_CLIENT_ERROR",
    "GOBBY_LOGGING_CLIENT_STDERR",
    "GOBBY_LOGGING_MCP_SERVER",
    "GOBBY_LOGGING_MCP_CLIENT",
    "GOBBY_LOGGING_HOOK_MANAGER",
)


def _apply_test_logging_overrides(config_dict: dict[str, Any]) -> None:
    """Apply test-safe logging directory overrides."""
    configured_dir = os.environ.get("GOBBY_LOGGING_DIR")
    legacy_paths = {
        name: value
        for name in _TEST_LOG_PATH_ENV_VARS
        if (value := os.environ.get(name)) is not None
    }
    legacy_dir = common_log_parent(legacy_paths) if legacy_paths else None

    if configured_dir is not None and legacy_dir is not None:
        resolved_configured_dir = Path(configured_dir).expanduser()
        if resolved_configured_dir != legacy_dir:
            details = ", ".join(
                [f"GOBBY_LOGGING_DIR={configured_dir!r}"]
                + [f"{name}={value!r}" for name, value in legacy_paths.items()]
            )
            raise ValueError(f"Conflicting test log directories: {details}")

    resolved_dir = configured_dir if configured_dir is not None else legacy_dir
    if resolved_dir is not None:
        logging_config = config_dict.setdefault("logging", {})
        if not isinstance(logging_config, dict):
            raise ValueError("logging config must be a mapping")
        logging_config["dir"] = str(resolved_dir)


class DaemonConfig(BaseModel):
    """
    Main configuration for Gobby daemon.

    Configuration is loaded with the following priority:
    1. CLI arguments (highest)
    2. DB config_store (runtime settings)
    3. Pydantic defaults (lowest)

    Pre-DB bootstrap settings (daemon_port, bind_host, websocket_port, ui_port,
    hub_backend, database_url, postgres_install_mode, and postgres_pool) are read from
    ~/.gobby/bootstrap.yaml.

    Note: machine_id is stored separately in ~/.gobby/machine_id
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def reject_removed_session_title_config(cls, data: Any) -> Any:
        """Reject removed config sections explicitly."""
        if isinstance(data, dict):
            if "session_title" in data:
                raise ValueError(
                    "session_title config has been removed. Use digest.profile, "
                    "digest.candidates, and digest.timeout instead."
                )
            if "conductor" in data:
                raise ValueError(
                    "conductor config has been removed. Remove the top-level conductor section."
                )
            if "memory_recall_helper" in data:
                raise ValueError(
                    "memory_recall_helper config has been removed. Use memory_recall instead."
                )
            if "local" in data:
                raise ValueError(
                    "local config has been removed. Use "
                    "ai.generation.local.endpoints.<name> instead."
                )
        return data

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
    auth_mode: str = Field(
        default="required",
        description="Daemon API authentication mode selected by bootstrap.yaml.",
    )
    daemon_health_check_interval: float = Field(
        default=10.0,
        description="Daemon health check interval in seconds",
    )
    test_mode: bool = Field(
        default=False,
        description="Run daemon in test mode (enables test endpoints)",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:*", "https://localhost:*"],
        description="Allowed CORS origins. Defaults to localhost only. "
        "Add your Tailscale hostname (e.g., 'https://myhost.tail*.ts.net') for remote access.",
    )

    # Hub connection settings
    hub_backend: Literal["postgres"] = Field(
        default="postgres",
        description=(
            'hub_backend (Literal["postgres"]) selected by bootstrap.yaml; only "postgres" '
            "is supported; use `gobby postgres install`."
        ),
    )
    database_url: str | None = Field(
        default=None,
        description="PostgreSQL DSN selected by bootstrap.yaml when hub_backend is postgres.",
        exclude=True,
    )
    postgres_install_mode: Literal["docker"] | None = Field(
        default=None,
        description="PostgreSQL install mode recorded by gobby postgres install.",
    )
    postgres_pool: PostgresPoolConfig = Field(
        default_factory=PostgresPoolConfig,
        description="PostgreSQL client pool settings selected by bootstrap.yaml.",
        exclude=True,
    )

    # Sub-configs
    websocket: WebSocketSettings = Field(
        default_factory=WebSocketSettings,
        description="WebSocket server configuration",
    )
    telemetry: TelemetrySettings = Field(
        default_factory=TelemetrySettings,
        description="OpenTelemetry tracing and metrics configuration",
    )
    logging: LoggingSettings = Field(
        default_factory=LoggingSettings,
        description="Application and runtime logging configuration",
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

    web_chat_sandbox: DaemonOwnedSandboxConfig = Field(
        default_factory=DaemonOwnedSandboxConfig,
        description="Daemon-owned sandbox defaults for web chat runtimes.",
    )
    agent_sandbox: DaemonOwnedSandboxConfig = Field(
        default_factory=DaemonOwnedSandboxConfig,
        description="Daemon-owned sandbox defaults for spawned agent runtimes.",
    )
    communications: CommunicationsConfig = Field(
        default_factory=CommunicationsConfig,
        description="Communications channel configuration",
    )
    digest: DigestConfig = Field(
        default_factory=DigestConfig,
        description="Rolling digest and title generation configuration",
    )
    memory_recall: MemoryRecallConfig = Field(
        default_factory=MemoryRecallConfig,
        description="Daemon-owned memory recall runner configuration",
    )
    memory_usefulness: MemoryUsefulnessConfig = Field(
        default_factory=MemoryUsefulnessConfig,
        description="Digest-pass memory-usefulness judge configuration (#17195)",
    )
    recommend_tools: RecommendToolsConfig = Field(
        default_factory=RecommendToolsConfig,
        description="Tool recommendation configuration",
    )
    tool_summarizer: ToolSummarizerConfig = Field(
        default_factory=ToolSummarizerConfig,
        description="Tool description summarization configuration",
    )
    import_mcp_server: ImportMCPServerConfig = Field(
        default_factory=ImportMCPServerConfig,
        description="MCP server import configuration",
    )
    knowledge_graph_queue: KnowledgeGraphQueueConfig = Field(
        default_factory=KnowledgeGraphQueueConfig,
        description="Background knowledge graph processing queue configuration",
    )
    hook_extensions: HookExtensionsConfig = Field(
        default_factory=HookExtensionsConfig,
        description="Hook extensions configuration",
    )
    workflow: WorkflowConfig = Field(
        default_factory=WorkflowConfig,
        description="Workflow engine configuration",
    )
    databases: DatabasesConfig = Field(
        default_factory=DatabasesConfig,
        description="Shared database connections (Qdrant, FalkorDB)",
    )
    embeddings: EmbeddingsConfig = Field(
        default_factory=EmbeddingsConfig,
        description="Embedding model configuration (shared by memory, tools, code index)",
    )
    ai: AIConfig = Field(
        default_factory=AIConfig,
        description="Daemon-owned AI generation configuration",
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
    project_verification_synthesis: ProjectVerificationSynthesisConfig = Field(
        default_factory=ProjectVerificationSynthesisConfig,
        description="LLM synthesis configuration for project verification refresh",
    )
    validation_detection: ValidationDetectionConfig = Field(
        default_factory=default_validation_detection_config,
        description="Validation command detection matchers for completion evidence",
    )
    search: SearchConfig = Field(
        default_factory=SearchConfig,
        description="Unified search configuration with embedding fallback",
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
    system_loops: SystemLoopsConfig = Field(
        default_factory=SystemLoopsConfig,
        description="Daemon-owned system loop configuration",
    )
    pipelines: PipelineConfig = Field(
        default_factory=PipelineConfig,
        description="Pipeline execution configuration",
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
    merge_resolution: MergeResolutionConfig = Field(
        default_factory=MergeResolutionConfig,
        description="Merge conflict resolution LLM configuration",
    )
    skill_description: SkillDescriptionConfig = Field(
        default_factory=SkillDescriptionConfig,
        description="Skill description synthesis LLM configuration",
    )
    context_window_overrides: dict[str, int] = Field(
        default_factory=dict,
        description="Override context window sizes by model substring match (e.g., {'opus': 1000000})",
    )
    code_index: CodeIndexConfig = Field(
        default_factory=CodeIndexConfig,
        description="Native AST-based code indexing configuration",
    )
    indexing: IndexingConfig = Field(
        default_factory=IndexingConfig,
        description="Shared indexing behavior for gcode and gwiki.",
    )
    bin_freshness: BinFreshnessConfig = Field(
        default_factory=BinFreshnessConfig,
        description="Managed native binary freshness checks.",
    )
    wiki: WikiConfig = Field(
        default_factory=WikiConfig,
        description="Daemon wiki file watcher configuration.",
    )
    clones_dir: str = Field(
        default="~/.gobby/clones",
        description="Base directory for git clones (survives reboots, unlike /tmp).",
    )
    worktrees_dir: str = Field(
        default="~/.gobby/worktrees",
        description="Base directory for git worktrees (survives reboots, unlike /tmp).",
    )

    def get_recommend_tools_config(self) -> RecommendToolsConfig:
        """Get recommend_tools configuration."""
        return self.recommend_tools

    def get_tool_summarizer_config(self) -> ToolSummarizerConfig:
        """Get tool_summarizer configuration."""
        return self.tool_summarizer

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

    def get_project_verification_synthesis_config(self) -> ProjectVerificationSynthesisConfig:
        """Get project verification synthesis configuration."""
        return self.project_verification_synthesis

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

    @model_validator(mode="after")
    def validate_remote_ui_auth(self) -> DaemonConfig:
        """Refuse unauthenticated UI exposure beyond the loopback interface."""
        if (
            self.ui.enabled
            and self.auth_mode != "required"
            and not is_loopback_bind_host(self.bind_host)
        ):
            raise ValueError(
                "ui.enabled requires auth_mode='required' when bind_host is not localhost "
                "or a numeric loopback address"
            )
        return self

    @model_validator(mode="after")
    def apply_generation_profile_defaults(self) -> DaemonConfig:
        """Apply global profile defaults to feature configs with omitted candidates."""
        profile_defaults = self.ai.generation.profile_defaults
        if not profile_defaults:
            return self
        for feature_config in iter_feature_default_configs(self):
            if not feature_config._candidates_omitted:
                continue
            candidates = profile_defaults.get(feature_config.profile)
            if candidates is not None:
                feature_config.candidates = validate_feature_candidates(candidates)
        return self

    @model_validator(mode="after")
    def validate_collection_prefix_consistency(self) -> DaemonConfig:
        """Ensure databases.qdrant and code_index collection prefixes match."""
        if self.databases.qdrant.collection_prefix != self.code_index.qdrant_collection_prefix:
            raise ValueError(
                f"databases.qdrant.collection_prefix "
                f"({self.databases.qdrant.collection_prefix!r}) must match "
                f"code_index.qdrant_collection_prefix "
                f"({self.code_index.qdrant_collection_prefix!r})"
            )
        return self


def load_config(
    config_file: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    secret_resolver: Callable[[str], str | None] | None = None,
    config_store: Any | None = None,
    resolve_database_url: bool = False,
) -> DaemonConfig:
    """
    Load configuration with hierarchy: CLI > DB > bootstrap > Pydantic defaults.

    When config_store is provided (Phase 2), config is loaded from the database.
    Otherwise reads bootstrap.yaml for pre-DB settings (Phase 1).

    Args:
        config_file: Path hint for locating bootstrap.yaml (default: ~/.gobby/)
        cli_overrides: Dictionary of CLI argument overrides
        secret_resolver: Optional callable for resolving secrets (checked before env vars)
        config_store: Optional ConfigStore instance for DB-first resolution
        resolve_database_url: Require bootstrap database_url for runtime DB startup

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

        # Layer 1: bootstrap values (ports, db_path, bind_host, hub backend)
        bootstrap = load_bootstrap(config_file, resolve_database_url=resolve_database_url)
        config_dict: dict[str, Any] = bootstrap.to_config_dict()

        # Layer 2: config file values (non-bootstrap settings like test_mode,
        # memory, logging, etc.). Only read if config_file points to a full
        # config YAML (not bootstrap.yaml itself).
        if config_file:
            config_path = Path(config_file)
            if config_path.exists() and config_path.name != "bootstrap.yaml":
                try:
                    file_dict = load_yaml(str(config_path), secret_resolver=secret_resolver)
                except (OSError, ValueError) as e:
                    logger.warning("Ignoring unreadable config file %s: %s", config_path, e)
                else:
                    _reject_removed_file_config_sections(file_dict, config_path)
                    deep_merge(config_dict, file_dict)

        # Layer 3: DB values (runtime overrides via config_store)
        delete_stale_default_feature_candidate_rows(config_store)
        flat_db = config_store.get_all()
        flat_db = _drop_legacy_embedding_config_store_keys(flat_db, config_store)
        flat_db = _migrate_code_index_symbol_summary_config_store_keys(flat_db, config_store)
        flat_db = _drop_removed_config_store_keys(flat_db, config_store)
        flat_db = _migrate_default_ui_mode_config_store_row(flat_db, config_store)
        if flat_db:
            db_dict = unflatten_config(storage_embedding_config_entries_to_runtime(flat_db))
            # Resolve $secret:NAME and ${VAR} patterns in DB values
            if secret_resolver is not None or any(
                isinstance(v, str) and ("$secret:" in v or "${" in v) for v in flat_db.values()
            ):
                db_dict = _resolve_config_values(db_dict, secret_resolver)
            # Deep merge: DB values override config file and bootstrap
            deep_merge(config_dict, db_dict)
        _restore_bootstrap_pre_database_settings(config_dict, bootstrap)
    else:
        # Phase 1: bootstrap.yaml for pre-DB settings (ports and hub connection).
        from gobby.config.bootstrap import load_bootstrap

        bootstrap = load_bootstrap(config_file, resolve_database_url=resolve_database_url)
        config_dict = bootstrap.to_config_dict()

    # Apply CLI argument overrides
    config_dict = apply_cli_overrides(config_dict, cli_overrides)

    # SAFETY SWITCH: Protect production resources during tests
    # If GOBBY_TEST_PROTECT is set, force safe paths from environment
    if os.environ.get("GOBBY_TEST_PROTECT") == "1":
        _apply_test_logging_overrides(config_dict)
    # Migrate legacy config keys (renamed/removed fields still in DB)
    config_dict = _migrate_legacy_config(config_dict)

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

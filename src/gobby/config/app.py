"""
Configuration management for Gobby daemon.

Runtime config: DB config_store + Pydantic defaults.
Pre-DB bootstrap: ~/.gobby/bootstrap.yaml.
YAML export: export_config_to_yaml() for backup/migration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from gobby.config._loading import (
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
from gobby.config.database_concurrency import DatabaseConcurrencyConfig
from gobby.config.extensions import HookExtensionsConfig
from gobby.config.feature_base import iter_feature_default_configs, validate_feature_candidates
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
    ToolResultOffloadConfig,
    ToolSummarizerConfig,
)
from gobby.config.hooks import HookTimeoutConfig
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
    ToolApprovalConfig,
    UIConfig,
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

    ConfigRuntime owns revisioned runtime snapshots built from the registry and
    database overrides. Restart-bound topology and authentication settings are
    projected once from ~/.gobby/bootstrap.yaml during daemon startup.

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
            if "memory_sync" in data:
                raise ValueError("memory_sync config has been removed. Use memory_backup instead.")
            if "local" in data:
                raise ValueError(
                    "local config has been removed. Use ai.generation.endpoints.<name> instead."
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
    datastore_mode: Literal["local", "remote"] = Field(
        default="local",
        description="Whether this machine owns the datastore stack or connects to a remote hub.",
    )
    files_home: str | None = Field(
        default=None,
        description="Absolute hub files bind directory. Local mode only.",
        exclude=True,
    )
    hub_daemon_url: str | None = Field(
        default=None,
        description="HTTP origin of the files-owner daemon. Remote mode only.",
        exclude=True,
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
    postgres_pool: PostgresPoolConfig = Field(
        default_factory=PostgresPoolConfig,
        description="PostgreSQL client pool settings selected by bootstrap.yaml.",
        exclude=True,
    )
    database_concurrency: DatabaseConcurrencyConfig = Field(
        default_factory=DatabaseConcurrencyConfig,
        description="Restart-required single-daemon database concurrency limits.",
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
        default_factory=lambda: DaemonOwnedSandboxConfig(
            backend="srt",
            allow_network=False,
        ),
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
    tool_result_offload: ToolResultOffloadConfig = Field(
        default_factory=ToolResultOffloadConfig,
        description="Oversized tool-result offload configuration",
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
    hooks: HookTimeoutConfig = Field(
        default_factory=HookTimeoutConfig,
        description="Coordinated daemon and provider hook timeout configuration",
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
    memory_backup: MemoryBackupConfig = Field(
        default_factory=MemoryBackupConfig,
        description="Memory JSONL backup configuration",
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

    def get_tool_result_offload_config(self) -> ToolResultOffloadConfig:
        """Get tool_result_offload configuration."""
        return self.tool_result_offload

    def get_import_mcp_server_config(self) -> ImportMCPServerConfig:
        """Get import_mcp_server configuration."""
        return self.import_mcp_server

    def get_gobby_tasks_config(self) -> GobbyTasksConfig:
        """Get gobby-tasks configuration."""
        return self.gobby_tasks

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

    @model_validator(mode="after")
    def validate_hook_timeout_order(self) -> DaemonConfig:
        """Require each enclosing hook layer to outlive the work it contains."""
        timeouts = (
            self.memory_recall.timeout,
            self.workflow.timeout,
            self.hooks.adapter_timeout,
            self.hooks.provider_timeout,
        )
        if not all(inner < outer for inner, outer in zip(timeouts, timeouts[1:], strict=False)):
            raise ValueError(
                "Hook timeouts must satisfy memory_recall.timeout < workflow.timeout < "
                "hooks.adapter_timeout < hooks.provider_timeout"
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

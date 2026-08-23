"""
Service container for dependency injection in Gobby daemon.

Holds references to singleton services to avoid prop-drilling in HTTPServer
and other components.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gobby.ai import TextGenerationService, ToolChatService
from gobby.llm import LLMService
from gobby.memory.manager import MemoryManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.concurrency import CoverageExecutor, DatabaseConcurrencyResolution
from gobby.storage.concurrency_watchdog import DatabaseSaturationWatchdog
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemoryBackupManager
from gobby.worktrees.executor import WorktreeDeleteExecutor, run_worktree_delete

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.config.runtime import ConfigRuntime
    from gobby.events.wake import WakeDispatcher
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.memory.dream.coordinator import MemoryDreamCoordinator


@dataclass
class ServiceContainer:
    """Container for daemon services."""

    # Core Infrastructure
    database: HubDatabase

    # Core Managers
    session_manager: SessionManager | None
    task_manager: LocalTaskManager
    db_executor: DatabaseExecutor | None = None
    worktree_delete_executor: WorktreeDeleteExecutor | None = None
    coverage_executor: CoverageExecutor | None = None
    database_concurrency: DatabaseConcurrencyResolution | None = None
    database_watchdog: DatabaseSaturationWatchdog | None = None
    span_storage: Any | None = None  # SpanStorage

    # Backup manager
    memory_backup_manager: MemoryBackupManager | None = None

    # Advanced Features
    memory_manager: MemoryManager | None = None
    memory_dream_coordinator: MemoryDreamCoordinator | None = None
    text_generation_service: TextGenerationService | None = None
    tool_chat_service: ToolChatService | None = None
    llm_service: LLMService | None = None
    vector_store: Any | None = None  # VectorStore (Qdrant)

    # MCP & Agents
    mcp_manager: MCPClientManager | None = None
    mcp_db_manager: Any | None = None  # LocalMCPManager
    metrics_manager: Any | None = None  # ToolMetricsManager
    agent_runner: Any | None = None  # AgentRunner
    message_processor: Any | None = None  # SessionMessageProcessor
    message_processor_resolver: Callable[[], Any | None] | None = None

    # Validation & Git
    task_validator: Any | None = None  # TaskValidator
    worktree_storage: LocalWorktreeManager | None = None
    clone_storage: LocalCloneManager | None = None
    git_manager: Any | None = None  # WorktreeGitManager

    # Pipelines
    pipeline_executor: Any | None = None  # PipelineExecutor
    workflow_loader: Any | None = None  # PipelineLoader
    pipeline_execution_manager: Any | None = None  # LocalPipelineExecutionManager

    # Completion Events
    completion_registry: Any | None = None  # CompletionEventRegistry
    wake_dispatcher: WakeDispatcher | None = None

    # Agent Lifecycle
    agent_lifecycle_monitor: Any | None = None  # AgentLifecycleMonitor
    attention_manager: Any | None = None  # AttentionStateManager
    attention_metadata_store: AttentionMetadataStore | None = None
    detection_registry: DetectionManifestRegistry | None = None

    # Communications
    communications_manager: Any | None = None  # CommunicationsManager

    # Cron Scheduler
    cron_storage: Any | None = None  # CronJobStorage
    cron_scheduler: Any | None = None  # CronScheduler

    # System Automation
    system_automation_loop: Any | None = None  # SystemAutomationLoop

    # Skills
    skill_manager: Any | None = None  # LocalSkillManager
    hub_manager: Any | None = None  # HubManager

    # Code Index
    code_indexer: Any | None = None  # CodeIndexContext
    code_index_pruner: Any | None = None  # CodeIndexPruner

    # Config
    config_runtime: ConfigRuntime | None = None
    config_documents_service: Any | None = None  # ConfigDocumentsService
    config_values_service: Any | None = None  # ConfigValuesService
    provider_capability_service: Any | None = None  # CapabilityRefreshCoordinator
    provider_capability_resolver: Any | None = None  # CapabilityResolver
    model_metadata_coverage_auditor: Any | None = None  # ModelMetadataCoverageAuditor
    web_chat_runtime_manager: Any | None = None  # WebChatRuntimeManager
    web_chat_session_registry: Any | None = None  # WebChatSessionRegistry

    # Prompts
    prompt_manager: Any | None = None  # LocalPromptManager
    dev_mode: bool = False

    # Transcripts
    transcript_reader: Any | None = None  # TranscriptReader

    # Context
    project_id: str | None = None
    websocket_server: Any | None = None  # GobbyWebSocketServer
    startup_ready: bool = False
    shutdown_in_progress: bool = False
    http_admission_closed: bool = False
    # The daemon's long-lived event loop, captured at run_daemon startup.
    # Fire-and-forget work spawned from short-lived loops (e.g. the HTTP build
    # route's worker-thread tick) must be scheduled here to survive the caller.
    main_loop: asyncio.AbstractEventLoop | None = None

    # Lazy wiring for per-project executors
    tool_proxy_getter: Any | None = None  # Callable[[], ToolProxyService]
    _project_infra_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run daemon database work on the bounded DB executor."""
        if self.db_executor is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self.db_executor.run(func, *args, **kwargs)

    def resolve_message_processor(self) -> Any | None:
        if self.message_processor_resolver is not None:
            return self.message_processor_resolver()
        return self.message_processor

    def db_executor_stats(self) -> dict[str, int | float | bool] | None:
        """Return DB executor diagnostics when configured."""
        if self.db_executor is None:
            return None
        return self.db_executor.stats().as_dict()

    async def run_worktree_delete(self, operation: Callable[..., Any]) -> Any:
        """Run a complete worktree deletion off the daemon event loop."""
        return await run_worktree_delete(self.worktree_delete_executor, operation)

    def worktree_delete_executor_stats(self) -> dict[str, int | float | bool] | None:
        """Return worktree deletion executor diagnostics when configured."""
        if self.worktree_delete_executor is None:
            return None
        return self.worktree_delete_executor.stats().as_dict()

    def get_git_manager(self, project_id: str) -> Any | None:
        """Get or create a WorktreeGitManager for a project.

        Looks up the project's repo_path from the database and creates a
        WorktreeGitManager, caching it for subsequent calls.

        Returns:
            WorktreeGitManager instance or None if project not found.
        """
        if project_id in self._project_infra_cache:
            cached = self._project_infra_cache[project_id].get("git_manager")
            if cached is not None:
                return cached

        try:
            from gobby.storage.projects import LocalProjectManager
            from gobby.worktrees.git import WorktreeGitManager

            pm = LocalProjectManager(self.database)
            project = pm.get(project_id)
            if not project or not project.repo_path:
                return None

            gm = WorktreeGitManager(project.repo_path)
            self._project_infra_cache.setdefault(project_id, {})["git_manager"] = gm
            return gm
        except (ValueError, OSError):
            return None

    def get_pipeline_executor(self, project_id: str | None = None) -> Any | None:
        """Get or lazily create a PipelineExecutor with event broadcasting and tool proxy wired.

        Reuses startup infrastructure only for the startup project. Otherwise creates a
        new executor for *project_id*, wires ``event_callback`` and ``tool_proxy_getter``,
        and caches it for subsequent calls.

        Returns:
            PipelineExecutor instance or None if required services are unavailable.
        """
        uses_startup_project = project_id in (None, "", self.project_id)

        # Fast path: executor already created for the startup project
        if uses_startup_project and self.pipeline_executor is not None:
            return self.pipeline_executor

        pid = project_id or self.project_id or ""

        # Check cache
        cached = self._project_infra_cache.get(pid, {}).get("pipeline_executor")
        if cached is not None:
            return cached

        # Lazy creation requires database, workflow_loader, and an execution manager
        if self.database is None or self.workflow_loader is None:
            return None

        _logger = logging.getLogger(__name__)

        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager
            from gobby.workflows.pipeline_executor import PipelineExecutor
            from gobby.workflows.templates import TemplateEngine

            execution_manager = self.pipeline_execution_manager if uses_startup_project else None
            if execution_manager is None and pid:
                execution_manager = LocalPipelineExecutionManager(
                    db=self.database,
                    project_id=pid,
                )

            if execution_manager is None:
                return None

            runtime = self.config_runtime
            pipeline_config = (
                runtime.capture().snapshot.active.pipelines if runtime is not None else None
            )
            pe = PipelineExecutor(
                db=self.database,
                execution_manager=execution_manager,
                llm_service=self.llm_service,
                loader=self.workflow_loader,
                template_engine=TemplateEngine(),
                session_manager=self.session_manager,
                completion_registry=self.completion_registry,
                run_db=self.run_db,
                pipeline_config=pipeline_config,
                pipeline_config_resolver=lambda: (
                    runtime.capture().snapshot.active.pipelines
                    if runtime is not None and runtime.ready
                    else pipeline_config
                ),
            )

            # Wire event broadcasting via WebSocket
            if self.websocket_server:
                ws = self.websocket_server  # capture for closure

                async def broadcast_pipeline_event(
                    event: str, execution_id: str, **kwargs: Any
                ) -> None:
                    if ws:
                        await ws.broadcast_pipeline_event(
                            event=event,
                            execution_id=execution_id,
                            **kwargs,
                        )

                pe.event_callback = broadcast_pipeline_event

            # Wire tool proxy for MCP steps
            if self.tool_proxy_getter:
                pe.tool_proxy_getter = self.tool_proxy_getter

            # Lazily created per-project executors are the only sweep point
            # for projects outside the runner's home project: the runner's
            # startup recovery is scoped to its own project_id, so restart
            # orphans here would otherwise stay RUNNING forever.
            try:
                pe.startup_sweep()
            except Exception:
                _logger.warning("Pipeline startup sweep failed for project %r", pid, exc_info=True)

            self._project_infra_cache.setdefault(pid, {})["pipeline_executor"] = pe
            _logger.debug("Lazily created PipelineExecutor for project %r", pid)
            return pe

        except Exception as e:
            _logger.warning("Failed to lazily create PipelineExecutor: %s", e)
            return None


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
_current_container: ServiceContainer | None = None


def set_app_context(container: ServiceContainer) -> None:
    """Store the global ServiceContainer singleton."""
    global _current_container
    _current_container = container


def clear_app_context() -> None:
    """Clear the global ServiceContainer singleton."""
    global _current_container
    _current_container = None


def get_app_context() -> ServiceContainer | None:
    """Retrieve the global ServiceContainer, or None if not yet initialised."""
    return _current_container

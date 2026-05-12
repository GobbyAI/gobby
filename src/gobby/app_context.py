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

from gobby.config.app import DaemonConfig
from gobby.llm import LLMService
from gobby.memory.manager import MemoryManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import DatabaseProtocol
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemorySyncManager
from gobby.sync.tasks import TaskSyncManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager


@dataclass
class ServiceContainer:
    """Container for daemon services."""

    # Core Infrastructure
    config: DaemonConfig
    database: DatabaseProtocol

    # Core Managers
    session_manager: SessionManager
    task_manager: LocalTaskManager
    db_executor: DatabaseExecutor | None = None
    span_storage: Any | None = None  # SpanStorage

    # Sync Managers
    task_sync_manager: TaskSyncManager | None = None
    memory_sync_manager: MemorySyncManager | None = None

    # Advanced Features
    memory_manager: MemoryManager | None = None
    llm_service: LLMService | None = None
    vector_store: Any | None = None  # VectorStore (Qdrant)

    # MCP & Agents
    mcp_manager: MCPClientManager | None = None
    mcp_db_manager: Any | None = None  # LocalMCPManager
    metrics_manager: Any | None = None  # ToolMetricsManager
    agent_runner: Any | None = None  # AgentRunner
    message_processor: Any | None = None  # SessionMessageProcessor

    # Validation & Git
    task_validator: Any | None = None  # TaskValidator
    worktree_storage: LocalWorktreeManager | None = None
    clone_storage: LocalCloneManager | None = None
    git_manager: Any | None = None  # WorktreeGitManager

    # Pipelines
    pipeline_executor: Any | None = None  # PipelineExecutor
    workflow_loader: Any | None = None  # WorkflowLoader
    pipeline_execution_manager: Any | None = None  # LocalPipelineExecutionManager

    # Completion Events
    completion_registry: Any | None = None  # CompletionEventRegistry

    # Agent Lifecycle
    agent_lifecycle_monitor: Any | None = None  # AgentLifecycleMonitor

    # Communications
    communications_manager: Any | None = None  # CommunicationsManager

    # Cron Scheduler
    cron_storage: Any | None = None  # CronJobStorage
    cron_scheduler: Any | None = None  # CronScheduler

    # Skills
    skill_manager: Any | None = None  # LocalSkillManager
    hub_manager: Any | None = None  # HubManager

    # Code Index
    code_indexer: Any | None = None  # CodeIndexContext

    # Config
    config_store: Any | None = None  # ConfigStore
    provider_model_catalog: Any | None = None  # ProviderModelCatalog
    web_chat_runtime_manager: Any | None = None  # WebChatRuntimeManager
    web_chat_session_registry: Any | None = None  # WebChatSessionRegistry

    # Caches (lazily populated by savings, etc.)
    _savings_tracker: Any | None = None  # SavingsTracker

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

    # Lazy wiring for per-project executors
    tool_proxy_getter: Any | None = None  # Callable[[], ToolProxyService]
    _project_infra_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run daemon SQLite work on the bounded DB executor."""
        if self.db_executor is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self.db_executor.run(func, *args, **kwargs)

    def db_executor_stats(self) -> dict[str, int | bool] | None:
        """Return DB executor diagnostics when configured."""
        if self.db_executor is None:
            return None
        return self.db_executor.stats().as_dict()

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

        If ``self.pipeline_executor`` is already set (startup path), returns it directly.
        Otherwise creates a new executor for *project_id*, wires ``event_callback`` and
        ``tool_proxy_getter``, and caches it for subsequent calls.

        Returns:
            PipelineExecutor instance or None if required services are unavailable.
        """
        # Fast path: executor already created at startup
        if self.pipeline_executor is not None:
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

            execution_manager = self.pipeline_execution_manager
            if execution_manager is None and pid:
                execution_manager = LocalPipelineExecutionManager(
                    db=self.database,
                    project_id=pid,
                )

            if execution_manager is None:
                return None

            pe = PipelineExecutor(
                db=self.database,
                execution_manager=execution_manager,
                llm_service=self.llm_service,
                loader=self.workflow_loader,
                template_engine=TemplateEngine(),
                session_manager=self.session_manager,
                completion_registry=self.completion_registry,
                run_db=self.run_db,
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

            self._project_infra_cache.setdefault(pid, {})["pipeline_executor"] = pe
            _logger.debug(f"Lazily created PipelineExecutor for project {pid!r}")
            return pe

        except Exception as e:
            _logger.warning(f"Failed to lazily create PipelineExecutor: {e}")
            return None


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
_current_container: ServiceContainer | None = None


def set_app_context(container: ServiceContainer) -> None:
    """Store the global ServiceContainer singleton."""
    global _current_container
    _current_container = container


def get_app_context() -> ServiceContainer | None:
    """Retrieve the global ServiceContainer, or None if not yet initialised."""
    return _current_container

"""
Service container for dependency injection in Gobby daemon.

Holds references to singleton services to avoid prop-drilling in HTTPServer
and other components.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gobby.config.app import DaemonConfig
from gobby.llm import LLMService
from gobby.memory.manager import MemoryManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemorySyncManager
from gobby.sync.tasks import TaskSyncManager


@dataclass
class ServiceContainer:
    """Container for daemon services."""

    # Core Infrastructure
    config: DaemonConfig
    database: DatabaseProtocol

    # Core Managers
    session_manager: LocalSessionManager
    task_manager: LocalTaskManager

    # Sync Managers
    task_sync_manager: TaskSyncManager | None = None
    memory_sync_manager: MemorySyncManager | None = None

    # Advanced Features
    memory_manager: MemoryManager | None = None
    llm_service: LLMService | None = None

    # MCP & Agents
    mcp_manager: Any | None = None  # MCPClientManager
    mcp_db_manager: Any | None = None  # LocalMCPManager
    metrics_manager: Any | None = None  # ToolMetricsManager
    agent_runner: Any | None = None  # AgentRunner
    message_processor: Any | None = None  # SessionMessageProcessor
    message_manager: Any | None = None  # LocalSessionMessageManager

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

    # Cron Scheduler
    cron_storage: Any | None = None  # CronJobStorage
    cron_scheduler: Any | None = None  # CronScheduler

    # Skills
    skill_manager: Any | None = None  # LocalSkillManager
    hub_manager: Any | None = None  # HubManager

    # Config
    config_store: Any | None = None  # ConfigStore

    # Prompts
    prompt_manager: Any | None = None  # LocalPromptManager
    dev_mode: bool = False

    # Context
    project_id: str | None = None
    websocket_server: Any | None = None

    # Lazy wiring for per-project executors
    tool_proxy_getter: Any | None = None  # Callable[[], ToolProxyService]
    _project_infra_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

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

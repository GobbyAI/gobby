"""Internal registry initialization."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalRegistryManager

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.config.app import DaemonConfig
    from gobby.hooks.hook_manager import HookManager
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
    from gobby.memory.manager import MemoryManager
    from gobby.sessions.manager import SessionManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.session_messages import LocalSessionMessageManager
    from gobby.storage.sessions import LocalSessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.sync.tasks import TaskSyncManager
    from gobby.tasks.validation import TaskValidator
    from gobby.workflows.loader import WorkflowLoader
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

logger = logging.getLogger("gobby.mcp.registries")


def setup_internal_registries(
    _config: DaemonConfig | None,
    _session_manager: SessionManager | None = None,
    memory_manager: MemoryManager | None = None,
    task_manager: LocalTaskManager | None = None,
    db: DatabaseProtocol | None = None,
    sync_manager: TaskSyncManager | None = None,
    task_validator: TaskValidator | None = None,
    message_manager: LocalSessionMessageManager | None = None,
    local_session_manager: LocalSessionManager | None = None,
    metrics_manager: ToolMetricsManager | None = None,
    llm_service: LLMService | None = None,
    agent_runner: AgentRunner | None = None,
    worktree_storage: LocalWorktreeManager | None = None,
    clone_storage: LocalCloneManager | None = None,
    git_manager: WorktreeGitManager | None = None,
    merge_storage: MergeResolutionManager | None = None,
    merge_resolver: MergeResolver | None = None,
    project_id: str | None = None,
    tool_proxy_getter: Callable[[], ToolProxyService | None] | None = None,
    inter_session_message_manager: InterSessionMessageManager | None = None,
    pipeline_executor: PipelineExecutor | None = None,
    workflow_loader: WorkflowLoader | None = None,
    pipeline_execution_manager: LocalPipelineExecutionManager | None = None,
    hook_manager_resolver: Callable[[], HookManager | None] | None = None,
    config_store: ConfigStore | None = None,
    config_setter: Callable[[DaemonConfig], None] | None = None,
    memory_sync_manager: Any | None = None,
) -> InternalRegistryManager:
    """
    Setup internal MCP registries (tasks, messages, memory, metrics, agents, worktrees).

    Args:
        _config: Daemon configuration (reserved for future use)
        _session_manager: Session manager (reserved for future use)
        memory_manager: Memory manager for memory operations
        task_manager: Task storage manager
        db: Database connection for registries that only need storage (skills)
        sync_manager: Task sync manager for git sync
        task_validator: Task validator for validation
        message_manager: Message storage manager
        local_session_manager: Local session manager for session CRUD
        metrics_manager: Tool metrics manager for metrics operations
        llm_service: LLM service for AI-powered operations
        agent_runner: Agent runner for spawning subagents
        worktree_storage: Worktree storage manager for worktree operations
        git_manager: Git manager for git worktree operations
        merge_storage: Merge storage manager for conflict resolution
        merge_resolver: Merge resolver for AI resolution
        project_id: Default project ID for worktree operations
        tool_proxy_getter: Callable that returns ToolProxyService for routing
            tool calls in in-process agents. Called lazily during agent execution.
        inter_session_message_manager: Inter-session message manager for agent messaging
        pipeline_executor: Pipeline executor for running pipelines
        workflow_loader: Workflow loader for loading pipeline definitions
        pipeline_execution_manager: Pipeline execution manager for tracking executions
        hook_manager_resolver: Lazy callable returning HookManager (or None).
            Solves timing: registries init before HookManager is created in HTTP lifespan.

    Returns:
        InternalRegistryManager containing all registries
    """
    manager = InternalRegistryManager()

    # Initialize tasks registry if enabled and task_manager is available
    if _config is None:
        gobby_tasks_enabled = False
        logger.warning("Tasks registry not initialized: config is None")
    else:
        gobby_tasks_enabled = _config.get_gobby_tasks_config().enabled
        if not gobby_tasks_enabled:
            logger.debug("Tasks registry disabled by config")

    if gobby_tasks_enabled:
        if task_manager is None:
            logger.warning("Tasks registry not initialized: task_manager is None")
        elif sync_manager is None:
            logger.warning("Tasks registry not initialized: sync_manager is None")
        else:
            from gobby.mcp_proxy.tools.tasks import create_task_registry

            tasks_registry = create_task_registry(
                task_manager=task_manager,
                sync_manager=sync_manager,
                task_validator=task_validator,
                config=_config,
                agent_runner=agent_runner,
                project_id=project_id,
            )
            manager.add_registry(tasks_registry)
            logger.debug("Tasks registry initialized")

    # Initialize sessions registry (messages + session CRUD)
    # Register if either message_manager or local_session_manager is available
    if message_manager is not None or local_session_manager is not None:
        from gobby.mcp_proxy.tools.sessions import create_session_messages_registry

        session_messages_registry = create_session_messages_registry(
            message_manager=message_manager,
            session_manager=local_session_manager,
            llm_service=llm_service,
            config=_config,
            db=db,
            worktree_manager=worktree_storage,
            inter_session_message_manager=inter_session_message_manager,
        )
        manager.add_registry(session_messages_registry)
        logger.debug("Sessions registry initialized")

    # Initialize memory registry if memory_manager is available
    if memory_manager is not None:
        from gobby.mcp_proxy.tools.memory import create_memory_registry

        # Set llm_service on memory_manager for remember_with_image support
        if llm_service is not None:
            memory_manager.llm_service = llm_service

        memory_registry = create_memory_registry(
            memory_manager=memory_manager,
            llm_service=llm_service,
            memory_sync_manager=memory_sync_manager,
            session_manager=local_session_manager,
            config=_config,
        )
        manager.add_registry(memory_registry)
        logger.debug("Memory registry initialized")

    # Initialize workflows registry (always available)
    from gobby.mcp_proxy.tools.workflows import create_workflows_registry

    workflows_registry = create_workflows_registry(
        session_manager=local_session_manager,
        db=getattr(local_session_manager, "db", None) if local_session_manager else None,
    )
    manager.add_registry(workflows_registry)
    logger.debug("Workflows registry initialized")

    # Initialize canvas registry (always available)
    from gobby.mcp_proxy.tools.canvas import create_canvas_registry

    canvas_registry = create_canvas_registry()
    manager.add_registry(canvas_registry)
    logger.debug("Canvas registry initialized")

    # Initialize metrics registry if metrics_manager is available
    if metrics_manager is not None:
        from gobby.mcp_proxy.tools.metrics import create_metrics_registry

        # Get daily budget from metrics config
        daily_budget_usd = 50.0  # Default
        if _config is not None:
            daily_budget_usd = _config.metrics.daily_budget_usd

        metrics_registry = create_metrics_registry(
            metrics_manager=metrics_manager,
            session_storage=local_session_manager,
            daily_budget_usd=daily_budget_usd,
        )
        manager.add_registry(metrics_registry)
        logger.debug("Metrics registry initialized with token tracking")

    # Initialize agents registry if agent_runner is available
    if agent_runner is not None:
        from gobby.mcp_proxy.tools.agents import create_agents_registry
        from gobby.workflows.state_manager import WorkflowStateManager

        # Create clone git manager if we have a git manager
        clone_git_manager = None
        if git_manager is not None:
            try:
                from gobby.clones.git import CloneGitManager

                clone_git_manager = CloneGitManager(git_manager.repo_path)
            except (TypeError, OSError, RuntimeError) as e:
                logger.debug(f"CloneGitManager not available for spawn_agent: {e}")

        # Create workflow state manager for mode=self workflow activation
        workflow_state_manager = None
        if db:
            try:
                workflow_state_manager = WorkflowStateManager(db)
            except (TypeError, OSError, RuntimeError) as e:
                logger.debug(f"WorkflowStateManager not available for agents registry: {e}")

        agents_registry = create_agents_registry(
            runner=agent_runner,
            session_manager=local_session_manager,
            task_manager=task_manager,
            worktree_storage=worktree_storage,
            git_manager=git_manager,
            clone_storage=clone_storage,
            clone_manager=clone_git_manager,
            workflow_state_manager=workflow_state_manager,
            db=db,
        )

        # Add inter-agent messaging tools if dependencies are available
        if (
            inter_session_message_manager is not None
            and local_session_manager is not None
            and db is not None
        ):
            from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
            from gobby.storage.agent_commands import AgentCommandManager
            from gobby.workflows.state_manager import SessionVariableManager

            add_messaging_tools(
                registry=agents_registry,
                message_manager=inter_session_message_manager,
                session_manager=local_session_manager,
                command_manager=AgentCommandManager(db),
                session_var_manager=SessionVariableManager(db),
                db=db,
            )
            logger.debug("Agent messaging tools added to agents registry")

        manager.add_registry(agents_registry)
        logger.debug("Agents registry initialized")

    # Initialize worktrees registry if worktree_storage is available
    if worktree_storage is not None:
        from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry

        worktrees_registry = create_worktrees_registry(
            worktree_storage=worktree_storage,
            git_manager=git_manager,
            project_id=project_id,
        )
        manager.add_registry(worktrees_registry)
        logger.debug("Worktrees registry initialized")

    # Initialize clones registry if clone_storage is available
    if clone_storage is not None:
        from gobby.mcp_proxy.tools.clones import create_clones_registry

        # Create CloneGitManager from the same repo path as WorktreeGitManager
        clone_git_manager = None
        if git_manager is not None:
            try:
                from gobby.clones.git import CloneGitManager

                clone_git_manager = CloneGitManager(git_manager.repo_path)
            except Exception as e:
                logger.warning(f"Failed to create CloneGitManager: {e}")

        clones_registry = create_clones_registry(
            clone_storage=clone_storage,
            git_manager=clone_git_manager,  # may be None; tools guard at call time
            project_id=project_id or "",
        )
        manager.add_registry(clones_registry)
        logger.debug("Clones registry initialized")

    # Initialize merge resolution registry if merge components are available
    if merge_storage is not None and merge_resolver is not None:
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        merge_registry = create_merge_registry(
            merge_storage=merge_storage,
            merge_resolver=merge_resolver,
            git_manager=git_manager,
            worktree_manager=worktree_storage,
        )
        manager.add_registry(merge_registry)
        logger.debug("Merge registry initialized")

    # Initialize hub registry (cross-project queries) if config has database_path
    if _config is not None and hasattr(_config, "database_path"):
        from pathlib import Path

        from gobby.mcp_proxy.tools.hub import create_hub_registry

        hub_db_path = Path(_config.database_path).expanduser()
        hub_registry = create_hub_registry(hub_db_path=hub_db_path)
        manager.add_registry(hub_registry)
        logger.debug("Hub registry initialized")

    # Initialize config registry if config_store is available
    if _config is not None and config_store is not None and config_setter is not None:
        from gobby.mcp_proxy.tools.config import create_config_registry

        config_registry = create_config_registry(
            config=_config,
            config_store=config_store,
            config_setter=config_setter,
            db=db,
        )
        manager.add_registry(config_registry)
        logger.debug("Config registry initialized")

    # Initialize voice registry if config_store is available
    if _config is not None and config_store is not None and config_setter is not None:
        from gobby.mcp_proxy.tools.voice import create_voice_registry

        voice_registry = create_voice_registry(
            config=_config,
            config_store=config_store,
            config_setter=config_setter,
        )
        manager.add_registry(voice_registry)
        logger.debug("Voice registry initialized")

    # Initialize skills registry if database is available
    if db is not None:
        from gobby.config.skills import SkillsConfig
        from gobby.mcp_proxy.tools.skills import create_skills_registry
        from gobby.skills.hubs import (
            ClaudePluginsProvider,
            ClawdHubProvider,
            GitHubCollectionProvider,
            HubManager,
            SkillHubProvider,
        )

        # Get skills config (or use defaults)
        skills_config = _config.skills if _config and hasattr(_config, "skills") else SkillsConfig()

        # Create hub manager with configured hubs
        hub_manager = HubManager(configs=skills_config.hubs)

        # Register provider factories
        hub_manager.register_provider_factory("clawdhub", ClawdHubProvider)
        hub_manager.register_provider_factory("skillhub", SkillHubProvider)
        hub_manager.register_provider_factory("github-collection", GitHubCollectionProvider)
        hub_manager.register_provider_factory("claude-plugins", ClaudePluginsProvider)
        hub_manager._skill_description_config = (
            getattr(_config, "skill_description", None) if _config else None
        )

        skills_registry = create_skills_registry(
            db=db,
            project_id=project_id,
            hub_manager=hub_manager,
        )
        manager.add_registry(skills_registry)
        logger.debug("Skills registry initialized")
    else:
        logger.debug("Skills registry not initialized: db is None")

    # Initialize pipelines registry (always registered; executor resolved lazily at tool call time)
    from gobby.mcp_proxy.tools.pipelines import create_pipelines_registry

    pipelines_registry = create_pipelines_registry(
        loader=workflow_loader,
        executor_getter=lambda: pipeline_executor,
        execution_manager_getter=lambda: pipeline_execution_manager,
        db=db,
        session_manager=local_session_manager,
    )
    manager.add_registry(pipelines_registry)
    logger.debug("Pipelines registry initialized")

    # Initialize cron registry if database is available
    if db is not None:
        try:
            from gobby.mcp_proxy.tools.cron import create_cron_registry
            from gobby.storage.cron import CronJobStorage

            cron_storage = CronJobStorage(db)
            cron_registry = create_cron_registry(cron_storage=cron_storage)
            manager.add_registry(cron_registry)
            logger.debug("Cron registry initialized")
        except (ImportError, RuntimeError, OSError) as e:
            logger.debug(f"Cron registry not initialized: {e}")

    logger.info(f"Internal registries initialized: {len(manager)} registries")
    return manager


# Re-export for convenience
__all__ = [
    "setup_internal_registries",
    "InternalRegistryManager",
]

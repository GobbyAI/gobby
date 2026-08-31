"""Internal registry initialization."""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalRegistryManager

if TYPE_CHECKING:
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner
    from gobby.config.app import DaemonConfig
    from gobby.config.values import ConfigValuesService
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.hooks.hook_manager import HookManager
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
    from gobby.memory.manager import MemoryManager
    from gobby.providers.capacity_service import ProviderCapacityService
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.concurrency import CoverageExecutor
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.tasks.validation import TaskValidator
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.pipeline_loader import PipelineLoader
    from gobby.worktrees.executor import WorktreeDeleteExecutor
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

logger = logging.getLogger("gobby.mcp.registries")


def setup_internal_registries(
    config_resolver: Callable[[], DaemonConfig | None],
    _session_manager: SessionManager | None = None,
    memory_manager_resolver: Callable[[], MemoryManager | None] | None = None,
    task_manager: LocalTaskManager | None = None,
    db: HubDatabase | None = None,
    task_validator_resolver: Callable[[], TaskValidator | None] | None = None,
    session_manager: SessionManager | None = None,
    metrics_manager: ToolMetricsManager | None = None,
    provider_capacity_resolver: Callable[[], ProviderCapacityService | None] | None = None,
    llm_service_resolver: Callable[[], LLMService | None] | None = None,
    agent_runner: AgentRunner | None = None,
    worktree_storage: LocalWorktreeManager | None = None,
    worktree_delete_executor: WorktreeDeleteExecutor | None = None,
    clone_storage: LocalCloneManager | None = None,
    git_manager: WorktreeGitManager | None = None,
    merge_storage: MergeResolutionManager | None = None,
    merge_resolver: MergeResolver | None = None,
    project_id: str | None = None,
    tool_proxy_getter: Callable[[], ToolProxyService | None] | None = None,
    inter_session_message_manager: InterSessionMessageManager | None = None,
    pipeline_executor: PipelineExecutor | None = None,
    workflow_loader: PipelineLoader | None = None,
    pipeline_execution_manager: LocalPipelineExecutionManager | None = None,
    hook_manager_resolver: Callable[[], HookManager | None] | None = None,
    config_service_getter: Callable[[], ConfigValuesService] | None = None,
    memory_backup_manager_resolver: Callable[[], Any | None] | None = None,
    completion_registry: CompletionEventRegistry | None = None,
    wake_dispatcher: WakeDispatcher | None = None,
    agent_lifecycle_monitor: AgentLifecycleMonitor | None = None,
    cron_scheduler: Any | None = None,
    mcp_manager_resolver: Callable[[], Any | None] | None = None,
    transcript_reader: Any | None = None,
    communications_manager: Any | None = None,
    web_chat_session_registry: Any | None = None,
    code_index: Any | None = None,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    coverage_executor: CoverageExecutor | None = None,
    detection_registry: DetectionManifestRegistry | None = None,
    dream_coordinator_resolver: Callable[[], Any | None] | None = None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
    write_coordinator: Any | None = None,
) -> InternalRegistryManager:
    """
    Setup internal MCP registries (tasks, messages, memory, metrics, agents, worktrees).

    Runtime-replaceable services (memory manager, task validator, LLM service,
    memory backup manager, external MCP manager) are passed as resolver
    callables so tool calls observe the current runtime epoch. Registry
    creation gates on a single resolve at setup time.

    Args:
        config_resolver: per-operation current Daemon configuration resolver
        _session_manager: Session manager (reserved for future use)
        memory_manager_resolver: per-call resolver for the current MemoryManager
        task_manager: Task storage manager
        db: Active hub database connection for registries that need storage
        task_validator_resolver: per-call resolver for the current TaskValidator
        session_manager: Session manager for session CRUD
        metrics_manager: Tool metrics manager for metrics operations
        llm_service_resolver: per-call resolver for the current LLM service
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
        wake_dispatcher: Dispatcher used to wake live sessions after mailbox messages.
        run_db: Optional bounded executor bridge for blocking database calls.

    Returns:
        InternalRegistryManager containing all registries
    """
    manager = InternalRegistryManager()
    initial_config = config_resolver()
    # Review learning needs an initial manager; memory tools resolve per call.
    initial_memory_manager = (
        memory_manager_resolver() if memory_manager_resolver is not None else None
    )
    review_learning_service = None
    if task_manager is not None and (
        initial_memory_manager is not None or memory_manager_resolver is not None
    ):
        from gobby.review_learning.service import ReviewLearningService

        review_learning_service = ReviewLearningService(
            memory_manager=initial_memory_manager,
            task_manager=task_manager,
            memory_manager_resolver=memory_manager_resolver,
        )

    # Initialize tasks registry if enabled and task_manager is available
    if initial_config is None:
        gobby_tasks_enabled = False
        logger.warning("Tasks registry not initialized: config is None")
    else:
        gobby_tasks_enabled = initial_config.get_gobby_tasks_config().enabled
        if not gobby_tasks_enabled:
            logger.debug("Tasks registry disabled by config")

    if gobby_tasks_enabled:
        if task_manager is None:
            logger.warning("Tasks registry not initialized: task_manager is None")
        else:
            from gobby.mcp_proxy.tools.tasks import create_task_registry

            tasks_registry = create_task_registry(
                task_manager=task_manager,
                task_validator_resolver=task_validator_resolver,
                startup_config=initial_config,
                config_resolver=config_resolver,
                project_id=project_id,
                review_learning_service=review_learning_service,
                completion_registry=completion_registry,
                agent_registry_resolver=lambda: manager.get_registry("gobby-agents"),
            )
            manager.add_registry(tasks_registry)
            logger.debug("Tasks registry initialized")

            # Initialize tasks-ops registry (expansion, affected files, github, reindex)
            from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

            ops_registry = create_task_ops_registry(
                task_manager=task_manager,
                task_validator_resolver=task_validator_resolver,
                startup_config=initial_config,
                config_resolver=config_resolver,
                llm_service_resolver=llm_service_resolver,
                completion_registry=completion_registry,
                mcp_manager_resolver=mcp_manager_resolver,
                review_learning_service=review_learning_service,
            )
            manager.add_registry(ops_registry)
            logger.debug("Tasks-ops registry initialized")

    if db is not None:
        from gobby.mcp_proxy.tools.plans import create_plan_registry

        plan_registry = create_plan_registry(
            db,
            default_project_id=project_id,
            run_db=run_db,
            coverage_executor=coverage_executor,
        )
        manager.add_registry(plan_registry)
        logger.debug("Plans registry initialized")

        from gobby.mcp_proxy.tools.profiles import create_profiles_registry

        profiles_registry = create_profiles_registry(db, default_project_id=project_id)
        manager.add_registry(profiles_registry)
        logger.debug("Profiles registry initialized")

        if initial_config is not None:
            offload_config = initial_config.get_tool_result_offload_config()
            if offload_config.enabled is True:
                from gobby.mcp_proxy.tools.results import create_results_registry

                results_registry = create_results_registry(
                    db,
                    lambda: (
                        config.get_tool_result_offload_config()
                        if (config := config_resolver()) is not None
                        else offload_config
                    ),
                    default_project_id=project_id,
                )
                manager.add_registry(results_registry)
                logger.debug("Results registry initialized")

    # Initialize sessions registry (messages + session CRUD)
    if session_manager is not None:
        from gobby.mcp_proxy.tools.sessions import create_session_messages_registry

        session_messages_registry = create_session_messages_registry(
            session_manager=session_manager,
            llm_service_resolver=llm_service_resolver,
            memory_manager_resolver=memory_manager_resolver,
            startup_config=initial_config,
            config_resolver=config_resolver,
            db=db,
            worktree_manager=worktree_storage,
            inter_session_message_manager=inter_session_message_manager,
            transcript_reader=transcript_reader,
            web_chat_session_registry=web_chat_session_registry,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
            write_coordinator=write_coordinator,
        )
        manager.add_registry(session_messages_registry)
        logger.debug("Sessions registry initialized")

    # Keep memory tools available across startup outages and runtime rebuilds.
    if memory_manager_resolver is not None:
        from gobby.mcp_proxy.tools.memory import create_memory_registry

        memory_registry = create_memory_registry(
            memory_manager_resolver=memory_manager_resolver,
            llm_service_resolver=llm_service_resolver,
            memory_backup_manager_resolver=memory_backup_manager_resolver,
            session_manager=session_manager,
            startup_config=initial_config,
            config_resolver=config_resolver,
            dream_coordinator_resolver=dream_coordinator_resolver,
            task_manager=task_manager,
        )
        manager.add_registry(memory_registry)
        logger.debug("Memory registry initialized")

    if review_learning_service is not None:
        from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry

        review_learning_registry = create_review_learning_registry(review_learning_service)
        manager.add_registry(review_learning_registry)
        logger.debug("Review-learning registry initialized")

    # Initialize workflows registry (always available — umbrella for pipelines + agent defs)
    from gobby.mcp_proxy.tools.workflows import create_workflows_registry, workflow_mcp_inventory

    workflows_registry = create_workflows_registry(
        loader=workflow_loader,
        session_manager=session_manager,
        db=getattr(session_manager, "db", None) if session_manager else None,
        internal_manager=manager,
        mcp_manager_resolver=mcp_manager_resolver,
        executor_getter=lambda: pipeline_executor,
        execution_manager_getter=lambda: pipeline_execution_manager,
        completion_registry=completion_registry,
        detection_registry=detection_registry,
    )
    manager.add_registry(workflows_registry)
    logger.debug("Workflows registry initialized")

    # Initialize wiki registry (always available; gateway availability is checked per call)
    from gobby.mcp_proxy.tools.wiki import create_wiki_registry

    wiki_registry = create_wiki_registry(
        db=db,
        default_project_id=project_id,
    )
    manager.add_registry(wiki_registry)
    logger.debug("Wiki registry initialized")

    # Initialize metrics registry if metrics_manager is available
    if metrics_manager is not None:
        from gobby.mcp_proxy.tools.metrics import create_metrics_registry

        metrics_registry = create_metrics_registry(
            metrics_manager=metrics_manager,
            session_storage=session_manager,
            event_store=metrics_manager.event_store,
            provider_capacity_resolver=provider_capacity_resolver,
        )
        manager.add_registry(metrics_registry)
        logger.debug("Metrics registry initialized with usage reporting")

    # Initialize agents registry if agent_runner is available
    if agent_runner is not None:
        from gobby.mcp_proxy.tools.agents import create_agents_registry

        # Create clone git manager if we have a git manager
        clone_git_manager = None
        if git_manager is not None:
            try:
                from gobby.clones.git import CloneGitManager

                clone_git_manager = CloneGitManager(git_manager.repo_path)
            except (TypeError, OSError, RuntimeError) as e:
                logger.debug("CloneGitManager not available for spawn_agent: %s", e)

        agents_registry = create_agents_registry(
            runner=agent_runner,
            session_manager=session_manager,
            task_manager=task_manager,
            worktree_storage=worktree_storage,
            git_manager=git_manager,
            clone_storage=clone_storage,
            clone_manager=clone_git_manager,
            db=db,
            workflow_loader=workflow_loader,
            mcp_inventory=workflow_mcp_inventory(manager, mcp_manager_resolver),
            completion_registry=completion_registry,
            lifecycle_monitor=agent_lifecycle_monitor,
            startup_config=initial_config,
            config_resolver=config_resolver,
            code_index=code_index,
            transcript_reader=transcript_reader,
            detection_registry=detection_registry,
        )

        # Add inter-agent messaging tools if dependencies are available
        if (
            inter_session_message_manager is not None
            and session_manager is not None
            and db is not None
        ):
            from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools

            add_messaging_tools(
                registry=agents_registry,
                message_manager=inter_session_message_manager,
                session_manager=session_manager,
                db=db,
                wake_dispatcher=wake_dispatcher,
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
            session_manager=session_manager,
            task_manager=task_manager,
            worktree_delete_executor=worktree_delete_executor,
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
                logger.warning("Failed to create CloneGitManager: %s", e)

        clones_registry = create_clones_registry(
            clone_storage=clone_storage,
            git_manager=clone_git_manager,  # may be None; tools guard at call time
            project_id=project_id,
            task_manager=task_manager,
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
            db=db,
        )
        manager.add_registry(merge_registry)
        logger.debug("Merge registry initialized")

    # Initialize hub registry (cross-project queries) from the active runtime database.
    if db is not None:
        from gobby.mcp_proxy.tools.hub import create_hub_registry

        hub_registry = create_hub_registry(db=db)
        manager.add_registry(hub_registry)
        logger.debug("Hub registry initialized")

    # Initialize config registry from the shared universal service.
    if config_service_getter is not None:
        from gobby.mcp_proxy.tools.config import create_config_registry

        config_registry = create_config_registry(config_service_getter)
        manager.add_registry(config_registry)
        logger.debug("Config registry initialized")

    # Voice uses the same typed, revisioned service as generic configuration tools.
    if config_service_getter is not None:
        from gobby.mcp_proxy.tools.voice import create_voice_registry

        voice_registry = create_voice_registry(config_service_getter)
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
            GitHubTopicProvider,
            HubManager,
            SkillsMPProvider,
        )
        from gobby.skills.hubs.manager import resolve_hub_api_keys
        from gobby.skills.search import SkillSearch
        from gobby.storage.secrets import SecretStore

        secret_store = SecretStore(db)

        last_resolved_config: DaemonConfig | None = None

        def active_daemon_config() -> DaemonConfig | None:
            nonlocal last_resolved_config
            resolved = config_resolver()
            if resolved is not None:
                last_resolved_config = resolved
                return resolved
            if last_resolved_config is not None:
                return last_resolved_config
            return initial_config

        def build_hub_manager(active_config: DaemonConfig | None) -> HubManager:
            skills_config = active_config.skills if active_config is not None else SkillsConfig()
            api_keys = resolve_hub_api_keys(skills_config.hubs, secret_store)
            manager = HubManager(configs=skills_config.hubs, api_keys=api_keys)
            manager.register_provider_factory("clawdhub", ClawdHubProvider)
            manager.register_provider_factory("skillsmp", SkillsMPProvider)
            manager.register_provider_factory("github-collection", GitHubCollectionProvider)
            manager.register_provider_factory("github-topic", GitHubTopicProvider)
            manager.register_provider_factory("claude-plugins", ClaudePluginsProvider)
            manager._skill_description_config = (
                active_config.skill_description if active_config is not None else None
            )
            manager.warn_missing_auth()
            return manager

        def build_skill_search(active_config: DaemonConfig | None) -> SkillSearch:
            embeddings = active_config.embeddings if active_config is not None else None
            return SkillSearch(
                config=active_config.get_search_config() if active_config is not None else None,
                db=db,
                embedding_model=embeddings.model if embeddings else "nomic-embed-text",
                embedding_api_base=embeddings.api_base if embeddings else None,
                embedding_api_key=embeddings.api_key if embeddings else None,
                embedding_dim=embeddings.dim if embeddings else None,
            )

        initial_skills_epoch = initial_config
        initial_hub_manager = build_hub_manager(initial_skills_epoch)
        initial_search = build_skill_search(initial_skills_epoch)
        hub_cache: tuple[object | None, HubManager] = (
            initial_skills_epoch,
            initial_hub_manager,
        )
        search_cache: tuple[object | None, SkillSearch] = (
            initial_skills_epoch,
            initial_search,
        )
        hub_cache_lock = threading.Lock()
        search_cache_lock = threading.Lock()

        def resolve_hub_manager() -> HubManager:
            nonlocal hub_cache
            with hub_cache_lock:
                active_config = active_daemon_config()
                if hub_cache[0] is not active_config:
                    hub_cache = (active_config, build_hub_manager(active_config))
                return hub_cache[1]

        def resolve_skill_search() -> SkillSearch:
            nonlocal search_cache
            with search_cache_lock:
                active_config = active_daemon_config()
                if search_cache[0] is not active_config:
                    search_cache = (active_config, build_skill_search(active_config))
                return search_cache[1]

        skills_registry = create_skills_registry(
            db=db,
            project_id=project_id,
            hub_manager=initial_hub_manager,
            search=initial_search,
            run_db=run_db,
            hub_manager_resolver=resolve_hub_manager,
            search_resolver=resolve_skill_search,
        )
        manager.add_registry(skills_registry)
        logger.debug("Skills registry initialized")
    else:
        logger.debug("Skills registry not initialized: db is None")

    # Initialize cron registry if database is available
    if db is not None:
        try:
            from gobby.mcp_proxy.tools.cron import create_cron_registry
            from gobby.storage.cron import CronJobStorage

            cron_storage = CronJobStorage(db)
            cron_registry = create_cron_registry(
                cron_storage=cron_storage, cron_scheduler=cron_scheduler
            )
            manager.add_registry(cron_registry)
            logger.debug("Cron registry initialized")
        except (ImportError, RuntimeError, OSError) as e:
            logger.debug("Cron registry not initialized: %s", e)

    if communications_manager is not None:
        try:
            from gobby.mcp_proxy.tools.communications import create_communications_registry

            communications_registry = create_communications_registry(
                communications_manager,
                db=db,
            )
            manager.add_registry(communications_registry)
            logger.debug("Communications registry initialized")
        except (ImportError, RuntimeError, OSError) as e:
            logger.debug("Communications registry not initialized: %s", e)

    logger.info("Internal registries initialized: %s registries", len(manager))
    return manager


# Re-export for convenience
__all__ = [
    "setup_internal_registries",
    "InternalRegistryManager",
]

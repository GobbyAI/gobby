from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.runner import AgentRunner
from gobby.app_context import ServiceContainer
from gobby.config.app import load_config
from gobby.llm import LLMService, create_llm_service
from gobby.llm.resolver import ExecutorRegistry
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.search.embeddings import generate_embedding
from gobby.servers.http import HTTPServer
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import DatabaseProtocol, LocalDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.migrations import run_migrations
from gobby.storage.session_messages import LocalSessionMessageManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemorySyncManager
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.validation import TaskValidator
from gobby.utils.logging import setup_file_logging
from gobby.utils.machine_id import get_machine_id

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Strip Claude Code session marker so SDK subprocess calls don't fail with
# "cannot be launched inside another Claude Code session" when the daemon
# was started/restarted from within a Claude Code session.
os.environ.pop("CLAUDECODE", None)

# Suppress litellm's never-awaited coroutine warnings (upstream bug in LoggingWorker)
import warnings

warnings.filterwarnings("ignore", message="coroutine.*async_success_handler.*was never awaited")

# Type hints for pipeline components (imported lazily at runtime)
if TYPE_CHECKING:
    from gobby.scheduler.scheduler import CronScheduler
    from gobby.storage.cron import CronJobStorage
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.loader import WorkflowLoader
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


class GobbyRunner:
    """Runner for Gobby daemon."""

    def __init__(self, config_path: Path | None = None, verbose: bool = False):
        setup_file_logging(verbose=verbose)

        if config_path is not None and not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}. "
                f"Use 'gobby install' to create bootstrap.yaml, "
                f"or omit --config to use the default path (~/.gobby/bootstrap.yaml)."
            )
        self._config_file = str(config_path) if config_path else None
        self.config = load_config(self._config_file)
        self.verbose = verbose
        self.machine_id = get_machine_id()

        # Check tmux availability (agent spawning requires it)
        import shutil

        from gobby.agents.tmux.wsl_compat import needs_wsl

        if needs_wsl():
            if not shutil.which("wsl"):
                logger.warning(
                    "WSL is not installed. Agent spawning in terminal mode will not work. "
                    "Install: wsl --install"
                )
        elif not shutil.which("tmux"):
            logger.warning(
                "tmux is not installed. Agent spawning in terminal mode will not work. "
                "Install: brew install tmux (macOS), apt install tmux (Linux)"
            )
        self._shutdown_requested = False
        self._metrics_cleanup_task: asyncio.Task[None] | None = None
        self._vector_rebuild_task: asyncio.Task[None] | None = None
        self._zombie_messages_task: asyncio.Task[None] | None = None

        # Initialize local storage with dual-write if in project context
        self.database = self._init_database()

        # Phase 2: Reload config from DB (secrets > env vars)
        # Phase 1 (above) used env vars only to bootstrap the database path.
        # Now that the DB is available, load config from config_store with secrets.
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.secrets import SecretStore

        self.secret_store = SecretStore(self.database)
        self.config_store = ConfigStore(self.database)
        self.config = load_config(
            config_file=self._config_file,
            secret_resolver=self.secret_store.get,
            config_store=self.config_store,
        )

        # Populate model costs from LiteLLM into DB and load into memory
        from gobby.llm.cost_table import init as init_cost_table
        from gobby.storage.model_costs import ModelCostStore

        try:
            cost_store = ModelCostStore(self.database)
            cost_store.populate_from_litellm()
            init_cost_table(self.database)
        except Exception as e:
            logger.warning(f"Failed to populate model costs: {e}")

        self.session_manager = LocalSessionManager(self.database)
        self.message_manager = LocalSessionMessageManager(self.database)
        self.task_manager = LocalTaskManager(self.database)
        self.session_task_manager = SessionTaskManager(self.database)

        from gobby.utils.dev import is_dev_mode

        self._dev_mode = is_dev_mode(Path.cwd())

        # In dev mode, auto-sync bundled content so YAML edits are picked up
        # on every daemon restart without needing a full `gobby install`.
        if self._dev_mode:
            from gobby.cli.installers.shared import sync_bundled_content_to_db

            sync_result = sync_bundled_content_to_db(self.database)
            total = sync_result["total_synced"]
            if total > 0:
                logger.info(f"Dev mode: synced {total} bundled items on startup")

        # Initialize Prompt Manager
        from gobby.storage.prompts import LocalPromptManager

        self.prompt_manager = LocalPromptManager(self.database, dev_mode=self._dev_mode)

        # Initialize Skill Manager and Hub Manager
        from gobby.storage.skills import LocalSkillManager

        self.skill_manager = LocalSkillManager(self.database)

        self.hub_manager: Any | None = None
        try:
            from gobby.config.skills import SkillsConfig
            from gobby.skills.hubs import (
                ClaudePluginsProvider,
                ClawdHubProvider,
                GitHubCollectionProvider,
                HubManager,
                SkillHubProvider,
            )

            skills_config = self.config.skills if hasattr(self.config, "skills") else SkillsConfig()
            self.hub_manager = HubManager(configs=skills_config.hubs)
            self.hub_manager.register_provider_factory("clawdhub", ClawdHubProvider)
            self.hub_manager.register_provider_factory("skillhub", SkillHubProvider)
            self.hub_manager.register_provider_factory(
                "github-collection", GitHubCollectionProvider
            )
            self.hub_manager.register_provider_factory("claude-plugins", ClaudePluginsProvider)
            self.hub_manager._skill_description_config = (
                self.config.skill_description if hasattr(self.config, "skill_description") else None
            )
            logger.debug(f"HubManager initialized with {len(skills_config.hubs)} hubs")
        except Exception as e:
            logger.warning(f"Failed to initialize HubManager: {e}")

        # Initialize LLM Service
        self.llm_service: LLMService | None = None  # Added type hint
        try:
            self.llm_service = create_llm_service(self.config)
            logger.debug(f"LLM service initialized: {self.llm_service.enabled_providers}")
        except Exception as e:
            logger.error(f"Failed to initialize LLM service: {e}")

        # Initialize VectorStore and Memory Manager
        self.vector_store: VectorStore | None = None
        self.memory_manager: MemoryManager | None = None
        if hasattr(self.config, "memory"):
            try:
                # Create VectorStore (async initialize() called during startup)
                gobby_home = Path(os.environ.get("GOBBY_HOME", str(Path.home() / ".gobby")))
                qdrant_path = self.config.memory.qdrant_path or str(
                    gobby_home / "services" / "qdrant"
                )
                self.vector_store = VectorStore(
                    path=qdrant_path if not self.config.memory.qdrant_url else None,
                    url=self.config.memory.qdrant_url,
                    api_key=self.config.memory.qdrant_api_key,
                )
                embed_fn: Callable[..., Any] | None = None
                if self.llm_service:
                    from functools import partial

                    embed_fn = partial(
                        generate_embedding,
                        model=self.config.memory.embedding_model,
                    )

                self.memory_manager = MemoryManager(
                    self.database,
                    self.config.memory,
                    llm_service=self.llm_service,
                    vector_store=self.vector_store,
                    embed_fn=embed_fn,
                )
            except Exception as e:
                logger.error(f"Failed to initialize MemoryManager: {e}")

        # MCP Proxy Manager - Initialize early for tool access
        # LocalMCPManager handles server/tool storage in SQLite
        self.mcp_db_manager = LocalMCPManager(self.database)

        # Tool Metrics Manager for tracking call statistics
        from gobby.mcp_proxy.metrics import ToolMetricsManager

        self.metrics_manager = ToolMetricsManager(self.database)

        # MCPClientManager loads servers from database on init
        self.mcp_proxy = MCPClientManager(
            mcp_db_manager=self.mcp_db_manager,
            metrics_manager=self.metrics_manager,
        )

        # Task Sync Manager
        self.task_sync_manager = TaskSyncManager(self.task_manager)

        # Import synced tasks before wiring export listener
        # (e.g. from git on a new machine with more tasks than local DB)
        try:
            self.task_sync_manager.import_from_jsonl()
            logger.info("Initial task sync import completed")
        except Exception as e:
            logger.warning(f"Task sync import failed: {e}")

        # Force initial synchronous export
        # Ensures disk state matches DB state before we start serving
        try:
            self.task_sync_manager.export_to_jsonl()
            logger.info("Initial task sync export completed")
        except Exception as e:
            logger.warning(f"Initial task sync export failed: {e}")

        # Wire up change listener for automatic export (after import to avoid race)
        self.task_manager.add_change_listener(self.task_sync_manager.trigger_export)

        # Initialize Memory Sync Manager (Phase 7) & Wire up listeners
        self.memory_sync_manager: MemorySyncManager | None = None
        if hasattr(self.config, "memory_sync") and self.config.memory_sync.enabled:
            if self.memory_manager:
                try:
                    self.memory_sync_manager = MemorySyncManager(
                        db=self.database,
                        memory_manager=self.memory_manager,
                        config=self.config.memory_sync,
                    )
                    # No per-change listener — the file is a backup, not a live mirror.
                    # Export happens at: daemon shutdown, session end, explicit sync_export.
                    logger.debug("MemorySyncManager initialized (export on shutdown/session-end)")

                    # Import synced memories before exporting
                    # (e.g. from git on a new machine with more memories than local DB)
                    try:
                        imported = self.memory_sync_manager.import_sync()
                        if imported > 0:
                            logger.info(f"Imported {imported} memories from sync file")
                    except (OSError, ValueError) as e:
                        logger.warning(f"Memory import failed: {e}")

                    # Force initial synchronous export
                    # Ensures disk state matches DB state before we start serving
                    try:
                        self.memory_sync_manager.export_sync()
                        logger.info("Initial memory sync export completed")
                    except (OSError, ValueError) as e:
                        logger.warning(f"Initial memory sync failed: {e}")

                except Exception as e:
                    logger.error(f"Failed to initialize MemorySyncManager: {e}")

        # Session Message Processor (Phase 6)
        # Created here and passed to HTTPServer which injects it into HookManager
        self.message_processor: SessionMessageProcessor | None = None
        if getattr(self.config, "message_tracking", None) and self.config.message_tracking.enabled:
            self.message_processor = SessionMessageProcessor(
                db=self.database,
                poll_interval=self.config.message_tracking.poll_interval,
            )

        # Initialize Task Validator (Phase 7.1)
        self.task_validator: TaskValidator | None = None

        if self.llm_service:
            gobby_tasks_config = self.config.gobby_tasks
            if gobby_tasks_config.validation.enabled:
                try:
                    self.task_validator = TaskValidator(
                        llm_service=self.llm_service,
                        config=gobby_tasks_config.validation,
                        db=self.database,
                    )
                except Exception as e:
                    logger.error(f"Failed to initialize TaskValidator: {e}")

        # Initialize Worktree Storage (Phase 7 - Subagents)
        self.worktree_storage = LocalWorktreeManager(self.database)

        # Initialize Clone Storage (local git clones for isolated development)
        self.clone_storage = LocalCloneManager(self.database)

        # Detect project context from cwd so daemon-level services (pipelines,
        # clones) can register their MCP tools.  Per-session resolution can
        # still override via ServiceContainer.
        self.git_manager: WorktreeGitManager | None = None
        self.project_id: str | None = None
        try:
            from gobby.utils.project_context import get_project_context
            from gobby.worktrees.git import WorktreeGitManager as _WGM

            project_ctx = get_project_context(Path.cwd())
            if project_ctx and project_ctx.get("id"):
                self.project_id = str(project_ctx["id"])
                project_path = project_ctx.get("project_path")
                if project_path:
                    self.git_manager = _WGM(str(project_path))
                    logger.debug(
                        f"Daemon project context: id={self.project_id}, path={project_path}"
                    )
        except Exception as e:
            logger.debug(f"Could not detect project context from cwd: {e}")

        # WorkflowLoader is project-agnostic; pipeline executor needs project context.
        self.workflow_loader: WorkflowLoader | None = None
        self.pipeline_execution_manager: LocalPipelineExecutionManager | None = None
        self.pipeline_executor: PipelineExecutor | None = None
        try:
            from gobby.workflows.loader import WorkflowLoader

            self.workflow_loader = WorkflowLoader(db=self.database)
        except Exception as e:
            logger.warning(f"Failed to initialize workflow loader: {e}")

        # Create pipeline executor at startup if we have project context
        if self.workflow_loader is not None and self.project_id:
            try:
                from gobby.storage.pipelines import LocalPipelineExecutionManager as _LPEM
                from gobby.workflows.pipeline_executor import PipelineExecutor as _PE
                from gobby.workflows.templates import TemplateEngine

                self.pipeline_execution_manager = _LPEM(
                    db=self.database, project_id=self.project_id
                )
                self.pipeline_executor = _PE(
                    db=self.database,
                    execution_manager=self.pipeline_execution_manager,
                    llm_service=self.llm_service,
                    loader=self.workflow_loader,
                    template_engine=TemplateEngine(),
                    session_manager=self.session_manager,
                )
                logger.info("Pipeline executor initialized at startup")
            except Exception as e:
                logger.warning(f"Failed to initialize pipeline executor at startup: {e}")

        # Initialize Agent Runner (Phase 7 - Subagents)
        # Create executor registry for lazy executor creation
        self.executor_registry = ExecutorRegistry(config=self.config)
        self.agent_runner: AgentRunner | None = None
        try:
            # Pre-initialize common executors
            executors = {}
            for provider in ["claude", "gemini", "litellm"]:
                try:
                    executors[provider] = self.executor_registry.get(provider=provider)
                    logger.info(f"Pre-initialized {provider} executor")
                except Exception as e:
                    logger.debug(f"Could not pre-initialize {provider} executor: {e}")

            self.agent_runner = AgentRunner(
                db=self.database,
                session_storage=self.session_manager,
                executors=executors,
                max_agent_depth=5,
            )
            logger.debug(f"AgentRunner initialized with executors: {list(executors.keys())}")
        except Exception as e:
            logger.error(f"Failed to initialize AgentRunner: {e}")

        # Agent Lifecycle Monitor (detect dead tmux sessions)
        from gobby.agents.registry import get_running_agent_registry
        from gobby.storage.agents import LocalAgentRunManager

        try:
            self.agent_lifecycle_monitor: AgentLifecycleMonitor | None = AgentLifecycleMonitor(
                agent_registry=get_running_agent_registry(),
                agent_run_manager=LocalAgentRunManager(self.database),
                clone_storage=self.clone_storage,
            )
        except Exception as e:
            logger.warning(f"Failed to initialize AgentLifecycleMonitor: {e}")
            self.agent_lifecycle_monitor = None

        # Session Lifecycle Manager (background jobs for expiring and processing)
        self.lifecycle_manager = SessionLifecycleManager(
            db=self.database,
            config=self.config.session_lifecycle,
            memory_manager=self.memory_manager,
            llm_service=self.llm_service,
            memory_sync_manager=self.memory_sync_manager,
        )
        self.lifecycle_manager._memory_extraction_config = (
            self.config.memory_extraction if hasattr(self.config, "memory_extraction") else None
        )

        # Cron Scheduler (background jobs for recurring tasks)
        self.cron_storage: CronJobStorage | None = None
        self.cron_scheduler: CronScheduler | None = None
        try:
            from gobby.scheduler.executor import CronExecutor
            from gobby.scheduler.scheduler import CronScheduler
            from gobby.storage.cron import CronJobStorage

            self.cron_storage = CronJobStorage(self.database)
            cron_executor = CronExecutor(
                storage=self.cron_storage,
                agent_runner=self.agent_runner,
                pipeline_executor=self.pipeline_executor,
            )
            self.cron_scheduler = CronScheduler(
                storage=self.cron_storage,
                executor=cron_executor,
                config=self.config.cron,
            )
            logger.debug("CronScheduler initialized")
        except Exception as e:
            logger.error(f"Failed to initialize CronScheduler: {e}")

        # HTTP Server
        # Bundle services into container
        services = ServiceContainer(
            config=self.config,
            database=self.database,
            session_manager=self.session_manager,
            task_manager=self.task_manager,
            task_sync_manager=self.task_sync_manager,
            memory_sync_manager=self.memory_sync_manager,
            memory_manager=self.memory_manager,
            llm_service=self.llm_service,
            mcp_manager=self.mcp_proxy,
            mcp_db_manager=self.mcp_db_manager,
            metrics_manager=self.metrics_manager,
            agent_runner=self.agent_runner,
            message_processor=self.message_processor,
            message_manager=self.message_manager,
            task_validator=self.task_validator,
            worktree_storage=self.worktree_storage,
            clone_storage=self.clone_storage,
            git_manager=self.git_manager,
            project_id=self.project_id,
            pipeline_executor=self.pipeline_executor,
            workflow_loader=self.workflow_loader,
            pipeline_execution_manager=self.pipeline_execution_manager,
            agent_lifecycle_monitor=self.agent_lifecycle_monitor,
            cron_storage=self.cron_storage,
            cron_scheduler=self.cron_scheduler,
            skill_manager=self.skill_manager,
            hub_manager=self.hub_manager,
            config_store=self.config_store,
            prompt_manager=self.prompt_manager,
            dev_mode=self._dev_mode,
            tool_proxy_getter=lambda: self.http_server.tool_proxy,
        )

        self.http_server: HTTPServer = HTTPServer(
            services=services,
            port=self.config.daemon_port,
            test_mode=self.config.test_mode,
        )

        # Inject server into container for circular ref if needed later
        # self.http_server.services = services

        # Ensure message_processor property is set (redundant but explicit):
        self.http_server.message_processor = self.message_processor

        # Wire tool_proxy_getter onto the startup-created pipeline executor.
        # The executor was created before http_server existed, so we do it now.
        if self.pipeline_executor is not None:
            self.pipeline_executor.tool_proxy_getter = lambda: self.http_server.tool_proxy

        # WebSocket Server (Optional)
        self.websocket_server: WebSocketServer | None = None
        if self.config.websocket and getattr(self.config.websocket, "enabled", True):
            websocket_config = WebSocketConfig(
                host=self.config.bind_host,
                port=self.config.websocket.port,
                ping_interval=self.config.websocket.ping_interval,
                ping_timeout=self.config.websocket.ping_timeout,
            )
            self.websocket_server = WebSocketServer(
                config=websocket_config,
                mcp_manager=self.mcp_proxy,
                session_manager=self.session_manager,
                message_manager=self.message_manager,
                daemon_config=self.config,
                internal_manager=self.http_server._internal_manager,
            )
            # Pass WebSocket server reference to HTTP server for broadcasting
            self.http_server.websocket_server = self.websocket_server
            # Also set on services container so lifespan can wire workflow_handler
            self.http_server.services.websocket_server = self.websocket_server
            # Also update the HTTPServer's broadcaster to use the same websocket_server
            self.http_server.broadcaster.websocket_server = self.websocket_server

            # Pass WebSocket server to message processor if enabled
            if self.message_processor:
                self.message_processor.websocket_server = self.websocket_server

            # Register agent event callback for WebSocket broadcasting
            from gobby.runner_broadcasting import (
                setup_agent_event_broadcasting,
                setup_pipeline_event_broadcasting,
            )

            setup_agent_event_broadcasting(self.websocket_server)

            # Register pipeline event callback for WebSocket broadcasting
            if self.pipeline_executor:
                setup_pipeline_event_broadcasting(self.websocket_server, self.pipeline_executor)

    def _init_database(self) -> DatabaseProtocol:
        """Initialize hub database."""
        hub_db_path = Path(self.config.database_path).expanduser()

        # Ensure hub db directory exists
        hub_db_path.parent.mkdir(parents=True, exist_ok=True)

        hub_db = LocalDatabase(hub_db_path)
        run_migrations(hub_db)

        logger.info(f"Database: {hub_db_path}")
        return hub_db

    async def run(self) -> None:
        from gobby.runner_maintenance import (
            cleanup_pid_file,
            cleanup_stale_tmux_sessions,
            cleanup_zombie_messages_loop,
            expire_approval_timeouts_loop,
            metrics_cleanup_loop,
            rebuild_vector_store,
            setup_signal_handlers,
        )

        try:
            setup_signal_handlers(lambda: setattr(self, "_shutdown_requested", True))

            # Connect MCP servers
            try:
                await asyncio.wait_for(self.mcp_proxy.connect_all(), timeout=10.0)
            except TimeoutError:
                logger.warning("MCP connection timed out")
            except Exception as e:
                logger.error(f"MCP connection failed: {e}")

            # Clean up stale tmux sessions from previous runs
            await cleanup_stale_tmux_sessions()

            # Neo4j health check: disable KG features if unreachable
            if (
                self.memory_manager
                and hasattr(self.config, "memory")
                and self.config.memory.neo4j_url
            ):
                from gobby.cli.services import is_neo4j_healthy

                neo4j_url = self.config.memory.neo4j_url
                if not await is_neo4j_healthy(neo4j_url):
                    logger.warning(
                        f"Neo4j configured but unreachable at {neo4j_url} — graph features disabled"
                    )
                    self.memory_manager._neo4j_client = None
                    self.memory_manager._kg_service = None

            # Run metrics cleanup on startup
            try:
                deleted = self.metrics_manager.cleanup_old_metrics()
                if deleted > 0:
                    logger.info(f"Startup metrics cleanup: removed {deleted} old entries")
            except Exception as e:
                logger.warning(f"Metrics cleanup failed: {e}")

            # Initialize VectorStore and schedule rebuild in background if needed
            if self.vector_store:
                try:
                    await self.vector_store.initialize()
                    qdrant_count = await self.vector_store.count()
                    if qdrant_count == 0 and self.memory_manager:
                        sqlite_memories = self.memory_manager.storage.list_memories(limit=10000)
                        if sqlite_memories:
                            embed_fn = self.memory_manager.embed_fn
                            if embed_fn:
                                logger.info(
                                    f"Qdrant empty, scheduling background rebuild from "
                                    f"{len(sqlite_memories)} SQLite memories..."
                                )
                                memory_dicts = [
                                    {"id": m.id, "content": m.content} for m in sqlite_memories
                                ]
                                self._vector_rebuild_task = asyncio.create_task(
                                    rebuild_vector_store(self.vector_store, memory_dicts, embed_fn),
                                    name="vector-store-rebuild",
                                )
                            else:
                                logger.warning(
                                    "No embed_fn configured, skipping VectorStore rebuild"
                                )
                except Exception as e:
                    logger.error(f"VectorStore initialization failed: {e}")

            # Start Message Processor
            if self.message_processor:
                await self.message_processor.start()

            # Start Session Lifecycle Manager
            await self.lifecycle_manager.start()

            # Start Agent Lifecycle Monitor (detect dead tmux sessions)
            if self.agent_lifecycle_monitor:
                await self.agent_lifecycle_monitor.cleanup_orphaned_db_runs()
                await self.agent_lifecycle_monitor.start()

            # Start Cron Scheduler
            if self.cron_scheduler:
                await self.cron_scheduler.start()

            # Start periodic metrics cleanup (every 24 hours)
            self._metrics_cleanup_task = asyncio.create_task(
                metrics_cleanup_loop(self.metrics_manager, lambda: self._shutdown_requested),
                name="metrics-cleanup",
            )

            # Start periodic zombie message cleanup (every 6 hours)
            self._zombie_messages_task = asyncio.create_task(
                cleanup_zombie_messages_loop(self.database, lambda: self._shutdown_requested),
                name="zombie-message-cleanup",
            )

            # Start periodic approval timeout expiry (every 60s)
            self._approval_timeout_task: asyncio.Task[None] | None = None
            if self.pipeline_execution_manager:
                self._approval_timeout_task = asyncio.create_task(
                    expire_approval_timeouts_loop(
                        self.pipeline_execution_manager,
                        lambda: self._shutdown_requested,
                    ),
                    name="approval-timeout-expiry",
                )

            # Resume interrupted pipelines and fail non-resumable stale executions
            if self.pipeline_executor and self.pipeline_execution_manager and self.workflow_loader:
                try:
                    from gobby.mcp_proxy.tools.pipelines._execution import (
                        resume_interrupted_pipelines,
                    )

                    resumed_ids = await resume_interrupted_pipelines(
                        loader=self.workflow_loader,
                        executor=self.pipeline_executor,
                        execution_manager=self.pipeline_execution_manager,
                        project_id=self.project_id,
                    )
                    if resumed_ids:
                        logger.info(
                            f"Resumed {len(resumed_ids)} pipeline(s) after restart: {resumed_ids}"
                        )

                    stale_count = self.pipeline_execution_manager.fail_stale_running_executions(
                        exclude_ids=set(resumed_ids),
                    )
                    if stale_count > 0:
                        logger.info(f"Failed {stale_count} non-resumable stale pipeline executions")
                except Exception as e:
                    logger.warning(f"Pipeline recovery after restart failed: {e}")

            # Start WebSocket server
            websocket_task = None
            if self.websocket_server:
                websocket_task = asyncio.create_task(self.websocket_server.start())

            # Auto-start UI dev server if configured
            if self.config.ui.enabled and self.config.ui.mode == "dev":
                from gobby.cli.utils import find_web_dir, spawn_ui_server

                web_dir = find_web_dir(self.config)
                if web_dir:
                    ui_log = Path(self.config.logging.client).expanduser().parent / "ui.log"
                    # Inherit bind_host so the Vite dev server is reachable
                    # over the network (e.g. Tailscale) when bind_host != localhost
                    ui_host = self.config.ui.host
                    if self.config.bind_host != "localhost" and ui_host == "localhost":
                        ui_host = self.config.bind_host
                    ui_pid = spawn_ui_server(
                        ui_host,
                        self.config.ui.port,
                        web_dir,
                        ui_log,
                        daemon_port=self.config.daemon_port,
                        ws_port=self.config.websocket.port if self.config.websocket else 60888,
                    )
                    if ui_pid:
                        logger.info(
                            f"UI dev server started (PID: {ui_pid}) "
                            f"at http://{ui_host}:{self.config.ui.port}"
                        )
                    else:
                        logger.warning("Failed to start UI dev server")
                else:
                    logger.warning("UI dev mode enabled but web/ directory not found")

            # Start HTTP server
            graceful_shutdown_timeout = 15
            config = uvicorn.Config(
                self.http_server.app,
                host=self.config.bind_host,
                port=self.http_server.port,
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=graceful_shutdown_timeout,
            )
            server = uvicorn.Server(config)
            server_task = asyncio.create_task(server.serve())

            # Wait for shutdown
            while not self._shutdown_requested:
                await asyncio.sleep(0.5)

            # Cleanup with timeouts to prevent hanging
            # Use timeout slightly longer than uvicorn's graceful shutdown to let it finish
            server.should_exit = True
            try:
                await asyncio.wait_for(server_task, timeout=graceful_shutdown_timeout + 5)
            except TimeoutError:
                logger.warning("HTTP server shutdown timed out")

            try:
                await asyncio.wait_for(self.lifecycle_manager.stop(), timeout=2.0)
            except TimeoutError:
                logger.warning("Lifecycle manager shutdown timed out")

            if self.agent_lifecycle_monitor:
                try:
                    await asyncio.wait_for(self.agent_lifecycle_monitor.stop(), timeout=2.0)
                except TimeoutError:
                    logger.warning("Agent lifecycle monitor shutdown timed out")

            if self.cron_scheduler:
                try:
                    await asyncio.wait_for(self.cron_scheduler.stop(), timeout=2.0)
                except TimeoutError:
                    logger.warning("Cron scheduler shutdown timed out")

            if self.message_processor:
                try:
                    await asyncio.wait_for(self.message_processor.stop(), timeout=2.0)
                except TimeoutError:
                    logger.warning("Message processor shutdown timed out")

            if websocket_task:
                websocket_task.cancel()
                try:
                    await asyncio.wait_for(websocket_task, timeout=3.0)
                except (asyncio.CancelledError, TimeoutError):
                    logger.warning("WebSocket server shutdown timed out or cancelled")

            # Cancel background pipeline tasks
            try:
                from gobby.mcp_proxy.tools.pipelines._execution import cleanup_background_tasks

                await asyncio.wait_for(cleanup_background_tasks(), timeout=5.0)
            except TimeoutError:
                logger.warning("Pipeline background tasks cleanup timed out")
            except Exception as e:
                logger.warning(f"Pipeline background tasks cleanup failed: {e}")

            # Cancel metrics cleanup task
            if self._metrics_cleanup_task and not self._metrics_cleanup_task.done():
                self._metrics_cleanup_task.cancel()
                try:
                    await asyncio.wait_for(self._metrics_cleanup_task, timeout=2.0)
                except (asyncio.CancelledError, TimeoutError):
                    pass

            # Cancel zombie message cleanup task
            if self._zombie_messages_task and not self._zombie_messages_task.done():
                self._zombie_messages_task.cancel()
                try:
                    await asyncio.wait_for(self._zombie_messages_task, timeout=2.0)
                except (asyncio.CancelledError, TimeoutError):
                    pass

            # Cancel vector store rebuild task
            if self._vector_rebuild_task and not self._vector_rebuild_task.done():
                self._vector_rebuild_task.cancel()
                try:
                    await asyncio.wait_for(self._vector_rebuild_task, timeout=2.0)
                except (asyncio.CancelledError, TimeoutError):
                    pass

            # Stop UI dev server if we started it
            if self.config.ui.enabled and self.config.ui.mode == "dev":
                from gobby.cli.utils import stop_ui_server

                stop_ui_server(quiet=True)

            # Close VectorStore connection
            if self.vector_store:
                try:
                    await asyncio.wait_for(self.vector_store.close(), timeout=5.0)
                except TimeoutError:
                    logger.warning("VectorStore close timed out")
                except Exception as e:
                    logger.warning(f"VectorStore close failed: {e}")

            # Export memories to JSONL backup on shutdown
            if self.memory_sync_manager:
                try:
                    count = await asyncio.wait_for(
                        self.memory_sync_manager.export_to_files(), timeout=5.0
                    )
                    if count > 0:
                        logger.info(f"Shutdown memory backup: exported {count} memories")
                except TimeoutError:
                    logger.warning("Memory backup on shutdown timed out")
                except Exception as e:
                    logger.warning(f"Memory backup on shutdown failed: {e}")

            try:
                await asyncio.wait_for(self.mcp_proxy.disconnect_all(), timeout=3.0)
            except TimeoutError:
                logger.warning("MCP disconnect timed out")

            # Clean up PID file on graceful shutdown
            cleanup_pid_file()

            logger.info("Shutdown complete")

        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            cleanup_pid_file()
            sys.exit(1)


async def run_gobby(config_path: Path | None = None, verbose: bool = False) -> None:
    runner = GobbyRunner(config_path=config_path, verbose=verbose)
    await runner.run()


def main(config_path: Path | None = None, verbose: bool = False) -> None:
    try:
        asyncio.run(run_gobby(config_path=config_path, verbose=verbose))
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Gobby daemon")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=Path, help="Path to config file")

    args = parser.parse_args()
    main(config_path=args.config, verbose=args.verbose)

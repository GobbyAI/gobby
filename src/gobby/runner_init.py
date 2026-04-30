"""GobbyRunner initialization phases.

Extracted from runner.py to keep the main module under the monolith limit.
Each phase function takes the GobbyRunner instance and sets attributes on it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.runner import AgentRunner
from gobby.app_context import ServiceContainer, set_app_context
from gobby.config.app import load_config
from gobby.llm import create_llm_service
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.search.embeddings import generate_embedding
from gobby.servers.http import HTTPServer
from gobby.servers.provider_models import ProviderModelCatalog
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import LocalDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.migrations import run_migrations
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemorySyncManager
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.validation import TaskValidator
from gobby.telemetry.logging import init_telemetry
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (were private methods on GobbyRunner)
# ---------------------------------------------------------------------------


def resolve_embedding_api_key(secret_store: Any, model: str) -> str | None:
    """Resolve the API key for an embedding model from the secret store.

    Maps model prefixes to well-known secret names so users don't need
    to manually wire $secret: references for standard embedding providers.
    """
    # Model-prefix to secret-name mapping
    prefix_to_secret: dict[str, str] = {
        "openai/": "openai_api_key",
        "gemini/": "gemini_api_key",
        "mistral/": "mistral_api_key",
        "azure/": "azure_api_key",
        "cohere/": "cohere_api_key",
    }

    for prefix, secret_name in prefix_to_secret.items():
        if model.startswith(prefix):
            result: str | None = secret_store.get(secret_name)
            return result

    # Local in-process models don't need an API key
    if model.startswith("local/"):
        return None

    # Ollama models don't need an API key
    if model.startswith("ollama/"):
        return None

    # Default (no prefix, e.g. "text-embedding-3-small") → OpenAI
    default_key: str | None = secret_store.get("openai_api_key")
    return default_key


_HEADLESS_SETTINGS = Path.home() / ".gobby" / "settings" / "headless.json"

_HEADLESS_HOOKS: dict[str, dict[str, list[str]]] = {
    "hooks": {
        "SessionStart": [],
        "SessionEnd": [],
        "UserPromptSubmit": [],
        "PreToolUse": [],
        "PostToolUse": [],
        "PreCompact": [],
        "Stop": [],
        "SubagentStart": [],
        "SubagentStop": [],
        "PermissionRequest": [],
    }
}


def _ensure_headless_settings() -> None:
    """Create headless settings file if it doesn't exist.

    This file is used by internal SDK calls (title synthesis, digest, summary)
    to suppress hooks and prevent session registration cascades.
    """
    if _HEADLESS_SETTINGS.exists():
        return
    try:
        _HEADLESS_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        import json as _json

        _HEADLESS_SETTINGS.write_text(_json.dumps(_HEADLESS_HOOKS, indent=2) + "\n")
        logger.info(f"Created headless settings: {_HEADLESS_SETTINGS}")
    except OSError as e:
        logger.error(f"Failed to create headless settings at {_HEADLESS_SETTINGS}: {e}")


def init_hub_database(config: Any) -> Any:
    """Initialize hub database."""
    hub_db_path = Path(config.database_path).expanduser()

    # Ensure hub db directory exists
    hub_db_path.parent.mkdir(parents=True, exist_ok=True)

    hub_db = LocalDatabase(hub_db_path)
    run_migrations(hub_db)

    logger.info(f"Database: {hub_db_path}")
    return hub_db


# ---------------------------------------------------------------------------
# Phase 1: Config, telemetry, database, secrets, core managers
# ---------------------------------------------------------------------------


def init_storage_and_config(runner: GobbyRunner, config_path: Path | None, verbose: bool) -> None:
    """Initialize config, telemetry, database, secrets, and core managers."""
    if config_path is not None and not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Use 'gobby install' to create bootstrap.yaml, "
            f"or omit --config to use the default path (~/.gobby/bootstrap.yaml)."
        )
    runner._config_file = str(config_path) if config_path else None
    runner.config = load_config(runner._config_file)
    runner.verbose = verbose

    # Initialize telemetry (logging, tracing, metrics)
    init_telemetry(runner.config.telemetry, verbose=verbose)

    runner.machine_id = get_machine_id()

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
    # Ensure headless settings file exists for internal SDK calls
    _ensure_headless_settings()

    runner._shutdown_requested = False
    runner._metrics_cleanup_task = None
    runner._vector_rebuild_task = None
    runner._zombie_messages_task = None
    runner._span_cleanup_task = None
    runner._metrics_archive_task = None
    runner._metric_snapshot_task = None
    runner._hook_inbox_task = None
    runner._expired_isolation_task = None

    # Initialize local storage with dual-write if in project context
    runner.database = init_hub_database(runner.config)

    # Phase 2: Reload config from DB (secrets > env vars)
    # Phase 1 (above) used env vars only to bootstrap the database path.
    # Now that the DB is available, load config from config_store with secrets.
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.secrets import SecretStore

    runner.secret_store = SecretStore(runner.database)
    runner.config_store = ConfigStore(runner.database)
    runner.config = load_config(
        config_file=runner._config_file,
        secret_resolver=runner.secret_store.get,
        config_store=runner.config_store,
    )

    # Auto-populate embeddings.api_key from secrets if not already set
    if hasattr(runner.config, "embeddings") and not runner.config.embeddings.api_key:
        resolved_key = resolve_embedding_api_key(
            runner.secret_store, runner.config.embeddings.model
        )
        if resolved_key:
            runner.config.embeddings.api_key = resolved_key

    # Populate model metadata (context_length, max_completion_tokens) from OpenRouter
    from gobby.storage.model_costs import ModelCostStore

    try:
        cost_store = ModelCostStore(runner.database)
        cost_store.populate()
    except Exception as e:
        logger.warning(f"Failed to populate model metadata: {e}")

    runner.session_manager = SessionManager(runner.database)
    runner.task_manager = LocalTaskManager(runner.database)
    runner.session_task_manager = SessionTaskManager(runner.database)

    # Initialize Span Storage and wire OTel exporter
    from gobby.storage.spans import SpanStorage

    runner.span_storage = SpanStorage(runner.database)

    if runner.config.telemetry and runner.config.telemetry.traces_enabled:
        from gobby.telemetry.providers import add_span_storage_exporter

        def _broadcast_proxy(span: dict[str, Any]) -> None:
            """Proxy for trace event broadcasting via WebSocket."""
            if hasattr(runner, "websocket_server") and runner.websocket_server:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(runner.websocket_server.broadcast_trace_event(span))
                except RuntimeError as e:
                    logger.debug(f"Trace broadcast skipped (no running loop): {e}")

        add_span_storage_exporter(runner.span_storage, broadcast_callback=_broadcast_proxy)
        logger.debug("Local span storage exporter wired to OTel")

    from gobby.utils.dev import is_dev_mode

    runner._dev_mode = is_dev_mode(Path.cwd())

    # In dev mode, auto-sync bundled content so YAML edits are picked up
    # on every daemon restart without needing a full `gobby install`.
    if runner._dev_mode:
        from gobby.cli.installers.shared import sync_bundled_content_to_db

        sync_result = sync_bundled_content_to_db(runner.database)
        total = sync_result["total_synced"]
        if total > 0:
            logger.info(f"Dev mode: synced {total} bundled items on startup")

    # Initialize Prompt Manager
    from gobby.storage.prompts import LocalPromptManager

    runner.prompt_manager = LocalPromptManager(runner.database, dev_mode=runner._dev_mode)

    # Initialize Skill Manager and Hub Manager
    from gobby.storage.skills import LocalSkillManager

    runner.skill_manager = LocalSkillManager(runner.database)

    runner.hub_manager = None
    try:
        from gobby.config.skills import SkillsConfig
        from gobby.skills.hubs import (
            ClaudePluginsProvider,
            ClawdHubProvider,
            GitHubCollectionProvider,
            HubManager,
            SkillsMPProvider,
        )

        skills_config = runner.config.skills if hasattr(runner.config, "skills") else SkillsConfig()

        # Resolve hub API keys from env vars
        api_keys: dict[str, str] = {}
        for _hub_name, hub_config in skills_config.hubs.items():
            if hub_config.auth_key_name:
                value = os.environ.get(hub_config.auth_key_name)
                if value:
                    api_keys[hub_config.auth_key_name] = value

        runner.hub_manager = HubManager(configs=skills_config.hubs, api_keys=api_keys)
        runner.hub_manager.register_provider_factory("clawdhub", ClawdHubProvider)
        runner.hub_manager.register_provider_factory("skillsmp", SkillsMPProvider)
        runner.hub_manager.register_provider_factory("github-collection", GitHubCollectionProvider)
        runner.hub_manager.register_provider_factory("claude-plugins", ClaudePluginsProvider)
        runner.hub_manager._skill_description_config = (
            runner.config.skill_description if hasattr(runner.config, "skill_description") else None
        )
        logger.debug(f"HubManager initialized with {len(skills_config.hubs)} hubs")
    except Exception as e:
        logger.warning(f"Failed to initialize HubManager: {e}")


# ---------------------------------------------------------------------------
# Phase 2: LLM, memory, code indexer, MCP proxy, sync, messaging
# ---------------------------------------------------------------------------


def init_services(runner: GobbyRunner) -> None:
    """Initialize LLM, memory, code indexer, MCP proxy, sync, and messaging."""
    # Initialize LLM Service
    runner.llm_service = None
    try:
        runner.llm_service = create_llm_service(runner.config)
        logger.debug(f"LLM service initialized: {runner.llm_service.enabled_providers}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM service: {e}")

    # Initialize VectorStore and Memory Manager
    runner.vector_store = None
    runner.memory_manager = None
    if hasattr(runner.config, "memory"):
        try:
            # Create VectorStore (async initialize() called during startup)
            db_cfg = runner.config.databases
            emb_cfg = runner.config.embeddings
            runner.vector_store = VectorStore(
                url=db_cfg.qdrant.url,
                api_key=db_cfg.qdrant.api_key,
                embedding_dim=emb_cfg.dim,
            )
            embed_fn: Callable[..., Any] | None = None
            if runner.llm_service:
                from functools import partial

                _mem_api_key = emb_cfg.api_key or resolve_embedding_api_key(
                    runner.secret_store, emb_cfg.model
                )
                _mem_embed_kwargs: dict[str, Any] = {
                    "model": emb_cfg.model,
                    "api_key": _mem_api_key,
                }
                if emb_cfg.api_base:
                    _mem_embed_kwargs["api_base"] = emb_cfg.api_base
                embed_fn = partial(
                    generate_embedding,
                    **_mem_embed_kwargs,
                    expected_dim=emb_cfg.dim,
                )

            runner.memory_manager = MemoryManager(
                runner.database,
                runner.config.memory,
                llm_service=runner.llm_service,
                vector_store=runner.vector_store,
                embed_fn=embed_fn,
                neo4j_url=db_cfg.neo4j.url,
                neo4j_auth=db_cfg.neo4j.auth,
                neo4j_database=db_cfg.neo4j.database,
                neo4j_graph_search=db_cfg.neo4j.graph_search,
                neo4j_graph_min_score=db_cfg.neo4j.graph_min_score,
                neo4j_rrf_k=db_cfg.neo4j.rrf_k,
                embedding_dim=emb_cfg.dim,
                collection_prefix=db_cfg.qdrant.collection_prefix,
            )
        except Exception as e:
            logger.error(f"Failed to initialize MemoryManager: {e}")

    # Code Index (daemon-side context — gcode handles actual indexing)
    runner.code_indexer = None
    if hasattr(runner.config, "code_index") and runner.config.code_index.enabled:
        try:
            from gobby.code_index.context import CodeIndexContext
            from gobby.code_index.graph import CodeGraph
            from gobby.code_index.storage import CodeIndexStorage

            ci_config = runner.config.code_index
            ci_storage = CodeIndexStorage(runner.database)
            # Share Neo4j client from memory_manager if available
            ci_neo4j = None
            if runner.memory_manager and getattr(runner.memory_manager, "_neo4j_client", None):
                ci_neo4j = runner.memory_manager._neo4j_client
            ci_graph = CodeGraph(neo4j_client=ci_neo4j)

            ci_vector_store = runner.vector_store if ci_config.embedding_enabled else None

            runner.code_indexer = CodeIndexContext(
                storage=ci_storage,
                vector_store=ci_vector_store,
                graph=ci_graph if ci_config.graph_enabled else None,
                config=ci_config,
            )

            logger.info("Code indexer initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize code indexer: {e}")

    # MCP Proxy Manager - Initialize early for tool access
    # LocalMCPManager handles server/tool storage in SQLite
    runner.mcp_db_manager = LocalMCPManager(runner.database)
    try:
        bundled_mcp_stats = runner.mcp_db_manager.normalize_bundled_servers()
    except Exception:
        logger.exception("error normalizing bundled MCP servers")
        bundled_mcp_stats = {"normalized": 0, "duplicates_removed": 0, "tools_migrated": 0}
    if bundled_mcp_stats["normalized"] or bundled_mcp_stats["duplicates_removed"]:
        logger.info(
            "Normalized bundled MCP servers: %s normalized, %s duplicates removed",
            bundled_mcp_stats["normalized"],
            bundled_mcp_stats["duplicates_removed"],
        )

    # Tool Metrics Manager for tracking call statistics
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.metrics_events import MetricsEventStore

    runner.metrics_event_store = MetricsEventStore(runner.database)
    runner.metrics_manager = ToolMetricsManager(
        runner.database, event_store=runner.metrics_event_store
    )

    # MCPClientManager loads servers from database on init
    runner.mcp_proxy = MCPClientManager(
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
    )

    # Task Sync Manager
    runner.task_sync_manager = TaskSyncManager(runner.task_manager)

    # NOTE: Startup export removed to avoid git noise (#10198).
    # JSONL files are backup/export artifacts. Reads are explicit only via CLI/MCP.
    # The pre-push hook exports and stages JSONL files before push.

    # Initialize Memory Sync Manager (Phase 7) & Wire up listeners
    runner.memory_sync_manager = None
    if hasattr(runner.config, "memory_sync") and runner.config.memory_sync.enabled:
        if runner.memory_manager:
            try:
                runner.memory_sync_manager = MemorySyncManager(
                    db=runner.database,
                    memory_manager=runner.memory_manager,
                    config=runner.config.memory_sync,
                )
                # No per-change listener — the file is a backup, not a live mirror.
                # Export happens via pre-push hook, CLI `memory backup`, or explicit sync_export.
                # Import happens only through explicit restore/import commands.
                logger.debug("MemorySyncManager initialized (backup/export only)")

            except Exception as e:
                logger.error(f"Failed to initialize MemorySyncManager: {e}")

    # Session Message Processor (Phase 6)
    # Created here and passed to HTTPServer which injects it into HookManager.
    # session_manager is required for the Codex rollout-tail synthesis path
    # to resolve the (external_id, machine_id, project_id) composite key that
    # HookManager.session_lookup uses to attribute rule effects.
    runner.message_processor = None
    if getattr(runner.config, "message_tracking", None) and runner.config.message_tracking.enabled:
        runner.message_processor = SessionMessageProcessor(
            db=runner.database,
            poll_interval=runner.config.message_tracking.poll_interval,
            session_manager=runner.session_manager,
        )

    # Initialize Task Validator (Phase 7.1)
    runner.task_validator = None

    if runner.llm_service:
        gobby_tasks_config = runner.config.gobby_tasks
        if gobby_tasks_config.validation.enabled:
            try:
                runner.task_validator = TaskValidator(
                    llm_service=runner.llm_service,
                    config=gobby_tasks_config.validation,
                    db=runner.database,
                )
            except Exception as e:
                logger.error(f"Failed to initialize TaskValidator: {e}")

    # Initialize Worktree Storage (Phase 7 - Subagents)
    runner.worktree_storage = LocalWorktreeManager(runner.database)

    # Initialize Clone Storage (local git clones for isolated development)
    runner.clone_storage = LocalCloneManager(runner.database)

    # Detect project context from cwd so daemon-level services (pipelines,
    # clones) can register their MCP tools.  Per-session resolution can
    # still override via ServiceContainer.
    runner.git_manager = None
    runner.project_id = None
    try:
        from gobby.utils.project_context import get_project_context
        from gobby.worktrees.git import WorktreeGitManager as _WGM

        project_ctx = get_project_context(Path.cwd())
        if project_ctx and project_ctx.get("id"):
            runner.project_id = str(project_ctx["id"])
            project_path = project_ctx.get("project_path")
            if project_path:
                runner.git_manager = _WGM(str(project_path))
                logger.debug(f"Daemon project context: id={runner.project_id}, path={project_path}")
    except Exception as e:
        logger.debug(f"Could not detect project context from cwd: {e}")


# ---------------------------------------------------------------------------
# Phase 3: Workflows, pipelines, agents, cron, communications
# ---------------------------------------------------------------------------


def init_orchestration(runner: GobbyRunner) -> None:
    """Initialize workflows, pipelines, agents, cron, and communications."""
    # WorkflowLoader is project-agnostic; pipeline executor needs project context.
    runner.workflow_loader = None
    runner.pipeline_execution_manager = None
    runner.pipeline_executor = None
    try:
        from gobby.workflows.loader import WorkflowLoader

        runner.workflow_loader = WorkflowLoader(db=runner.database)
    except Exception as e:
        logger.warning(f"Failed to initialize workflow loader: {e}")

    # Initialize completion event registry with wake dispatcher
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    ism_manager = InterSessionMessageManager(runner.database)
    agent_run_manager = LocalAgentRunManager(runner.database)

    # tmux sender: wraps the global TmuxSessionManager singleton
    async def _tmux_send(tmux_session_name: str, message: str) -> None:
        from gobby.agents.tmux import get_tmux_session_manager

        mgr = get_tmux_session_manager()
        await mgr.send_keys(tmux_session_name, message)

    async def _tmux_pane_send(
        pane_id: str,
        message: str,
        tmux_socket_path: str | None,
    ) -> None:
        text = message.rstrip("\n")
        tmux_cmd = ["tmux"]
        if tmux_socket_path:
            tmux_cmd.extend(["-S", tmux_socket_path])
        proc = await asyncio.create_subprocess_exec(
            *tmux_cmd,
            "send-keys",
            "-t",
            pane_id,
            "-l",
            text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"tmux send-keys to {pane_id} timed out after 10s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux send-keys to {pane_id} failed: {stderr.decode(errors='replace')}"
            )

        proc = await asyncio.create_subprocess_exec(
            *tmux_cmd,
            "send-keys",
            "-t",
            pane_id,
            "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"tmux send-keys to {pane_id} timed out after 10s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux send-keys to {pane_id} failed: {stderr.decode(errors='replace')}"
            )

    runner.wake_dispatcher = WakeDispatcher(
        session_manager=runner.session_manager,
        ism_manager=ism_manager,
        tmux_sender=_tmux_send,
        tmux_pane_sender=_tmux_pane_send,
        agent_run_manager=agent_run_manager,
    )

    runner.completion_registry = CompletionEventRegistry(
        wake_callback=runner.wake_dispatcher.wake,
    )

    # Create pipeline executor at startup if we have project context
    if runner.workflow_loader is not None and runner.project_id:
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager as _LPEM
            from gobby.workflows.pipeline_executor import PipelineExecutor as _PE
            from gobby.workflows.templates import TemplateEngine

            runner.pipeline_execution_manager = _LPEM(
                db=runner.database, project_id=runner.project_id
            )
            runner.pipeline_executor = _PE(
                db=runner.database,
                execution_manager=runner.pipeline_execution_manager,
                llm_service=runner.llm_service,
                loader=runner.workflow_loader,
                template_engine=TemplateEngine(),
                session_manager=runner.session_manager,
                completion_registry=runner.completion_registry,
            )
            logger.info("Pipeline executor initialized at startup")
        except Exception as e:
            logger.warning(f"Failed to initialize pipeline executor at startup: {e}")

    # Initialize Agent Runner (Phase 7 - Subagents)
    runner.agent_runner = None
    try:
        runner.agent_runner = AgentRunner(
            db=runner.database,
            session_storage=runner.session_manager,
            max_agent_depth=5,
        )
        logger.debug("AgentRunner initialized")
    except Exception as e:
        logger.error(f"Failed to initialize AgentRunner: {e}")

    # Agent Lifecycle Monitor (detect dead agent processes — fully DB-driven)
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.checkpoints import LocalCheckpointManager

    try:
        runner.agent_lifecycle_monitor = AgentLifecycleMonitor(
            agent_run_manager=LocalAgentRunManager(runner.database),
            db=runner.database,
            session_manager=runner.session_manager,
            clone_storage=runner.clone_storage,
            completion_registry=runner.completion_registry,
            task_manager=runner.task_manager,
            tmux_config=runner.config.tmux if hasattr(runner.config, "tmux") else None,
            checkpoint_storage=LocalCheckpointManager(runner.database),
            worktree_storage=runner.worktree_storage,
        )
    except Exception as e:
        logger.warning(f"Failed to initialize AgentLifecycleMonitor: {e}")
        runner.agent_lifecycle_monitor = None

    # Session Lifecycle Manager (background jobs for expiring and processing)
    runner.lifecycle_manager = SessionLifecycleManager(
        db=runner.database,
        config=runner.config.session_lifecycle,
        memory_manager=runner.memory_manager,
        llm_service=runner.llm_service,
        memory_sync_manager=runner.memory_sync_manager,
    )

    # Cron Scheduler (background jobs for recurring tasks)
    runner.cron_storage = None
    runner.cron_scheduler = None
    try:
        from gobby.scheduler.executor import CronExecutor
        from gobby.scheduler.scheduler import CronScheduler
        from gobby.storage.cron import CronJobStorage

        runner.cron_storage = CronJobStorage(runner.database)
        cron_executor = CronExecutor(
            storage=runner.cron_storage,
            agent_runner=runner.agent_runner,
            pipeline_executor=runner.pipeline_executor,
        )
        cron_executor.register_handler(
            "dispatch.tick",
            cron_executor._execute_dispatcher,
        )
        if runner.project_id:
            from gobby.runner import install_dispatcher_cron_row

            install_dispatcher_cron_row(runner.database, project_id=runner.project_id)
            logger.info("Installed system cron job: gobby:dispatcher")

        # Register pipeline heartbeat handler
        try:
            from gobby.storage.agents import LocalAgentRunManager
            from gobby.workflows.pipeline_heartbeat import PipelineHeartbeat

            if runner.pipeline_execution_manager is None:
                raise RuntimeError("pipeline_execution_manager required for heartbeat")

            heartbeat = PipelineHeartbeat(
                execution_manager=runner.pipeline_execution_manager,
                task_manager=runner.task_manager,
                agent_run_manager=LocalAgentRunManager(runner.database),
                session_manager=runner.session_manager,
            )
            cron_executor.register_handler("pipeline_heartbeat", heartbeat)

            # Auto-create system cron job if missing
            existing = runner.cron_storage.get_job_by_name("gobby:pipeline-heartbeat")
            if not existing and runner.project_id:
                runner.cron_storage.create_job(
                    project_id=runner.project_id,
                    name="gobby:pipeline-heartbeat",
                    description="Safety net: detects stalled pipelines and marks dead executions as failed",
                    schedule_type="interval",
                    interval_seconds=60,
                    action_type="handler",
                    action_config={"handler": "pipeline_heartbeat"},
                    enabled=True,
                )
                logger.info("Created system cron job: gobby:pipeline-heartbeat")
            logger.debug("PipelineHeartbeat handler registered")
        except Exception as e:
            logger.error(f"Failed to register pipeline heartbeat: {e}")

        # Retired with the old conductor package; disable pre-existing system jobs.
        try:
            conductor_job = runner.cron_storage.get_job_by_name("gobby:conductor-tick")
            if conductor_job and conductor_job.enabled:
                runner.cron_storage.update_job(conductor_job.id, enabled=False, next_run_at=None)
                logger.info("Disabled retired system cron job: gobby:conductor-tick")
        except Exception as e:
            logger.warning(f"Failed to disable retired conductor cron job: {e}")

        # Register Linear sync handler (for projects with Linear integration)
        try:
            from gobby.storage.projects import LocalProjectManager
            from gobby.sync.linear import create_linear_sync_handler

            pm = LocalProjectManager(runner.database)
            for project in pm.list():
                if project.linear_team_id:
                    handler = create_linear_sync_handler(
                        mcp_manager=runner.mcp_proxy,
                        task_manager=runner.task_manager,
                        project_id=project.id,
                        team_id=project.linear_team_id,
                    )
                    handler_name = f"linear_sync:{project.id}"
                    cron_executor.register_handler(handler_name, handler)

                    job_name = f"gobby:linear-sync:{project.id}"
                    existing = runner.cron_storage.get_job_by_name(job_name)
                    if not existing:
                        runner.cron_storage.create_job(
                            project_id=project.id,
                            name=job_name,
                            description=f"Bidirectional Linear sync for project {project.name}",
                            schedule_type="interval",
                            interval_seconds=300,
                            action_type="handler",
                            action_config={"handler": handler_name},
                            enabled=True,
                        )
                        logger.info(f"Created system cron job: {job_name}")
            logger.debug("Linear sync handlers registered")
        except Exception as e:
            logger.error(f"Failed to register Linear sync handlers: {e}")

        runner.cron_scheduler = CronScheduler(
            storage=runner.cron_storage,
            executor=cron_executor,
            config=runner.config.cron,
        )
        logger.debug("CronScheduler initialized")
    except Exception as e:
        logger.error(f"Failed to initialize CronScheduler: {e}")

    # Communications Manager
    runner.communications_manager = None
    if hasattr(runner.config, "communications") and runner.config.communications.enabled:
        try:
            from gobby.communications.manager import CommunicationsManager
            from gobby.storage.communications import LocalCommunicationsStore

            comms_store = LocalCommunicationsStore(runner.database)
            runner.communications_manager = CommunicationsManager(
                config=runner.config.communications,
                store=comms_store,
                secret_store=runner.secret_store,
                session_store=runner.session_manager,
            )
            logger.debug("CommunicationsManager initialized")
        except Exception as e:
            logger.error(f"Failed to initialize CommunicationsManager: {e}")


# ---------------------------------------------------------------------------
# Phase 4: HTTP server, WebSocket server, broadcasting
# ---------------------------------------------------------------------------


def init_servers(runner: GobbyRunner) -> None:
    """Initialize HTTP server, WebSocket server, and broadcasting."""
    # HTTP Server
    # Bundle services into container
    services = ServiceContainer(
        config=runner.config,
        database=runner.database,
        session_manager=runner.session_manager,
        task_manager=runner.task_manager,
        span_storage=runner.span_storage,
        task_sync_manager=runner.task_sync_manager,
        memory_sync_manager=runner.memory_sync_manager,
        memory_manager=runner.memory_manager,
        llm_service=runner.llm_service,
        vector_store=runner.vector_store,
        mcp_manager=runner.mcp_proxy,
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
        agent_runner=runner.agent_runner,
        message_processor=runner.message_processor,
        task_validator=runner.task_validator,
        worktree_storage=runner.worktree_storage,
        clone_storage=runner.clone_storage,
        git_manager=runner.git_manager,
        project_id=runner.project_id,
        pipeline_executor=runner.pipeline_executor,
        workflow_loader=runner.workflow_loader,
        pipeline_execution_manager=runner.pipeline_execution_manager,
        completion_registry=runner.completion_registry,
        agent_lifecycle_monitor=runner.agent_lifecycle_monitor,
        communications_manager=runner.communications_manager,
        code_indexer=runner.code_indexer,
        cron_storage=runner.cron_storage,
        cron_scheduler=runner.cron_scheduler,
        skill_manager=runner.skill_manager,
        hub_manager=runner.hub_manager,
        config_store=runner.config_store,
        provider_model_catalog=ProviderModelCatalog(runner.config),
        web_chat_runtime_manager=None,
        prompt_manager=runner.prompt_manager,
        dev_mode=runner._dev_mode,
        tool_proxy_getter=lambda: runner.http_server.tool_proxy,
    )

    set_app_context(services)

    if runner.communications_manager:
        from gobby.communications.reactions import ReactionHandler

        runner.communications_manager.reaction_handler = ReactionHandler(
            runner.communications_manager._store, services
        )

    # Create shared web-chat backends. Startup is handled in HTTP lifespan so
    # backend failures are non-fatal to daemon boot.
    codex_client = None
    from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter

    if CodexAdapter.is_codex_available():
        from gobby.adapters.codex_impl.client import CodexAppServerClient

        codex_client = CodexAppServerClient()
        logger.info("Codex app-server client created (will start in HTTP lifespan)")

    gemini_default_model: str | None = None
    gemini_config = getattr(runner.config.llm_providers, "gemini", None)
    if gemini_config is not None:
        gemini_default_model = gemini_config.default_model
        if not gemini_default_model:
            models = gemini_config.get_models_list()
            gemini_default_model = models[0] if models else None

    services.web_chat_runtime_manager = WebChatRuntimeManager(
        codex_client=codex_client,
        gemini_default_model=gemini_default_model,
        daemon_config=runner.config,
    )

    runner.http_server = HTTPServer(
        services=services,
        port=runner.config.daemon_port,
        test_mode=runner.config.test_mode,
        codex_client=codex_client,
    )

    # Ensure message_processor property is set (redundant but explicit):
    runner.http_server.message_processor = runner.message_processor

    # Wire tool_proxy_getter onto the startup-created pipeline executor.
    # The executor was created before http_server existed, so we do it now.
    if runner.pipeline_executor is not None:
        runner.pipeline_executor.tool_proxy_getter = lambda: runner.http_server.tool_proxy

    # WebSocket Server (Optional)
    runner.websocket_server = None
    if runner.config.websocket and getattr(runner.config.websocket, "enabled", True):
        websocket_config = WebSocketConfig(
            host=runner.config.bind_host,
            port=runner.config.websocket.port,
            ping_interval=runner.config.websocket.ping_interval,
            ping_timeout=runner.config.websocket.ping_timeout,
        )
        runner.websocket_server = WebSocketServer(
            config=websocket_config,
            mcp_manager=runner.mcp_proxy,
            session_manager=runner.session_manager,
            daemon_config=runner.config,
            internal_manager=runner.http_server._internal_manager,
        )
        runner.websocket_server.web_chat_runtime_manager = services.web_chat_runtime_manager
        # Pass WebSocket server reference to HTTP server for broadcasting
        runner.http_server.websocket_server = runner.websocket_server
        # Also set on services container so lifespan can wire workflow_handler
        runner.http_server.services.websocket_server = runner.websocket_server
        # Also update the HTTPServer's broadcaster to use the same websocket_server
        runner.http_server.broadcaster.websocket_server = runner.websocket_server

        # Pass WebSocket server and session manager to message processor
        if runner.message_processor:
            runner.message_processor.websocket_server = runner.websocket_server
            runner.message_processor.session_manager = runner.session_manager

        # Register agent event callback for WebSocket broadcasting
        from gobby.runner_broadcasting import (
            setup_agent_event_broadcasting,
            setup_communications_event_broadcasting,
            setup_cron_event_broadcasting,
            setup_pipeline_event_broadcasting,
        )

        setup_agent_event_broadcasting(runner.websocket_server)

        # Register pipeline event callback for WebSocket broadcasting
        if runner.pipeline_executor:
            setup_pipeline_event_broadcasting(runner.websocket_server, runner.pipeline_executor)

        # Register cron event callback for WebSocket broadcasting
        if runner.cron_scheduler:
            setup_cron_event_broadcasting(runner.websocket_server, runner.cron_scheduler)

        # Register communications event callback for WebSocket broadcasting
        if runner.communications_manager:
            setup_communications_event_broadcasting(
                runner.websocket_server,
                runner.communications_manager,
            )

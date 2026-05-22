"""Service setup for GobbyRunner."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.config.persistence import is_falkordb_enabled
from gobby.llm import create_llm_service
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.memory.falkor_client import FalkorClient
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.runner_init.helpers import resolve_embedding_api_key
from gobby.search.embeddings import generate_embedding
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.clones import LocalCloneManager
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemorySyncManager
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.validation import TaskValidator

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


def init_services(runner: GobbyRunner) -> None:
    """Initialize LLM, memory, code indexer, MCP proxy, sync, and messaging."""
    _init_llm_service(runner)
    _init_memory_stack(runner)
    _init_code_indexer(runner)
    _init_mcp_stack(runner)
    _init_sync_managers(runner)
    _init_message_processor(runner)
    _init_task_validator(runner)
    _init_project_context(runner)


def _init_llm_service(runner: GobbyRunner) -> None:
    runner.llm_service = None
    try:
        runner.llm_service = create_llm_service(runner.config)
        logger.debug(f"LLM service initialized: {runner.llm_service.enabled_providers}")
    except Exception as e:
        logger.error(f"Failed to initialize LLM service: {e}")


def _init_memory_stack(runner: GobbyRunner) -> None:
    runner.vector_store = None
    runner.memory_manager = None
    if hasattr(runner.config, "memory"):
        try:
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

            falkor_cfg = db_cfg.falkordb if is_falkordb_enabled(db_cfg) else None
            runner.memory_manager = MemoryManager(
                runner.database,
                runner.config.memory,
                llm_service=runner.llm_service,
                vector_store=runner.vector_store,
                embed_fn=embed_fn,
                falkordb_host=falkor_cfg.host if falkor_cfg else None,
                falkordb_port=falkor_cfg.port if falkor_cfg else 16379,
                falkordb_password=falkor_cfg.requirepass if falkor_cfg else None,
                falkordb_graph_name=falkor_cfg.graph_name if falkor_cfg else "gobby_kg",
                falkordb_graph_search=falkor_cfg.graph_search if falkor_cfg else True,
                falkordb_graph_min_score=falkor_cfg.graph_min_score if falkor_cfg else 0.5,
                falkordb_rrf_k=falkor_cfg.rrf_k if falkor_cfg else 60,
                embedding_dim=emb_cfg.dim,
                collection_prefix=db_cfg.qdrant.collection_prefix,
                run_db=runner.db_executor.run,
            )
        except Exception as e:
            logger.error(f"Failed to initialize MemoryManager: {e}")


def _init_code_indexer(runner: GobbyRunner) -> None:
    runner.code_indexer = None
    if hasattr(runner.config, "code_index") and runner.config.code_index.enabled:
        try:
            from gobby.code_index.context import CodeIndexContext
            from gobby.code_index.graph import CodeGraph
            from gobby.code_index.storage import CodeIndexStorage

            ci_config = runner.config.code_index
            db_cfg = runner.config.databases
            ci_storage = CodeIndexStorage(runner.database)
            ci_graph = None

            if ci_config.graph_enabled and is_falkordb_enabled(db_cfg):
                falkor_cfg = db_cfg.falkordb
                ci_falkor = FalkorClient(
                    host=falkor_cfg.host,
                    port=falkor_cfg.port,
                    password=falkor_cfg.requirepass,
                    graph_name="gobby_code",
                )
                ci_graph = CodeGraph(falkor_client=ci_falkor)

            ci_vector_store = runner.vector_store if ci_config.embedding_enabled else None

            runner.code_indexer = CodeIndexContext(
                storage=ci_storage,
                vector_store=ci_vector_store,
                graph=ci_graph,
                config=ci_config,
                run_db=runner.db_executor.run,
            )

            logger.info("Code indexer initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize code indexer: {e}")


def _init_mcp_stack(runner: GobbyRunner) -> None:
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

    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.metrics_events import MetricsEventStore

    runner.metrics_event_store = MetricsEventStore(runner.database)
    runner.metrics_manager = ToolMetricsManager(
        runner.database, event_store=runner.metrics_event_store
    )

    runner.mcp_proxy = MCPClientManager(
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
    )


def _init_sync_managers(runner: GobbyRunner) -> None:
    runner.task_sync_manager = TaskSyncManager(runner.task_manager)

    runner.memory_sync_manager = None
    if hasattr(runner.config, "memory_sync") and runner.config.memory_sync.enabled:
        if runner.memory_manager:
            try:
                runner.memory_sync_manager = MemorySyncManager(
                    db=runner.database,
                    memory_manager=runner.memory_manager,
                    config=runner.config.memory_sync,
                )
                logger.debug("MemorySyncManager initialized (backup/export only)")

            except Exception as e:
                logger.error(f"Failed to initialize MemorySyncManager: {e}")


def _init_message_processor(runner: GobbyRunner) -> None:
    runner.message_processor = None
    if getattr(runner.config, "message_tracking", None) and runner.config.message_tracking.enabled:
        runner.message_processor = SessionMessageProcessor(
            db=runner.database,
            poll_interval=runner.config.message_tracking.poll_interval,
            session_manager=runner.session_manager,
        )


def _init_task_validator(runner: GobbyRunner) -> None:
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


def _init_project_context(runner: GobbyRunner) -> None:
    runner.worktree_storage = LocalWorktreeManager(runner.database)

    runner.clone_storage = LocalCloneManager(runner.database)

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

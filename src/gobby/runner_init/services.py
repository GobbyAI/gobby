"""Service setup for GobbyRunner."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.ai import (
    build_daemon_text_generation_service,
    build_daemon_tool_chat_service,
)
from gobby.ai.embeddings import EmbeddingService
from gobby.config.embedding_keys import EMBEDDING_API_KEY_SECRET_NAME
from gobby.config.logging import RUNTIME_LOG_FILENAME, resolved_log_path
from gobby.config.persistence import EmbeddingsConfig, is_falkordb_enabled
from gobby.llm import create_llm_service
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.clones import LocalCloneManager
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemoryBackupManager
from gobby.tasks.validation import TaskValidator

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


def mark_service_degraded(runner: GobbyRunner, service_name: str) -> None:
    """Record a failed or dependency-skipped runner service."""
    degraded_services = getattr(runner, "degraded_services", None)
    if degraded_services is None:
        degraded_services = set()
        runner.degraded_services = degraded_services
    degraded_services.add(service_name)


def init_services(runner: GobbyRunner) -> None:
    """Initialize LLM, memory, code indexer, MCP proxy, sync, and messaging."""
    _init_llm_service(runner)
    _init_memory_stack(runner)
    _init_code_indexer(runner)
    _init_mcp_stack(runner)
    _init_memory_backup(runner)
    _init_message_processor(runner)
    _init_task_validator(runner)
    _init_project_context(runner)


def _init_llm_service(runner: GobbyRunner) -> None:
    runner.codex_client = None
    runner.text_generation_service = None
    runner.tool_chat_service = None
    runner.llm_service = None
    try:
        runner.text_generation_service = build_daemon_text_generation_service(
            runner.config,
        )
        runner.llm_service = create_llm_service(
            runner.config,
            text_generation=runner.text_generation_service,
        )
        runner.tool_chat_service = build_daemon_tool_chat_service(runner.config)
        logger.debug("LLM service initialized: %s", runner.llm_service.enabled_providers)
    except Exception:
        mark_service_degraded(runner, "llm_service")
        logger.exception("Failed to initialize LLM service")


def _validate_memory_embedding_config(
    emb_cfg: EmbeddingsConfig,
    *,
    api_key: str | None = None,
) -> None:
    service = EmbeddingService(
        model=emb_cfg.model,
        api_base=emb_cfg.api_base,
        api_key=api_key if api_key is not None else emb_cfg.api_key,
        dim=emb_cfg.dim,
        query_prefix=_embedding_query_prefix(emb_cfg),
    )
    if service.is_configured():
        return
    raise ValueError(
        "Embedding configuration is incomplete for memory embeddings: set "
        "embedding API base for local embeddings or "
        "embedding API key for OpenAI embeddings"
    )


def _resolve_embedding_api_key(runner: GobbyRunner, emb_cfg: EmbeddingsConfig) -> str | None:
    if emb_cfg.api_key:
        return emb_cfg.api_key
    for secret_name in (EMBEDDING_API_KEY_SECRET_NAME, "api_key", "openai_api_key"):
        try:
            value = runner.secret_store.get(secret_name)
        except (AttributeError, KeyError, LookupError):
            logger.debug("Failed to resolve embedding secret %s", secret_name, exc_info=True)
            continue
        if isinstance(value, str) and value:
            return value
    return None


def _embedding_query_prefix(emb_cfg: EmbeddingsConfig) -> str | None:
    value = getattr(emb_cfg, "query_prefix", None)
    return value if isinstance(value, str) and value else None


def _init_memory_stack(runner: GobbyRunner) -> None:
    runner.vector_store = None
    runner.memory_manager = None
    if hasattr(runner.config, "memory"):
        try:
            db_cfg = runner.config.databases
            emb_cfg = runner.config.embeddings
            embedding_api_key = _resolve_embedding_api_key(runner, emb_cfg)
            embeddings_enabled = False
            if runner.llm_service:
                try:
                    _validate_memory_embedding_config(emb_cfg, api_key=embedding_api_key)
                except ValueError as e:
                    mark_service_degraded(runner, "memory_embeddings")
                    logger.warning("Memory embeddings disabled: %s", e, exc_info=True)
                else:
                    embeddings_enabled = True
            runner.vector_store = VectorStore(
                url=db_cfg.qdrant.url,
                api_key=db_cfg.qdrant.api_key,
                embedding_dim=emb_cfg.dim,
            )
            embed_fn: Callable[..., Any] | None = None
            if embeddings_enabled:
                embedding_service = EmbeddingService(
                    model=emb_cfg.model,
                    api_base=emb_cfg.api_base,
                    api_key=embedding_api_key,
                    dim=emb_cfg.dim,
                    query_prefix=_embedding_query_prefix(emb_cfg),
                )
                embed_fn = embedding_service.generate_embedding

            falkor_cfg = db_cfg.falkordb if is_falkordb_enabled(db_cfg) else None
            runner.memory_manager = MemoryManager(
                runner.database,
                runner.config.memory,
                llm_service=runner.llm_service,
                vector_store=runner.vector_store,
                embed_fn=embed_fn,
                falkordb_host=falkor_cfg.host if falkor_cfg else None,
                falkordb_port=falkor_cfg.port if falkor_cfg else 16379,
                falkordb_password=falkor_cfg.password if falkor_cfg else None,
                falkordb_graph_name=falkor_cfg.graph_name if falkor_cfg else "gobby_kg",
                falkordb_graph_search=falkor_cfg.graph_search if falkor_cfg else True,
                falkordb_graph_min_score=falkor_cfg.graph_min_score if falkor_cfg else 0.5,
                falkordb_rrf_k=falkor_cfg.rrf_k if falkor_cfg else 60,
                embedding_dim=emb_cfg.dim,
                collection_prefix=db_cfg.qdrant.collection_prefix,
                run_db=runner.db_executor.run,
                max_graph_deterministic_attempts=(
                    runner.config.knowledge_graph_queue.max_deterministic_attempts
                ),
            )
            runner.memory_manager.start_projection_scope_repair()
        except Exception:
            mark_service_degraded(runner, "memory_manager")
            logger.exception("Failed to initialize MemoryManager")


def _init_code_indexer(runner: GobbyRunner) -> None:
    runner.code_indexer = None
    if hasattr(runner.config, "code_index") and runner.config.code_index.enabled:
        try:
            from gobby.code_index.context import CodeIndexContext
            from gobby.code_index.gcode_gateway import GcodeGateway
            from gobby.code_index.storage import CodeIndexStorage

            ci_config = runner.config.code_index
            ci_storage = CodeIndexStorage(runner.database)
            ci_gcode_gateway = GcodeGateway()

            runner.code_indexer = CodeIndexContext(
                storage=ci_storage,
                gcode_gateway=ci_gcode_gateway,
                config=ci_config,
                run_db=runner.db_executor.run,
            )

            logger.info("Code indexer initialized")
        except Exception:
            mark_service_degraded(runner, "code_indexer")
            logger.warning("Failed to initialize code indexer", exc_info=True)


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
        stdio_errlog_path=str(resolved_log_path(runner.config.logging, RUNTIME_LOG_FILENAME)),
    )


def _init_memory_backup(runner: GobbyRunner) -> None:
    runner.memory_backup_manager = None
    if hasattr(runner.config, "memory_backup") and runner.config.memory_backup.enabled:
        if runner.memory_manager:
            try:
                runner.memory_backup_manager = MemoryBackupManager(
                    db=runner.database,
                    memory_manager=runner.memory_manager,
                    config=runner.config.memory_backup,
                )
                logger.debug("MemoryBackupManager initialized")

            except Exception:
                mark_service_degraded(runner, "memory_backup_manager")
                logger.exception("Failed to initialize MemoryBackupManager")
        else:
            mark_service_degraded(runner, "memory_backup_manager")
            logger.warning(
                "Skipping MemoryBackupManager initialization; MemoryManager is unavailable"
            )


def _init_message_processor(runner: GobbyRunner) -> None:
    runner.message_processor = None
    if getattr(runner.config, "message_tracking", None) and runner.config.message_tracking.enabled:
        runner.message_processor = SessionMessageProcessor(
            db=runner.database,
            poll_interval=runner.config.message_tracking.poll_interval,
            session_manager=runner.session_manager,
            run_db=runner.db_executor.run,
        )


def _init_task_validator(runner: GobbyRunner) -> None:
    runner.task_validator = None

    gobby_tasks_config = runner.config.gobby_tasks
    if not gobby_tasks_config.validation.enabled:
        return
    if runner.llm_service is None:
        mark_service_degraded(runner, "task_validator")
        logger.warning("Skipping TaskValidator initialization; LLM service is unavailable")
        return
    try:
        runner.task_validator = TaskValidator(
            llm_service=runner.llm_service,
            config=gobby_tasks_config.validation,
            db=runner.database,
        )
    except Exception:
        mark_service_degraded(runner, "task_validator")
        logger.exception("Failed to initialize TaskValidator")


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
                logger.debug(
                    "Daemon project context: id=%s, path=%s", runner.project_id, project_path
                )
    except Exception:
        mark_service_degraded(runner, "project_context")
        logger.warning("Could not detect project context from cwd", exc_info=True)

"""Service setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from gobby.ai import (
    build_daemon_text_generation_service,
    build_daemon_tool_chat_service,
)
from gobby.ai.embedding_switch import CompletedSwitchRecord
from gobby.ai.embeddings import EmbeddingService
from gobby.config.embedding_keys import (
    EMBEDDING_API_KEY_SECRET_NAME,
    EMBEDDING_SWITCH_COMPLETED_KEY,
)
from gobby.config.logging import RUNTIME_LOG_FILENAME, resolved_log_path
from gobby.config.persistence import EmbeddingsConfig, is_falkordb_enabled
from gobby.llm import create_llm_service
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.clones import LocalCloneManager
from gobby.storage.config_store import ConfigStore
from gobby.storage.embedding_generation_state import (
    EmbeddingGenerationState,
    managed_projection_targets,
)
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.sync.memories import MemoryBackupManager
from gobby.tasks.validation import TaskValidator

from .config_subscribers import PreparedService, ServiceSubscriber, live_subscriber_keys
from .embedding_lease import _EMBEDDING_REACQUIRE_POLL_SECONDS, _ManagedEmbeddingLease

if TYPE_CHECKING:
    from gobby.ai import TextGenerationService, ToolChatService
    from gobby.code_index.context import CodeIndexContext
    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.semantic_search import SemanticToolSearch
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

_EMBEDDING_LEASE_SECONDS = 30.0
_UNMANAGED_GENERATION_PREFIX = "config-revision:"


def mark_service_degraded(runner: GobbyRunner, service_name: str) -> None:
    """Record a failed or dependency-skipped runner service."""
    degraded_services = getattr(runner, "degraded_services", None)
    if degraded_services is None:
        degraded_services = set()
        runner.degraded_services = degraded_services
    degraded_services.add(service_name)


async def init_stateful_services(runner: GobbyRunner) -> None:
    """Initialize LLM, memory, code indexer, MCP proxy, sync, and messaging."""
    _init_stateful_dependencies(runner)
    await _register_stateful_services(runner)
    _apply_stateful_services(runner)
    _init_project_context(runner)
    listener = getattr(runner, "definition_revision_listener", None)
    start = getattr(listener, "start", None)
    if callable(start):
        await start()


def init_services(runner: GobbyRunner) -> None:
    """Initialize services for the synchronous construction path."""
    _init_llm_service(runner)
    _init_memory_stack(runner)
    _init_code_indexer(runner)
    _init_mcp_stack(runner)
    _init_memory_backup(runner)
    _init_message_processor(runner)
    _init_task_validator(runner)
    _init_project_context(runner)


@dataclass(frozen=True)
class AIServiceBundle:
    text_generation_service: TextGenerationService
    llm_service: LLMService
    tool_chat_service: ToolChatService


def _resolve_llm_service(runner: GobbyRunner) -> LLMService | None:
    runtime = getattr(runner, "config_runtime", None)
    if runtime is not None and runtime.ready:
        service = runtime.capture().services.get("ai_services")
        return service.llm_service if isinstance(service, AIServiceBundle) else None
    return getattr(runner, "llm_service", None)


@dataclass
class MemoryServiceBundle:
    vector_store: VectorStore
    _memory_manager: MemoryManager
    memory_backup_manager: MemoryBackupManager | None
    semantic_search: SemanticToolSearch
    _repair_started: bool = False

    @property
    def memory_manager(self) -> MemoryManager:
        if not self._repair_started:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return self._memory_manager
            self._memory_manager.start_projection_scope_repair()
            self._repair_started = True
        return self._memory_manager


def _init_stateful_dependencies(runner: GobbyRunner) -> None:
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.metrics_events import MetricsEventStore
    from gobby.projects.write_fence import ProjectWriteFence
    from gobby.storage.projects import LocalProjectManager

    runner.project_write_fence = ProjectWriteFence(LocalProjectManager(runner.database).get)
    runner.mcp_db_manager = LocalMCPManager(runner.database)
    try:
        bundled = runner.mcp_db_manager.normalize_bundled_servers()
    except Exception:
        logger.exception("error normalizing bundled MCP servers")
        bundled = {"normalized": 0, "duplicates_removed": 0, "tools_migrated": 0}
    if bundled["normalized"] or bundled["duplicates_removed"]:
        logger.info(
            "Normalized bundled MCP servers: %s normalized, %s duplicates removed",
            bundled["normalized"],
            bundled["duplicates_removed"],
        )
    runner.metrics_event_store = MetricsEventStore(runner.database)
    runner.metrics_manager = ToolMetricsManager(
        runner.database,
        event_store=runner.metrics_event_store,
    )


async def _register_stateful_services(runner: GobbyRunner) -> None:
    loop = asyncio.get_running_loop()
    subscribers = (
        ServiceSubscriber(
            name="ai_services",
            keys=live_subscriber_keys("ai_services"),
            builder=lambda change: _build_ai_services(runner, change.desired),
        ),
        ServiceSubscriber(
            name="memory_services",
            keys=live_subscriber_keys("memory_services"),
            builder=lambda change: _build_memory_services(
                runner,
                change.desired,
                loop,
                change.managed,
                change.revision,
            ),
            # Watermark preparation can wait behind active shared producer transactions.
            prepare_timeout=30.0,
        ),
        ServiceSubscriber(
            name="code_index",
            keys=live_subscriber_keys("code_index"),
            builder=lambda change: _build_code_index(runner, change.desired),
        ),
        ServiceSubscriber(
            name="mcp_manager",
            keys=live_subscriber_keys("mcp_manager"),
            builder=lambda change: _build_mcp_manager(runner, change.desired, loop),
            required=True,
        ),
        ServiceSubscriber(
            name="message_processor",
            keys=live_subscriber_keys("message_processor"),
            builder=lambda change: _build_message_processor(runner, change.desired, loop),
        ),
        ServiceSubscriber(
            name="task_validator",
            keys=live_subscriber_keys("task_validator"),
            builder=lambda change: _build_task_validator(runner, change.desired),
        ),
        ServiceSubscriber(
            name="mcp_proxy_config",
            keys=live_subscriber_keys("mcp_proxy_config"),
            builder=lambda change: change.desired.mcp_client_proxy,
            required=True,
        ),
        ServiceSubscriber(
            name="chat_config",
            keys=live_subscriber_keys("chat_config"),
            builder=lambda change: change.desired.chat,
            required=True,
        ),
    )
    for subscriber in subscribers:
        await runner.config_runtime.register_subscriber(subscriber)


def _build_ai_services(runner: GobbyRunner, config: DaemonConfig) -> AIServiceBundle:
    text_generation = build_daemon_text_generation_service(config)
    return AIServiceBundle(
        text_generation_service=text_generation,
        llm_service=create_llm_service(config, text_generation=text_generation),
        tool_chat_service=build_daemon_tool_chat_service(
            config,
            credential_manager=runner.managed_credential_manager,
        ),
    )


def _build_memory_services(
    runner: GobbyRunner,
    config: DaemonConfig,
    loop: asyncio.AbstractEventLoop,
    managed: Mapping[str, object],
    revision: int,
) -> PreparedService:
    from gobby.mcp_proxy.semantic_search import SemanticToolSearch
    from gobby.projects.fenced_vector_store import ProjectFencedVectorStore

    db_cfg = config.databases
    emb_cfg = config.embeddings
    api_key = _resolve_embedding_api_key(runner, emb_cfg)
    generation_state = EmbeddingGenerationState(runner.database)
    generation, serving_revision, caught_up_watermark = _serving_lease_identity(
        managed,
        revision,
        generation_state,
    )
    lease = generation_state.prepare_serving_lease(
        UUID(runner.machine_id),
        generation,
        serving_revision,
        lease_seconds=_EMBEDDING_LEASE_SECONDS,
        caught_up_watermark=caught_up_watermark,
        required_watermark=caught_up_watermark,
    )
    lease_handle = _ManagedEmbeddingLease(
        lease,
        loop,
        read_completed_record=lambda: _read_completed_switch_record(runner),
        request_rebuild=lambda record: _request_memory_services_rebuild(runner, loop, record),
        request_projection_repair=lambda: _request_memory_projection_repair(runner, loop),
    )
    raw_vector_store = VectorStore(
        url=db_cfg.qdrant.url,
        api_key=db_cfg.qdrant.api_key,
        collection_name=_managed_embedding_collection(managed, "memories"),
        embedding_dim=emb_cfg.dim,
        generation_state=generation_state,
        serving_guard=lease_handle.assert_serving,
        projection_targets_provider=lambda source_kind, primary: managed_projection_targets(
            runner.config_runtime.capture().managed, source_kind, primary
        ),
    )
    vector_store = cast(
        VectorStore,
        ProjectFencedVectorStore(raw_vector_store, runner.project_write_fence),
    )
    embed_fn: Callable[..., Any] | None = None
    try:
        _validate_memory_embedding_config(emb_cfg, api_key=api_key)
    except ValueError as exc:
        logger.warning("Memory embeddings disabled: %s", exc)
    else:
        embedding_service = EmbeddingService(
            model=emb_cfg.model,
            api_base=emb_cfg.api_base,
            api_key=api_key,
            dim=emb_cfg.dim,
            query_prefix=_embedding_query_prefix(emb_cfg),
        )
        embed_fn = embedding_service.generate_embedding
    text_generation = build_daemon_text_generation_service(config)
    llm_service = create_llm_service(config, text_generation=text_generation)
    falkor_cfg = db_cfg.falkordb if is_falkordb_enabled(db_cfg) else None
    memory_manager = MemoryManager(
        runner.database,
        config.memory,
        llm_service=llm_service,
        llm_service_resolver=lambda: _resolve_llm_service(runner),
        vector_store=vector_store,
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
        max_graph_deterministic_attempts=config.knowledge_graph_queue.max_deterministic_attempts,
        project_write_fence=runner.project_write_fence,
    )
    backup = (
        MemoryBackupManager(
            db=runner.database,
            memory_manager=memory_manager,
            config=config.memory_backup,
        )
        if config.memory_backup.enabled
        else None
    )
    semantic_search = SemanticToolSearch(
        db=runner.database,
        embedding_api_key=api_key,
        embedding_model=emb_cfg.model,
        embedding_dim=emb_cfg.dim,
        api_base=emb_cfg.api_base,
        vector_store=vector_store,
        collection_name=_managed_embedding_collection(managed, "tool_embeddings"),
    )
    bundle = MemoryServiceBundle(vector_store, memory_manager, backup, semantic_search)

    def dispose() -> None:
        lease_handle.dispose()
        _dispose_async(loop, memory_manager.close)

    return PreparedService(
        bundle,
        dispose,
        lease_handle.activate,
    )


def _serving_lease_identity(
    managed: Mapping[str, object],
    revision: int,
    generation_state: EmbeddingGenerationState,
) -> tuple[str, int, int]:
    """Resolve (generation, revision, watermark proof) for one serving lease.

    A managed completed switch carries its own catch-up proof; the unmanaged
    path proves catch-up against the ledger watermark captured here, in the
    builder thread, before the bundle starts serving."""
    record = managed.get(EMBEDDING_SWITCH_COMPLETED_KEY)
    if isinstance(record, CompletedSwitchRecord):
        return record.run_id, record.committed_revision, record.caught_up_watermark
    return (
        f"{_UNMANAGED_GENERATION_PREFIX}{revision}",
        revision,
        generation_state.watermark(),
    )


def _read_completed_switch_record(runner: GobbyRunner) -> CompletedSwitchRecord | None:
    """Read the current completed-switch record; None when absent or malformed."""
    raw = ConfigStore(runner.database).get_internal_lifecycle(EMBEDDING_SWITCH_COMPLETED_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        return CompletedSwitchRecord.from_dict(raw)
    except (TypeError, ValueError):
        return None


_background_runtime_tasks: set[asyncio.Task[None]] = set()


def _request_memory_projection_repair(
    runner: GobbyRunner,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Schedule store reconcile against the live memory bundle after serving resumes."""

    async def _repair() -> None:
        try:
            bundle = runner.config_runtime.capture().services.get("memory_services")
            if not isinstance(bundle, MemoryServiceBundle):
                return
            await bundle.memory_manager.reconcile_stores(dry_run=False)
        except Exception:
            logger.warning("Embedding projection repair failed", exc_info=True)

    def _schedule() -> None:
        task = loop.create_task(_repair())
        _background_runtime_tasks.add(task)
        task.add_done_callback(_background_runtime_tasks.discard)

    try:
        loop.call_soon_threadsafe(_schedule)
    except RuntimeError:
        logger.debug("Embedding projection repair skipped; event loop is closed")


def _request_memory_services_rebuild(
    runner: GobbyRunner,
    loop: asyncio.AbstractEventLoop,
    record: CompletedSwitchRecord | None,
) -> None:
    """Schedule a reconcile that rebuilds memory services past a stale lease."""
    runtime = runner.config_runtime
    try:
        current_revision = runtime.capture().snapshot.revision
    except RuntimeError:
        logger.debug("Embedding lease rebuild skipped before config runtime startup")
        return
    target_revision = record.committed_revision if record is not None else current_revision

    async def _reconcile() -> None:
        try:
            while True:
                before = runtime.capture().services.get("memory_services")
                await runtime.reconcile_revision(target_revision)
                bundle = runtime.capture()
                snapshot = bundle.snapshot
                if bundle.services.get("memory_services") is before:
                    snapshot = await runtime.reprepare_subscriber("memory_services")
                if not any(
                    failure.subscriber == "memory_services"
                    for failure in snapshot.failed_live_keys.values()
                ):
                    _request_memory_projection_repair(runner, loop)
                    return
                await asyncio.sleep(_EMBEDDING_REACQUIRE_POLL_SECONDS)
        except Exception:
            logger.exception("Embedding lease rebuild reconcile failed")

    task = loop.create_task(_reconcile())
    _background_runtime_tasks.add(task)
    task.add_done_callback(_background_runtime_tasks.discard)


def _managed_embedding_collection(managed: Mapping[str, object], kind: str) -> str:
    record = managed.get(EMBEDDING_SWITCH_COMPLETED_KEY)
    if not isinstance(record, CompletedSwitchRecord):
        return kind
    try:
        return record.physical_names[kind]
    except KeyError as exc:
        raise RuntimeError(f"Managed embedding generation lacks collection {kind}") from exc


def _build_code_index(runner: GobbyRunner, config: DaemonConfig) -> CodeIndexContext | None:
    if not config.code_index.enabled:
        return None
    from gobby.code_index.context import CodeIndexContext
    from gobby.code_index.gcode_gateway import GcodeGateway
    from gobby.code_index.storage import CodeIndexStorage

    return CodeIndexContext(
        storage=CodeIndexStorage(runner.database),
        gcode_gateway=GcodeGateway(),
        config=config.code_index,
        run_db=runner.db_executor.run,
    )


def _build_mcp_manager(
    runner: GobbyRunner,
    config: DaemonConfig,
    loop: asyncio.AbstractEventLoop,
) -> PreparedService:
    manager = MCPClientManager(
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
        stdio_errlog_path=str(resolved_log_path(config.logging, RUNTIME_LOG_FILENAME)),
    )
    return PreparedService(manager, lambda: _dispose_async(loop, manager.disconnect_all))


def _build_message_processor(
    runner: GobbyRunner,
    config: DaemonConfig,
    loop: asyncio.AbstractEventLoop,
) -> PreparedService | None:
    if not config.message_tracking.enabled:
        return None
    processor = SessionMessageProcessor(
        db=runner.database,
        poll_interval=config.message_tracking.poll_interval,
        session_manager=runner.session_manager,
        run_db=runner.db_executor.run,
    )

    def activate() -> None:
        # Rewire live-server references so a rebuilt processor keeps streaming
        # (startup activation runs before the servers exist; those refs are
        # attached later by server init and the app lifespan instead).
        processor.session_manager = runner.session_manager
        processor.websocket_server = getattr(runner, "websocket_server", None)
        http_server = getattr(runner, "http_server", None)
        hook_manager = getattr(http_server, "_hook_manager", None)
        if hook_manager is not None:
            processor.set_hook_manager(hook_manager)
        runner.message_processor = processor
        future = asyncio.run_coroutine_threadsafe(processor.start(), loop)
        try:
            future.result(timeout=4.5)
        except BaseException:
            future.cancel()
            if runner.message_processor is processor:
                runner.message_processor = None
            raise
        if hook_manager is not None:
            hook_manager._session_coordinator.reregister_active_sessions(
                message_processor=processor
            )

    def dispose() -> None:
        if runner.message_processor is processor:
            runner.message_processor = None
        _dispose_async(loop, processor.stop)

    return PreparedService(processor, dispose, activate)


def _build_task_validator(runner: GobbyRunner, config: DaemonConfig) -> TaskValidator | None:
    if not config.gobby_tasks.validation.enabled:
        return None
    text_generation = build_daemon_text_generation_service(config)
    return TaskValidator(
        llm_service=create_llm_service(config, text_generation=text_generation),
        config=config.gobby_tasks.validation,
        db=runner.database,
    )


def _dispose_async(
    loop: asyncio.AbstractEventLoop,
    operation: Callable[[], Any],
) -> None:
    future = asyncio.run_coroutine_threadsafe(operation(), loop)
    try:
        future.result(timeout=4.5)
    except BaseException:
        future.cancel()
        raise


def _apply_stateful_services(runner: GobbyRunner) -> None:
    bundle = runner.config_runtime.capture()
    ai = bundle.services.get("ai_services")
    runner.text_generation_service = (
        ai.text_generation_service if isinstance(ai, AIServiceBundle) else None
    )
    runner.llm_service = ai.llm_service if isinstance(ai, AIServiceBundle) else None
    runner.tool_chat_service = ai.tool_chat_service if isinstance(ai, AIServiceBundle) else None
    memory = bundle.services.get("memory_services")
    runner.vector_store = memory.vector_store if isinstance(memory, MemoryServiceBundle) else None
    runner.memory_manager = (
        memory.memory_manager if isinstance(memory, MemoryServiceBundle) else None
    )
    runner.memory_backup_manager = (
        memory.memory_backup_manager if isinstance(memory, MemoryServiceBundle) else None
    )
    runner.code_indexer = bundle.services.get("code_index")
    mcp_manager = bundle.services.get("mcp_manager")
    if not isinstance(mcp_manager, MCPClientManager):
        raise RuntimeError("required MCP configuration subscriber is unavailable")
    runner.mcp_proxy = mcp_manager
    message_processor = bundle.services.get("message_processor")
    runner.message_processor = (
        message_processor if isinstance(message_processor, SessionMessageProcessor) else None
    )
    task_validator = bundle.services.get("task_validator")
    runner.task_validator = task_validator if isinstance(task_validator, TaskValidator) else None


def _init_llm_service(runner: GobbyRunner) -> None:
    runner.codex_client = None
    runner.text_generation_service = None
    runner.tool_chat_service = None
    runner.llm_service = None
    try:
        runner.text_generation_service = build_daemon_text_generation_service(
            runner.startup_config,
        )
        runner.llm_service = create_llm_service(
            runner.startup_config,
            text_generation=runner.text_generation_service,
        )
        runner.tool_chat_service = build_daemon_tool_chat_service(
            runner.startup_config,
            credential_manager=runner.managed_credential_manager,
        )
        if runner.llm_service is not None:
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
    from gobby.projects.fenced_vector_store import ProjectFencedVectorStore
    from gobby.projects.write_fence import ProjectWriteFence
    from gobby.storage.projects import LocalProjectManager

    runner.project_write_fence = ProjectWriteFence(LocalProjectManager(runner.database).get)
    runner.vector_store = None
    runner.memory_manager = None
    if hasattr(runner.startup_config, "memory"):
        try:
            db_cfg = runner.startup_config.databases
            emb_cfg = runner.startup_config.embeddings
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
            raw_vector_store = VectorStore(
                url=db_cfg.qdrant.url,
                api_key=db_cfg.qdrant.api_key,
                embedding_dim=emb_cfg.dim,
                generation_state=EmbeddingGenerationState(runner.database),
            )
            runner.vector_store = cast(
                VectorStore,
                ProjectFencedVectorStore(raw_vector_store, runner.project_write_fence),
            )
            # The synchronous path serves without a generation lease or ack:
            # projection dual-writes still flow via generation_state, but GC
            # cannot see this daemon, so surface the gap explicitly.
            mark_service_degraded(runner, "embedding_serving_lease")
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
                runner.startup_config.memory,
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
                    runner.startup_config.knowledge_graph_queue.max_deterministic_attempts
                ),
                project_write_fence=runner.project_write_fence,
            )
            if falkor_cfg and (
                runner.memory_manager.graph_initialization_failed is True
                or runner.memory_manager.falkor_client is None
            ):
                mark_service_degraded(runner, "memory_knowledge_graph")
            runner.memory_manager.start_projection_scope_repair()
        except Exception:
            mark_service_degraded(runner, "memory_manager")
            logger.exception("Failed to initialize MemoryManager")


def _init_code_indexer(runner: GobbyRunner) -> None:
    runner.code_indexer = None
    if runner.startup_config.code_index.enabled:
        try:
            from gobby.code_index.context import CodeIndexContext
            from gobby.code_index.gcode_gateway import GcodeGateway
            from gobby.code_index.storage import CodeIndexStorage

            ci_config = runner.startup_config.code_index
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
        stdio_errlog_path=str(
            resolved_log_path(runner.startup_config.logging, RUNTIME_LOG_FILENAME)
        ),
    )


def _init_memory_backup(runner: GobbyRunner) -> None:
    runner.memory_backup_manager = None
    if runner.startup_config.memory_backup.enabled:
        if runner.memory_manager:
            try:
                runner.memory_backup_manager = MemoryBackupManager(
                    db=runner.database,
                    memory_manager=runner.memory_manager,
                    config=runner.startup_config.memory_backup,
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
    if runner.startup_config.message_tracking.enabled:
        runner.message_processor = SessionMessageProcessor(
            db=runner.database,
            poll_interval=runner.startup_config.message_tracking.poll_interval,
            session_manager=runner.session_manager,
            run_db=runner.db_executor.run,
        )


def _init_task_validator(runner: GobbyRunner) -> None:
    runner.task_validator = None

    gobby_tasks_config = runner.startup_config.gobby_tasks
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

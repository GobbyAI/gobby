"""High-level memory manager compatibility facade."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import TYPE_CHECKING, Any

from gobby.config.persistence import MemoryConfig
from gobby.memory.backends.storage_adapter import StorageAdapter
from gobby.memory.facade import (
    DEFAULT_GRAPH_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    MAX_REINDEX_LIMIT,
    MemoryManagerFacadeMethods,
)
from gobby.memory.falkor_client import FalkorClient
from gobby.memory.protocol import MemoryBackendProtocol
from gobby.memory.recall_constants import resolve_recall_constants
from gobby.memory.recall_signal_log import make_recall_signal_sink
from gobby.memory.services.crossref import CrossrefRebuildError, CrossrefService
from gobby.memory.services.indexing import IndexingService
from gobby.memory.services.keyword import MemoryKeywordSearchService
from gobby.memory.services.knowledge_graph import (
    ActiveMemoryPreview,
    KnowledgeGraphRebuildService,
    KnowledgeGraphService,
)
from gobby.memory.services.lifecycle import MemoryLifecycleService
from gobby.memory.services.projection_repair import (
    ProjectionScopeRepairResult,
    ProjectionScopeRepairService,
)
from gobby.memory.services.repository import DEFAULT_LIST_LIMIT, MemoryRepository
from gobby.memory.services.search import SearchService
from gobby.memory.vectorstore_logging import (
    VECTORSTORE_WARNING_INTERVAL_SECONDS,
    log_rate_limited_warning,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import ALL_MEMORIES, LocalMemoryManager, MemoryScope

if TYPE_CHECKING:
    from gobby.projects.fenced_vector_store import VectorWriteFence

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.memory.vectorstore import VectorStore

__all__ = [
    "CrossrefRebuildError",
    "DEFAULT_GRAPH_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_REINDEX_LIMIT",
    "MemoryManager",
    "ProjectionScopeRepairResult",
]

logger = logging.getLogger(__name__)


class MemoryManager(MemoryManagerFacadeMethods):
    """Stable public facade for memory operations."""

    def __init__(
        self,
        db: HubDatabase,
        config: MemoryConfig,
        llm_service: LLMService | None = None,
        vector_store: VectorStore | None = None,
        embed_fn: Callable[..., Any] | None = None,
        *,
        llm_service_resolver: Callable[[], LLMService | None] | None = None,
        falkordb_host: str | None = None,
        falkordb_port: int = 16379,
        falkordb_password: str | None = None,
        falkordb_graph_name: str = "gobby_kg",
        falkordb_graph_search: bool = True,
        falkordb_graph_min_score: float = 0.5,
        rrf_k: int = 60,
        falkordb_rrf_k: int = 60,
        embedding_dim: int = 768,
        collection_prefix: str = "code_symbols_",
        run_db: Callable[..., Awaitable[Any]] | None = None,
        max_graph_deterministic_attempts: int = 3,
        project_write_fence: VectorWriteFence | None = None,
    ):
        self.db = db
        self.config = config
        self._run_db = run_db
        self._llm_service = llm_service
        self._llm_service_resolver = llm_service_resolver or (lambda: self._llm_service)
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._project_write_fence = project_write_fence

        self.storage = LocalMemoryManager(db)
        self._backend: MemoryBackendProtocol = StorageAdapter(self.storage, run_db=run_db)
        # #17200: daemon-global effective recall ranking constants, resolved once.
        self._recall_constants = resolve_recall_constants(config)
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._last_vector_store_warning_at = -VECTORSTORE_WARNING_INTERVAL_SECONDS

        self._graph_initialization_error: BaseException | None = None
        try:
            self._falkor_client = self._build_falkor_client(
                falkordb_host=falkordb_host,
                falkordb_port=falkordb_port,
                falkordb_password=falkordb_password,
                falkordb_graph_name=falkordb_graph_name,
            )
        except Exception as exc:
            self._graph_initialization_error = exc
            self._falkor_client = None
            logger.exception("Failed to initialize FalkorDB graph subsystem")
        self._kg_service = self._build_kg_service(
            llm_service=llm_service,
            llm_service_resolver=llm_service_resolver,
            vector_store=vector_store,
            embed_fn=embed_fn,
            collection_prefix=collection_prefix,
            embedding_dim=embedding_dim,
        )

        self._keyword_service = MemoryKeywordSearchService(db)
        self._repository = MemoryRepository(
            db=db,
            storage_provider=lambda: self.storage,
            backend_provider=lambda: self._backend,
        )
        self._crossref_service = CrossrefService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            config=config,
            run_db=run_db,
        )
        self._lifecycle_service = MemoryLifecycleService(
            config=config,
            storage_provider=lambda: self.storage,
            backend_provider=lambda: self._backend,
            vector_store=vector_store,
            embed_fn=embed_fn,
            crossref_service=self._crossref_service,
            kg_service_provider=lambda: self._kg_service,
            background_tasks=self._background_tasks,
            record_to_memory=self._record_to_memory,
            get_memory=self.get_memory,
            embed_and_upsert=lambda *args, **kwargs: self._embed_and_upsert(*args, **kwargs),
            vector_store_failure_logger=self._log_vector_store_failure,
            run_db=run_db,
        )
        self._search_service = SearchService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            kg_service=self._kg_service,
            keyword_search=self._keyword_search,
            config=config,
            falkordb_graph_search=falkordb_graph_search,
            falkordb_graph_min_score=falkordb_graph_min_score,
            rrf_k=rrf_k,
            falkordb_rrf_k=falkordb_rrf_k,
            vector_store_failure_logger=self._log_vector_store_failure,
            run_db=run_db,
            search_debug_sink=make_recall_signal_sink(
                config, db, recall_constants=self._recall_constants
            ),
            recall_constants=self._recall_constants,
        )
        self._indexing_service = IndexingService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            kg_service=self._kg_service,
            crossref_service=self._crossref_service,
            kg_rebuilder=self.rebuild_knowledge_graph,
            reconcile_memory=self._lifecycle_service.reconcile_memory_indices,
            rebuild_crossrefs=self._lifecycle_service.rebuild_crossrefs_for_memory,
            cleanup_rowless=self._lifecycle_service.purge_secondary_indices,
            run_db=run_db,
        )
        self._kg_rebuild_service = KnowledgeGraphRebuildService(
            storage_provider=lambda: self.storage,
            kg_service_provider=lambda: self._kg_service,
            falkor_client_provider=lambda: self._falkor_client,
            run_db=self.run_db,
            list_memories=self.list_memories,
            fetch_all_project_memories=lambda project_id: self._fetch_all_project_memories(
                project_id
            ),
            mark_graph_processed=lambda memory_id: self.mark_graph_processed(memory_id),
            record_graph_failure=lambda memory_id, **kwargs: self.record_graph_failure(
                memory_id, **kwargs
            ),
            max_rebuild_concurrency=config.kg.max_rebuild_concurrency,
            max_deterministic_attempts=max_graph_deterministic_attempts,
        )
        self._projection_repair_service = ProjectionScopeRepairService(
            storage_provider=lambda: self.storage,
            run_db=self.run_db,
            restore_memory_indices=self._lifecycle_service.restore_memory_indices,
            falkor_client_provider=lambda: self._falkor_client,
        )

    def start_projection_scope_repair(self) -> asyncio.Task[Any]:
        """Start the daemon-owned secondary scope repair pass."""
        return self.schedule_background_task(
            self.repair_secondary_scope_projections(),
            name="memory-projection-scope-repair",
        )

    def _build_falkor_client(
        self,
        *,
        falkordb_host: str | None,
        falkordb_port: int,
        falkordb_password: str | None,
        falkordb_graph_name: str,
    ) -> FalkorClient | None:
        if not falkordb_host:
            return None
        return FalkorClient(
            host=falkordb_host,
            port=falkordb_port,
            password=falkordb_password,
            graph_name=falkordb_graph_name,
        )

    def _build_kg_service(
        self,
        *,
        llm_service: LLMService | None,
        llm_service_resolver: Callable[[], LLMService | None] | None,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        collection_prefix: str,
        embedding_dim: int,
    ) -> KnowledgeGraphService | None:
        resolved_llm = llm_service
        if resolved_llm is None and llm_service_resolver is not None:
            resolved_llm = llm_service_resolver()
        if resolved_llm is None or not self._falkor_client:
            return None
        try:
            from gobby.prompts.loader import PromptLoader

            prompt_loader = PromptLoader(db=self.db)

            async def _active_memory_lookup(
                memory_ids: Sequence[str], project_id: str | None
            ) -> dict[str, ActiveMemoryPreview]:
                """Return previews for the active (not soft-hidden) subset of ``memory_ids``.

                The memory store is the visibility source of truth; the graph keeps
                soft-hidden Memory nodes until purge, so entity-graph reads consult this
                to drop entities/relationships backed only by hidden rows. The preview
                content feeds the graph UI's entity cards.
                """
                ids = list(memory_ids)
                if not ids:
                    return {}
                active = await asyncio.to_thread(
                    self.storage.get_memories,
                    ids,
                    (
                        ALL_MEMORIES
                        if project_id is None
                        else MemoryScope.project_visible(project_id)
                    ),
                    visibility="active",
                )
                return {
                    memory.id: ActiveMemoryPreview(
                        content=memory.content, updated_at=memory.updated_at
                    )
                    for memory in active
                }

            kg_service = KnowledgeGraphService(
                falkor_client=self._falkor_client,
                embed_fn=embed_fn,
                prompt_loader=prompt_loader,
                vector_store=vector_store,
                code_link_min_score=self.config.code_link_min_score,
                code_symbol_collection_prefix=collection_prefix,
                embedding_dim=embedding_dim,
                llm_service=llm_service,
                llm_service_resolver=llm_service_resolver,
                feature_config=self.config.kg,
                graph_edge_weighting=self.config.graph_edge_weighting,
                materialize_cooccurrence=self.config.materialize_cooccurrence,
                graph_edge_decay=self.config.graph_edge_decay,
                edge_half_life_days=self.config.edge_half_life_days,
                cluster_recall_expansion=self.config.cluster_recall_expansion,
                cluster_expansion_per_entity=self.config.cluster_expansion_per_entity,
                cluster_min_cluster_size=self.config.cluster_min_cluster_size,
                cluster_min_samples=self.config.cluster_min_samples,
                cooccur_alpha=(
                    self._recall_constants.cooccur_alpha
                    if self._recall_constants.source == "fitted"
                    else None
                ),
                cooccur_support_cap=(
                    self._recall_constants.cooccur_support_cap
                    if self._recall_constants.source == "fitted"
                    else None
                ),
                active_memory_lookup=_active_memory_lookup,
                write_fence=self._project_write_fence,
            )
            logger.debug("KnowledgeGraphService initialized")
            return kg_service
        except Exception as exc:
            self._graph_initialization_error = exc
            self._falkor_client = None
            logger.exception("Failed to initialize KnowledgeGraphService")
            return None

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run memory storage work on the daemon DB executor when available."""
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    def schedule_background_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Schedule and retain daemon-owned background work until it completes."""
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def close(self) -> None:
        """Drain daemon-owned work before closing secondary clients."""
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self._lifecycle_service.close_related_evidence_sessions()

        if self._falkor_client:
            try:
                await self._falkor_client.close()
            except Exception as e:
                logger.warning("Failed to close FalkorDB client: %s", e)
            self.clear_graph_clients()

    def clear_graph_clients(self) -> None:
        """Disable graph features by clearing FalkorDB client and KG service references."""
        self._falkor_client = None
        self._kg_service = None
        self._search_service._kg_service = None
        self._indexing_service._kg_service = None

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        return self._kg_service

    @property
    def vector_store(self) -> Any | None:
        return self._vector_store

    @property
    def embed_fn(self) -> Callable[..., Any] | None:
        return self._embed_fn

    @property
    def llm_service(self) -> LLMService | None:
        return self._llm_service_resolver()

    @property
    def falkor_client(self) -> FalkorClient | None:
        """Shared FalkorDB client for graph-backed subsystems, when configured."""
        return self._falkor_client

    @property
    def graph_initialization_failed(self) -> bool:
        """Whether configured graph construction failed while core memory stayed available."""
        return self._graph_initialization_error is not None

    def _log_vector_store_failure(self, message: str, error: BaseException) -> None:
        """Rate-limit noisy VectorStore availability warnings."""
        self._last_vector_store_warning_at = log_rate_limited_warning(
            logger,
            self._last_vector_store_warning_at,
            message,
            error,
        )

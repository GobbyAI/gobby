"""High-level memory manager compatibility facade."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
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
from gobby.memory.recall_signal_log import make_recall_signal_sink
from gobby.memory.services.crossref import CrossrefRebuildError, CrossrefService
from gobby.memory.services.indexing import IndexingService
from gobby.memory.services.keyword import MemoryKeywordSearchService
from gobby.memory.services.knowledge_graph import (
    KnowledgeGraphRebuildService,
    KnowledgeGraphService,
)
from gobby.memory.services.lifecycle import MemoryLifecycleService
from gobby.memory.services.project_repair import (
    NullProjectMemoryRepair,
    NullProjectMemoryRepairResult,
    NullProjectMemoryRepairService,
)
from gobby.memory.services.repository import DEFAULT_LIST_LIMIT, MemoryRepository
from gobby.memory.services.search import SearchService
from gobby.memory.vectorstore import (
    VECTORSTORE_WARNING_INTERVAL_SECONDS,
    log_rate_limited_warning,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.memory.services.dedup import DedupService
    from gobby.memory.vectorstore import VectorStore

__all__ = [
    "CrossrefRebuildError",
    "DEFAULT_GRAPH_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_REINDEX_LIMIT",
    "MemoryManager",
    "NullProjectMemoryRepair",
    "NullProjectMemoryRepairResult",
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
    ):
        self.db = db
        self.config = config
        self._run_db = run_db
        self._llm_service = llm_service
        self._vector_store = vector_store
        self._embed_fn = embed_fn

        self.storage = LocalMemoryManager(db)
        self._backend: MemoryBackendProtocol = StorageAdapter(self.storage, run_db=run_db)
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._last_vector_store_warning_at = -VECTORSTORE_WARNING_INTERVAL_SECONDS

        self._falkor_client = self._build_falkor_client(
            falkordb_host=falkordb_host,
            falkordb_port=falkordb_port,
            falkordb_password=falkordb_password,
            falkordb_graph_name=falkordb_graph_name,
        )
        self._dedup_service = self._build_dedup_service(vector_store, embed_fn)
        self._kg_service = self._build_kg_service(
            llm_service=llm_service,
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
            dedup_service_provider=lambda: self._dedup_service,
            kg_service_provider=lambda: self._kg_service,
            background_tasks=self._background_tasks,
            record_to_memory=self._record_to_memory,
            get_memory=self.get_memory,
            embed_and_upsert=lambda *args, **kwargs: self._embed_and_upsert(*args, **kwargs),
            vector_store_failure_logger=self._log_vector_store_failure,
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
            search_debug_sink=make_recall_signal_sink(config),
        )
        self._indexing_service = IndexingService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            kg_service=self._kg_service,
            crossref_service=self._crossref_service,
            kg_rebuilder=self.rebuild_knowledge_graph,
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
        )
        self._project_repair_service = NullProjectMemoryRepairService(
            db=db,
            storage_provider=lambda: self.storage,
            run_db=self.run_db,
            embed_and_upsert=lambda *args, **kwargs: self._embed_and_upsert(*args, **kwargs),
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

    def _build_dedup_service(
        self,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
    ) -> DedupService | None:
        if not vector_store or not embed_fn:
            return None
        try:
            from gobby.memory.services.dedup import DedupService as _DedupService

            dedup_service = _DedupService(
                vector_store=vector_store,
                storage=self.storage,
                embed_fn=embed_fn,
            )
            logger.debug("DedupService initialized")
            return dedup_service
        except Exception as e:
            logger.warning(f"Failed to initialize DedupService: {e}")
            return None

    def _build_kg_service(
        self,
        *,
        llm_service: LLMService | None,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        collection_prefix: str,
        embedding_dim: int,
    ) -> KnowledgeGraphService | None:
        if not llm_service or not self._falkor_client:
            return None
        try:
            from gobby.prompts.loader import PromptLoader

            prompt_loader = PromptLoader(db=self.db)

            async def _active_memory_filter(
                memory_ids: Sequence[str], project_id: str | None
            ) -> set[str]:
                """Return the active (not soft-hidden) subset of ``memory_ids``.

                The memory store is the visibility source of truth; the graph keeps
                soft-hidden Memory nodes until purge, so entity-graph reads consult this
                to drop entities/relationships backed only by hidden rows.
                """
                ids = list(memory_ids)
                if not ids:
                    return set()
                active = await asyncio.to_thread(
                    self.storage.get_memories, ids, project_id, visibility="active"
                )
                return {memory.id for memory in active}

            kg_service = KnowledgeGraphService(
                falkor_client=self._falkor_client,
                embed_fn=embed_fn,
                prompt_loader=prompt_loader,
                vector_store=vector_store,
                code_link_min_score=self.config.code_link_min_score,
                code_symbol_collection_prefix=collection_prefix,
                embedding_dim=embedding_dim,
                llm_service=llm_service,
                feature_config=self.config.kg,
                graph_edge_weighting=self.config.graph_edge_weighting,
                materialize_cooccurrence=self.config.materialize_cooccurrence,
                graph_edge_decay=self.config.graph_edge_decay,
                edge_half_life_days=self.config.edge_half_life_days,
                cluster_recall_expansion=self.config.cluster_recall_expansion,
                cluster_expansion_per_entity=self.config.cluster_expansion_per_entity,
                cluster_min_cluster_size=self.config.cluster_min_cluster_size,
                cluster_min_samples=self.config.cluster_min_samples,
                active_memory_filter=_active_memory_filter,
            )
            logger.debug("KnowledgeGraphService initialized")
            return kg_service
        except Exception as e:
            logger.warning(f"Failed to initialize KnowledgeGraphService: {e}")
            return None

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run memory storage work on the daemon DB executor when available."""
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def close(self) -> None:
        """Close underlying graph clients."""
        if self._falkor_client:
            try:
                await self._falkor_client.close()
            except Exception as e:
                logger.warning(f"Failed to close FalkorDB client: {e}")
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
        return self._llm_service

    @llm_service.setter
    def llm_service(self, service: LLMService | None) -> None:
        self._llm_service = service

    @property
    def falkor_client(self) -> FalkorClient | None:
        """Shared FalkorDB client for graph-backed subsystems, when configured."""
        return self._falkor_client

    @property
    def _embeddings_available(self) -> bool | None:
        return self._lifecycle_service.embeddings_available

    @_embeddings_available.setter
    def _embeddings_available(self, value: bool | None) -> None:
        self._lifecycle_service.embeddings_available = value

    def _log_vector_store_failure(self, message: str, error: BaseException) -> None:
        """Rate-limit noisy VectorStore availability warnings."""
        self._last_vector_store_warning_at = log_rate_limited_warning(
            logger,
            self._last_vector_store_warning_at,
            message,
            error,
        )

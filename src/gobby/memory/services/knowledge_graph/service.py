"""Knowledge graph service orchestration facade."""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import TYPE_CHECKING, Any

from gobby.llm.base import LLMProviderCancellation
from gobby.memory.falkor_client import FalkorConnectionError, FalkorQueryError
from gobby.search.similarity import cosine_similarity as _cosine_similarity
from gobby.storage.projects import GLOBAL_PROJECT_ID, PERSONAL_PROJECT_ID

from .clustering import ClusterRunResult, recluster_project_entities
from .code_linker import KnowledgeGraphCodeLinker
from .densify import CooccurrenceDensifyResult, densify_cooccurrence
from .extraction import KnowledgeGraphExtractor
from .maintenance import KnowledgeGraphMaintenance
from .models import (
    Entity,
    KnowledgeGraphResult,
    KnowledgeGraphStatus,
    Relationship,
    _GraphEntity,
)
from .normalization import display_entity_name, normalize_entities, normalize_relationships
from .reader import KnowledgeGraphReader, RelatedMemoryTraversal
from .writer import COOCCUR_MAX_ENTITIES, KnowledgeGraphWriter

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.config.persistence import MemoryKnowledgeGraphConfig
    from gobby.llm.service import LLMService
    from gobby.memory.falkor_client import FalkorClient
    from gobby.memory.vectorstore import VectorStore
    from gobby.prompts.loader import PromptLoader

    from .reader import ActiveMemoryFilter

logger = logging.getLogger(__name__)


class KnowledgeGraphService:
    """Manages knowledge graph extraction, FalkorDB projection, and graph reads.

    Args:
        falkor_client: FalkorDB client for graph operations
        llm_service: LLM service for feature-routed entity/relationship extraction
        embed_fn: Async function to generate embeddings for entity names
        prompt_loader: PromptLoader for rendering extraction prompts
    """

    def __init__(
        self,
        falkor_client: FalkorClient,
        embed_fn: Callable[..., Any] | None,
        prompt_loader: PromptLoader,
        llm_service: LLMService,
        feature_config: MemoryKnowledgeGraphConfig,
        vector_store: VectorStore | None = None,
        code_link_min_score: float = 0.82,
        code_symbol_collection_prefix: str = "code_symbols_",
        embedding_dim: int = 768,
        graph_edge_weighting: bool = False,
        materialize_cooccurrence: bool = False,
        graph_edge_decay: bool = False,
        edge_half_life_days: float = 30.0,
        cluster_recall_expansion: bool = False,
        cluster_expansion_per_entity: int = 3,
        cluster_min_cluster_size: int = 5,
        cluster_min_samples: int | None = 2,
        cooccur_alpha: float | None = None,
        cooccur_support_cap: int | None = None,
        active_memory_filter: ActiveMemoryFilter | None = None,
        write_fence: Any | None = None,
    ) -> None:
        self._falkor = falkor_client
        self._embed_fn = embed_fn
        self._prompt_loader = prompt_loader
        self._vector_store = vector_store
        self._code_link_min_score = code_link_min_score
        self._code_symbol_collection_prefix = code_symbol_collection_prefix
        self._embedding_dim = embedding_dim
        self._graph_edge_weighting = graph_edge_weighting
        self._materialize_cooccurrence = materialize_cooccurrence
        self._cluster_min_cluster_size = cluster_min_cluster_size
        self._cluster_min_samples = cluster_min_samples
        self._write_fence = write_fence

        self._writer = KnowledgeGraphWriter(
            falkor_client,
            cooccur_alpha=cooccur_alpha,
            cooccur_support_cap=cooccur_support_cap,
        )
        self._extractor = KnowledgeGraphExtractor(
            prompt_loader,
            llm_service=llm_service,
            feature_config=feature_config,
        )
        self._maintenance = KnowledgeGraphMaintenance(falkor_client)
        self._reader = KnowledgeGraphReader(
            falkor_client,
            embed_fn,
            embedding_dim=embedding_dim,
            graph_edge_decay=graph_edge_decay,
            edge_half_life_days=edge_half_life_days,
            cluster_recall_expansion=cluster_recall_expansion,
            cluster_expansion_per_entity=cluster_expansion_per_entity,
            cooccur_alpha=cooccur_alpha,
            cooccur_support_cap=cooccur_support_cap,
            active_memory_filter=active_memory_filter,
        )
        self._code_linker = KnowledgeGraphCodeLinker(
            falkor_client,
            vector_store,
            code_link_min_score=code_link_min_score,
            code_symbol_collection_prefix=code_symbol_collection_prefix,
        )

    @property
    def _graph_schema_ensured(self) -> bool:
        return self._writer.graph_schema_ensured

    @_graph_schema_ensured.setter
    def _graph_schema_ensured(self, value: bool) -> None:
        self._writer.graph_schema_ensured = value

    @property
    def _graph_schema_lock(self) -> asyncio.Lock:
        return self._writer.graph_schema_lock

    @property
    def _vector_index_ensured(self) -> bool:
        return self._reader.vector_index_ensured

    @_vector_index_ensured.setter
    def _vector_index_ensured(self, value: bool) -> None:
        self._reader.vector_index_ensured = value

    # -----------------------------------------------------------------------
    # Write path
    # -----------------------------------------------------------------------

    async def add_to_graph(
        self,
        content: str,
        memory_id: str | None = None,
        project_id: str = PERSONAL_PROJECT_ID,
        is_global: bool = False,
    ) -> KnowledgeGraphResult:
        """Fence the project while applying derived knowledge-graph writes."""
        if self._write_fence is None:
            return await self._add_to_graph_unfenced(content, memory_id, project_id, is_global)
        write_project_id = GLOBAL_PROJECT_ID if is_global else project_id
        async with self._write_fence.writer(write_project_id):
            return await self._add_to_graph_unfenced(content, memory_id, project_id, is_global)

    async def _add_to_graph_unfenced(
        self,
        content: str,
        memory_id: str | None = None,
        project_id: str = PERSONAL_PROJECT_ID,
        is_global: bool = False,
    ) -> KnowledgeGraphResult:
        """Extract entities and relationships from content and merge into FalkorDB.

        Pipeline:
        1. Extract entities via LLM
        2. Extract relationships via LLM
        3. Fetch existing relationships for overlap detection
        4. Delete outdated relationships via LLM decision
        5. Merge nodes and relationships into FalkorDB
        6. Add type labels while preserving _Entity identity
        7. Set embedding vectors on nodes
        8. Link entities to source memory via MENTIONED_IN
        9. Cross-link entities to code symbols via RELATES_TO_CODE
        """
        memory_ref = memory_id or "<unknown>"
        try:
            await self._ensure_graph_schema()
        except (FalkorConnectionError, FalkorQueryError, TimeoutError) as e:
            logger.warning("Knowledge-graph schema unavailable for memory %s: %s", memory_ref, e)
            return KnowledgeGraphResult(
                status=KnowledgeGraphStatus.RETRYABLE_FAILURE,
                errors=[str(e)],
            )

        try:
            extracted_entities = await self._extract_entities(content)
        except LLMProviderCancellation as e:
            logger.info("Entity extraction cancelled for memory %s: %s", memory_ref, e)
            return self._cancellation_result(e)
        except Exception as e:
            logger.warning("Entity extraction failed for memory %s: %s", memory_ref, e)
            return self._failure_result(e)

        entities = self._normalize_entities(
            extracted_entities,
            project_id=project_id,
            is_global=is_global,
        )
        if not entities:
            return KnowledgeGraphResult(status=KnowledgeGraphStatus.NOOP_NO_ENTITIES)

        partial_errors: list[str] = []
        made_progress = False

        try:
            extracted_relationships = await self._extract_relationships(content, extracted_entities)
        except LLMProviderCancellation as e:
            logger.info("Relationship extraction cancelled for memory %s: %s", memory_ref, e)
            return self._cancellation_result(e, entities=len(entities))
        except Exception as e:
            logger.warning("Relationship extraction failed for memory %s: %s", memory_ref, e)
            partial_errors.append(f"relationship_extraction:{e}")
            extracted_relationships = []

        relationships = self._normalize_relationships(
            extracted_relationships,
            entities=entities,
            project_id=project_id,
            is_global=is_global,
        )

        try:
            await self._delete_outdated_relations(
                entities=entities,
                new_relations=relationships,
                project_id=GLOBAL_PROJECT_ID if is_global else project_id,
                is_global=is_global,
            )
        except LLMProviderCancellation as e:
            logger.info("Relation cleanup cancelled for memory %s: %s", memory_ref, e)
            return self._cancellation_result(
                e,
                entities=len(entities),
                relationships=len(relationships),
            )
        except Exception as e:
            logger.warning("Relation cleanup failed for memory %s: %s", memory_ref, e)
            partial_errors.append(f"relation_cleanup:{e}")

        for entity in entities:
            try:
                await self._writer.merge_entity(entity)
                made_progress = True
            except FalkorConnectionError as e:
                logger.warning(
                    "FalkorDB unreachable during merge_node for memory %s: %s",
                    memory_ref,
                    e,
                )
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(
                    "Failed to merge node %s for memory %s: %s",
                    entity.name,
                    memory_ref,
                    e,
                )
                partial_errors.append(f"merge_node:{entity.name}:{e}")

        # Embeddings are built before the weighted writes so cosine-derived edge
        # weights (typed relations and CO_OCCURS) have vectors available.
        entity_embeddings: dict[str, list[float]] = {}
        if self._embed_fn is not None:
            for entity in entities:
                try:
                    embedding = await self._embed_fn(entity.name)
                    entity_embeddings[entity.entity_key] = embedding
                    await self._writer.set_entity_vector(
                        entity_key=entity.entity_key,
                        embedding=embedding,
                    )
                    made_progress = True
                except FalkorConnectionError as e:
                    logger.warning(
                        "FalkorDB unreachable during set_node_vector for memory %s: %s",
                        memory_ref,
                        e,
                    )
                    return self._connection_failure_result(
                        e,
                        made_progress=made_progress,
                        partial_errors=partial_errors,
                        entities=len(entities),
                        relationships=len(relationships),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to set embedding for %s in memory %s: %s",
                        entity.name,
                        memory_ref,
                        e,
                    )
                    partial_errors.append(f"set_embedding:{entity.name}:{e}")

        for rel in relationships:
            try:
                await self._writer.merge_relationship(
                    rel,
                    properties=self._typed_relationship_properties(rel, entity_embeddings),
                )
                made_progress = True
            except FalkorConnectionError as e:
                logger.warning(
                    "FalkorDB unreachable during merge_relationship for memory %s: %s",
                    memory_ref,
                    e,
                )
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(
                    "Failed to merge relationship %s for memory %s: %s",
                    rel,
                    memory_ref,
                    e,
                )
                partial_errors.append(f"merge_relationship:{rel.relationship}:{e}")

        if memory_id:
            try:
                await self._link_entities_to_memory(
                    entities,
                    memory_id,
                    project_id=project_id,
                    is_global=is_global,
                )
                made_progress = True
            except FalkorConnectionError as e:
                logger.warning(
                    "FalkorDB unreachable during MENTIONED_IN link for memory %s: %s",
                    memory_ref,
                    e,
                )
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning("Failed to link entities to memory %s: %s", memory_ref, e)
                partial_errors.append(f"mentioned_in:{memory_id}:{e}")

        # CO_OCCURS support edges run after MENTIONED_IN: their support query reads
        # the just-written bipartite structure for the current memory.
        if memory_id and self._materialize_cooccurrence:
            try:
                await self._merge_cooccurrence_edges(
                    entities,
                    entity_embeddings,
                    GLOBAL_PROJECT_ID if is_global else project_id,
                    is_global,
                )
                made_progress = True
            except FalkorConnectionError as e:
                logger.warning(
                    "FalkorDB unreachable during CO_OCCURS materialization for memory %s: %s",
                    memory_ref,
                    e,
                )
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(
                    "Failed to materialize CO_OCCURS edges for memory %s: %s", memory_ref, e
                )
                partial_errors.append(f"co_occurs:{memory_id}:{e}")

        if (
            not is_global
            and self._vector_store
            and self._embed_fn is not None
            and entity_embeddings
        ):
            try:
                await self._link_entities_to_code(entities, entity_embeddings, project_id)
                made_progress = True
            except Exception as e:
                logger.warning(
                    "Failed to link entities to code for memory %s in project %s: %s",
                    memory_ref,
                    project_id,
                    e,
                )
                partial_errors.append(f"relates_to_code:{project_id}:{e}")

        status = (
            KnowledgeGraphStatus.PARTIAL_FAILURE if partial_errors else KnowledgeGraphStatus.SUCCESS
        )
        return KnowledgeGraphResult(
            status=status,
            entities_extracted=len(entities),
            relationships_extracted=len(relationships),
            errors=partial_errors,
        )

    def _typed_relationship_properties(
        self,
        rel: Relationship,
        entity_embeddings: dict[str, list[float]],
    ) -> dict[str, Any] | None:
        """Weighted-edge properties for a typed relation, or ``None`` when off.

        ``typed_weight = cos01`` (cosine clamped to >= 0). The reinforcement count is
        managed by the client as edge metadata, not folded into the weight, so the
        Python side only supplies the weight. Returns ``None`` when weighting is
        disabled or either endpoint lacks an embedding, preserving unweighted writes.
        """
        if not self._graph_edge_weighting:
            return None
        source_emb = entity_embeddings.get(rel.source)
        target_emb = entity_embeddings.get(rel.target)
        if not source_emb or not target_emb:
            return None
        cos01 = max(_cosine_similarity(source_emb, target_emb), 0.0)
        return {"weight": cos01}

    async def _merge_cooccurrence_edges(
        self,
        entities: list[_GraphEntity],
        entity_embeddings: dict[str, list[float]],
        project_id: str,
        is_global: bool,
    ) -> None:
        """Write canonical co-occurrence edges over the memory's salient entities.

        Salience: the extractor exposes no per-entity score, so the first
        ``COOCCUR_MAX_ENTITIES`` entities in extractor order are taken. Pairs are
        canonicalized (a < b) by sorting the selected keys before combining.
        """
        salient = entities[:COOCCUR_MAX_ENTITIES]
        keys = sorted({entity.entity_key for entity in salient})
        pairs = list(itertools.combinations(keys, 2))
        if not pairs:
            return
        # graph_edge_weighting gates whether CO_OCCURS carries a weight: when off the
        # edges are densification-only (neutral traversal weight), isolating the
        # densification effect from the weighting effect in the recall benchmark.
        await self._writer.merge_cooccurrence_edges(
            pairs,
            project_id,
            is_global,
            entity_embeddings,
            weighted=self._graph_edge_weighting,
        )

    async def _ensure_graph_schema(self) -> None:
        """Lazily ensure the memory knowledge-graph schema exists."""
        await self._writer.ensure_graph_schema()

    @staticmethod
    def _display_entity_name(name: str) -> str:
        """Normalize an entity name for display while preserving case."""
        return display_entity_name(name)

    def _normalize_entities(
        self,
        entities: list[Entity],
        *,
        project_id: str,
        is_global: bool,
    ) -> list[_GraphEntity]:
        """Normalize and deduplicate extracted entities by stable key."""
        return normalize_entities(entities, project_id=project_id, is_global=is_global)

    def _normalize_relationships(
        self,
        relationships: list[Relationship],
        *,
        entities: list[_GraphEntity],
        project_id: str,
        is_global: bool,
    ) -> list[Relationship]:
        """Normalize relationships to stable entity keys."""
        return normalize_relationships(
            relationships,
            entities=entities,
            project_id=project_id,
            is_global=is_global,
        )

    @staticmethod
    def _failure_result(error: Exception) -> KnowledgeGraphResult:
        """Build a deterministic or retryable failure result."""
        status = (
            KnowledgeGraphStatus.RETRYABLE_FAILURE
            if isinstance(error, FalkorConnectionError)
            else KnowledgeGraphStatus.DETERMINISTIC_FAILURE
        )
        return KnowledgeGraphResult(status=status, errors=[str(error)])

    @staticmethod
    def _cancellation_result(
        error: LLMProviderCancellation,
        *,
        entities: int = 0,
        relationships: int = 0,
    ) -> KnowledgeGraphResult:
        """Build a retryable result for provider shutdown cancellation."""
        return KnowledgeGraphResult(
            status=KnowledgeGraphStatus.RETRYABLE_FAILURE,
            entities_extracted=entities,
            relationships_extracted=relationships,
            errors=[str(error) or error.__class__.__name__],
        )

    @staticmethod
    def _connection_failure_result(
        error: Exception,
        *,
        made_progress: bool,
        partial_errors: list[str],
        entities: int,
        relationships: int,
    ) -> KnowledgeGraphResult:
        """Build a result for a FalkorDB connectivity failure."""
        status = (
            KnowledgeGraphStatus.PARTIAL_FAILURE
            if made_progress or partial_errors
            else KnowledgeGraphStatus.RETRYABLE_FAILURE
        )
        return KnowledgeGraphResult(
            status=status,
            entities_extracted=entities,
            relationships_extracted=relationships,
            errors=[*partial_errors, str(error)],
        )

    async def _extract_entities(self, content: str) -> list[Entity]:
        """Extract entities from content using LLM."""
        return await self._extractor.extract_entities(content)

    async def _extract_relationships(
        self,
        content: str,
        entities: list[Entity],
    ) -> list[Relationship]:
        """Extract relationships between entities using LLM."""
        return await self._extractor.extract_relationships(content, entities)

    async def _delete_outdated_relations(
        self,
        entities: list[_GraphEntity],
        new_relations: list[Relationship],
        project_id: str,
        is_global: bool,
    ) -> None:
        """Find and delete outdated relationships from FalkorDB."""
        entity_keys = [e.entity_key for e in entities]
        if not entity_keys:
            return

        try:
            existing = await self._fetch_existing_relations(entity_keys)
        except FalkorConnectionError:
            return

        if not existing:
            return

        to_delete = await self._extractor.select_outdated_relations(
            entities=entities,
            new_relations=new_relations,
            existing_relations=existing,
        )
        await self._writer.delete_relations(to_delete, project_id, is_global)

    async def _fetch_existing_relations(self, entity_keys: list[str]) -> list[dict[str, str]]:
        """Fetch existing relationships involving the given entities."""
        return await self._writer.fetch_existing_relations(entity_keys)

    async def _link_entities_to_memory(
        self,
        entities: list[_GraphEntity],
        memory_id: str,
        project_id: str,
        is_global: bool,
    ) -> None:
        """Create Memory node and MENTIONED_IN relationships from entities."""
        await self._writer.link_entities_to_memory(
            entities,
            memory_id,
            project_id=project_id,
            is_global=is_global,
        )

    async def remove_memory_from_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
        is_global: bool | None = None,
    ) -> None:
        """Remove a Memory node and all its MENTIONED_IN edges from FalkorDB."""
        await self._maintenance.remove_memory_from_graph(
            memory_id,
            project_id=project_id,
            is_global=is_global,
        )

    async def remove_memories_from_graph(self, memory_ids: set[str]) -> int:
        """Batch-remove Memory nodes and their MENTIONED_IN edges from FalkorDB."""
        return await self._maintenance.remove_memories_from_graph(memory_ids)

    async def get_all_memory_node_ids(self) -> set[str]:
        """Return all memory_id values from Memory nodes in FalkorDB."""
        return await self._maintenance.get_all_memory_node_ids()

    async def remove_orphaned_entities(
        self,
        scope: str = "all",
        project_id: str | None = None,
    ) -> int:
        """Delete Entity nodes with no MENTIONED_IN edges. Return count deleted."""
        return await self._maintenance.remove_orphaned_entities(scope=scope, project_id=project_id)

    async def clear_graph(self, project_id: str | None = None) -> dict[str, int]:
        """Delete all KG projection nodes for a project or all scopes."""
        return await self._maintenance.clear_graph(project_id=project_id)

    async def get_graph_counts(self, project_id: str | None = None) -> dict[str, Any]:
        """Return actual FalkorDB knowledge-graph counts."""
        return await self._falkor.get_graph_counts(project_id=project_id)

    async def recluster_entities(self, project_id: str | None = None) -> ClusterRunResult:
        """Recompute and persist deterministic HDBSCAN entity cluster IDs."""

        async def _recluster() -> ClusterRunResult:
            return await recluster_project_entities(
                self._reader,
                self._writer,
                project_id,
                min_cluster_size=self._cluster_min_cluster_size,
                min_samples=self._cluster_min_samples,
            )

        if self._write_fence is None:
            return await _recluster()
        writer = (
            self._write_fence.writer(project_id)
            if project_id is not None
            else self._write_fence.global_writer()
        )
        async with writer:
            return await _recluster()

    async def clear_project_graph(self, project_id: str) -> dict[str, int]:
        """Delete all Memory nodes for a project, then clean orphaned entities."""
        return await self._maintenance.clear_project_graph(project_id)

    async def clear_project_graph_strict(self, project_id: str) -> dict[str, int]:
        """Delete project graph state while exposing connection failures."""
        return await self._maintenance.clear_project_graph_strict(project_id)

    async def densify_cooccurrence(
        self, project_id: str | None = None
    ) -> CooccurrenceDensifyResult:
        """Bulk-retrofit derived CO_OCCURS edges from MENTIONED_IN structure (no LLM).

        Weighting follows the same ``graph_edge_weighting`` gate as the per-memory
        write path, so densified edges match what the write path would produce.
        """

        async def _densify() -> CooccurrenceDensifyResult:
            await self._ensure_graph_schema()
            return await densify_cooccurrence(
                self._falkor,
                self._writer,
                project_id,
                weighted=self._graph_edge_weighting,
            )

        if self._write_fence is None:
            return await _densify()
        writer = (
            self._write_fence.writer(project_id)
            if project_id is not None
            else self._write_fence.global_writer()
        )
        async with writer:
            return await _densify()

    async def _link_entities_to_code(
        self,
        entities: list[_GraphEntity],
        entity_embeddings: dict[str, list[float]],
        project_id: str,
    ) -> None:
        """Cross-link entities to code symbols via RELATES_TO_CODE edges."""
        await self._code_linker.link_entities_to_code(entities, entity_embeddings, project_id)

    async def _ensure_vector_index(self) -> None:
        """Lazily ensure the entity vector index exists."""
        await self._reader.ensure_vector_index()

    async def search_entities_by_vector(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """Search entities by vector similarity and return with linked memory IDs."""
        return await self._reader.search_entities_by_vector(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
            include_global=include_global,
        )

    async def find_related_memory_ids(
        self,
        entity_keys: list[str],
        max_hops: int = 2,
        limit: int = 20,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> RelatedMemoryTraversal:
        """Traverse from entities through relationships to find related memory IDs."""
        return await self._reader.find_related_memory_ids(
            entity_keys=entity_keys,
            max_hops=max_hops,
            limit=limit,
            project_id=project_id,
            include_global=include_global,
        )

    # -----------------------------------------------------------------------
    # Read path
    # -----------------------------------------------------------------------

    async def get_entity_graph(
        self,
        limit: int = 500,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the entity graph for visualization."""
        return await self._reader.get_entity_graph(limit=limit, project_id=project_id)

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single entity."""
        return await self._reader.get_entity_neighbors(entity_key, project_id=project_id)

    async def search_graph(
        self,
        query: str,
        limit: int = 10,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """Search the knowledge graph for entities matching a query."""
        return await self._reader.search_graph(
            query,
            limit=limit,
            project_id=project_id,
            include_global=include_global,
        )

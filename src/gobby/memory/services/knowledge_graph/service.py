"""Knowledge graph service orchestration facade."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError, FalkorQueryError

from .code_linker import KnowledgeGraphCodeLinker
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
from .reader import KnowledgeGraphReader
from .writer import KnowledgeGraphWriter

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.llm.base import LLMProvider
    from gobby.memory.falkor_client import FalkorClient
    from gobby.memory.vectorstore import VectorStore
    from gobby.prompts.loader import PromptLoader

logger = logging.getLogger(__name__)


class KnowledgeGraphService:
    """Manages knowledge graph extraction, FalkorDB projection, and graph reads.

    Args:
        falkor_client: FalkorDB client for graph operations
        llm_provider: LLM provider for entity/relationship extraction
        embed_fn: Async function to generate embeddings for entity names
        prompt_loader: PromptLoader for rendering extraction prompts
    """

    def __init__(
        self,
        falkor_client: FalkorClient,
        llm_provider: LLMProvider,
        embed_fn: Callable[..., Any],
        prompt_loader: PromptLoader,
        vector_store: VectorStore | None = None,
        code_link_min_score: float = 0.82,
        code_symbol_collection_prefix: str = "code_symbols_",
        embedding_dim: int = 768,
        model: str | None = None,
    ) -> None:
        self._falkor = falkor_client
        self._llm = llm_provider
        self._embed_fn = embed_fn
        self._prompt_loader = prompt_loader
        self._vector_store = vector_store
        self._code_link_min_score = code_link_min_score
        self._code_symbol_collection_prefix = code_symbol_collection_prefix
        self._embedding_dim = embedding_dim
        self._model = model

        self._writer = KnowledgeGraphWriter(falkor_client)
        self._extractor = KnowledgeGraphExtractor(llm_provider, prompt_loader, model=model)
        self._maintenance = KnowledgeGraphMaintenance(falkor_client)
        self._reader = KnowledgeGraphReader(falkor_client, embed_fn, embedding_dim=embedding_dim)
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
        project_id: str | None = None,
    ) -> KnowledgeGraphResult:
        """Extract entities and relationships from content and merge into FalkorDB.

        Pipeline:
        1. Extract entities via LLM
        2. Extract relationships via LLM
        3. Fetch existing relationships for overlap detection
        4. Delete outdated relationships via LLM decision
        5. Merge nodes and relationships into FalkorDB
        6. Add _Entity label for vector index compatibility
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
        except Exception as e:
            logger.warning("Entity extraction failed for memory %s: %s", memory_ref, e)
            return self._failure_result(e)

        entities = self._normalize_entities(extracted_entities, project_id=project_id)
        if not entities:
            return KnowledgeGraphResult(status=KnowledgeGraphStatus.NOOP_NO_ENTITIES)

        partial_errors: list[str] = []
        made_progress = False

        try:
            extracted_relationships = await self._extract_relationships(content, extracted_entities)
        except Exception as e:
            logger.warning("Relationship extraction failed for memory %s: %s", memory_ref, e)
            partial_errors.append(f"relationship_extraction:{e}")
            extracted_relationships = []

        relationships = self._normalize_relationships(
            extracted_relationships,
            entities=entities,
            project_id=project_id,
        )

        try:
            await self._delete_outdated_relations(
                entities=entities,
                new_relations=relationships,
                project_id=project_id,
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

        for rel in relationships:
            try:
                await self._writer.merge_relationship(rel)
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

        entity_embeddings: dict[str, list[float]] = {}
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

        if memory_id:
            try:
                await self._link_entities_to_memory(entities, memory_id, project_id=project_id)
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

        if project_id and self._vector_store and entity_embeddings:
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
        project_id: str | None,
    ) -> list[_GraphEntity]:
        """Normalize and deduplicate extracted entities by stable key."""
        return normalize_entities(entities, project_id=project_id)

    def _normalize_relationships(
        self,
        relationships: list[Relationship],
        *,
        entities: list[_GraphEntity],
        project_id: str | None,
    ) -> list[Relationship]:
        """Normalize relationships to stable entity keys."""
        return normalize_relationships(
            relationships,
            entities=entities,
            project_id=project_id,
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
        project_id: str | None,
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
        await self._writer.delete_relations(to_delete, project_id)

    async def _fetch_existing_relations(self, entity_keys: list[str]) -> list[dict[str, str]]:
        """Fetch existing relationships involving the given entities."""
        return await self._writer.fetch_existing_relations(entity_keys)

    async def _link_entities_to_memory(
        self,
        entities: list[_GraphEntity],
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Create Memory node and MENTIONED_IN relationships from entities."""
        await self._writer.link_entities_to_memory(entities, memory_id, project_id=project_id)

    async def remove_memory_from_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Remove a Memory node and all its MENTIONED_IN edges from FalkorDB."""
        await self._maintenance.remove_memory_from_graph(memory_id, project_id=project_id)

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

    async def clear_project_graph(self, project_id: str) -> dict[str, int]:
        """Delete all Memory nodes for a project, then clean orphaned entities."""
        return await self._maintenance.clear_project_graph(project_id)

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
    ) -> list[dict[str, Any]]:
        """Search entities by vector similarity and return with linked memory IDs."""
        return await self._reader.search_entities_by_vector(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )

    async def find_related_memory_ids(
        self,
        entity_keys: list[str],
        max_hops: int = 2,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[str]:
        """Traverse from entities through relationships to find related memory IDs."""
        return await self._reader.find_related_memory_ids(
            entity_keys=entity_keys,
            max_hops=max_hops,
            limit=limit,
            project_id=project_id,
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

    async def search_graph(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the knowledge graph for entities matching a query."""
        return await self._reader.search_graph(query, limit=limit)

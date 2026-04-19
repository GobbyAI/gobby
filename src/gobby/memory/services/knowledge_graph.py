"""Knowledge graph service for entity/relationship extraction and Neo4j storage.

Extracts entities and relationships from content using LLM prompts,
then merges them into a Neo4j knowledge graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from gobby.memory.identity import entity_key, normalize_entity_name
from gobby.memory.neo4j_client import Neo4jConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.llm.base import LLMProvider
    from gobby.memory.neo4j_client import Neo4jClient
    from gobby.memory.vectorstore import VectorStore
    from gobby.prompts.loader import PromptLoader

logger = logging.getLogger(__name__)
_DISPLAY_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class Entity:
    """An extracted entity from content."""

    name: str
    entity_type: str


@dataclass
class Relationship:
    """An extracted relationship between entities."""

    source: str
    target: str
    relationship: str


class KnowledgeGraphStatus(StrEnum):
    """Status for a knowledge-graph projection attempt."""

    SUCCESS = "success"
    NOOP_NO_ENTITIES = "noop_no_entities"
    PARTIAL_FAILURE = "partial_failure"
    RETRYABLE_FAILURE = "retryable_failure"
    DETERMINISTIC_FAILURE = "deterministic_failure"


@dataclass
class KnowledgeGraphResult:
    """Result of a knowledge-graph projection attempt."""

    status: KnowledgeGraphStatus
    entities_extracted: int = 0
    relationships_extracted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _GraphEntity:
    """Normalized entity record used for Neo4j writes."""

    entity_key: str
    name: str
    entity_type: str
    project_id: str | None
    normalized_name: str


class KnowledgeGraphService:
    """Manages knowledge graph operations: entity/relationship extraction and Neo4j storage.

    Args:
        neo4j_client: Neo4j HTTP client for graph operations
        llm_provider: LLM provider for entity/relationship extraction
        embed_fn: Async function to generate embeddings for entity names
        prompt_loader: PromptLoader for rendering extraction prompts
    """

    def __init__(
        self,
        neo4j_client: Neo4jClient,
        llm_provider: LLMProvider,
        embed_fn: Callable[..., Any],
        prompt_loader: PromptLoader,
        vector_store: VectorStore | None = None,
        code_link_min_score: float = 0.82,
        code_symbol_collection_prefix: str = "code_symbols_",
        embedding_dim: int = 768,
        model: str | None = None,
    ):
        self._neo4j = neo4j_client
        self._llm = llm_provider
        self._embed_fn = embed_fn
        self._prompt_loader = prompt_loader
        self._vector_store = vector_store
        self._code_link_min_score = code_link_min_score
        self._code_symbol_collection_prefix = code_symbol_collection_prefix
        self._embedding_dim = embedding_dim
        self._model = model
        self._graph_schema_ensured = False
        self._graph_schema_lock = asyncio.Lock()
        self._vector_index_ensured = False

    # -----------------------------------------------------------------------
    # Write path
    # -----------------------------------------------------------------------

    async def add_to_graph(
        self,
        content: str,
        memory_id: str | None = None,
        project_id: str | None = None,
    ) -> KnowledgeGraphResult:
        """Extract entities and relationships from content and merge into Neo4j.

        Pipeline:
        1. Extract entities via LLM
        2. Extract relationships via LLM
        3. Fetch existing relationships for overlap detection
        4. Delete outdated relationships via LLM decision
        5. Merge nodes and relationships into Neo4j
        6. Add _Entity label for vector index compatibility
        7. Set embedding vectors on nodes
        8. Link entities to source memory via MENTIONED_IN
        9. Cross-link entities to code symbols via RELATES_TO_CODE
        """
        await self._ensure_graph_schema()

        try:
            extracted_entities = await self._extract_entities(content)
        except Exception as e:
            logger.warning(f"Entity extraction failed: {e}")
            return self._failure_result(e)

        entities = self._normalize_entities(extracted_entities, project_id=project_id)
        if not entities:
            return KnowledgeGraphResult(status=KnowledgeGraphStatus.NOOP_NO_ENTITIES)

        partial_errors: list[str] = []
        made_progress = False

        try:
            extracted_relationships = await self._extract_relationships(content, extracted_entities)
        except Exception as e:
            logger.warning(f"Relationship extraction failed: {e}")
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
            logger.warning(f"Relation cleanup failed: {e}")
            partial_errors.append(f"relation_cleanup:{e}")

        for entity in entities:
            try:
                await self._neo4j.merge_node(
                    entity_key=entity.entity_key,
                    name=entity.name,
                    project_id=entity.project_id,
                    labels=[entity.entity_type.capitalize(), "_Entity"],
                    properties={
                        "entity_type": entity.entity_type,
                        "project_id": entity.project_id,
                    },
                )
                made_progress = True
            except Neo4jConnectionError as e:
                logger.warning(f"Neo4j unreachable during merge_node: {e}")
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(f"Failed to merge node {entity.name}: {e}")
                partial_errors.append(f"merge_node:{entity.name}:{e}")

        for rel in relationships:
            try:
                await self._neo4j.merge_relationship(
                    source_key=rel.source,
                    target_key=rel.target,
                    rel_type=rel.relationship,
                )
                made_progress = True
            except Neo4jConnectionError as e:
                logger.warning(f"Neo4j unreachable during merge_relationship: {e}")
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(f"Failed to merge relationship {rel}: {e}")
                partial_errors.append(f"merge_relationship:{rel.relationship}:{e}")

        entity_embeddings: dict[str, list[float]] = {}
        for entity in entities:
            try:
                embedding = await self._embed_fn(entity.name)
                entity_embeddings[entity.entity_key] = embedding
                await self._neo4j.set_node_vector(
                    entity_key=entity.entity_key,
                    embedding=embedding,
                )
                made_progress = True
            except Neo4jConnectionError as e:
                logger.warning(f"Neo4j unreachable during set_node_vector: {e}")
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(f"Failed to set embedding for {entity.name}: {e}")
                partial_errors.append(f"set_embedding:{entity.name}:{e}")

        if memory_id:
            try:
                await self._link_entities_to_memory(entities, memory_id, project_id=project_id)
                made_progress = True
            except Neo4jConnectionError as e:
                logger.warning(f"Neo4j unreachable during MENTIONED_IN link: {e}")
                return self._connection_failure_result(
                    e,
                    made_progress=made_progress,
                    partial_errors=partial_errors,
                    entities=len(entities),
                    relationships=len(relationships),
                )
            except Exception as e:
                logger.warning(f"Failed to link entities to memory {memory_id}: {e}")
                partial_errors.append(f"mentioned_in:{memory_id}:{e}")

        if project_id and self._vector_store and entity_embeddings:
            try:
                await self._link_entities_to_code(entities, entity_embeddings, project_id)
                made_progress = True
            except Exception as e:
                logger.warning(f"Failed to link entities to code for project {project_id}: {e}")
                partial_errors.append(f"relates_to_code:{project_id}:{e}")

        status = (
            KnowledgeGraphStatus.PARTIAL_FAILURE
            if partial_errors
            else KnowledgeGraphStatus.SUCCESS
        )
        return KnowledgeGraphResult(
            status=status,
            entities_extracted=len(entities),
            relationships_extracted=len(relationships),
            errors=partial_errors,
        )

    async def _ensure_graph_schema(self) -> None:
        """Lazily ensure the memory knowledge-graph schema exists."""
        if self._graph_schema_ensured:
            return
        async with self._graph_schema_lock:
            if self._graph_schema_ensured:
                return
            try:
                await self._neo4j.ensure_memory_graph_schema()
                self._graph_schema_ensured = True
            except Neo4jConnectionError:
                logger.debug("Neo4j unreachable, skipping knowledge-graph schema creation")
            except Exception as e:
                logger.warning(f"Failed to ensure knowledge-graph schema: {e}")

    @staticmethod
    def _display_entity_name(name: str) -> str:
        """Normalize an entity name for display while preserving case."""
        normalized = unicodedata.normalize("NFKC", name)
        normalized = normalized.strip()
        return _DISPLAY_WHITESPACE_RE.sub(" ", normalized)

    def _normalize_entities(
        self,
        entities: list[Entity],
        *,
        project_id: str | None,
    ) -> list[_GraphEntity]:
        """Normalize and deduplicate extracted entities by stable key."""
        deduped: dict[str, _GraphEntity] = {}
        for entity in entities:
            display_name = self._display_entity_name(entity.name)
            normalized_name = normalize_entity_name(display_name)
            if not normalized_name:
                continue
            key = entity_key(project_id, display_name)
            if key in deduped:
                continue
            deduped[key] = _GraphEntity(
                entity_key=key,
                name=display_name,
                entity_type=entity.entity_type,
                project_id=project_id,
                normalized_name=normalized_name,
            )
        return list(deduped.values())

    def _normalize_relationships(
        self,
        relationships: list[Relationship],
        *,
        entities: list[_GraphEntity],
        project_id: str | None,
    ) -> list[Relationship]:
        """Normalize relationships to stable entity keys."""
        entity_map = {entity.entity_key: entity for entity in entities}
        deduped: dict[tuple[str, str, str], Relationship] = {}
        for relationship in relationships:
            source_key = entity_key(project_id, relationship.source)
            target_key = entity_key(project_id, relationship.target)
            if source_key not in entity_map or target_key not in entity_map:
                continue
            dedupe_key = (source_key, relationship.relationship, target_key)
            deduped[dedupe_key] = Relationship(
                source=source_key,
                target=target_key,
                relationship=relationship.relationship,
            )
        return list(deduped.values())

    @staticmethod
    def _failure_result(error: Exception) -> KnowledgeGraphResult:
        """Build a deterministic or retryable failure result."""
        status = (
            KnowledgeGraphStatus.RETRYABLE_FAILURE
            if isinstance(error, Neo4jConnectionError)
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
        """Build a result for a Neo4j connectivity failure."""
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
        prompt = self._prompt_loader.render(
            "memory/extract_entities",
            {"content": content},
        )
        response = await self._llm.generate_json(prompt, model=self._model)
        raw_entities = response.get("entities", [])
        logger.debug(
            "Entity extraction response keys: %s, raw_entities count: %d",
            list(response.keys()),
            len(raw_entities),
        )
        entities = [
            Entity(name=e["entity"], entity_type=e["entity_type"])
            for e in raw_entities
            if isinstance(e, dict) and "entity" in e and "entity_type" in e
        ]
        dropped = len(raw_entities) - len(entities)
        if dropped:
            logger.warning(
                "Entity extraction dropped %d malformed entries from %d raw entities",
                dropped,
                len(raw_entities),
            )
        return entities

    async def _extract_relationships(
        self, content: str, entities: list[Entity]
    ) -> list[Relationship]:
        """Extract relationships between entities using LLM."""
        entities_json = json.dumps(
            [{"entity": e.name, "entity_type": e.entity_type} for e in entities]
        )
        prompt = self._prompt_loader.render(
            "memory/extract_relations",
            {"content": content, "entities": entities_json},
        )
        response = await self._llm.generate_json(prompt, model=self._model)
        raw_relations = response.get("relations", [])
        return [
            Relationship(
                source=r["source"],
                target=r["destination"],
                relationship=r["relationship"],
            )
            for r in raw_relations
            if isinstance(r, dict)
            and all(k in r for k in ("source", "relationship", "destination"))
        ]

    async def _delete_outdated_relations(
        self,
        entities: list[_GraphEntity],
        new_relations: list[Relationship],
        project_id: str | None,
    ) -> None:
        """Find and delete outdated relationships from Neo4j."""
        entity_keys = [e.entity_key for e in entities]
        if not entity_keys:
            return

        try:
            existing = await self._fetch_existing_relations(entity_keys)
        except Neo4jConnectionError:
            return

        if not existing:
            return

        name_by_key = {entity.entity_key: entity.name for entity in entities}
        new_relations_json = json.dumps(
            [
                {
                    "source": name_by_key.get(r.source, r.source),
                    "relationship": r.relationship,
                    "destination": name_by_key.get(r.target, r.target),
                }
                for r in new_relations
            ]
        )
        existing_json = json.dumps(existing)

        prompt = self._prompt_loader.render(
            "memory/delete_relations",
            {"existing_relations": existing_json, "new_relations": new_relations_json},
        )
        response = await self._llm.generate_json(prompt, model=self._model)
        to_delete = response.get("relations_to_delete", [])

        for rel in to_delete:
            if not isinstance(rel, dict):
                continue
            source = rel.get("source", "")
            relationship = rel.get("relationship", "")
            destination = rel.get("destination", "")
            if source and relationship and destination:
                try:
                    await self._neo4j.query(
                        "MATCH (a:_Entity {entity_key: $source_key})-[r]->"
                        "(b:_Entity {entity_key: $target_key}) "
                        "WHERE type(r) = $rel_type DELETE r",
                        {
                            "source_key": entity_key(project_id, source),
                            "target_key": entity_key(project_id, destination),
                            "rel_type": relationship,
                        },
                    )
                except Neo4jConnectionError as e:
                    logger.warning(f"Neo4j unreachable during relation delete: {e}")
                    return

    async def _fetch_existing_relations(self, entity_keys: list[str]) -> list[dict[str, str]]:
        """Fetch existing relationships involving the given entities."""
        rows = await self._neo4j.query(
            "MATCH (a:_Entity)-[r]->(b:_Entity) "
            "WHERE a.entity_key IN $keys OR b.entity_key IN $keys "
            "RETURN a.name AS source, type(r) AS rel_type, b.name AS target",
            {"keys": entity_keys},
        )
        return [
            {"source": r["source"], "relationship": r["rel_type"], "destination": r["target"]}
            for r in rows
        ]

    async def _link_entities_to_memory(
        self,
        entities: list[_GraphEntity],
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Create Memory node and MENTIONED_IN relationships from entities."""
        await self._neo4j.query(
            "MERGE (m:Memory {memory_id: $memory_id}) "
            "ON CREATE SET m.project_id = $project_id, "
            "m.created_at = datetime(), m.updated_at = datetime() "
            "ON MATCH SET m.project_id = coalesce($project_id, m.project_id), "
            "m.updated_at = datetime()",
            {"memory_id": memory_id, "project_id": project_id},
        )
        for entity in entities:
            await self._neo4j.query(
                "MATCH (e:_Entity {entity_key: $entity_key}), "
                "(m:Memory {memory_id: $memory_id}) "
                "MERGE (e)-[:MENTIONED_IN]->(m)",
                {"entity_key": entity.entity_key, "memory_id": memory_id},
            )

    async def remove_memory_from_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Remove a Memory node and all its MENTIONED_IN edges from Neo4j."""
        try:
            memory_scope = project_id
            if memory_scope is None:
                scope_rows = await self._neo4j.query(
                    "MATCH (m:Memory {memory_id: $memory_id}) RETURN m.project_id AS project_id",
                    {"memory_id": memory_id},
                )
                if scope_rows:
                    memory_scope = scope_rows[0].get("project_id")
            await self._neo4j.query(
                "MATCH (m:Memory {memory_id: $memory_id}) DETACH DELETE m",
                {"memory_id": memory_id},
            )
            await self.remove_orphaned_entities(
                scope="project" if memory_scope is not None else "global",
                project_id=memory_scope,
            )
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during memory deletion: {e}")
        except Exception as e:
            logger.warning(f"Failed to delete Memory node {memory_id} from graph: {e}")

    async def remove_memories_from_graph(self, memory_ids: set[str]) -> int:
        """Batch-remove Memory nodes and their MENTIONED_IN edges from Neo4j.

        Returns the number of nodes deleted.
        """
        if not memory_ids:
            return 0
        try:
            scope_rows = await self._neo4j.query(
                "MATCH (m:Memory) WHERE m.memory_id IN $ids "
                "RETURN m.memory_id AS memory_id, m.project_id AS project_id",
                {"ids": list(memory_ids)},
            )
            impacted_scopes = {row.get("project_id") for row in scope_rows}
            records = await self._neo4j.query(
                "MATCH (m:Memory) WHERE m.memory_id IN $ids "
                "WITH count(m) AS total, collect(m) AS nodes "
                "UNWIND nodes AS n DETACH DELETE n "
                "RETURN total AS deleted",
                {"ids": list(memory_ids)},
            )
            for scope in impacted_scopes:
                await self.remove_orphaned_entities(
                    scope="project" if scope is not None else "global",
                    project_id=scope,
                )
            return int(records[0]["deleted"]) if records else 0
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during batch memory deletion: {e}")
            return 0
        except Exception as e:
            logger.warning(f"Failed to batch-delete {len(memory_ids)} Memory nodes: {e}")
            return 0

    async def get_all_memory_node_ids(self) -> set[str]:
        """Return all memory_id values from Memory nodes in Neo4j."""
        try:
            records = await self._neo4j.query(
                "MATCH (m:Memory) RETURN m.memory_id AS id",
                {},
            )
            return {r["id"] for r in records if r.get("id")}
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during memory node enumeration: {e}")
            return set()
        except Exception as e:
            logger.warning(f"Failed to enumerate Memory nodes: {e}")
            return set()

    async def remove_orphaned_entities(
        self,
        scope: str = "all",
        project_id: str | None = None,
    ) -> int:
        """Delete Entity nodes with no MENTIONED_IN edges. Return count deleted."""
        if scope == "project":
            if project_id is None:
                raise ValueError("project_id is required when scope='project'")
            where_clause = (
                "e.project_id = $project_id AND NOT (e)-[:MENTIONED_IN]->(:Memory)"
            )
            params: dict[str, Any] = {"project_id": project_id}
        elif scope == "global":
            where_clause = "e.project_id IS NULL AND NOT (e)-[:MENTIONED_IN]->(:Memory)"
            params = {}
        elif scope == "all":
            where_clause = "NOT (e)-[:MENTIONED_IN]->(:Memory)"
            params = {}
        else:
            raise ValueError(f"Unsupported orphan cleanup scope: {scope}")

        try:
            count_records = await self._neo4j.query(
                f"MATCH (e:_Entity) WHERE {where_clause} RETURN count(e) AS total",
                params,
            )
            total = int(count_records[0]["total"]) if count_records else 0
            if total > 0:
                await self._neo4j.query(
                    f"MATCH (e:_Entity) WHERE {where_clause} DETACH DELETE e",
                    params,
                )
            return total
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during orphan entity cleanup: {e}")
            return 0
        except Exception as e:
            logger.warning(f"Failed to remove orphaned entities: {e}")
            return 0

    async def clear_graph(self, project_id: str | None = None) -> dict[str, int]:
        """Delete all KG projection nodes for a project or all scopes."""
        try:
            if project_id is None:
                memory_count_rows = await self._neo4j.query(
                    "MATCH (m:Memory) RETURN count(m) AS total",
                    {},
                )
                entity_count_rows = await self._neo4j.query(
                    "MATCH (e:_Entity) RETURN count(e) AS total",
                    {},
                )
                await self._neo4j.query(
                    "MATCH (n) WHERE n:Memory OR n:_Entity DETACH DELETE n",
                    {},
                )
                return {
                    "memories_deleted": int(memory_count_rows[0]["total"]) if memory_count_rows else 0,
                    "entities_deleted": int(entity_count_rows[0]["total"]) if entity_count_rows else 0,
                }
            return await self.clear_project_graph(project_id)
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during clear_graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}
        except Exception as e:
            logger.warning(f"Failed to clear graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}

    async def clear_project_graph(self, project_id: str) -> dict[str, int]:
        """Delete all Memory nodes (and relationships) for a project, then clean orphaned entities.

        Args:
            project_id: Required — scopes the clear to a single project.

        Returns:
            Dict with memories_deleted and entities_deleted counts.
        """
        try:
            records = await self._neo4j.query(
                "MATCH (m:Memory {project_id: $project_id}) "
                "WITH count(m) AS total, collect(m) AS nodes "
                "UNWIND nodes AS n DETACH DELETE n "
                "RETURN total AS deleted",
                {"project_id": project_id},
            )
            memories_deleted = int(records[0]["deleted"]) if records else 0
            entities_deleted = await self.remove_orphaned_entities(
                scope="project",
                project_id=project_id,
            )
            return {
                "memories_deleted": memories_deleted,
                "entities_deleted": entities_deleted,
            }
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during clear_project_graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}
        except Exception as e:
            logger.warning(f"Failed to clear project graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}

    async def _link_entities_to_code(
        self,
        entities: list[_GraphEntity],
        entity_embeddings: dict[str, list[float]],
        project_id: str,
    ) -> None:
        """Cross-link entities to code symbols via RELATES_TO_CODE edges.

        Searches the code symbol Qdrant collection for each entity embedding
        and writes edges to Neo4j for matches above the similarity threshold.
        Gracefully no-ops if the collection doesn't exist.
        """
        assert self._vector_store is not None  # noqa: S101

        collection = f"{self._code_symbol_collection_prefix}{project_id}"
        links: list[dict[str, Any]] = []

        for entity in entities:
            embedding = entity_embeddings.get(entity.entity_key)
            if not embedding:
                continue
            try:
                results = await self._vector_store.search(
                    query_embedding=embedding,
                    collection_name=collection,
                    limit=3,
                )
                for symbol_id, score in results:
                    if score >= self._code_link_min_score:
                        links.append(
                            {
                                "entity_key": entity.entity_key,
                                "symbol_id": symbol_id,
                                "score": score,
                            }
                        )
            except Exception as e:
                logger.debug(f"Code symbol search failed for entity '{entity.name}': {e}")
                continue  # Collection likely missing — skip this entity

        if not links:
            return

        try:
            await self._neo4j.query(
                "UNWIND $links AS link "
                "MATCH (e:_Entity {entity_key: link.entity_key}) "
                "MATCH (c:CodeSymbol {id: link.symbol_id, project: $project_id}) "
                "MERGE (e)-[r:RELATES_TO_CODE]->(c) "
                "SET r.score = link.score, r.updated_at = datetime()",
                {"links": links, "project_id": project_id},
            )
            logger.debug(f"Wrote {len(links)} RELATES_TO_CODE edges for project {project_id}")
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during RELATES_TO_CODE write: {e}")
        except Exception as e:
            logger.warning(f"Failed to write RELATES_TO_CODE edges: {e}")

    async def _ensure_vector_index(self) -> None:
        """Lazily ensure the entity vector index exists."""
        if self._vector_index_ensured:
            return
        try:
            await self._neo4j.ensure_vector_index(dimensions=self._embedding_dim)
            self._vector_index_ensured = True
        except Neo4jConnectionError:
            logger.debug("Neo4j unreachable, skipping vector index creation")
        except Exception as e:
            logger.warning(f"Failed to ensure vector index: {e}")

    async def search_entities_by_vector(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search entities by vector similarity and return with linked memory IDs.

        Returns:
            List of dicts with name, labels, score, and memory_ids
        """
        await self._ensure_vector_index()

        try:
            entity_rows = await self._neo4j.vector_search(
                query_embedding=query_embedding,
                limit=limit,
                min_score=min_score,
                project_id=project_id,
            )

            if not entity_rows:
                return []

            entity_keys = [r.get("entity_key", "") for r in entity_rows if r.get("entity_key")]
            memory_map: dict[str, list[str]] = {key: [] for key in entity_keys}

            if entity_keys:
                try:
                    mem_rows = await self._neo4j.query(
                        "UNWIND $entity_keys AS entity_key "
                        "MATCH (e:_Entity {entity_key: entity_key})-[:MENTIONED_IN]->(m:Memory) "
                        "WHERE m.project_id = $project_id "
                        "OR ($project_id IS NULL AND m.project_id IS NULL) "
                        "RETURN entity_key, m.memory_id AS memory_id",
                        {"entity_keys": entity_keys, "project_id": project_id},
                    )
                    for r in mem_rows:
                        key = r.get("entity_key", "")
                        mid = r.get("memory_id")
                        if key in memory_map and mid:
                            memory_map[key].append(mid)
                except Exception as e:
                    logger.debug(f"Failed to batch-fetch memory links: {e}")

            results = []
            for row in entity_rows:
                key = row.get("entity_key", "")
                name = row.get("name", "")
                if not key or not name:
                    continue
                results.append(
                    {
                        "entity_key": key,
                        "name": name,
                        "entity_type": row.get("entity_type") or "entity",
                        "project_id": row.get("project_id"),
                        "labels": row.get("labels", []),
                        "score": row.get("score", 0.0),
                        "memory_ids": memory_map.get(key, []),
                    }
                )

            return results

        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during entity vector search: {e}")
            return []
        except Exception as e:
            logger.warning(f"Entity vector search failed: {e}")
            return []

    async def find_related_memory_ids(
        self,
        entity_keys: list[str],
        max_hops: int = 2,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[str]:
        """Traverse from entities through relationships to find related memory IDs.

        Args:
            entity_keys: Starting entity keys
            max_hops: Maximum relationship hops (1-3)
            limit: Maximum memory IDs to return

        Returns:
            List of memory IDs found via graph traversal
        """
        if not entity_keys:
            return []

        max_hops = max(1, min(max_hops, 3))

        try:
            rows = await self._neo4j.query(
                "UNWIND $entity_keys AS entity_key "
                f"MATCH (start:_Entity {{entity_key: entity_key}})-[*1..{max_hops}]-(related:_Entity)"
                "-[:MENTIONED_IN]->(m:Memory) "
                "WHERE m.project_id = $project_id "
                "OR ($project_id IS NULL AND m.project_id IS NULL) "
                "RETURN DISTINCT m.memory_id AS memory_id LIMIT $limit",
                {"entity_keys": entity_keys, "limit": limit, "project_id": project_id},
            )
            return [r["memory_id"] for r in rows if r.get("memory_id")]
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during graph traversal: {e}")
            return []
        except Exception as e:
            logger.warning(f"Graph traversal failed: {e}")
            return []

    # -----------------------------------------------------------------------
    # Read path
    # -----------------------------------------------------------------------

    async def get_entity_graph(
        self,
        limit: int = 500,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the entity graph for visualization.

        Returns None if Neo4j is unreachable.
        """
        try:
            return await self._neo4j.get_entity_graph(limit=limit, project_id=project_id)
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"Neo4j query failed: {e}")
            return None

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single entity.

        Returns None if Neo4j is unreachable.
        """
        try:
            return await self._neo4j.get_entity_neighbors(entity_key, project_id=project_id)
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"Neo4j query failed: {e}")
            return None

    async def search_graph(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the knowledge graph for entities matching a query.

        Tries vector search first (if embedding function available), falls back
        to substring match. Returns empty list if Neo4j is unreachable.
        """
        # Try vector search first
        if self._embed_fn is not None:
            try:
                embedding = await self._embed_fn(query, is_query=True)
                results = await self.search_entities_by_vector(
                    query_embedding=embedding,
                    limit=limit,
                    min_score=0.3,
                )
                if results:
                    return [
                        {
                            "entity_key": r["entity_key"],
                            "name": r["name"],
                            "entity_type": r.get("entity_type") or "entity",
                            "project_id": r.get("project_id"),
                            "labels": r["labels"],
                            "score": r["score"],
                        }
                        for r in results
                    ]
            except Exception as e:
                logger.debug(f"Vector graph search failed, falling back to substring: {e}")

        # Fallback: substring match
        try:
            rows = await self._neo4j.query(
                "MATCH (n:_Entity) WHERE toLower(n.name) CONTAINS toLower($query) "
                "RETURN n.entity_key AS entity_key, n.name AS name, "
                "n.entity_type AS entity_type, n.project_id AS project_id, "
                "labels(n) AS labels, properties(n) AS props "
                "LIMIT $limit",
                {"query": query, "limit": limit},
            )
            return rows
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable: {e}")
            return []
        except Exception as e:
            logger.warning(f"Graph search failed: {e}")
            return []

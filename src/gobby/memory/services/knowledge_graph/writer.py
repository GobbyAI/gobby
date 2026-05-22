"""FalkorDB write helpers for the memory knowledge graph."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError
from gobby.memory.identity import entity_key

from .models import Relationship, _GraphEntity

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient

logger = logging.getLogger(__name__)


class KnowledgeGraphWriter:
    """Owns FalkorDB schema and write mechanics for graph projection."""

    def __init__(self, falkor_client: FalkorClient) -> None:
        self._falkor = falkor_client
        self._graph_schema_ensured = False
        self._graph_schema_lock = asyncio.Lock()

    @property
    def graph_schema_ensured(self) -> bool:
        return self._graph_schema_ensured

    @graph_schema_ensured.setter
    def graph_schema_ensured(self, value: bool) -> None:
        self._graph_schema_ensured = value

    @property
    def graph_schema_lock(self) -> asyncio.Lock:
        return self._graph_schema_lock

    async def ensure_graph_schema(self) -> None:
        """Lazily ensure the memory knowledge-graph schema exists."""
        if self._graph_schema_ensured:
            return
        async with self._graph_schema_lock:
            if self._graph_schema_ensured:
                return
            try:
                await self._falkor.ensure_memory_graph_schema()
                self._graph_schema_ensured = True
            except FalkorConnectionError:
                logger.debug("FalkorDB unreachable, skipping knowledge-graph schema creation")
            except Exception as e:
                logger.warning(f"Failed to ensure knowledge-graph schema: {e}")

    async def merge_entity(self, entity: _GraphEntity) -> None:
        """Merge a normalized entity node."""
        await self._falkor.merge_node(
            entity_key=entity.entity_key,
            name=entity.name,
            project_id=entity.project_id,
            labels=[entity.entity_type.capitalize(), "_Entity"],
            properties={
                "entity_type": entity.entity_type,
                "project_id": entity.project_id,
            },
        )

    async def merge_relationship(self, relationship: Relationship) -> None:
        """Merge an entity relationship."""
        await self._falkor.merge_relationship(
            source_key=relationship.source,
            target_key=relationship.target,
            rel_type=relationship.relationship,
        )

    async def set_entity_vector(self, entity_key: str, embedding: list[float]) -> None:
        """Set an entity node embedding vector."""
        await self._falkor.set_node_vector(
            entity_key=entity_key,
            embedding=embedding,
        )

    async def fetch_existing_relations(self, entity_keys: list[str]) -> list[dict[str, str]]:
        """Fetch existing relationships involving the given entities."""
        rows = await self._falkor.query(
            "MATCH (a:_Entity)-[r]->(b:_Entity) "
            "WHERE a.entity_key IN $keys OR b.entity_key IN $keys "
            "RETURN a.name AS source, type(r) AS rel_type, b.name AS target",
            {"keys": entity_keys},
        )
        return [
            {"source": r["source"], "relationship": r["rel_type"], "destination": r["target"]}
            for r in rows
        ]

    async def delete_relations(
        self,
        relations: list[dict[str, Any]],
        project_id: str | None,
    ) -> list[dict[str, Any]]:
        """Delete selected relationships from FalkorDB and return failed entries."""
        failures: list[dict[str, Any]] = []
        for rel in relations:
            source = rel.get("source", "")
            relationship = rel.get("relationship", "")
            destination = rel.get("destination", "")
            if not (source and relationship and destination):
                logger.warning("Skipping malformed relation delete request: %s", rel)
                failures.append(
                    {"relation": rel, "error": "missing source/relationship/destination"}
                )
                continue
            try:
                await self._falkor.query(
                    "MATCH (a:_Entity {entity_key: $source_key})-[r]->"
                    "(b:_Entity {entity_key: $target_key}) "
                    "WHERE type(r) = $rel_type DELETE r",
                    {
                        "source_key": entity_key(project_id, source),
                        "target_key": entity_key(project_id, destination),
                        "rel_type": relationship,
                    },
                )
            except FalkorConnectionError as e:
                logger.warning("FalkorDB unreachable during relation delete: %s", e)
                failures.append({"relation": rel, "error": str(e)})
            except Exception as e:
                logger.warning("Failed to delete relation %s: %s", rel, e)
                failures.append({"relation": rel, "error": str(e)})
        return failures

    async def link_entities_to_memory(
        self,
        entities: list[_GraphEntity],
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Create Memory node and MENTIONED_IN relationships from entities."""
        await self._falkor.query(
            "MERGE (m:Memory {memory_id: $memory_id}) "
            "ON CREATE SET m.project_id = $project_id, "
            "m.created_at = timestamp(), m.updated_at = timestamp() "
            "ON MATCH SET m.project_id = coalesce($project_id, m.project_id), "
            "m.updated_at = timestamp()",
            {"memory_id": memory_id, "project_id": project_id},
        )
        entity_keys = [entity.entity_key for entity in entities]
        if not entity_keys:
            return
        await self._falkor.query(
            "UNWIND $entity_keys AS entity_key "
            "MATCH (e:_Entity {entity_key: entity_key}), "
            "(m:Memory {memory_id: $memory_id}) "
            "MERGE (e)-[:MENTIONED_IN]->(m)",
            {"entity_keys": entity_keys, "memory_id": memory_id},
        )

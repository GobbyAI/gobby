"""Maintenance operations for memory knowledge graph projection nodes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient

logger = logging.getLogger(__name__)


class KnowledgeGraphMaintenance:
    """Owns cleanup and clear operations for Memory and _Entity nodes."""

    def __init__(self, falkor_client: FalkorClient) -> None:
        self._falkor = falkor_client

    async def remove_memory_from_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Remove a Memory node and all its MENTIONED_IN edges from FalkorDB."""
        try:
            memory_scope = project_id
            if memory_scope is None:
                scope_rows = await self._falkor.query(
                    "MATCH (m:Memory {memory_id: $memory_id}) RETURN m.project_id AS project_id",
                    {"memory_id": memory_id},
                )
                if scope_rows:
                    memory_scope = scope_rows[0].get("project_id")
            await self._falkor.query(
                "MATCH (m:Memory {memory_id: $memory_id}) DETACH DELETE m",
                {"memory_id": memory_id},
            )
            await self.remove_orphaned_entities(
                scope="project" if memory_scope is not None else "global",
                project_id=memory_scope,
            )
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during memory deletion: {e}")
        except Exception as e:
            logger.warning(f"Failed to delete Memory node {memory_id} from graph: {e}")

    async def remove_memories_from_graph(self, memory_ids: set[str]) -> int:
        """Batch-remove Memory nodes and their MENTIONED_IN edges from FalkorDB.

        Returns the number of nodes deleted.
        """
        if not memory_ids:
            return 0
        try:
            scope_rows = await self._falkor.query(
                "MATCH (m:Memory) WHERE m.memory_id IN $ids "
                "RETURN m.memory_id AS memory_id, m.project_id AS project_id",
                {"ids": list(memory_ids)},
            )
            impacted_scopes = {row.get("project_id") for row in scope_rows}
            records = await self._falkor.query(
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
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during batch memory deletion: {e}")
            return 0
        except Exception as e:
            logger.warning(f"Failed to batch-delete {len(memory_ids)} Memory nodes: {e}")
            return 0

    async def get_all_memory_node_ids(self) -> set[str]:
        """Return all memory_id values from Memory nodes in FalkorDB."""
        try:
            records = await self._falkor.query(
                "MATCH (m:Memory) RETURN m.memory_id AS id",
                {},
            )
            return {r["id"] for r in records if r.get("id")}
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during memory node enumeration: {e}")
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
            where_clause = "e.project_id = $project_id AND NOT (e)-[:MENTIONED_IN]->(:Memory)"
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
            count_records = await self._falkor.query(
                f"MATCH (e:_Entity) WHERE {where_clause} RETURN count(e) AS total",
                params,
            )
            total = int(count_records[0]["total"]) if count_records else 0
            if total > 0:
                await self._falkor.query(
                    f"MATCH (e:_Entity) WHERE {where_clause} DETACH DELETE e",
                    params,
                )
            return total
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during orphan entity cleanup: {e}")
            return 0
        except Exception as e:
            logger.warning(f"Failed to remove orphaned entities: {e}")
            return 0

    async def clear_graph(self, project_id: str | None = None) -> dict[str, int]:
        """Delete all KG projection nodes for a project or all scopes."""
        try:
            if project_id is None:
                memory_count_rows = await self._falkor.query(
                    "MATCH (m:Memory) RETURN count(m) AS total",
                    {},
                )
                entity_count_rows = await self._falkor.query(
                    "MATCH (e:_Entity) RETURN count(e) AS total",
                    {},
                )
                await self._falkor.query(
                    "MATCH (n) WHERE n:Memory OR n:_Entity DETACH DELETE n",
                    {},
                )
                return {
                    "memories_deleted": int(memory_count_rows[0]["total"])
                    if memory_count_rows
                    else 0,
                    "entities_deleted": int(entity_count_rows[0]["total"])
                    if entity_count_rows
                    else 0,
                }
            return await self.clear_project_graph(project_id)
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during clear_graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}
        except Exception as e:
            logger.warning(f"Failed to clear graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}

    async def clear_project_graph(self, project_id: str) -> dict[str, int]:
        """Delete all Memory nodes for a project, then clean orphaned entities."""
        try:
            records = await self._falkor.query(
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
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during clear_project_graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}
        except Exception as e:
            logger.warning(f"Failed to clear project graph: {e}")
            return {"memories_deleted": 0, "entities_deleted": 0}

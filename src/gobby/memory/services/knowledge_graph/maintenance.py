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
        is_global: bool | None = None,
    ) -> None:
        """Remove a Memory node and all its MENTIONED_IN edges from FalkorDB."""
        try:
            memory_scope = project_id
            global_scope = is_global
            if memory_scope is None or global_scope is None:
                scope_rows = await self._falkor.query(
                    "MATCH (m:Memory {memory_id: $memory_id}) "
                    "RETURN m.project_id AS project_id, m.is_global AS is_global",
                    {"memory_id": memory_id},
                )
                if scope_rows:
                    memory_scope = scope_rows[0].get("project_id")
                    global_scope = bool(scope_rows[0].get("is_global"))
            await self._falkor.query(
                "MATCH (m:Memory {memory_id: $memory_id}) DETACH DELETE m",
                {"memory_id": memory_id},
            )
            if memory_scope is None and global_scope is None:
                return
            await self.remove_orphaned_entities(
                scope="global" if global_scope else "project",
                project_id=memory_scope,
            )
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable during memory deletion: %s", e)
        except Exception as e:
            logger.warning("Failed to delete Memory node %s from graph: %s", memory_id, e)

    async def remove_memories_from_graph(self, memory_ids: set[str]) -> int:
        """Batch-remove Memory nodes and their MENTIONED_IN edges from FalkorDB.

        Returns the number of nodes deleted.
        """
        if not memory_ids:
            return 0
        try:
            scope_rows = await self._falkor.query(
                "MATCH (m:Memory) WHERE m.memory_id IN $ids "
                "RETURN m.memory_id AS memory_id, m.project_id AS project_id, "
                "m.is_global AS is_global",
                {"ids": list(memory_ids)},
            )
            impacted_scopes = {
                (row.get("project_id"), bool(row.get("is_global"))) for row in scope_rows
            }
            records = await self._falkor.query(
                "MATCH (m:Memory) WHERE m.memory_id IN $ids "
                "WITH count(m) AS total, collect(m) AS nodes "
                "UNWIND nodes AS n DETACH DELETE n "
                "RETURN total AS deleted",
                {"ids": list(memory_ids)},
            )
            for project_id, is_global in impacted_scopes:
                await self.remove_orphaned_entities(
                    scope="global" if is_global else "project",
                    project_id=project_id,
                )
            return int(records[0]["deleted"]) if records else 0
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable during batch memory deletion: %s", e)
            return 0
        except Exception as e:
            logger.warning("Failed to batch-delete %s Memory nodes: %s", len(memory_ids), e)
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
            logger.warning("FalkorDB unreachable during memory node enumeration: %s", e)
            return set()
        except Exception as e:
            logger.warning("Failed to enumerate Memory nodes: %s", e)
            return set()

    async def remove_orphaned_entities(
        self,
        scope: str = "all",
        project_id: str | None = None,
    ) -> int:
        """Delete entities with neither memory nor code-symbol backing edges."""
        try:
            return await self._remove_orphaned_entities_strict(
                scope=scope,
                project_id=project_id,
            )
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable during orphan entity cleanup: %s", e)
            return 0
        except Exception as e:
            logger.warning("Failed to remove orphaned entities: %s", e)
            return 0

    async def _remove_orphaned_entities_strict(
        self,
        *,
        scope: str,
        project_id: str | None,
    ) -> int:
        """Delete orphan entities while propagating graph failures."""
        orphan_predicate = (
            "NOT (e)-[:MENTIONED_IN]->(:Memory) AND NOT (e)-[:RELATES_TO_CODE]->(:CodeSymbol)"
        )
        if scope == "project":
            if project_id is None:
                raise ValueError("project_id is required when scope='project'")
            where_clause = (
                f"e.project_id = $project_id AND e.is_global = false AND {orphan_predicate}"
            )
            params: dict[str, Any] = {"project_id": project_id}
        elif scope == "global":
            where_clause = f"e.is_global = true AND {orphan_predicate}"
            params = {}
        elif scope == "all":
            where_clause = orphan_predicate
            params = {}
        else:
            raise ValueError(f"Unsupported orphan cleanup scope: {scope}")

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
            logger.warning("FalkorDB unreachable during clear_graph: %s", e)
            return {"memories_deleted": 0, "entities_deleted": 0}
        except Exception as e:
            logger.warning("Failed to clear graph: %s", e)
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
            logger.warning("FalkorDB unreachable during clear_project_graph: %s", e)
            return {"memories_deleted": 0, "entities_deleted": 0}
        except Exception as e:
            logger.warning("Failed to clear project graph: %s", e)
            return {"memories_deleted": 0, "entities_deleted": 0}

    async def clear_project_graph_strict(self, project_id: str) -> dict[str, int]:
        """Delete project graph state and expose failures to retrying callers."""
        records = await self._falkor.query(
            "MATCH (m:Memory {project_id: $project_id}) "
            "WITH count(m) AS total, collect(m) AS nodes "
            "UNWIND nodes AS n DETACH DELETE n "
            "RETURN total AS deleted",
            {"project_id": project_id},
        )
        memories_deleted = int(records[0]["deleted"]) if records else 0
        entities_deleted = await self._remove_orphaned_entities_strict(
            scope="project",
            project_id=project_id,
        )
        return {
            "memories_deleted": memories_deleted,
            "entities_deleted": entities_deleted,
        }

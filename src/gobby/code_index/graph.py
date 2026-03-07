"""Neo4j call/import graph operations for code symbols.

Wraps existing Neo4jClient with code-specific node types and relationships.
All methods return empty results if Neo4j is not available.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CodeGraph:
    """Code-specific graph operations wrapping Neo4jClient."""

    def __init__(self, neo4j_client: Any | None = None) -> None:
        self._client: Any = neo4j_client

    @property
    def available(self) -> bool:
        return self._client is not None

    async def add_relationships(
        self,
        project_id: str,
        file_path: str,
        imports: list[dict[str, Any]] | None = None,
        calls: list[dict[str, Any]] | None = None,
        contains: list[dict[str, Any]] | None = None,
    ) -> int:
        """Add import/call/contains relationships to the graph.

        Returns count of relationships added.
        """
        if not self.available:
            return 0

        count = 0
        try:
            # Add import relationships
            for imp in imports or []:
                await self._client.execute_write(
                    """MERGE (f:CodeFile {path: $source, project: $project})
                       MERGE (m:CodeModule {name: $target, project: $project})
                       MERGE (f)-[:IMPORTS]->(m)""",
                    {
                        "source": imp.get("source_file", ""),
                        "target": imp.get("target_module", ""),
                        "project": project_id,
                    },
                )
                count += 1

            # Add call relationships
            for call in calls or []:
                await self._client.execute_write(
                    """MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
                       MERGE (callee:CodeSymbol {name: $callee_name, project: $project})
                       MERGE (caller)-[:CALLS {file: $file, line: $line}]->(callee)""",
                    {
                        "caller_id": call.get("caller_symbol_id", ""),
                        "callee_name": call.get("callee_name", ""),
                        "file": call.get("file_path", ""),
                        "line": call.get("line", 0),
                        "project": project_id,
                    },
                )
                count += 1

            # Add contains relationships (file contains symbol)
            for cont in contains or []:
                await self._client.execute_write(
                    """MERGE (f:CodeFile {path: $file, project: $project})
                       MERGE (s:CodeSymbol {id: $symbol_id, project: $project})
                       SET s.name = $name, s.kind = $kind
                       MERGE (f)-[:DEFINES]->(s)""",
                    {
                        "file": file_path,
                        "symbol_id": cont.get("id", ""),
                        "name": cont.get("name", ""),
                        "kind": cont.get("kind", ""),
                        "project": project_id,
                    },
                )
                count += 1

        except Exception as e:
            logger.warning(f"Graph relationship add failed: {e}")

        return count

    async def find_callers(
        self, symbol_name: str, project_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Find symbols that call the given symbol name."""
        if not self.available:
            return []

        try:
            result = await self._client.execute_read(
                """MATCH (caller:CodeSymbol)-[r:CALLS]->(callee:CodeSymbol {name: $name, project: $project})
                   RETURN caller.id AS caller_id, caller.name AS caller_name,
                          r.file AS file, r.line AS line
                   LIMIT $limit""",
                {"name": symbol_name, "project": project_id, "limit": limit},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"find_callers failed: {e}")
            return []

    async def find_usages(
        self, symbol_name: str, project_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Find all usages of a symbol (callers + imports)."""
        if not self.available:
            return []

        try:
            result = await self._client.execute_read(
                """MATCH (n)-[r]->(target {name: $name, project: $project})
                   WHERE type(r) IN ['CALLS', 'IMPORTS']
                   RETURN n.id AS source_id, n.name AS source_name,
                          type(r) AS rel_type, r.file AS file, r.line AS line
                   LIMIT $limit""",
                {"name": symbol_name, "project": project_id, "limit": limit},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"find_usages failed: {e}")
            return []

    async def get_imports(self, file_path: str, project_id: str) -> list[dict[str, Any]]:
        """Get import graph for a file."""
        if not self.available:
            return []

        try:
            result = await self._client.execute_read(
                """MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule)
                   RETURN m.name AS module_name""",
                {"path": file_path, "project": project_id},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"get_imports failed: {e}")
            return []

    async def get_import_chain(
        self, module: str, project_id: str, depth: int = 3
    ) -> list[dict[str, Any]]:
        """Get transitive import chain for a module."""
        if not self.available:
            return []

        try:
            result = await self._client.execute_read(
                """MATCH path = (f:CodeFile)-[:IMPORTS*1..$depth]->(m:CodeModule {name: $module, project: $project})
                   UNWIND nodes(path) AS n
                   RETURN DISTINCT n.name AS name, n.path AS path, labels(n)[0] AS type""",
                {"module": module, "project": project_id, "depth": depth},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"get_import_chain failed: {e}")
            return []

    async def clear_project(self, project_id: str) -> None:
        """Remove all graph data for a project."""
        if not self.available:
            return

        try:
            await self._client.execute_write(
                """MATCH (n {project: $project}) DETACH DELETE n""",
                {"project": project_id},
            )
        except Exception as e:
            logger.warning(f"Graph clear_project failed: {e}")

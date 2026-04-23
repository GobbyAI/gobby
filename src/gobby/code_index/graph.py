"""Neo4j call/import graph operations for code symbols.

Canonical project symbols are stored as ``CodeSymbol {id, project}``.
Call targets that cannot be resolved to a project symbol are stored as
``UnresolvedCallee`` or ``ExternalSymbol`` nodes with stable IDs.
"""

from __future__ import annotations

import logging
from typing import Any

from gobby.code_index.models import make_external_symbol_id, make_unresolved_callee_id

logger = logging.getLogger(__name__)

_CALL_TARGET_PREDICATE = "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol"
_NEIGHBOR_PREDICATE = "neighbor:CodeSymbol OR neighbor:UnresolvedCallee OR neighbor:ExternalSymbol"
_PROJECT_NODE_PREDICATE = (
    "n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol"
)
_TARGET_TYPE_CASE = (
    "CASE "
    "WHEN target:CodeSymbol THEN coalesce(target.kind, 'function') "
    "WHEN target:ExternalSymbol THEN 'external' "
    "ELSE 'unresolved' "
    "END"
)
_NODE_TYPE_CASE = (
    "CASE "
    "WHEN n:CodeFile THEN 'file' "
    "WHEN n:CodeModule THEN 'module' "
    "WHEN n:CodeSymbol THEN coalesce(n.kind, 'function') "
    "WHEN n:ExternalSymbol THEN 'external' "
    "ELSE 'unresolved' "
    "END"
)


class CodeGraph:
    """Code-specific graph operations wrapping Neo4jClient."""

    def __init__(self, neo4j_client: Any | None = None) -> None:
        self._client: Any = neo4j_client

    @property
    def available(self) -> bool:
        return self._client is not None

    @staticmethod
    def _target_id(project_id: str, call: dict[str, Any]) -> str:
        callee_kind = call.get("callee_target_kind") or "unresolved"
        callee_symbol_id = call.get("callee_symbol_id") or ""
        callee_name = call.get("callee_name") or ""
        external_module = call.get("callee_external_module") or ""
        if callee_symbol_id:
            return callee_symbol_id
        if not callee_name:
            return ""
        if callee_kind == "external":
            return make_external_symbol_id(project_id, callee_name, external_module)
        return make_unresolved_callee_id(project_id, callee_name)

    async def _cleanup_orphans(self, project_id: str) -> None:
        assert self._client is not None  # noqa: S101
        await self._client.execute_write(
            """
            MATCH (m:CodeModule {project: $project})
            WHERE NOT (m)<-[:IMPORTS]-()
            DETACH DELETE m
            """,
            {"project": project_id},
        )
        await self._client.execute_write(
            """
            MATCH (n {project: $project})
            WHERE (n:UnresolvedCallee OR n:ExternalSymbol)
              AND NOT ()-[:CALLS]->(n)
            DETACH DELETE n
            """,
            {"project": project_id},
        )
        await self._client.execute_write(
            """
            MATCH (s:CodeSymbol {project: $project})
            WHERE s.file_path IS NULL
              AND NOT ()-[:DEFINES]->(s)
              AND NOT ()-[:CALLS]->(s)
              AND NOT (s)-[:CALLS]->()
            DETACH DELETE s
            """,
            {"project": project_id},
        )

    async def sync_file(
        self,
        project_id: str,
        file_path: str,
        imports: list[dict[str, Any]] | None = None,
        calls: list[dict[str, Any]] | None = None,
        contains: list[dict[str, Any]] | None = None,
    ) -> int:
        """Replace graph edges for one file while preserving cross-file callers."""
        if not self.available:
            return 0

        assert self._client is not None  # noqa: S101
        contains = contains or []
        symbol_ids = [symbol["id"] for symbol in contains if symbol.get("id")]

        await self._client.execute_write(
            """
            MERGE (f:CodeFile {path: $file_path, project: $project})
            SET f.updated_at = datetime(), f.symbol_count = $symbol_count
            """,
            {
                "file_path": file_path,
                "project": project_id,
                "symbol_count": len(symbol_ids),
            },
        )
        await self._client.execute_write(
            """
            MATCH (f:CodeFile {path: $file_path, project: $project})-[r:IMPORTS]->(:CodeModule)
            DELETE r
            """,
            {"file_path": file_path, "project": project_id},
        )
        await self._client.execute_write(
            """
            MATCH (f:CodeFile {path: $file_path, project: $project})-[r:DEFINES]->(:CodeSymbol)
            DELETE r
            """,
            {"file_path": file_path, "project": project_id},
        )
        await self._client.execute_write(
            """
            MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->()
            DELETE r
            """,
            {"file_path": file_path, "project": project_id},
        )

        if symbol_ids:
            await self._client.execute_write(
                """
                MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
                WHERE NOT s.id IN $symbol_ids
                DETACH DELETE s
                """,
                {
                    "project": project_id,
                    "file_path": file_path,
                    "symbol_ids": symbol_ids,
                },
            )
        else:
            await self._client.execute_write(
                """
                MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
                DETACH DELETE s
                """,
                {"project": project_id, "file_path": file_path},
            )

        relationship_count = 0

        for imp in imports or []:
            target_module = imp.get("target_module")
            if not target_module:
                continue
            await self._client.execute_write(
                """
                MERGE (f:CodeFile {path: $source, project: $project})
                MERGE (m:CodeModule {name: $target, project: $project})
                MERGE (f)-[:IMPORTS]->(m)
                """,
                {
                    "source": imp.get("source_file", file_path),
                    "target": target_module,
                    "project": project_id,
                },
            )
            relationship_count += 1

        for cont in contains:
            symbol_id = cont.get("id")
            symbol_name = cont.get("name")
            if not symbol_id or not symbol_name:
                continue
            await self._client.execute_write(
                """
                MERGE (f:CodeFile {path: $file, project: $project})
                MERGE (s:CodeSymbol {id: $symbol_id, project: $project})
                SET s.name = $name,
                    s.kind = $kind,
                    s.file_path = $file,
                    s.line_start = $line_start,
                    s.updated_at = datetime()
                MERGE (f)-[:DEFINES]->(s)
                """,
                {
                    "file": file_path,
                    "symbol_id": symbol_id,
                    "name": symbol_name,
                    "kind": cont.get("kind", ""),
                    "line_start": cont.get("line_start", 0),
                    "project": project_id,
                },
            )
            relationship_count += 1

        for call in calls or []:
            callee_kind = call.get("callee_target_kind") or "unresolved"
            target_id = self._target_id(project_id, call)
            caller_id = call.get("caller_symbol_id")
            if not target_id or not caller_id:
                continue
            params = {
                "caller_id": caller_id,
                "target_id": target_id,
                "callee_name": call.get("callee_name", ""),
                "callee_module": call.get("callee_external_module", ""),
                "file": call.get("file_path", file_path),
                "line": call.get("line", 0),
                "project": project_id,
            }
            if callee_kind == "symbol":
                cypher = """
                    MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
                    MERGE (callee:CodeSymbol {id: $target_id, project: $project})
                    ON CREATE SET callee.name = $callee_name, callee.updated_at = datetime()
                    MERGE (caller)-[:CALLS {file: $file, line: $line}]->(callee)
                """
            elif callee_kind == "external":
                cypher = """
                    MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
                    MERGE (callee:ExternalSymbol {id: $target_id, project: $project})
                    SET callee.name = $callee_name,
                        callee.external_module = $callee_module,
                        callee.updated_at = datetime()
                    MERGE (caller)-[:CALLS {file: $file, line: $line}]->(callee)
                """
            else:
                cypher = """
                    MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
                    MERGE (callee:UnresolvedCallee {id: $target_id, project: $project})
                    SET callee.name = $callee_name,
                        callee.updated_at = datetime()
                    MERGE (caller)-[:CALLS {file: $file, line: $line}]->(callee)
                """
            await self._client.execute_write(cypher, params)
            relationship_count += 1

        await self._cleanup_orphans(project_id)
        return relationship_count

    async def add_relationships(
        self,
        project_id: str,
        file_path: str,
        imports: list[dict[str, Any]] | None = None,
        calls: list[dict[str, Any]] | None = None,
        contains: list[dict[str, Any]] | None = None,
    ) -> int:
        """Backward-compatible wrapper around sync_file()."""
        return await self.sync_file(
            project_id=project_id,
            file_path=file_path,
            imports=imports,
            calls=calls,
            contains=contains,
        )

    async def find_callers(
        self,
        symbol_id: str,
        project_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find callers of any call-target node by stable ID."""
        if not self.available:
            return []

        assert self._client is not None  # noqa: S101
        try:
            result = await self._client.execute_read(
                f"""
                MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}})
                WHERE {_CALL_TARGET_PREDICATE}
                RETURN caller.id AS caller_id, caller.name AS caller_name,
                       r.file AS file, r.line AS line
                LIMIT $limit
                """,
                {"id": symbol_id, "project": project_id, "limit": limit},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"find_callers failed: {e}")
            return []

    async def find_usages(
        self,
        symbol_id: str,
        project_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find incoming CALLS usages for a canonical, unresolved, or external target."""
        if not self.available:
            return []

        assert self._client is not None  # noqa: S101
        try:
            result = await self._client.execute_read(
                f"""
                MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}})
                WHERE {_CALL_TARGET_PREDICATE}
                RETURN source.id AS source_id, source.name AS source_name,
                       'CALLS' AS rel_type, r.file AS file, r.line AS line
                LIMIT $limit
                """,
                {"id": symbol_id, "project": project_id, "limit": limit},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"find_usages failed: {e}")
            return []

    async def get_imports(self, file_path: str, project_id: str) -> list[dict[str, Any]]:
        """Get import graph for a file."""
        if not self.available:
            return []

        assert self._client is not None  # noqa: S101
        try:
            result = await self._client.execute_read(
                """
                MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule)
                RETURN m.name AS module_name
                """,
                {"path": file_path, "project": project_id},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"get_imports failed: {e}")
            return []

    async def get_import_chain(
        self,
        module: str,
        project_id: str,
        depth: int = 3,
    ) -> list[dict[str, Any]]:
        """Get transitive import chain for a module."""
        if not self.available:
            return []

        assert self._client is not None  # noqa: S101
        try:
            depth = max(1, min(int(depth), 5))
            result = await self._client.execute_read(
                f"""
                MATCH path = (f:CodeFile)-[:IMPORTS*1..{depth}]->(m:CodeModule {{name: $module, project: $project}})
                UNWIND nodes(path) AS n
                RETURN DISTINCT n.name AS name, n.path AS path, labels(n)[0] AS type
                """,
                {"module": module, "project": project_id},
            )
            return [dict(record) for record in result]
        except Exception as e:
            logger.debug(f"get_import_chain failed: {e}")
            return []

    async def find_blast_radius(
        self,
        symbol_id: str | None,
        file_path: str | None,
        project_id: str,
        depth: int = 3,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Find transitive callers/importers affected by a symbol or file change."""
        if not self.available:
            return []

        if bool(symbol_id) == bool(file_path):
            raise ValueError("Exactly one of symbol_id or file_path must be provided")

        assert self._client is not None  # noqa: S101
        depth = max(1, min(depth, 5))
        results: list[dict[str, Any]] = []

        try:
            if symbol_id:
                records = await self._client.execute_read(
                    f"""
                    MATCH (target {{id: $id, project: $project}})
                    WHERE {_CALL_TARGET_PREDICATE}
                    MATCH path = (affected:CodeSymbol)-[:CALLS*1..{depth}]->(target)
                    WITH affected, min(length(path)) AS distance
                    OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected)
                    RETURN DISTINCT affected.id AS node_id,
                           affected.name AS node_name,
                           affected.kind AS kind,
                           file.path AS file_path,
                           affected.line_start AS line,
                           distance,
                           'call' AS rel_type,
                           coalesce(affected.kind, 'function') AS node_type
                    ORDER BY distance ASC, affected.name ASC
                    LIMIT $limit
                    """,
                    {"id": symbol_id, "project": project_id, "limit": limit},
                )
                results.extend(dict(r) for r in records)
            else:
                call_records = await self._client.execute_read(
                    f"""
                    MATCH (tf:CodeFile {{path: $path, project: $project}})-[:DEFINES]->(target_sym:CodeSymbol)
                    MATCH path = (affected:CodeSymbol)-[:CALLS*1..{depth}]->(target_sym)
                    WITH affected, min(length(path)) AS distance
                    OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected)
                    RETURN DISTINCT affected.id AS node_id,
                           affected.name AS node_name,
                           affected.kind AS kind,
                           file.path AS file_path,
                           affected.line_start AS line,
                           distance,
                           'call' AS rel_type,
                           coalesce(affected.kind, 'function') AS node_type
                    ORDER BY distance ASC, affected.name ASC
                    LIMIT $limit
                    """,
                    {"path": file_path, "project": project_id, "limit": limit},
                )
                results.extend(dict(r) for r in call_records)

                import_records = await self._client.execute_read(
                    f"""
                    MATCH (tf:CodeFile {{path: $path, project: $project}})-[:IMPORTS]->(m:CodeModule)
                    MATCH path = (importer:CodeFile)-[:IMPORTS*1..{depth}]->(m)
                    WHERE importer.path <> $path
                    WITH importer, min(length(path)) AS distance
                    RETURN DISTINCT importer.path AS node_id,
                           importer.path AS node_name,
                           NULL AS kind,
                           importer.path AS file_path,
                           NULL AS line,
                           distance,
                           'import' AS rel_type,
                           'file' AS node_type
                    ORDER BY distance ASC
                    LIMIT $limit
                    """,
                    {"path": file_path, "project": project_id, "limit": limit},
                )
                results.extend(dict(r) for r in import_records)
        except Exception as e:
            logger.debug(f"find_blast_radius failed: {e}")

        return results

    async def get_file_graph(self, project_id: str, limit: int = 200) -> dict[str, Any]:
        """Get a file-level overview graph for visualization."""
        if not self.available:
            return {"nodes": [], "links": []}

        assert self._client is not None  # noqa: S101
        max_nodes = limit * 8
        link_limit = limit * 4

        try:
            file_records = await self._client.execute_read(
                """
                MATCH (f:CodeFile {project: $project})
                OPTIONAL MATCH (f)-[:DEFINES]->(s:CodeSymbol)
                WITH f, count(DISTINCT s) AS sym_count
                OPTIONAL MATCH (f)-[:IMPORTS]->(m:CodeModule)
                WITH f, sym_count, count(m) AS imp_count
                RETURN f.path AS id, f.path AS name, 'file' AS type,
                       f.path AS file_path, sym_count AS symbol_count
                ORDER BY imp_count DESC, sym_count DESC, f.path
                LIMIT $limit
                """,
                {"project": project_id, "limit": limit},
            )
            nodes = [dict(r) for r in file_records]
            node_ids = {n["id"] for n in nodes}

            import_records = await self._client.execute_read(
                """
                MATCH (f:CodeFile {project: $project})-[:IMPORTS]->(m:CodeModule {project: $project})
                WHERE f.path IN $file_paths
                RETURN f.path AS source, m.name AS target, 'IMPORTS' AS type
                LIMIT $link_limit
                """,
                {
                    "project": project_id,
                    "file_paths": list(node_ids),
                    "link_limit": link_limit,
                },
            )

            links: list[dict[str, Any]] = []
            module_ids: set[str] = set()
            for r in import_records:
                rec = dict(r)
                links.append(rec)
                mid = rec["target"]
                if mid not in node_ids and mid not in module_ids and len(nodes) < max_nodes:
                    module_ids.add(mid)
                    nodes.append({"id": mid, "name": mid, "type": "module"})

            defines_records = await self._client.execute_read(
                """
                MATCH (f:CodeFile {project: $project})-[:DEFINES]->(s:CodeSymbol {project: $project})
                WHERE f.path IN $file_paths
                RETURN f.path AS source, s.id AS target, 'DEFINES' AS type,
                       s.name AS symbol_name, s.kind AS symbol_kind,
                       s.file_path AS symbol_file_path, s.line_start AS line_start
                LIMIT $link_limit
                """,
                {
                    "project": project_id,
                    "file_paths": list(node_ids),
                    "link_limit": link_limit,
                },
            )

            for r in defines_records:
                rec = dict(r)
                sid = rec["target"]
                links.append({"source": rec["source"], "target": sid, "type": "DEFINES"})
                if sid not in node_ids and len(nodes) < max_nodes:
                    node_ids.add(sid)
                    nodes.append(
                        {
                            "id": sid,
                            "name": rec.get("symbol_name", sid),
                            "type": rec.get("symbol_kind") or "function",
                            "kind": rec.get("symbol_kind"),
                            "file_path": rec.get("symbol_file_path") or rec["source"],
                            "line_start": rec.get("line_start"),
                        }
                    )

            call_records = await self._client.execute_read(
                f"""
                MATCH (f:CodeFile {{project: $project}})-[:DEFINES]->(s:CodeSymbol {{project: $project}})-[:CALLS]->(target)
                WHERE f.path IN $file_paths AND ({_CALL_TARGET_PREDICATE})
                RETURN s.id AS source, target.id AS target, 'CALLS' AS type,
                       target.name AS target_name,
                       {_TARGET_TYPE_CASE} AS target_type,
                       target.kind AS target_kind,
                       target.file_path AS target_file_path,
                       target.line_start AS target_line_start
                LIMIT $link_limit
                """,
                {
                    "project": project_id,
                    "file_paths": list(node_ids),
                    "link_limit": link_limit,
                },
            )

            for r in call_records:
                rec = dict(r)
                links.append({"source": rec["source"], "target": rec["target"], "type": "CALLS"})
                tid = rec["target"]
                if tid not in node_ids and len(nodes) < max_nodes:
                    node_ids.add(tid)
                    nodes.append(
                        {
                            "id": tid,
                            "name": rec.get("target_name", tid),
                            "type": rec.get("target_type") or "unresolved",
                            "kind": rec.get("target_kind"),
                            "file_path": rec.get("target_file_path"),
                            "line_start": rec.get("target_line_start"),
                        }
                    )

            return {"nodes": nodes, "links": links}
        except Exception as e:
            logger.debug(f"get_file_graph failed: {e}")
            return {"nodes": [], "links": []}

    async def get_file_symbols(self, file_path: str, project_id: str) -> dict[str, Any]:
        """Expand a file into its defined symbols and adjacent CALLS edges."""
        if not self.available:
            return {"nodes": [], "links": []}

        assert self._client is not None  # noqa: S101
        try:
            sym_records = await self._client.execute_read(
                """
                MATCH (:CodeFile {path: $path, project: $project})-[:DEFINES]->(s:CodeSymbol {project: $project})
                RETURN s.id AS id, s.name AS name, coalesce(s.kind, 'function') AS type,
                       s.kind AS kind, s.file_path AS file_path,
                       s.line_start AS line_start, s.signature AS signature
                """,
                {"path": file_path, "project": project_id},
            )
            nodes: list[dict[str, Any]] = [dict(r) for r in sym_records]
            node_ids = {n["id"] for n in nodes}
            links: list[dict[str, Any]] = [
                {"source": file_path, "target": node["id"], "type": "DEFINES"} for node in nodes
            ]

            call_records = await self._client.execute_read(
                f"""
                MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target)
                WHERE ({_CALL_TARGET_PREDICATE})
                  AND (source.file_path = $path OR (target:CodeSymbol AND target.file_path = $path))
                RETURN source.id AS source_id, source.name AS source_name,
                       coalesce(source.kind, 'function') AS source_type,
                       source.kind AS source_kind, source.file_path AS source_file_path,
                       source.line_start AS source_line_start, source.signature AS source_signature,
                       target.id AS target_id, target.name AS target_name,
                       {_TARGET_TYPE_CASE} AS target_type,
                       target.kind AS target_kind, target.file_path AS target_file_path,
                       target.line_start AS target_line_start, target.signature AS target_signature,
                       r.line AS line
                """,
                {"path": file_path, "project": project_id},
            )

            def _ensure_node(
                node_id: str,
                name: str,
                node_type: str,
                kind: str | None,
                node_file_path: str | None,
                line_start: int | None,
                signature: str | None,
            ) -> None:
                if node_id in node_ids:
                    return
                node_ids.add(node_id)
                nodes.append(
                    {
                        "id": node_id,
                        "name": name,
                        "type": node_type,
                        "kind": kind,
                        "file_path": node_file_path,
                        "line_start": line_start,
                        "signature": signature,
                    }
                )

            for r in call_records:
                rec = dict(r)
                _ensure_node(
                    rec["source_id"],
                    rec["source_name"],
                    rec.get("source_type") or "function",
                    rec.get("source_kind"),
                    rec.get("source_file_path"),
                    rec.get("source_line_start"),
                    rec.get("source_signature"),
                )
                _ensure_node(
                    rec["target_id"],
                    rec["target_name"],
                    rec.get("target_type") or "unresolved",
                    rec.get("target_kind"),
                    rec.get("target_file_path"),
                    rec.get("target_line_start"),
                    rec.get("target_signature"),
                )
                links.append(
                    {
                        "source": rec["source_id"],
                        "target": rec["target_id"],
                        "type": "CALLS",
                        "line": rec.get("line"),
                    }
                )

            return {"nodes": nodes, "links": links}
        except Exception as e:
            logger.debug(f"get_file_symbols failed: {e}")
            return {"nodes": [], "links": []}

    async def get_symbol_neighbors(
        self,
        symbol_id: str,
        project_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Expand a code-graph node into its direct callers and callees."""
        if not self.available:
            return {"nodes": [], "links": []}

        assert self._client is not None  # noqa: S101
        try:
            records = await self._client.execute_read(
                f"""
                MATCH (center {{id: $id, project: $project}})
                WHERE center:CodeSymbol OR center:UnresolvedCallee OR center:ExternalSymbol
                MATCH (center)-[r:CALLS]-(neighbor)
                WHERE {_NEIGHBOR_PREDICATE}
                RETURN neighbor.id AS id, neighbor.name AS name,
                       CASE
                         WHEN neighbor:CodeSymbol THEN coalesce(neighbor.kind, 'function')
                         WHEN neighbor:ExternalSymbol THEN 'external'
                         ELSE 'unresolved'
                       END AS type,
                       neighbor.kind AS kind,
                       neighbor.file_path AS file_path,
                       neighbor.line_start AS line_start,
                       neighbor.signature AS signature,
                       CASE WHEN startNode(r) = center THEN 'outgoing' ELSE 'incoming' END AS direction,
                       r.line AS line
                LIMIT $limit
                """,
                {"id": symbol_id, "project": project_id, "limit": limit},
            )

            nodes: list[dict[str, Any]] = []
            links: list[dict[str, Any]] = []
            seen = set()

            for r in records:
                rec = dict(r)
                nid = rec["id"]
                if nid not in seen:
                    seen.add(nid)
                    nodes.append(
                        {
                            "id": nid,
                            "name": rec["name"],
                            "type": rec.get("type") or "unresolved",
                            "kind": rec.get("kind"),
                            "file_path": rec.get("file_path"),
                            "line_start": rec.get("line_start"),
                            "signature": rec.get("signature"),
                        }
                    )
                if rec["direction"] == "outgoing":
                    links.append(
                        {
                            "source": symbol_id,
                            "target": nid,
                            "type": "CALLS",
                            "line": rec.get("line"),
                        }
                    )
                else:
                    links.append(
                        {
                            "source": nid,
                            "target": symbol_id,
                            "type": "CALLS",
                            "line": rec.get("line"),
                        }
                    )

            return {"nodes": nodes, "links": links}
        except Exception as e:
            logger.debug(f"get_symbol_neighbors failed: {e}")
            return {"nodes": [], "links": []}

    async def get_blast_radius_graph(
        self,
        symbol_id: str | None,
        file_path: str | None,
        project_id: str,
        depth: int = 3,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Get blast radius as visualization-ready graph data."""
        results = await self.find_blast_radius(
            symbol_id=symbol_id,
            file_path=file_path,
            project_id=project_id,
            depth=depth,
            limit=limit,
        )

        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        center_id = symbol_id or file_path
        if not center_id:
            raise ValueError("Either symbol_id or file_path must be provided")

        center_name = center_id
        center_type = "file"
        center_kind: str | None = None
        center_file_path = file_path

        if symbol_id and self.available:
            assert self._client is not None  # noqa: S101
            try:
                center_rows = await self._client.execute_read(
                    f"""
                    MATCH (n {{id: $id, project: $project}})
                    WHERE n:CodeSymbol OR n:UnresolvedCallee OR n:ExternalSymbol
                    RETURN n.name AS name,
                           {_NODE_TYPE_CASE} AS type,
                           n.kind AS kind,
                           n.file_path AS file_path
                    LIMIT 1
                    """,
                    {"id": symbol_id, "project": project_id},
                )
                if center_rows:
                    center_name = center_rows[0].get("name") or center_id
                    center_type = center_rows[0].get("type") or "function"
                    center_kind = center_rows[0].get("kind")
                    center_file_path = center_rows[0].get("file_path")
                else:
                    center_type = "function"
            except Exception as e:
                logger.debug(f"Failed to load blast-radius center node: {e}")
                center_type = "function"

        nodes.append(
            {
                "id": center_id,
                "name": center_name,
                "type": center_type,
                "kind": center_kind,
                "file_path": center_file_path,
                "blast_distance": 0,
            }
        )
        seen_ids.add(center_id)

        for r in results:
            nid = r.get("node_id", "")
            if not nid or nid in seen_ids:
                continue
            seen_ids.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "name": r.get("node_name") or nid,
                    "type": r.get("node_type") or "function",
                    "kind": r.get("kind"),
                    "file_path": r.get("file_path"),
                    "blast_distance": r.get("distance", 1),
                }
            )
            links.append(
                {
                    "source": nid,
                    "target": center_id,
                    "type": "CALLS" if r.get("rel_type") == "call" else "IMPORTS",
                    "distance": r.get("distance", 1),
                }
            )

        return {"nodes": nodes, "links": links, "center": center_id}

    async def clear_project(self, project_id: str) -> None:
        """Remove all graph data for a project."""
        if not self.available:
            return

        assert self._client is not None  # noqa: S101
        await self._client.execute_write(
            f"""
            MATCH (n {{project: $project}})
            WHERE {_PROJECT_NODE_PREDICATE}
            DETACH DELETE n
            """,
            {"project": project_id},
        )

    async def delete_file(self, file_path: str, project_id: str) -> None:
        """Remove all graph data for a specific file."""
        if not self.available:
            return

        assert self._client is not None  # noqa: S101
        await self._client.execute_write(
            """
            MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
            DETACH DELETE s
            """,
            {"file_path": file_path, "project": project_id},
        )
        await self._client.execute_write(
            """
            MATCH (f:CodeFile {path: $file_path, project: $project})
            DETACH DELETE f
            """,
            {"file_path": file_path, "project": project_id},
        )
        await self._cleanup_orphans(project_id)

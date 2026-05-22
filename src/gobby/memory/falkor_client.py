"""Async FalkorDB client for memory and code graph operations."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_VECTOR_SEARCH_PROJECT_OVERFETCH_FACTOR = 5
_VECTOR_SEARCH_PROJECT_OVERFETCH_LIMIT = 200
_CONSTRAINT_OPERATIONAL_TIMEOUT_SECONDS = 30.0
_CONSTRAINT_POLL_INTERVAL_SECONDS = 0.25


class FalkorConnectionError(Exception):
    """Raised when unable to connect to FalkorDB."""


class FalkorQueryError(Exception):
    """Raised when a Cypher query returns an error."""

    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body


_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_cypher_identifier(value: str, kind: str = "identifier") -> None:
    """Validate that a value is a safe Cypher identifier."""
    if not _CYPHER_IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid Cypher {kind}: {value!r}. Must match [A-Za-z_][A-Za-z0-9_]*.")


def _is_already_indexed_error(error: BaseException) -> bool:
    message = str(error).lower()
    return "already indexed" in message or "index already exists" in message


def _is_constraint_exists_error(error: BaseException) -> bool:
    message = str(error).lower()
    return "constraint already exists" in message or "already exists" in message


def _is_connection_error(error: BaseException) -> bool:
    try:
        from redis import exceptions as redis_exceptions
    except ModuleNotFoundError:
        return False
    return isinstance(error, (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError))


def _is_response_error(error: BaseException) -> bool:
    try:
        from redis import exceptions as redis_exceptions
    except ModuleNotFoundError:
        return False
    return isinstance(error, redis_exceptions.ResponseError)


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return value
    return _Immediate(value)


class _Immediate:
    def __init__(self, value: Any) -> None:
        self._value = value

    def __await__(self) -> Any:
        if False:
            yield None
        return self._value


class FalkorClient:
    """Async FalkorDB client for the knowledge graph."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str | None = None,
        graph_name: str = "gobby_kg",
        timeout: float = 15.0,
    ) -> None:
        try:
            from falkordb.asyncio import FalkorDB
        except ModuleNotFoundError as exc:
            raise FalkorConnectionError("falkordb package is not installed") from exc

        self._host = host
        self._port = port
        self._graph_name = graph_name
        self._db = FalkorDB(
            host=host,
            port=port,
            password=password,
            socket_timeout=timeout,
        )
        self._graph = self._db.select_graph(graph_name)

    @property
    def base_url(self) -> str:
        return f"redis://{self._host}:{self._port}"

    async def close(self) -> None:
        """Close the underlying connection when the client exposes a closer."""
        for target in (getattr(self, "_db", None), getattr(self, "_client", None)):
            if target is None:
                continue
            close = getattr(target, "aclose", None) or getattr(target, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result
            return

    async def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute Cypher and return rows as flat dictionaries."""
        try:
            result = await self._graph.query(cypher, params or {})
        except Exception as exc:
            self._raise_mapped_error(exc)
        return self._parse_response(result)

    @staticmethod
    def _raise_mapped_error(error: BaseException) -> None:
        if _is_connection_error(error):
            raise FalkorConnectionError(f"FalkorDB connection failed: {error}") from error
        if _is_response_error(error):
            raise FalkorQueryError(
                str(error), response_body=getattr(error, "args", None)
            ) from error
        raise error

    @staticmethod
    def _parse_response(data: Any) -> list[dict[str, Any]]:
        """Collapse FalkorDB result envelopes into list[dict] rows."""
        if data is None:
            return []
        if isinstance(data, list):
            if not data or isinstance(data[0], dict):
                return data
            return [{"value": value} for value in data]

        header = getattr(data, "header", None)
        records = (
            getattr(data, "result_set", None)
            or getattr(data, "records", None)
            or getattr(data, "rows", None)
        )
        if isinstance(data, tuple) and len(data) >= 2:
            header, records = data[0], data[1]
        if header is None or records is None:
            return []

        fields = [FalkorClient._field_name(field) for field in header]
        rows: list[dict[str, Any]] = []
        for record in records:
            values = getattr(record, "values", record)
            rows.append(
                {
                    field: values[index] if index < len(values) else None
                    for index, field in enumerate(fields)
                }
            )
        return rows

    @staticmethod
    def _field_name(field: Any) -> str:
        if isinstance(field, str):
            return field
        if isinstance(field, dict):
            return str(field.get("name") or field.get("property") or field)
        return str(getattr(field, "name", field))

    @staticmethod
    def _clean_props(props: Any) -> dict[str, Any]:
        """Strip large/unhelpful properties like embedding vectors."""
        if not isinstance(props, dict):
            return {}
        return {
            key: value
            for key, value in props.items()
            if not (isinstance(value, list) and len(value) > 20) and key != "embedding"
        }

    async def ensure_memory_graph_schema(self) -> None:
        """Ensure constraints and indexes for the memory knowledge graph."""
        await self.ensure_supporting_index("_Entity", "entity_key")
        await self.ensure_unique_constraint("_Entity", "entity_key")
        await self.ensure_supporting_index("Memory", "memory_id")
        await self.ensure_unique_constraint("Memory", "memory_id")
        await self._query_ignoring_existing_index(
            "CREATE INDEX FOR (n:_Entity) ON (n.project_id, n.entity_type)"
        )

    async def ensure_supporting_index(self, label: str, prop: str) -> None:
        """Create the exact-match index FalkorDB requires before unique constraints."""
        _validate_cypher_identifier(label, "label")
        _validate_cypher_identifier(prop, "property")
        await self._query_ignoring_existing_index(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")

    async def _query_ignoring_existing_index(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.query(cypher, params)
        except FalkorQueryError as exc:
            if not _is_already_indexed_error(exc):
                raise

    async def ensure_unique_constraint(self, label: str, prop: str) -> None:
        """Create and poll a unique constraint until it is operational."""
        _validate_cypher_identifier(label, "label")
        _validate_cypher_identifier(prop, "property")
        try:
            await self._execute_command(
                "GRAPH.CONSTRAINT",
                "CREATE",
                self._graph_name,
                "UNIQUE",
                "NODE",
                label,
                "PROPERTIES",
                1,
                prop,
            )
        except Exception as exc:
            if not _is_constraint_exists_error(exc):
                self._raise_mapped_error(exc)

        await self._poll_constraint_operational(label, prop)

    async def _execute_command(self, *parts: Any) -> Any:
        for attr in ("_db", "_redis", "_client"):
            target = getattr(self, attr, None)
            execute = getattr(target, "execute_command", None)
            if execute is None:
                continue
            return await _maybe_await(execute(*parts))
        raise FalkorConnectionError("FalkorDB client does not expose execute_command")

    async def _poll_constraint_operational(self, label: str, prop: str) -> None:
        deadline = time.monotonic() + _CONSTRAINT_OPERATIONAL_TIMEOUT_SECONDS
        while True:
            rows = await self.query("CALL db.constraints()")
            status = self._matching_constraint_status(rows, label, prop)
            if status == "OPERATIONAL":
                return
            if status == "FAILED":
                raise FalkorQueryError(
                    f"FalkorDB unique constraint for {label}.{prop} status FAILED"
                )
            if time.monotonic() >= deadline:
                raise FalkorQueryError(
                    f"Timed out waiting for FalkorDB unique constraint {label}.{prop}"
                )
            await asyncio.sleep(_CONSTRAINT_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _matching_constraint_status(
        rows: list[dict[str, Any]],
        label: str,
        prop: str,
    ) -> str | None:
        for row in rows:
            if str(row.get("type", "")).upper() != "UNIQUE":
                continue
            if row.get("label") != label:
                continue
            if str(row.get("entitytype", "")).upper() != "NODE":
                continue
            properties = row.get("properties", [])
            if isinstance(properties, str):
                properties = [properties]
            if list(properties) == [prop]:
                return str(row.get("status", "")).upper()
        return None

    async def get_entity_graph(
        self,
        limit: int = 500,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get entities and relationships for visualization."""
        entity_rows = await self.query(
            "MATCH (n:_Entity) "
            "WHERE n.project_id = $project_id "
            "OR ($project_id IS NULL AND n.project_id IS NULL) "
            "RETURN n.entity_key AS entity_key, n.name AS name, n.entity_type AS entity_type, "
            "n.project_id AS project_id, properties(n) AS props "
            "ORDER BY n.updated_at DESC LIMIT $limit",
            {"limit": limit, "project_id": project_id},
        )

        entities: list[dict[str, Any]] = []
        seen_entities: set[str] = set()
        for row in entity_rows:
            key = row.get("entity_key") or ""
            name = row.get("name") or ""
            if not key or not name or key in seen_entities:
                continue
            seen_entities.add(key)
            props = row.get("props", {})
            entities.append(
                {
                    "entity_key": key,
                    "name": name,
                    "entity_type": row.get("entity_type") or props.get("entity_type") or "entity",
                    "project_id": row.get("project_id"),
                    "properties": self._clean_props(props),
                }
            )

        relationships: list[dict[str, Any]] = []
        entity_keys = list(seen_entities)
        if entity_keys:
            rel_rows = await self.query(
                "MATCH (a:_Entity)-[r]->(b:_Entity) "
                "WHERE a.entity_key IN $keys AND b.entity_key IN $keys "
                "RETURN a.entity_key AS source_key, b.entity_key AS target_key, "
                "type(r) AS rel_type, properties(r) AS props "
                "LIMIT $limit",
                {"keys": entity_keys, "limit": limit * 4},
            )
            for row in rel_rows:
                source_key = row.get("source_key") or ""
                target_key = row.get("target_key") or ""
                if source_key in seen_entities and target_key in seen_entities:
                    relationships.append(
                        {
                            "source_key": source_key,
                            "target_key": target_key,
                            "type": row.get("rel_type", "RELATED"),
                            "properties": self._clean_props(row.get("props", {})),
                        }
                    )

        return {"entities": entities, "relationships": relationships}

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Expand a single entity's connections."""
        rows = await self.query(
            "MATCH (a:_Entity {entity_key: $entity_key})-[r]-(b:_Entity) "
            "WHERE a.project_id = $project_id "
            "OR ($project_id IS NULL AND a.project_id IS NULL) "
            "RETURN a.entity_key AS source_key, a.name AS source_name, properties(a) AS source_props, "
            "b.entity_key AS target_key, b.name AS target_name, properties(b) AS target_props, "
            "type(r) AS rel_type, properties(r) AS rel_props, "
            "startNode(r) = a AS is_outgoing "
            "LIMIT 50",
            {"entity_key": entity_key, "project_id": project_id},
        )

        entities: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            target_key = row.get("target_key") or ""
            target_name = row.get("target_name") or ""
            target_props = row.get("target_props", {})
            if target_key and target_name and target_key not in seen:
                seen.add(target_key)
                entities.append(
                    {
                        "entity_key": target_key,
                        "name": target_name,
                        "entity_type": target_props.get("entity_type") or "entity",
                        "project_id": target_props.get("project_id"),
                        "properties": self._clean_props(target_props),
                    }
                )

            source_key = row.get("source_key") or ""
            if source_key and target_key:
                rel_props = self._clean_props(row.get("rel_props", {}))
                if row.get("is_outgoing", True):
                    relationships.append(
                        {
                            "source_key": source_key,
                            "target_key": target_key,
                            "type": row.get("rel_type", "RELATED"),
                            "properties": rel_props,
                        }
                    )
                else:
                    relationships.append(
                        {
                            "source_key": target_key,
                            "target_key": source_key,
                            "type": row.get("rel_type", "RELATED"),
                            "properties": rel_props,
                        }
                    )

        if entity_key not in seen:
            center_rows = await self.query(
                "MATCH (n:_Entity {entity_key: $entity_key}) "
                "WHERE n.project_id = $project_id "
                "OR ($project_id IS NULL AND n.project_id IS NULL) "
                "RETURN n.name AS name, properties(n) AS props LIMIT 1",
                {"entity_key": entity_key, "project_id": project_id},
            )
            props: dict[str, Any] = {}
            name = entity_key
            if center_rows:
                name = center_rows[0].get("name") or entity_key
                props = center_rows[0].get("props", {})
            entities.append(
                {
                    "entity_key": entity_key,
                    "name": name,
                    "entity_type": props.get("entity_type") or "entity",
                    "project_id": props.get("project_id"),
                    "properties": self._clean_props(props),
                }
            )

        return {"entities": entities, "relationships": relationships}

    async def merge_node(
        self,
        entity_key: str,
        name: str,
        project_id: str | None = None,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Merge a node by stable key, creating or updating it."""
        props = dict(properties or {})
        props.setdefault("entity_key", entity_key)
        props.setdefault("name", name)
        props.setdefault("project_id", project_id)
        if labels:
            for label in labels:
                _validate_cypher_identifier(label, "label")
        label_clause = ":" + ":".join(labels) if labels else ""
        cypher = (
            f"MERGE (n{label_clause} {{entity_key: $entity_key}}) "
            "ON CREATE SET n += $props, n.created_at = timestamp(), n.updated_at = timestamp() "
            "ON MATCH SET n += $props, n.updated_at = timestamp() "
            "RETURN n.entity_key AS entity_key, n.name AS name"
        )
        return await self.query(cypher, {"entity_key": entity_key, "props": props})

    async def merge_relationship(
        self,
        source_key: str,
        target_key: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Merge a relationship between two nodes matched by entity_key."""
        rel_type = re.sub(r"[^A-Za-z0-9_]", "_", rel_type)
        if rel_type and rel_type[0].isdigit():
            rel_type = "_" + rel_type
        _validate_cypher_identifier(rel_type, "relationship type")
        props = dict(properties or {})
        cypher = (
            "MATCH (a:_Entity {entity_key: $source_key}), (b:_Entity {entity_key: $target_key}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "ON CREATE SET r += $props "
            "ON MATCH SET r += $props "
            "RETURN type(r) AS rel_type"
        )
        return await self.query(
            cypher,
            {"source_key": source_key, "target_key": target_key, "props": props},
        )

    async def set_node_vector(
        self,
        entity_key: str,
        embedding: list[float],
        property_name: str = "embedding",
    ) -> list[dict[str, Any]]:
        """Set a vector property on a node."""
        _validate_cypher_identifier(property_name, "property name")
        cypher = (
            "MATCH (n:_Entity {entity_key: $entity_key}) "
            f"SET n.{property_name} = vecf32($embedding) "
            "RETURN n.entity_key AS entity_key"
        )
        return await self.query(cypher, {"entity_key": entity_key, "embedding": embedding})

    async def ensure_vector_index(
        self,
        dimension: int,
        similarity: str = "cosine",
        index_name: str = "entity_embedding_index",
    ) -> None:
        """Create a vector index on _Entity nodes."""
        _validate_cypher_identifier(index_name, "index name")
        cypher = (
            "CREATE VECTOR INDEX FOR (n:_Entity) ON (n.embedding) "
            f"OPTIONS {{dimension: $dim, similarityFunction: '{similarity}'}}"
        )
        try:
            await self.query(cypher, {"dim": int(dimension)})
        except FalkorQueryError as exc:
            if not _is_already_indexed_error(exc):
                raise

    async def vector_search(
        self,
        query_embedding: list[float],
        index_name: str = "entity_embedding_index",
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entities using vector similarity."""
        _ = index_name
        if limit <= 0:
            return []

        candidate_limit = min(
            limit * _VECTOR_SEARCH_PROJECT_OVERFETCH_FACTOR,
            _VECTOR_SEARCH_PROJECT_OVERFETCH_LIMIT,
        )
        cypher = (
            "CALL db.idx.vector.queryNodes('_Entity', 'embedding', "
            "$candidate_limit, vecf32($embedding)) "
            "YIELD node, score "
            "WHERE score >= $min_score "
            "RETURN node.entity_key AS entity_key, node.name AS name, "
            "node.entity_type AS entity_type, node.project_id AS project_id, "
            "labels(node) AS labels, score, "
            "properties(node) AS props"
        )
        rows = await self.query(
            cypher,
            {
                "embedding": query_embedding,
                "candidate_limit": candidate_limit,
                "min_score": min_score,
            },
        )
        filtered = [
            row
            for row in rows
            if row.get("project_id") == project_id and row.get("score", 0.0) >= min_score
        ]
        return filtered[:limit]

    async def execute_read(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Adapter for CodeGraph compatibility. Delegates to query()."""
        return await self.query(cypher, params)

    async def execute_write(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Adapter for CodeGraph compatibility. Delegates to query()."""
        return await self.query(cypher, params)

    async def ping(self) -> bool:
        """Check if FalkorDB is reachable."""
        try:
            await self.query("RETURN 1 AS ok")
            return True
        except (FalkorConnectionError, FalkorQueryError):
            return False

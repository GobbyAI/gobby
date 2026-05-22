"""Async FalkorDB client for graph memory operations."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Sequence
from typing import Any

import redis.exceptions

logger = logging.getLogger(__name__)
_VECTOR_SEARCH_PROJECT_OVERFETCH_FACTOR = 5
_VECTOR_SEARCH_PROJECT_OVERFETCH_LIMIT = 200
_CONSTRAINT_POLL_INTERVAL_SECONDS = 0.25
_CONSTRAINT_READY_TIMEOUT_SECONDS = 30.0


class FalkorConnectionError(Exception):
    """Raised when unable to connect to FalkorDB."""


class FalkorQueryError(Exception):
    """Raised when a Cypher query returns an error."""

    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body


_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AUTH_ERROR_PREFIXES = ("NOAUTH", "WRONGPASS", "AUTH")


def _validate_cypher_identifier(value: str, kind: str = "identifier") -> None:
    """Validate a value before interpolating it into Cypher."""
    if not _CYPHER_IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid Cypher {kind}: {value!r}. Must match [A-Za-z_][A-Za-z0-9_]*.")


def _response_error_message(exc: BaseException) -> str:
    raw_message = exc.args[0] if exc.args else str(exc)
    return str(raw_message)


def _is_auth_response_error(exc: BaseException) -> bool:
    message = _response_error_message(exc).upper()
    return message.startswith(_AUTH_ERROR_PREFIXES)


def _is_already_exists_error(exc: FalkorQueryError | redis.exceptions.ResponseError) -> bool:
    message = str(exc).lower()
    return (
        "already indexed" in message
        or "already exists" in message
        or "already exist" in message
        or "constraint already exists" in message
        or "index already exists" in message
    )


def _as_string(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _header_name(column: Any) -> str:
    if isinstance(column, (str, bytes)):
        return _as_string(column)
    if isinstance(column, Sequence) and not isinstance(column, (str, bytes)):
        values = list(column)
        if len(values) >= 2 and not isinstance(values[0], (str, bytes)):
            return _as_string(values[1])
        if values:
            return _as_string(values[0])
    if isinstance(column, dict):
        for key in ("name", "alias", "column"):
            if key in column:
                return _as_string(column[key])
    return _as_string(column)


def _constraint_properties(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode()]
    if isinstance(value, Sequence):
        return [_as_string(item) for item in value]
    return [_as_string(value)]


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
        from falkordb.asyncio import FalkorDB

        self._host = host
        self._port = port
        self._graph_name = graph_name
        self._db: Any = FalkorDB(
            host=host,
            port=port,
            password=password,
            socket_timeout=timeout,
        )
        self._graph: Any = self._db.select_graph(graph_name)

    @property
    def base_url(self) -> str:
        return f"redis://{self._host}:{self._port}"

    async def close(self) -> None:
        """Close the underlying FalkorDB connection."""
        close = getattr(self._db, "aclose", None)
        if close is None:
            connection = getattr(self._db, "connection", None)
            close = getattr(connection, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def query(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query and return rows as flat dictionaries."""
        try:
            result = await self._graph.query(cypher, params)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise FalkorConnectionError(f"FalkorDB connection failed: {exc}") from exc
        except redis.exceptions.ResponseError as exc:
            if _is_auth_response_error(exc):
                raise FalkorConnectionError(f"FalkorDB authentication failed: {exc}") from exc
            raise FalkorQueryError(message=str(exc), response_body=exc.args) from exc

        return self._parse_falkor_result(result)

    @staticmethod
    def _parse_falkor_result(result: Any) -> list[dict[str, Any]]:
        """Parse a FalkorDB query result into Neo4jClient-compatible row dicts."""
        header = getattr(result, "header", None)
        records = getattr(result, "result_set", None)

        if header is None and isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
            if len(result) >= 2:
                header = result[0]
                records = result[1]
            else:
                header = []
                records = []

        fields = [_header_name(column) for column in header or []]
        rows: list[dict[str, Any]] = []
        for record in records or []:
            if isinstance(record, dict):
                rows.append(dict(record))
                continue

            row: dict[str, Any] = {}
            if isinstance(record, Sequence) and not isinstance(record, (str, bytes)):
                for index, field in enumerate(fields):
                    row[field] = record[index] if index < len(record) else None
            rows.append(row)
        return rows

    @staticmethod
    def _clean_props(props: Any) -> dict[str, Any]:
        """Strip large/unhelpful properties like embedding vectors."""
        if not isinstance(props, dict):
            return {}
        return {
            k: v
            for k, v in props.items()
            if not (isinstance(v, list) and len(v) > 20) and k not in ("embedding",)
        }

    async def ensure_memory_graph_schema(self) -> None:
        """Ensure constraints and indexes for the memory knowledge graph."""
        await self.ensure_supporting_index("_Entity", "entity_key")
        await self.ensure_unique_constraint("_Entity", "entity_key")
        await self.ensure_supporting_index("Memory", "memory_id")
        await self.ensure_unique_constraint("Memory", "memory_id")
        await self._ensure_index("_Entity", ("project_id", "entity_type"))

    async def _ensure_index(self, label: str, props: tuple[str, ...]) -> None:
        _validate_cypher_identifier(label, "label")
        for prop in props:
            _validate_cypher_identifier(prop, "property name")
        prop_clause = ", ".join(f"n.{prop}" for prop in props)
        try:
            await self.query(f"CREATE INDEX FOR (n:{label}) ON ({prop_clause})")
        except FalkorQueryError as exc:
            if not _is_already_exists_error(exc):
                raise
            logger.debug("FalkorDB index already exists for %s(%s)", label, ", ".join(props))

    async def ensure_supporting_index(self, label: str, prop: str) -> None:
        """Ensure the exact-match index required before a unique constraint."""
        await self._ensure_index(label, (prop,))

    async def ensure_unique_constraint(self, label: str, prop: str) -> None:
        """Create a unique node constraint and wait until FalkorDB reports it ready."""
        _validate_cypher_identifier(label, "label")
        _validate_cypher_identifier(prop, "property name")

        try:
            await self._db.execute_command(
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
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise FalkorConnectionError(f"FalkorDB connection failed: {exc}") from exc
        except redis.exceptions.ResponseError as exc:
            if _is_auth_response_error(exc):
                raise FalkorConnectionError(f"FalkorDB authentication failed: {exc}") from exc
            if not _is_already_exists_error(exc):
                raise FalkorQueryError(message=str(exc), response_body=exc.args) from exc

        await self._wait_for_unique_constraint(label, prop)

    async def _wait_for_unique_constraint(self, label: str, prop: str) -> None:
        deadline = time.monotonic() + _CONSTRAINT_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            rows = await self.query("CALL db.constraints()")
            status = self._matching_constraint_status(rows, label, prop)
            if status == "OPERATIONAL":
                return
            if status == "FAILED":
                raise FalkorQueryError(
                    f"FalkorDB unique constraint failed for {label}.{prop}",
                    response_body=rows,
                )
            await asyncio.sleep(_CONSTRAINT_POLL_INTERVAL_SECONDS)

        raise FalkorQueryError(
            f"Timed out waiting for FalkorDB unique constraint {label}.{prop}",
            response_body={"label": label, "property": prop},
        )

    @staticmethod
    def _matching_constraint_status(
        rows: list[dict[str, Any]],
        label: str,
        prop: str,
    ) -> str | None:
        for row in rows:
            constraint_type = _as_string(row.get("type", "")).upper()
            row_label = _as_string(row.get("label", ""))
            entity_type = _as_string(
                row.get("entitytype", row.get("entityType", row.get("entity_type", "")))
            ).upper()
            properties = _constraint_properties(row.get("properties"))
            if (
                constraint_type == "UNIQUE"
                and row_label == label
                and entity_type == "NODE"
                and properties == [prop]
            ):
                return _as_string(row.get("status", "")).upper()
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
                if not source_key or not target_key:
                    continue
                if source_key not in seen_entities or target_key not in seen_entities:
                    continue
                rel_type = row.get("rel_type", "RELATED")
                props = row.get("props", {})
                relationships.append(
                    {
                        "source_key": source_key,
                        "target_key": target_key,
                        "type": rel_type,
                        "properties": self._clean_props(props),
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
            "RETURN a.entity_key AS source_key, a.name AS source_name, "
            "properties(a) AS source_props, "
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
            rel_type = row.get("rel_type", "RELATED")
            is_outgoing = row.get("is_outgoing", True)

            if source_key and target_key:
                rel_props = self._clean_props(row.get("rel_props", {}))
                if is_outgoing:
                    relationships.append(
                        {
                            "source_key": source_key,
                            "target_key": target_key,
                            "type": rel_type,
                            "properties": rel_props,
                        }
                    )
                else:
                    relationships.append(
                        {
                            "source_key": target_key,
                            "target_key": source_key,
                            "type": rel_type,
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
        """Set a vector property on a node using FalkorDB's vecf32 value."""
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
        """Create a vector index on _Entity embedding values if it does not exist."""
        _validate_cypher_identifier(index_name, "index name")
        _validate_cypher_identifier(similarity, "similarity function")
        cypher = (
            "CREATE VECTOR INDEX FOR (n:_Entity) ON (n.embedding) "
            f"OPTIONS {{dimension: {int(dimension)}, similarityFunction: '{similarity}'}}"
        )
        try:
            await self.query(cypher)
        except FalkorQueryError as exc:
            if not _is_already_exists_error(exc):
                raise
            logger.debug("FalkorDB vector index already exists: %s", index_name)

    async def vector_search(
        self,
        query_embedding: list[float],
        index_name: str = "entity_embedding_index",
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entities using vector similarity."""
        if limit <= 0:
            return []

        _validate_cypher_identifier(index_name, "index name")
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

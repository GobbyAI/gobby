"""Async FalkorDB client for memory and code graph operations."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Sequence
from typing import Any, NoReturn

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


def _raise_mapped_response_error(exc: redis.exceptions.ResponseError) -> NoReturn:
    if _is_auth_response_error(exc):
        raise FalkorConnectionError(f"FalkorDB authentication failed: {exc}") from exc
    raise FalkorQueryError(message=str(exc), response_body=exc.args) from exc


def _is_already_exists_error(exc: BaseException) -> bool:
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


def _is_non_text_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _header_name(column: Any) -> str:
    if isinstance(column, (str, bytes)):
        return _as_string(column)
    if isinstance(column, dict):
        for key in ("name", "alias", "column", "property"):
            if key in column:
                return _as_string(column[key])
    if _is_non_text_sequence(column):
        values = list(column)
        if len(values) >= 2 and not isinstance(values[0], (str, bytes)):
            return _as_string(values[1])
        if values:
            return _as_string(values[0])
    return _as_string(getattr(column, "name", column))


def _constraint_properties(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode()]
    if _is_non_text_sequence(value):
        return [_as_string(item) for item in value]
    return [_as_string(value)]


def _normalize_relationship_type(rel_type: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", rel_type)
    if normalized and normalized[0].isdigit():
        normalized = "_" + normalized
    _validate_cypher_identifier(normalized, "relationship type")
    return normalized


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class FalkorClient:
    """Async FalkorDB client for graph operations."""

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
        targets = (
            getattr(self, "_db", None),
            getattr(getattr(self, "_db", None), "connection", None),
            getattr(self, "_client", None),
        )
        for target in targets:
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
        """Execute a Cypher query and return rows as flat dictionaries."""
        try:
            result = await self._graph.query(cypher, params)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise FalkorConnectionError(f"FalkorDB connection failed: {exc}") from exc
        except redis.exceptions.ResponseError as exc:
            _raise_mapped_response_error(exc)

        return self._parse_falkor_result(result)

    @staticmethod
    def _parse_falkor_result(result: Any) -> list[dict[str, Any]]:
        """Parse a FalkorDB query result into flat row dictionaries."""
        if result is None:
            return []
        if isinstance(result, list):
            if not result:
                return []
            if isinstance(result[0], dict):
                return [dict(row) for row in result]
            if len(result) < 2:
                return [{"value": value} for value in result]

        header = getattr(result, "header", None)
        records = getattr(result, "result_set", None)
        if records is None:
            records = getattr(result, "records", None)
        if records is None:
            records = getattr(result, "rows", None)

        if header is None and _is_non_text_sequence(result):
            values = list(result)
            if len(values) >= 2:
                header = values[0]
                records = values[1]
            else:
                header = []
                records = []

        fields = [_header_name(column) for column in header or []]
        rows: list[dict[str, Any]] = []
        for record in records or []:
            if isinstance(record, dict):
                rows.append(dict(record))
                continue

            values = getattr(record, "values", record)
            if callable(values):
                values = list(values())

            row: dict[str, Any] = {}
            if _is_non_text_sequence(values):
                for index, field in enumerate(fields):
                    row[field] = values[index] if index < len(values) else None
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

    @classmethod
    def _entity_payload(
        cls,
        *,
        key: str,
        name: str,
        props: Any,
        entity_type: Any = None,
        project_id: Any = None,
    ) -> dict[str, Any]:
        props = cls._clean_props(props)
        return {
            "entity_key": key,
            "name": name,
            "entity_type": entity_type or props.get("entity_type") or "entity",
            "project_id": project_id if project_id is not None else props.get("project_id"),
            "properties": props,
        }

    @classmethod
    def _relationship_payload(
        cls,
        *,
        source_key: str,
        target_key: str,
        rel_type: Any,
        props: Any,
    ) -> dict[str, Any]:
        return {
            "source_key": source_key,
            "target_key": target_key,
            "type": rel_type or "RELATED",
            "properties": cls._clean_props(props),
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
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise FalkorConnectionError(f"FalkorDB connection failed: {exc}") from exc
        except redis.exceptions.ResponseError as exc:
            if not _is_already_exists_error(exc):
                _raise_mapped_response_error(exc)
        except Exception as exc:
            if not _is_already_exists_error(exc):
                raise

        await self._wait_for_unique_constraint(label, prop)

    async def _execute_command(self, *parts: Any) -> Any:
        targets = (
            getattr(self, "_db", None),
            getattr(getattr(self, "_db", None), "connection", None),
            getattr(self, "_redis", None),
            getattr(self, "_client", None),
        )
        for target in targets:
            execute = getattr(target, "execute_command", None)
            if execute is None:
                continue
            return await _maybe_await(execute(*parts))
        raise FalkorConnectionError("FalkorDB client does not expose execute_command")

    async def _wait_for_unique_constraint(self, label: str, prop: str) -> None:
        deadline = time.monotonic() + _CONSTRAINT_READY_TIMEOUT_SECONDS
        while True:
            rows = await self.query("CALL db.constraints()")
            status = self._matching_constraint_status(rows, label, prop)
            if status == "OPERATIONAL":
                return
            if status == "FAILED":
                raise FalkorQueryError(
                    f"FalkorDB unique constraint failed with status FAILED for {label}.{prop}",
                    response_body=rows,
                )
            if time.monotonic() >= deadline:
                raise FalkorQueryError(
                    f"Timed out waiting for FalkorDB unique constraint {label}.{prop}",
                    response_body={"label": label, "property": prop},
                )
            await asyncio.sleep(_CONSTRAINT_POLL_INTERVAL_SECONDS)

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
            entities.append(
                self._entity_payload(
                    key=key,
                    name=name,
                    props=row.get("props", {}),
                    entity_type=row.get("entity_type"),
                    project_id=row.get("project_id"),
                )
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
                relationships.append(
                    self._relationship_payload(
                        source_key=source_key,
                        target_key=target_key,
                        rel_type=row.get("rel_type"),
                        props=row.get("props", {}),
                    )
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
                    self._entity_payload(
                        key=target_key,
                        name=target_name,
                        props=target_props,
                    )
                )

            source_key = row.get("source_key") or ""
            rel_type = row.get("rel_type", "RELATED")
            is_outgoing = row.get("is_outgoing", True)

            if source_key and target_key:
                relationship_source, relationship_target = (
                    (source_key, target_key) if is_outgoing else (target_key, source_key)
                )
                relationships.append(
                    self._relationship_payload(
                        source_key=relationship_source,
                        target_key=relationship_target,
                        rel_type=rel_type,
                        props=row.get("rel_props", {}),
                    )
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
                self._entity_payload(
                    key=entity_key,
                    name=name,
                    props=props,
                )
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
        extra_labels: list[str] = []
        if labels:
            for label in labels:
                _validate_cypher_identifier(label, "label")
                if label != "_Entity" and label not in extra_labels:
                    extra_labels.append(label)
        clauses = [
            "MERGE (n:_Entity {entity_key: $entity_key})",
            "ON CREATE SET n += $props, n.created_at = timestamp(), n.updated_at = timestamp()",
            "ON MATCH SET n += $props, n.updated_at = timestamp()",
        ]
        if extra_labels:
            clauses.append(f"SET n:{':'.join(extra_labels)}")
        clauses.append("RETURN n.entity_key AS entity_key, n.name AS name")
        cypher = " ".join(clauses)
        return await self.query(cypher, {"entity_key": entity_key, "props": props})

    async def get_graph_counts(self, project_id: str | None = None) -> dict[str, Any]:
        """Return actual FalkorDB knowledge-graph counts."""
        if project_id is None:
            params: dict[str, Any] = {}
            node_filter = ""
            rel_filter = ""
            entity_rel_filter = ""
            mentioned_filter = ""
            code_filter = ""
            code_rel_filter = ""
        else:
            params = {"project_id": project_id}
            node_filter = "WHERE n.project_id = $project_id OR n.project = $project_id "
            rel_filter = (
                "WHERE a.project_id = $project_id OR b.project_id = $project_id "
                "OR a.project = $project_id OR b.project = $project_id "
            )
            entity_rel_filter = "WHERE a.project_id = $project_id AND b.project_id = $project_id "
            mentioned_filter = "WHERE e.project_id = $project_id AND m.project_id = $project_id "
            code_filter = "WHERE n.project = $project_id "
            code_rel_filter = (
                "WHERE e.project_id = $project_id OR c.project_id = $project_id "
                "OR c.project = $project_id "
            )

        async def _count(cypher: str) -> int:
            rows = await self.query(cypher, params)
            return int(rows[0]["total"]) if rows else 0

        return {
            "graph": self._graph_name,
            "project_id": project_id,
            "total_nodes": await _count(f"MATCH (n) {node_filter}RETURN count(n) AS total"),
            "memory_nodes": await _count(f"MATCH (n:Memory) {node_filter}RETURN count(n) AS total"),
            "entity_nodes": await _count(
                f"MATCH (n:_Entity) {node_filter}RETURN count(n) AS total"
            ),
            "code_symbol_nodes": await _count(
                f"MATCH (n:CodeSymbol) {code_filter}RETURN count(n) AS total"
            ),
            "relationships": await _count(
                f"MATCH (a)-[r]->(b) {rel_filter}RETURN count(r) AS total"
            ),
            "entity_relationships": await _count(
                f"MATCH (a:_Entity)-[r]->(b:_Entity) {entity_rel_filter}RETURN count(r) AS total"
            ),
            "mentioned_in_relationships": await _count(
                "MATCH (e:_Entity)-[r:MENTIONED_IN]->(m:Memory) "
                f"{mentioned_filter}RETURN count(r) AS total"
            ),
            "relates_to_code_relationships": await _count(
                "MATCH (e:_Entity)-[r:RELATES_TO_CODE]->(c:CodeSymbol) "
                f"{code_rel_filter}RETURN count(r) AS total"
            ),
        }

    async def merge_relationship(
        self,
        source_key: str,
        target_key: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Merge a relationship between two nodes matched by entity_key."""
        rel_type = _normalize_relationship_type(rel_type)
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
            f"OPTIONS {{dimension: $dim, similarityFunction: '{similarity}'}}"
        )
        try:
            await self.query(cypher, {"dim": int(dimension)})
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

    async def ping(self) -> bool:
        """Check if FalkorDB is reachable."""
        try:
            await self.query("RETURN 1 AS ok")
            return True
        except (FalkorConnectionError, FalkorQueryError):
            return False

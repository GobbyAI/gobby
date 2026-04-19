"""Async Neo4j HTTP client.

Provides a direct HTTP client for the Neo4j HTTP Query API v2,
used to query the knowledge graph built by the KnowledgeGraphService.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Neo4jConnectionError(Exception):
    """Raised when unable to connect to the Neo4j HTTP API."""


class Neo4jQueryError(Exception):
    """Raised when a Cypher query returns an error."""

    def __init__(self, message: str, status_code: int = 0, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_cypher_identifier(value: str, kind: str = "identifier") -> None:
    """Validate that a value is a safe Cypher identifier.

    Raises ValueError if the value contains characters that could
    enable Cypher injection when interpolated into a query.
    """
    if not _CYPHER_IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid Cypher {kind}: {value!r}. Must match [A-Za-z_][A-Za-z0-9_]*.")


class Neo4jClient:
    """Async HTTP client for the Neo4j HTTP Query API v2.

    Args:
        url: Neo4j HTTP API base URL (e.g. http://localhost:7474)
        auth: Authentication string in 'user:password' format
        database: Neo4j database name (default: 'neo4j')
        timeout: Request timeout in seconds
    """

    def __init__(
        self,
        url: str,
        auth: str | None = None,
        database: str = "neo4j",
        timeout: float = 15.0,
    ):
        self._base_url = url.rstrip("/")
        self._database = database
        self._timeout = timeout

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth:
            encoded = base64.b64encode(auth.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def query(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query via the HTTP Query API v2.

        Args:
            cypher: Cypher query string
            params: Query parameters

        Returns:
            List of result rows as dicts
        """
        body: dict[str, Any] = {"statement": cypher}
        if params:
            body["parameters"] = params

        path = f"/db/{self._database}/query/v2"

        try:
            response = await self._client.post(path, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise Neo4jConnectionError(f"Neo4j connection refused: {e}") from e
        except httpx.TimeoutException as e:
            raise Neo4jConnectionError(f"Neo4j request timed out: {e}") from e

        if not response.is_success:
            try:
                resp_body = response.json()
            except Exception:
                resp_body = response.text
            raise Neo4jQueryError(
                f"Neo4j query error: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=resp_body,
            )

        data = response.json()
        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse Neo4j HTTP API v2 response into flat row dicts.

        The v2 API returns: {"data": {"fields": [...], "values": [[...], ...]}}
        """
        result_data = data.get("data", {})
        fields = result_data.get("fields", [])
        values = result_data.get("values", [])

        rows: list[dict[str, Any]] = []
        for row_values in values:
            row: dict[str, Any] = {}
            for i, field in enumerate(fields):
                val = row_values[i] if i < len(row_values) else None
                row[field] = val
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
            if not (isinstance(v, list) and len(v) > 20)  # skip embedding vectors
            and k not in ("embedding",)
        }

    async def ensure_memory_graph_schema(self) -> None:
        """Ensure constraints and indexes for the memory knowledge graph."""
        await self.query(
            "CREATE CONSTRAINT entity_key_unique IF NOT EXISTS "
            "FOR (e:_Entity) REQUIRE e.entity_key IS UNIQUE"
        )
        await self.query(
            "CREATE CONSTRAINT memory_id_unique IF NOT EXISTS "
            "FOR (m:Memory) REQUIRE m.memory_id IS UNIQUE"
        )
        await self.query(
            "CREATE INDEX entity_scope_lookup IF NOT EXISTS "
            "FOR (e:_Entity) ON (e.project_id, e.entity_type)"
        )

    async def get_entity_graph(
        self,
        limit: int = 500,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get entities and relationships for visualization.

        Returns:
            Dict with 'entities' and 'relationships' lists
        """
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
        """Expand a single entity's connections.

        Args:
            entity_key: Stable key of the entity to expand

        Returns:
            Dict with 'entities' and 'relationships' for the neighborhood
        """
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

        # Add the center entity itself if not already present
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
        """Merge a node by stable key, creating or updating it.

        Uses MERGE with ON CREATE SET / ON MATCH SET to upsert.

        Args:
            entity_key: Stable entity key used as the merge identity
            name: Display name for the node
            labels: Neo4j labels to apply (e.g. ["Person", "Engineer"])
            properties: Additional properties to set on the node
        """
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
            "ON CREATE SET n += $props, n.created_at = datetime(), n.updated_at = datetime() "
            "ON MATCH SET n += $props, n.updated_at = datetime() "
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
        """Merge a relationship between two nodes matched by entity_key.

        Args:
            source_key: Source entity key
            target_key: Target entity key
            rel_type: Relationship type (e.g. "KNOWS")
            properties: Properties to set on the relationship
        """
        # Sanitize LLM-extracted relationship types: replace non-alphanumeric characters with underscores
        rel_type = re.sub(r"[^A-Za-z0-9_]", "_", rel_type)
        # Cypher identifiers cannot start with a digit
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
            cypher, {"source_key": source_key, "target_key": target_key, "props": props}
        )

    async def set_node_vector(
        self,
        entity_key: str,
        embedding: list[float],
        property_name: str = "embedding",
    ) -> list[dict[str, Any]]:
        """Set a vector property on a node using Neo4j's vector procedure.

        Args:
            entity_key: Stable key of the node to set the vector on
            embedding: The embedding vector
            property_name: Vector property name (default: 'embedding')
        """
        _validate_cypher_identifier(property_name, "property name")
        cypher = (
            "MATCH (n:_Entity {entity_key: $entity_key}) "
            f"CALL db.create.setNodeVectorProperty(n, '{property_name}', $embedding) "
            "RETURN n.entity_key AS entity_key"
        )
        return await self.query(
            cypher,
            {"entity_key": entity_key, "embedding": embedding},
        )

    async def ensure_vector_index(
        self,
        index_name: str = "entity_embedding_index",
        dimensions: int = 1536,
        similarity: str = "cosine",
    ) -> None:
        """Create a vector index on _Entity nodes if it doesn't exist.

        Args:
            index_name: Name for the vector index
            dimensions: Embedding vector dimensions
            similarity: Similarity function (cosine, euclidean)
        """
        _validate_cypher_identifier(index_name, "index name")
        cypher = (
            f"CREATE VECTOR INDEX {index_name} IF NOT EXISTS "
            "FOR (n:_Entity) ON (n.embedding) "
            "OPTIONS {indexConfig: {"
            f"`vector.dimensions`: {int(dimensions)}, "
            f"`vector.similarity_function`: '{similarity}'"
            "}}"
        )
        await self.query(cypher)

    async def vector_search(
        self,
        query_embedding: list[float],
        index_name: str = "entity_embedding_index",
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entities using vector similarity.

        Args:
            query_embedding: The query embedding vector
            index_name: Name of the vector index to search
            limit: Maximum number of results
            min_score: Minimum similarity score threshold
            project_id: Scope to a project or globals when None

        Returns:
            List of dicts with name, labels, score, and properties
        """
        _validate_cypher_identifier(index_name, "index name")
        cypher = (
            f"CALL db.index.vector.queryNodes('{index_name}', $limit, $embedding) "
            "YIELD node, score "
            "WHERE score >= $min_score "
            "AND (node.project_id = $project_id OR ($project_id IS NULL AND node.project_id IS NULL)) "
            "RETURN node.entity_key AS entity_key, node.name AS name, "
            "node.entity_type AS entity_type, node.project_id AS project_id, "
            "labels(node) AS labels, score, "
            "properties(node) AS props"
        )
        return await self.query(
            cypher,
            {
                "embedding": query_embedding,
                "limit": limit,
                "min_score": min_score,
                "project_id": project_id,
            },
        )

    async def execute_read(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Adapter for CodeGraph compatibility. Delegates to query()."""
        return await self.query(cypher, params)

    async def execute_write(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Adapter for CodeGraph compatibility. Delegates to query()."""
        return await self.query(cypher, params)

    async def ping(self) -> bool:
        """Check if Neo4j is reachable."""
        try:
            await self.query("RETURN 1 AS ok")
            return True
        except (Neo4jConnectionError, Neo4jQueryError):
            return False

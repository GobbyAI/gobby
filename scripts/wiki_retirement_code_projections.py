"""Exact code projection recovery for the one-time wiki retirement.

Only the native manifest's files, versions and symbols define owned records. Shared
collection settings and graph endpoints are recovery scaffolding, never deletion
targets. The caller binds backend identities and fences isolated writers before
constructing an adapter with ``isolated=True``; this module never deletes records.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import UUID

from falkordb.graph import Graph
from falkordb.node import Node
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from scripts.wiki_retirement_inventory import RetirementError, canonical, sha

GRAPH = "gobby_code"
PREFIX = "code_symbols_"
BATCH = 256
NODE_KEYS = {
    "CodeFile": "path",
    "CodeSymbol": "id",
    "CodeModule": "name",
    "ExternalSymbol": "id",
    "UnresolvedCallee": "id",
}
HERITAGE = {"INHERITS", "EXTENDS", "IMPLEMENTS"}
RELATIONS = HERITAGE | {"IMPORTS", "DEFINES", "CALLS"}


class RedisClient(Protocol):
    """Synchronous redis-py surface used by FalkorDB's official Graph adapter."""

    def exists(self, *names: str) -> object: ...

    def execute_command(self, *args: Any, **kwargs: Any) -> Any: ...


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Version(Record):
    id: str
    content_hash: str = Field(min_length=1)
    symbol_ids: list[str]


class File(Record):
    file_path: str
    versions: list[Version]


class GraphNode(Record):
    label: str
    properties: dict[str, Any]

    @property
    def key(self) -> str:
        return canonical(
            [self.label, self.properties["project"], self.properties[NODE_KEYS[self.label]]]
        ).decode()


class GraphEdge(Record):
    source: str
    target: str
    relation: str
    properties: dict[str, Any]
    count: int = Field(gt=0)

    @property
    def key(self) -> str:
        return canonical([self.source, self.target, self.relation, self.properties]).decode()


class VectorSnapshot(Record):
    collection: str
    config: dict[str, Any] | None
    payload_schema: dict[str, Any]
    points: dict[str, dict[str, Any]]


class GraphSnapshot(Record):
    name: str = GRAPH
    nodes: dict[str, GraphNode]
    edges: dict[str, GraphEdge]
    endpoints: dict[str, GraphNode]


class Snapshot(Record):
    schema_version: int = 1
    project_id: str
    selection: list[File]
    selection_digest: str
    qdrant: VectorSnapshot
    graph: GraphSnapshot


def _selection(project_id: str, files: list[dict[str, Any]]) -> list[File]:
    if str(UUID(project_id)) != project_id:
        raise RetirementError("Code projection project must be a canonical UUID")
    selected = [File.model_validate(item) for item in files]
    paths: set[str] = set()
    versions: set[str] = set()
    symbols: set[str] = set()
    for file in selected:
        path = PurePosixPath(file.file_path)
        if (
            not file.file_path
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != file.file_path
            or "\x00" in file.file_path
            or file.file_path in paths
        ):
            raise RetirementError("Code projection selection has an invalid or duplicate path")
        paths.add(file.file_path)
        hashes: set[str] = set()
        for version in file.versions:
            if str(UUID(version.id)) != version.id or not re.fullmatch(
                "[0-9a-f]{64}", version.content_hash
            ):
                raise RetirementError("Code projection selection has an invalid version identity")
            if version.id in versions or version.content_hash in hashes:
                raise RetirementError("Code projection selection repeats a content version")
            versions.add(version.id)
            hashes.add(version.content_hash)
            for symbol in version.symbol_ids:
                if str(UUID(symbol)) != symbol or symbol in symbols:
                    raise RetirementError(
                        "Code projection selection repeats or misidentifies a symbol"
                    )
                symbols.add(symbol)
            version.symbol_ids.sort()
        file.versions.sort(key=lambda item: item.id)
    return sorted(selected, key=lambda item: item.file_path)


def _selection_digest(files: list[File]) -> str:
    return sha(canonical([file.model_dump(mode="json") for file in files]))


def _owned(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {
        "points": snapshot.qdrant.points,
        "nodes": {key: node.model_dump(mode="json") for key, node in snapshot.graph.nodes.items()},
        "edges": {key: edge.model_dump(mode="json") for key, edge in snapshot.graph.edges.items()},
    }


def owned_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Stable keyed owned records; an empty map means absence for every selected target."""
    return _owned(_validate_snapshot(snapshot))


def fingerprint(snapshot: dict[str, Any]) -> str:
    return sha(canonical(owned_snapshot(snapshot)))


def is_absent(snapshot: dict[str, Any]) -> bool:
    return not any(owned_snapshot(snapshot).values())


def assert_remaining(current: dict[str, Any], expected: dict[str, Any]) -> None:
    """Admit only unchanged subsets, including duplicate-edge counts, after interruption."""
    actual = _validate_snapshot(current)
    before = _validate_snapshot(expected)
    if (actual.project_id, actual.selection_digest) != (before.project_id, before.selection_digest):
        raise RetirementError("Code projection snapshots belong to different native selections")
    for kind, records in _owned(actual).items():
        baseline = _owned(before)[kind]
        for key, record in records.items():
            if kind == "edges" and key in baseline:
                if record["count"] <= baseline[key]["count"]:
                    continue
            if baseline.get(key) != record:
                raise RetirementError(f"Code projection drift in {kind}: {key}")
    # Shared structures can disappear in an empty isolated recovery environment,
    # but an existing structure must not silently change meaning.
    if actual.qdrant.config is not None and (
        actual.qdrant.config != before.qdrant.config
        or any(
            before.qdrant.payload_schema.get(key) != value
            for key, value in actual.qdrant.payload_schema.items()
        )
    ):
        raise RetirementError("Shared code collection configuration changed")
    for key, node in actual.graph.endpoints.items():
        if before.graph.endpoints.get(key) != node and before.graph.nodes.get(key) != node:
            raise RetirementError(f"Shared code graph endpoint changed: {key}")


def _node(value: object, project_id: str) -> GraphNode:
    if not isinstance(value, Node) or not value.labels or len(value.labels) != 1:
        raise RetirementError("Code projection has an unsupported graph node")
    node = GraphNode(label=value.labels[0], properties=value.properties)
    if (
        node.label not in NODE_KEYS
        or node.properties.get("project") != project_id
        or not isinstance(node.properties.get(NODE_KEYS[node.label]), str)
        or not node.properties[NODE_KEYS[node.label]]
    ):
        raise RetirementError("Code projection graph endpoint lacks exact project identity")
    canonical(node.model_dump(mode="json"))
    return node


def _validate_node(node: GraphNode, project_id: str) -> None:
    _node(Node(labels=[node.label], properties=node.properties), project_id)


def _index(files: list[File]) -> tuple[dict[str, set[str]], dict[str, tuple[str, str]]]:
    hashes = {file.file_path: {v.content_hash for v in file.versions} for file in files}
    symbols = {
        symbol: (file.file_path, version.content_hash)
        for file in files
        for version in file.versions
        for symbol in version.symbol_ids
    }
    return hashes, symbols


def _validate_owned_node(node: GraphNode, files: list[File]) -> None:
    hashes, symbols = _index(files)
    props = node.properties
    if node.label == "CodeFile" and props.get("path") in hashes:
        return
    symbol_id = props.get("id")
    if (
        node.label == "CodeSymbol"
        and isinstance(symbol_id, str)
        and symbols.get(symbol_id)
        == (
            props.get("file_path"),
            props.get("file_content_hash"),
        )
    ):
        return
    raise RetirementError(
        f"Unknown projected file/version/symbol outside native selection: {node.key}"
    )


def _validate_point(point: dict[str, Any], project_id: str, files: list[File]) -> None:
    _, symbols = _index(files)
    payload = point.get("payload") or {}
    symbol = str(point.get("id"))
    if (
        symbol not in symbols
        or payload.get("symbol_id") != symbol
        or payload.get("project_id") != project_id
        or payload.get("file_path") != symbols[symbol][0]
        or payload.get("source_symbol_id", symbol) != symbol
        or payload.get("source_file_path", symbols[symbol][0]) != symbols[symbol][0]
        or point.get("vector") is None
    ):
        raise RetirementError(f"Unknown projected point outside native selection: {symbol}")
    models.PointStruct.model_validate(point)


def _validate_edge(edge: GraphEdge, snapshot: Snapshot) -> None:
    graph = snapshot.graph
    nodes = graph.nodes | graph.endpoints
    if edge.relation not in RELATIONS or edge.source not in nodes or edge.target not in nodes:
        raise RetirementError("Code projection edge has unknown endpoints or relationship")
    hashes, _ = _index(snapshot.selection)
    source_path = edge.properties.get("source_file_path")
    content_hash = edge.properties.get("content_hash")
    if source_path in hashes and content_hash not in hashes[source_path]:
        raise RetirementError("Code projection edge contains an unknown native content version")
    incident = edge.source in graph.nodes or edge.target in graph.nodes
    owned_heritage = edge.relation in HERITAGE and source_path in hashes
    if not incident and not owned_heritage:
        raise RetirementError("Code projection edge is outside the selected files")
    source = nodes[edge.source]
    source_file = source.properties.get("file_path", source.properties.get("path"))
    if source_file in hashes and content_hash not in hashes[source_file]:
        raise RetirementError("Incident code edge has an unknown native content version")


def _validate_snapshot(value: dict[str, Any]) -> Snapshot:
    snapshot = Snapshot.model_validate(value)
    files = _selection(snapshot.project_id, [f.model_dump() for f in snapshot.selection])
    if (
        snapshot.schema_version != 1
        or snapshot.selection != files
        or snapshot.selection_digest != _selection_digest(files)
        or snapshot.qdrant.collection != PREFIX + snapshot.project_id
        or snapshot.graph.name != GRAPH
        or set(snapshot.graph.nodes) & set(snapshot.graph.endpoints)
    ):
        raise RetirementError("Code projection snapshot identity differs from native selection")
    for key, node in (snapshot.graph.nodes | snapshot.graph.endpoints).items():
        _validate_node(node, snapshot.project_id)
        if key != node.key:
            raise RetirementError("Code projection snapshot node key changed")
        if key in snapshot.graph.nodes:
            _validate_owned_node(node, files)
        else:
            hashes, symbols = _index(files)
            if (node.label == "CodeFile" and node.properties.get("path") in hashes) or (
                node.label == "CodeSymbol"
                and (
                    node.properties.get("file_path") in hashes
                    or node.properties.get("id") in symbols
                )
            ):
                raise RetirementError(
                    "Owned code node was mislabeled as shared recovery scaffolding"
                )
    for key, edge in snapshot.graph.edges.items():
        if key != edge.key:
            raise RetirementError("Code projection snapshot edge key changed")
        _validate_edge(edge, snapshot)
    referenced = {
        key for edge in snapshot.graph.edges.values() for key in (edge.source, edge.target)
    }
    if set(snapshot.graph.endpoints) - referenced:
        raise RetirementError("Code recovery contains unnecessary external graph scaffolding")
    if snapshot.qdrant.config is None and (
        snapshot.qdrant.points or snapshot.qdrant.payload_schema
    ):
        raise RetirementError("Absent code collection cannot contain recovery records")
    for key, point in snapshot.qdrant.points.items():
        if key != str(point.get("id")):
            raise RetirementError("Code projection snapshot point key changed")
        _validate_point(point, snapshot.project_id, files)
    return snapshot


class CodeProjections:
    def __init__(self, qdrant: QdrantClient, redis: RedisClient, *, isolated: bool = False):
        self.qdrant = qdrant
        self.redis = redis
        self.graph = Graph(redis, GRAPH)
        self.isolated = isolated

    def discover_paths(self, project_id: str) -> set[str]:
        """Read candidate paths, without granting retirement ownership to any path.

        The caller must prove vault containment or an archived Git deletion before
        adding a candidate to the native selection. Absence never creates a store.
        """
        _selection(project_id, [])
        paths: set[str] = set()

        def add(value: object) -> None:
            if value is None:
                return
            if not isinstance(value, str) or not value:
                raise RetirementError("Code projection census found an invalid path property")
            paths.add(value)

        collection = PREFIX + project_id
        if self.qdrant.collection_exists(collection):
            offset = None
            while True:
                points, offset = self.qdrant.scroll(
                    collection,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="project_id", match=models.MatchValue(value=project_id)
                            )
                        ]
                    ),
                    offset=offset,
                    limit=BATCH,
                    with_payload=["file_path", "source_file_path"],
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    add(payload.get("file_path"))
                    add(payload.get("source_file_path"))
                if offset is None:
                    break
        if not self.redis.exists(GRAPH):
            return paths
        for row in self._read(
            "MATCH (n {project:$project}) WHERE (n:CodeFile OR n:CodeSymbol)",
            {"project": project_id},
        ):
            node = _node(row[0], project_id)
            add(node.properties.get("path" if node.label == "CodeFile" else "file_path"))
        after = -1
        while True:
            rows = self.graph.ro_query(
                "MATCH (s {project:$project})-[r]->() "
                "WHERE r.source_file_path IS NOT NULL AND id(r)>$after "
                "RETURN id(r), r.source_file_path ORDER BY id(r) LIMIT $limit",
                {"project": project_id, "after": after, "limit": BATCH},
                timeout=30000,
            ).result_set
            for row in rows:
                add(row[1])
            if not rows or len(rows) < BATCH:
                return paths
            after = rows[-1][0]

    def capture(self, project_id: str, files: list[dict[str, Any]]) -> dict[str, Any]:
        selected = _selection(project_id, files)
        snapshot = Snapshot(
            project_id=project_id,
            selection=selected,
            selection_digest=_selection_digest(selected),
            qdrant=self._capture_vectors(project_id, selected),
            graph=self._capture_graph(project_id, selected),
        )
        value = snapshot.model_dump(mode="json")
        _validate_snapshot(value)
        return value

    def _capture_vectors(self, project_id: str, files: list[File]) -> VectorSnapshot:
        collection = PREFIX + project_id
        snapshot = VectorSnapshot(collection=collection, config=None, payload_schema={}, points={})
        if not self.qdrant.collection_exists(collection):
            return snapshot
        info = self.qdrant.get_collection(collection)
        snapshot.config = info.config.model_dump(mode="json")
        snapshot.payload_schema = {
            key: value.model_dump(mode="json", exclude={"points"})
            for key, value in info.payload_schema.items()
        }
        paths, symbols = _index(files)
        clauses: list[models.Condition] = []
        for offset in range(0, len(paths), BATCH):
            for field in ("file_path", "source_file_path"):
                clauses.append(
                    models.FieldCondition(
                        key=field, match=models.MatchAny(any=list(paths)[offset : offset + BATCH])
                    )
                )
        for offset in range(0, len(symbols), BATCH):
            clauses.append(models.HasIdCondition(has_id=list(symbols)[offset : offset + BATCH]))
        for clause in clauses:
            offset_id = None
            while True:
                points, offset_id = self.qdrant.scroll(
                    collection,
                    scroll_filter=models.Filter(should=[clause]),
                    offset=offset_id,
                    limit=BATCH,
                    with_payload=True,
                    with_vectors=True,
                )
                for point in points:
                    if point.shard_key is not None:
                        raise RetirementError(
                            "Custom-sharded code points require explicit recovery support"
                        )
                    value = point.model_dump(mode="json", include={"id", "payload", "vector"})
                    _validate_point(value, project_id, files)
                    key = str(point.id)
                    if key in snapshot.points and snapshot.points[key] != value:
                        raise RetirementError("Code point changed while capturing the inventory")
                    snapshot.points[key] = value
                if offset_id is None:
                    break
        return snapshot

    def _read(self, cypher: str, params: dict[str, Any]) -> list[list[Any]]:
        rows: list[list[Any]] = []
        after = -1
        while True:
            result = self.graph.ro_query(
                cypher + " AND id(n) > $after RETURN n ORDER BY id(n) LIMIT $limit",
                params | {"after": after, "limit": BATCH},
                timeout=30000,
            ).result_set
            if not result:
                return rows
            rows.extend(result)
            after = result[-1][0].id
            if len(result) < BATCH:
                return rows

    def _capture_graph(self, project_id: str, files: list[File]) -> GraphSnapshot:
        snapshot = GraphSnapshot(nodes={}, edges={}, endpoints={})
        if not self.redis.exists(GRAPH):
            return snapshot
        paths, symbols = _index(files)
        internal: dict[str, int] = {}
        node_ids: set[int] = set()
        for key, values in (("paths", list(paths)), ("symbols", list(symbols))):
            for offset in range(0, len(values), BATCH):
                condition = (
                    "((n:CodeFile AND n.path IN $values) OR "
                    "(n:CodeSymbol AND n.file_path IN $values))"
                    if key == "paths"
                    else "(n:CodeSymbol AND n.id IN $values)"
                )
                rows = self._read(
                    f"MATCH (n {{project: $project}}) WHERE {condition}",
                    {"project": project_id, "values": values[offset : offset + BATCH]},
                )
                for row in rows:
                    node = _node(row[0], project_id)
                    _validate_owned_node(node, files)
                    if node.key in internal and internal[node.key] != row[0].id:
                        raise RetirementError("Duplicate logical code graph node")
                    if node.key in snapshot.nodes and snapshot.nodes[node.key] != node:
                        raise RetirementError("Code graph node changed during capture")
                    internal[node.key] = row[0].id
                    node_ids.add(row[0].id)
                    snapshot.nodes[node.key] = node
        edges: dict[int, GraphEdge] = {}
        queries: list[tuple[str, list[int] | list[str]]] = [
            (
                "MATCH (s)-[r]->(t) WHERE (id(s) IN $values OR id(t) IN $values)",
                sorted(node_ids),
            ),
            (
                "MATCH (s {project: $project})-[r]->(t) WHERE r.source_file_path IN $values",
                list(paths),
            ),
        ]
        for query, edge_values in queries:
            for offset in range(0, len(edge_values), BATCH):
                after = -1
                while True:
                    result = self.graph.ro_query(
                        query + " AND id(r) > $after RETURN s, r, t ORDER BY id(r) LIMIT $limit",
                        {
                            "project": project_id,
                            "values": edge_values[offset : offset + BATCH],
                            "after": after,
                            "limit": BATCH,
                        },
                        timeout=30000,
                    ).result_set
                    for source, edge, target in result:
                        nodes = [_node(source, project_id), _node(target, project_id)]
                        for raw, node in zip((source, target), nodes, strict=True):
                            if node.key in internal and internal[node.key] != raw.id:
                                raise RetirementError("Duplicate logical code graph endpoint")
                            internal[node.key] = raw.id
                            existing = snapshot.nodes.get(node.key) or snapshot.endpoints.get(
                                node.key
                            )
                            if existing is not None and existing != node:
                                raise RetirementError("Code graph endpoint changed during capture")
                            if node.key not in snapshot.nodes:
                                snapshot.endpoints[node.key] = node
                        record = GraphEdge(
                            source=nodes[0].key,
                            target=nodes[1].key,
                            relation=edge.relation,
                            properties=edge.properties,
                            count=1,
                        )
                        if edge.id in edges and edges[edge.id] != record:
                            raise RetirementError("Code graph edge changed during capture")
                        edges[edge.id] = record
                    if not result or len(result) < BATCH:
                        break
                    after = result[-1][1].id
        counts = Counter(edge.key for edge in edges.values())
        snapshot.edges = {
            edge.key: edge.model_copy(update={"count": counts[edge.key]}) for edge in edges.values()
        }
        return snapshot

    def restore(self, project_id: str, files: list[dict[str, Any]], value: dict[str, Any]) -> None:
        if not self.isolated:
            raise RetirementError(
                "Code projection restore requires explicitly isolated connections"
            )
        snapshot = _validate_snapshot(value)
        selected = _selection(project_id, files)
        if (snapshot.project_id, snapshot.selection_digest) != (
            project_id,
            _selection_digest(selected),
        ):
            raise RetirementError("Code projection recovery belongs to another native selection")
        current = self.capture(project_id, files)
        assert_remaining(current, value)
        # Preflight every shared endpoint before creating anything. Existing nodes
        # are never SET: MERGE's ON CREATE clause preserves unrelated consumers.
        for node in (snapshot.graph.endpoints | snapshot.graph.nodes).values():
            self._restore_node(node, create=False)
        self._restore_vectors(snapshot.qdrant)
        for node in (snapshot.graph.endpoints | snapshot.graph.nodes).values():
            self._restore_node(node, create=True)
        for edge in snapshot.graph.edges.values():
            self._restore_edge(edge, snapshot.graph)
        restored = self.capture(project_id, files)
        assert_remaining(restored, value)
        if (
            fingerprint(restored) != fingerprint(value)
            or restored["qdrant"]["config"] != value["qdrant"]["config"]
            or restored["qdrant"]["payload_schema"] != value["qdrant"]["payload_schema"]
        ):
            raise RetirementError("Restored code projections differ from the scoped backup")

    def _restore_vectors(self, snapshot: VectorSnapshot) -> None:
        if snapshot.config is None:
            return
        if not self.qdrant.collection_exists(snapshot.collection):
            config = models.CollectionConfig.model_validate(snapshot.config)
            params = config.params
            if params.read_fan_out_factor is not None or params.read_fan_out_delay_ms is not None:
                raise RetirementError(
                    "Code collection uses unsupported read fan-out recovery settings"
                )
            self.qdrant.create_collection(
                snapshot.collection,
                vectors_config=params.vectors,
                sparse_vectors_config=params.sparse_vectors,
                shard_number=params.shard_number,
                sharding_method=params.sharding_method,
                replication_factor=params.replication_factor,
                write_consistency_factor=params.write_consistency_factor,
                on_disk_payload=params.on_disk_payload,
                payload=params.payload,
                hnsw_config=models.HnswConfigDiff.model_validate(config.hnsw_config.model_dump()),
                optimizers_config=models.OptimizersConfigDiff.model_validate(
                    config.optimizer_config.model_dump()
                ),
                wal_config=models.WalConfigDiff.model_validate(config.wal_config.model_dump())
                if config.wal_config
                else None,
                quantization_config=config.quantization_config,
                strict_mode_config=(
                    models.StrictModeConfig.model_validate(config.strict_mode_config.model_dump())
                    if config.strict_mode_config
                    else None
                ),
                metadata=config.metadata,
            )
        schema = self.qdrant.get_collection(snapshot.collection).payload_schema
        for key, field in snapshot.payload_schema.items():
            if key not in schema:
                self.qdrant.create_payload_index(
                    snapshot.collection,
                    key,
                    field_schema=field.get("params") or field["data_type"],
                    wait=True,
                )
        points = list(snapshot.points.values())
        for offset in range(0, len(points), BATCH):
            batch = points[offset : offset + BATCH]
            existing = {
                str(point.id)
                for point in self.qdrant.retrieve(
                    snapshot.collection,
                    [point["id"] for point in batch],
                    with_payload=False,
                    with_vectors=False,
                )
            }
            missing = [
                models.PointStruct.model_validate(point)
                for point in batch
                if str(point["id"]) not in existing
            ]
            if missing:
                self.qdrant.upsert(snapshot.collection, points=missing, wait=True)

    def _restore_node(self, node: GraphNode, *, create: bool) -> None:
        params = {
            "project": node.properties["project"],
            "identity": node.properties[NODE_KEYS[node.label]],
            "properties": node.properties,
        }
        selector = f"(n:{node.label} {{project: $project, {NODE_KEYS[node.label]}: $identity}})"
        if not create and not self.redis.exists(GRAPH):
            return
        if create:
            result = self.graph.query(
                f"MERGE {selector} ON CREATE SET n = $properties RETURN n", params, timeout=30000
            ).result_set
        else:
            result = self.graph.ro_query(
                f"MATCH {selector} RETURN n", params, timeout=30000
            ).result_set
        if len(result) > 1 or any(_node(row[0], params["project"]) != node for row in result):
            raise RetirementError(f"Code graph restore refuses changed endpoint: {node.key}")

    def _restore_edge(self, edge: GraphEdge, graph: GraphSnapshot) -> None:
        nodes = graph.nodes | graph.endpoints
        source, target = nodes[edge.source], nodes[edge.target]
        selectors = []
        params: dict[str, Any] = {"properties": edge.properties, "expected": edge.count}
        for alias, node in (("s", source), ("t", target)):
            selectors.append(
                f"({alias}:{node.label} {{project: ${alias}_project, "
                f"{NODE_KEYS[node.label]}: ${alias}_identity}})"
            )
            params[f"{alias}_project"] = node.properties["project"]
            params[f"{alias}_identity"] = node.properties[NODE_KEYS[node.label]]
        query = (
            f"MATCH {', '.join(selectors)} OPTIONAL MATCH (s)-[r:{edge.relation}]->(t) "
            "WHERE properties(r) = $properties WITH s, t, count(r) AS found "
            "WHERE found < $expected UNWIND range(found + 1, $expected) AS copy "
            f"CREATE (s)-[restored:{edge.relation}]->(t) SET restored = $properties"
        )
        self.graph.query(query, params, timeout=30000)

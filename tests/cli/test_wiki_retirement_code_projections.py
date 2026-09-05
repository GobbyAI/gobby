"""Scoped code projection recovery, including real isolated FalkorDB roundtrips."""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from contextlib import closing
from typing import Any
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models
from redis import Redis

from scripts.wiki_retirement_code_projections import (
    GRAPH,
    CodeProjections,
    File,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    Snapshot,
    VectorSnapshot,
    _selection_digest,
    assert_remaining,
    fingerprint,
    is_absent,
    owned_snapshot,
)
from scripts.wiki_retirement_inventory import RetirementError

pytestmark = pytest.mark.cli


class AbsentRedis:
    def exists(self, *names: str) -> int:
        assert names == (GRAPH,)
        return 0

    def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("An absent graph must not be queried or created during capture")


def selection() -> tuple[str, list[dict[str, Any]]]:
    return str(uuid4()), [
        {
            "file_path": ".gobby/wiki/page.py",
            "versions": [
                {"id": str(uuid4()), "content_hash": "a" * 64, "symbol_ids": [str(uuid4())]}
            ],
        }
    ]


def point(project: str, files: list[dict[str, Any]]) -> models.PointStruct:
    symbol = files[0]["versions"][0]["symbol_ids"][0]
    return models.PointStruct(
        id=symbol,
        vector=[1.0, 0.0],
        payload={
            "project_id": project,
            "file_path": files[0]["file_path"],
            "symbol_id": symbol,
            "source_symbol_id": symbol,
            "source_file_path": files[0]["file_path"],
        },
    )


def test_absence_binds_every_selected_target_without_creating_stores() -> None:
    project, files = selection()
    with closing(QdrantClient(location=":memory:")) as qdrant:
        adapter = CodeProjections(qdrant, AbsentRedis())
        snapshot = adapter.capture(project, files)
        assert is_absent(snapshot)
        assert snapshot["selection"] == files
        assert owned_snapshot(snapshot) == {"points": {}, "nodes": {}, "edges": {}}
        assert not qdrant.get_collections().collections
        with pytest.raises(RetirementError, match="explicitly isolated"):
            adapter.restore(project, files, snapshot)


@pytest.mark.parametrize("mutation", ["project", "file", "symbol", "unselected"])
def test_capture_refuses_misbound_or_unselected_vector_points(mutation: str) -> None:
    project, files = selection()
    bad = point(project, files)
    assert bad.payload is not None
    if mutation == "project":
        bad.payload["project_id"] = str(uuid4())
    elif mutation == "file":
        bad.payload["file_path"] = "outside.py"
    elif mutation == "symbol":
        bad.payload["symbol_id"] = str(uuid4())
    else:
        bad.id = str(uuid4())
        bad.payload["symbol_id"] = bad.id
    with closing(QdrantClient(location=":memory:")) as qdrant:
        name = "code_symbols_" + project
        qdrant.create_collection(
            name, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE)
        )
        qdrant.upsert(name, [bad], wait=True)
        with pytest.raises(RetirementError, match="Unknown projected point"):
            CodeProjections(qdrant, AbsentRedis()).capture(project, files)
        assert qdrant.count(name, exact=True).count == 1


def test_vector_restore_preserves_unrelated_points_and_refuses_payload_drift() -> None:
    project, files = selection()
    with closing(QdrantClient(location=":memory:")) as qdrant:
        name = "code_symbols_" + project
        qdrant.create_collection(
            name, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE)
        )
        selected = point(project, files)
        unrelated = models.PointStruct(
            id=str(uuid4()), vector=[0.0, 1.0], payload={"file_path": "outside.py"}
        )
        qdrant.upsert(name, [selected, unrelated], wait=True)
        adapter = CodeProjections(qdrant, AbsentRedis(), isolated=True)
        before = adapter.capture(project, files)
        qdrant.delete(name, models.PointIdsList(points=[selected.id]), wait=True)
        assert is_absent(adapter.capture(project, files))
        adapter.restore(project, files, before)
        adapter.restore(project, files, before)
        assert fingerprint(adapter.capture(project, files)) == fingerprint(before)
        assert (
            qdrant.retrieve(name, [unrelated.id], with_vectors=True)[0].payload == unrelated.payload
        )
        qdrant.set_payload(name, {"name": "concurrent change"}, points=[selected.id], wait=True)
        with pytest.raises(RetirementError, match="drift in points"):
            adapter.restore(project, files, before)
        changed_payload = qdrant.retrieve(name, [selected.id])[0].payload
        assert changed_payload is not None
        assert changed_payload["name"] == "concurrent change"


def graph_snapshot() -> dict[str, Any]:
    project, files = selection()
    parsed = [File.model_validate(file) for file in files]
    node = GraphNode(
        label="CodeFile", properties={"project": project, "path": files[0]["file_path"]}
    )
    endpoint = GraphNode(label="CodeModule", properties={"project": project, "name": "outside"})
    edge = GraphEdge(
        source=node.key,
        target=endpoint.key,
        relation="IMPORTS",
        count=2,
        properties={"content_hash": "a" * 64, "source_file_path": files[0]["file_path"]},
    )
    return Snapshot(
        project_id=project,
        selection=parsed,
        selection_digest=_selection_digest(parsed),
        qdrant=VectorSnapshot(
            collection="code_symbols_" + project, config=None, payload_schema={}, points={}
        ),
        graph=GraphSnapshot(
            nodes={node.key: node}, edges={edge.key: edge}, endpoints={endpoint.key: endpoint}
        ),
    ).model_dump(mode="json")


def test_remaining_accepts_only_exact_subsets_and_edge_multiplicity() -> None:
    before = graph_snapshot()
    remaining = copy.deepcopy(before)
    edge = next(iter(remaining["graph"]["edges"].values()))
    edge["count"] = 1
    assert_remaining(remaining, before)
    edge["count"] = 3
    with pytest.raises(RetirementError, match="drift in edges"):
        assert_remaining(remaining, before)
    edge["count"] = 2
    node = next(iter(remaining["graph"]["nodes"].values()))
    node["properties"]["symbol_count"] = 99
    with pytest.raises(RetirementError, match="drift in nodes"):
        assert_remaining(remaining, before)


def test_recovery_rejects_unnecessary_or_mislabeled_scaffolding() -> None:
    snapshot = graph_snapshot()
    forged = GraphNode(
        label="CodeFile",
        properties={"project": snapshot["project_id"], "path": "original-source.py"},
    )
    snapshot["graph"]["endpoints"][forged.key] = forged.model_dump()
    with pytest.raises(RetirementError, match="unnecessary external"):
        owned_snapshot(snapshot)
    snapshot = graph_snapshot()
    key, node = snapshot["graph"]["nodes"].popitem()
    snapshot["graph"]["endpoints"][key] = node
    with pytest.raises(RetirementError, match="mislabeled"):
        owned_snapshot(snapshot)


@pytest.fixture
def real_projections() -> Iterator[tuple[CodeProjections, str, list[dict[str, Any]]]]:
    if os.environ.get("GOBBY_RETIRE_PROJECTION_TESTS") != "1":
        pytest.skip("Requires the exclusive epic21771 isolated Qdrant/Falkor fixture slot")
    assert os.environ.get("GOBBY_TEST_PROTECT") == "1"
    project, files = selection()
    qdrant = QdrantClient(url="http://127.0.0.1:6338", timeout=30)
    redis = Redis.from_url("redis://127.0.0.1:16389/0", socket_timeout=30)
    adapter = CodeProjections(qdrant, redis, isolated=True)
    try:
        yield adapter, project, files
    finally:
        name = "code_symbols_" + project
        if qdrant.collection_exists(name):
            qdrant.delete_collection(name)
        if redis.exists(GRAPH):
            adapter.graph.query(
                "MATCH (n {project: $project}) DETACH DELETE n", {"project": project}
            )
        qdrant.close()
        redis.close()


def seed_real(adapter: CodeProjections, project: str, files: list[dict[str, Any]]) -> str:
    file = files[0]
    symbol = file["versions"][0]["symbol_ids"][0]
    props = {"project": project}
    unrelated_id = str(uuid4())
    node_props = {
        "file": props | {"path": file["file_path"], "symbol_count": 1},
        "symbol": props
        | {"id": symbol, "file_path": file["file_path"], "file_content_hash": "a" * 64},
        "external": props | {"id": "external-" + project, "name": "external"},
        "base": props | {"id": "base-" + project, "name": "base"},
        "module": props | {"name": "outside"},
        "unrelated": props
        | {"id": unrelated_id, "file_path": "original.py", "file_content_hash": "b" * 64},
    }
    params = node_props | {
        "owned": {"source_file_path": file["file_path"], "content_hash": "a" * 64},
        "foreign": {"source_file_path": "original.py", "content_hash": "b" * 64},
    }
    adapter.graph.query(
        "CREATE (f:CodeFile), (s:CodeSymbol), (x:ExternalSymbol), (y:ExternalSymbol), "
        "(m:CodeModule), (u:CodeSymbol) "
        "SET f=$file, s=$symbol, x=$external, y=$base, m=$module, u=$unrelated "
        "CREATE (f)-[d:DEFINES]->(s), (f)-[i:IMPORTS]->(m), (s)-[c:CALLS]->(x), "
        "(u)-[incoming:CALLS]->(s), (x)-[heritage:EXTENDS]->(y), (u)-[keep:CALLS]->(y) "
        "SET d=$owned, i=$owned, c=$owned, incoming=$foreign, heritage=$owned, keep=$foreign",
        params,
    )
    name = "code_symbols_" + project
    adapter.qdrant.create_collection(
        name, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE)
    )
    adapter.qdrant.upsert(name, [point(project, files)], wait=True)
    return unrelated_id


def erase_selected(adapter: CodeProjections, project: str, files: list[dict[str, Any]]) -> None:
    adapter.graph.query(
        "MATCH (n {project: $project}) WHERE (n:CodeFile AND n.path=$path) "
        "OR (n:CodeSymbol AND n.file_path=$path) DETACH DELETE n",
        {"project": project, "path": files[0]["file_path"]},
    )
    adapter.graph.query(
        "MATCH (s {project:$project})-[r:INHERITS|EXTENDS|IMPLEMENTS]->() "
        "WHERE r.source_file_path=$path AND r.content_hash=$hash DELETE r",
        {"project": project, "path": files[0]["file_path"], "hash": "a" * 64},
    )
    adapter.qdrant.delete(
        "code_symbols_" + project,
        models.PointIdsList(points=files[0]["versions"][0]["symbol_ids"]),
        wait=True,
    )


@pytest.mark.integration
def test_real_scoped_graph_roundtrip_preserves_external_edges_and_repeated_restore(
    real_projections: tuple[CodeProjections, str, list[dict[str, Any]]],
) -> None:
    adapter, project, files = real_projections
    unrelated = seed_real(adapter, project, files)
    before = adapter.capture(project, files)
    assert len(before["graph"]["nodes"]) == 2
    assert len(before["graph"]["edges"]) == 5
    assert len(before["graph"]["endpoints"]) == 4
    erase_selected(adapter, project, files)
    absent = adapter.capture(project, files)
    assert is_absent(absent)
    assert_remaining(absent, before)
    assert adapter.graph.ro_query(
        "MATCH (:CodeSymbol {project:$project,id:$id})-[r:CALLS]->(:ExternalSymbol) RETURN count(r)",
        {"project": project, "id": unrelated},
    ).result_set == [[1]]
    adapter.restore(project, files, before)
    adapter.restore(project, files, before)
    assert fingerprint(adapter.capture(project, files)) == fingerprint(before)
    assert adapter.graph.ro_query(
        "MATCH (n {project:$project}) RETURN count(n)", {"project": project}
    ).result_set == [[6]]


@pytest.mark.integration
def test_real_restore_refuses_changed_shared_endpoint_before_mutation(
    real_projections: tuple[CodeProjections, str, list[dict[str, Any]]],
) -> None:
    adapter, project, files = real_projections
    seed_real(adapter, project, files)
    before = adapter.capture(project, files)
    erase_selected(adapter, project, files)
    adapter.graph.query(
        "MATCH (n:CodeModule {project:$project}) SET n.changed=true", {"project": project}
    )
    with pytest.raises(RetirementError, match="changed endpoint"):
        adapter.restore(project, files, before)
    assert is_absent(adapter.capture(project, files))


@pytest.mark.integration
def test_real_empty_target_recovery_resumes_interruption_and_preserves_duplicate_edges(
    real_projections: tuple[CodeProjections, str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, project, files = real_projections
    seed_real(adapter, project, files)
    adapter.graph.query(
        "MATCH (f:CodeFile {project:$project})-[r:IMPORTS]->(m:CodeModule) "
        "CREATE (f)-[duplicate:IMPORTS]->(m) SET duplicate=properties(r)",
        {"project": project},
    )
    before = adapter.capture(project, files)
    assert max(edge["count"] for edge in before["graph"]["edges"].values()) == 2
    # Remove only this fixture's randomly identified records to model an empty
    # isolated recovery destination; other projects in the shared graph survive.
    adapter.graph.query("MATCH (n {project:$project}) DETACH DELETE n", {"project": project})
    adapter.qdrant.delete_collection("code_symbols_" + project)
    original_query = adapter.graph.query
    edge_writes = 0

    def interrupted(query: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal edge_writes
        if "CREATE (s)-[restored:" in query:
            edge_writes += 1
            if edge_writes == 2:
                raise RuntimeError("simulated interrupted graph recovery")
        return original_query(query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(adapter.graph, "query", interrupted)
        with pytest.raises(RuntimeError, match="simulated interrupted"):
            adapter.restore(project, files, before)
    assert_remaining(adapter.capture(project, files), before)
    adapter.restore(project, files, before)
    adapter.restore(project, files, before)
    assert fingerprint(adapter.capture(project, files)) == fingerprint(before)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["symbol", "version", "inheritance"])
def test_real_capture_refuses_unselected_graph_identities(
    real_projections: tuple[CodeProjections, str, list[dict[str, Any]]],
    kind: str,
) -> None:
    adapter, project, files = real_projections
    seed_real(adapter, project, files)
    if kind == "symbol":
        query = "MATCH (n:CodeSymbol {project:$project,file_path:$path}) SET n.id=$unknown"
    elif kind == "version":
        query = "MATCH (n:CodeSymbol {project:$project,file_path:$path}) SET n.file_content_hash=$unknown"
    else:
        query = "MATCH ({project:$project})-[r:EXTENDS]->() SET r.content_hash=$unknown"
    adapter.graph.query(
        query, {"project": project, "path": files[0]["file_path"], "unknown": str(uuid4())}
    )
    with pytest.raises(RetirementError, match="Unknown projected|unknown native"):
        adapter.capture(project, files)


@pytest.mark.integration
def test_real_candidate_census_finds_projection_only_paths_without_granting_ownership(
    real_projections: tuple[CodeProjections, str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, project, files = real_projections
    seed_real(adapter, project, files)
    monkeypatch.setattr("scripts.wiki_retirement_code_projections.BATCH", 2)
    bare = ".gobby/wiki/bare.py"
    orphan = ".gobby/wiki/orphan.py"
    vector = ".gobby/wiki/vector.py"
    source_only = ".gobby/wiki/source-only.py"
    foreign = str(uuid4())
    foreign_path = ".gobby/wiki/foreign.py"
    adapter.graph.query(
        "MATCH (s:ExternalSymbol {project:$project,id:$source}), "
        "(t:ExternalSymbol {project:$project,id:$target}) "
        "CREATE (:CodeFile {project:$project,path:$bare}), "
        "(:CodeFile {project:$foreign,path:$foreign_path}), "
        "(s)-[:CALLS {source_file_path:$orphan,content_hash:$hash}]->(t)",
        {
            "project": project,
            "source": "external-" + project,
            "target": "base-" + project,
            "bare": bare,
            "foreign": foreign,
            "foreign_path": foreign_path,
            "orphan": orphan,
            "hash": "c" * 64,
        },
    )
    collection = "code_symbols_" + project
    adapter.qdrant.upsert(
        collection,
        [
            models.PointStruct(
                id=str(uuid4()),
                vector=[1.0, 0.0],
                payload={"project_id": project, "file_path": vector},
            ),
            models.PointStruct(
                id=str(uuid4()),
                vector=[1.0, 0.0],
                payload={
                    "project_id": project,
                    "file_path": "original.py",
                    "source_file_path": source_only,
                },
            ),
            models.PointStruct(
                id=str(uuid4()),
                vector=[1.0, 0.0],
                payload={"project_id": foreign, "file_path": foreign_path},
            ),
        ],
        wait=True,
    )
    try:
        assert adapter.discover_paths(project) == {
            files[0]["file_path"],
            "original.py",
            bare,
            orphan,
            vector,
            source_only,
        }
        assert not is_absent(adapter.capture(project, [{"file_path": bare, "versions": []}]))
        for path in (orphan, vector, source_only):
            with pytest.raises(RetirementError, match="unknown native|Unknown projected"):
                adapter.capture(project, [{"file_path": path, "versions": []}])
        untouched = str(uuid4())
        before = {item.name for item in adapter.qdrant.get_collections().collections}
        assert adapter.discover_paths(untouched) == set()
        assert {item.name for item in adapter.qdrant.get_collections().collections} == before
    finally:
        adapter.graph.query(
            "MATCH (n:CodeFile {project:$project,path:$path}) DELETE n",
            {"project": foreign, "path": foreign_path},
        )

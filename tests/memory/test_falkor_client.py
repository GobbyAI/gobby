"""Tests for FalkorClient connection, query, and public surface contracts."""

from __future__ import annotations

import inspect
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

pytestmark = pytest.mark.unit


class FakeGraph:
    def __init__(self) -> None:
        self.next_result: Any = types.SimpleNamespace(
            header=["name", "score"],
            result_set=[["Python", 0.95], ["FalkorDB", 0.91]],
            statistics={},
        )
        self.results: list[Any] = []
        self.next_error: Exception | None = None
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> Any:
        self.queries.append((cypher, params))
        if self.next_error is not None:
            raise self.next_error
        if self.results:
            return self.results.pop(0)
        return self.next_result

    async def ro_query(self, cypher: str, params: dict[str, Any] | None = None) -> Any:
        return await self.query(cypher, params)


class FakeFalkorDB:
    last_instance: FakeFalkorDB | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.graph = FakeGraph()
        self.commands: list[tuple[Any, ...]] = []
        self.next_command_error: Exception | None = None
        self.closed = False
        FakeFalkorDB.last_instance = self

    def select_graph(self, graph_name: str) -> FakeGraph:
        self.graph_name = graph_name
        return self.graph

    async def execute_command(self, *args: Any) -> str:
        self.commands.append(args)
        if self.next_command_error is not None:
            raise self.next_command_error
        return "PENDING"

    async def aclose(self) -> None:
        self.closed = True


def _install_fake_falkordb(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_package = types.ModuleType("falkordb")
    fake_asyncio = types.ModuleType("falkordb.asyncio")
    fake_asyncio.FalkorDB = FakeFalkorDB
    monkeypatch.setitem(sys.modules, "falkordb", fake_package)
    monkeypatch.setitem(sys.modules, "falkordb.asyncio", fake_asyncio)


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    _install_fake_falkordb(monkeypatch)
    from gobby.memory.falkor_client import FalkorClient

    return FalkorClient(
        host="127.0.0.1",
        port=16379,
        password="secret",
        graph_name="gobby_kg",
        timeout=9.5,
    )


def test_constructor_uses_async_falkordb_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor should select the configured graph without opening HTTP state."""
    client = _client(monkeypatch)

    fake_db = FakeFalkorDB.last_instance
    assert fake_db is not None
    assert fake_db.kwargs == {
        "host": "127.0.0.1",
        "port": 16379,
        "password": "secret",
        "socket_timeout": 9.5,
    }
    assert fake_db.graph_name == "gobby_kg"
    assert client.base_url == "redis://127.0.0.1:16379"


def test_public_surface_matches_neo4j_client_contract() -> None:
    """FalkorClient keeps the graph client methods callers already use."""
    from gobby.memory.falkor_client import FalkorClient

    expected_methods = {
        "close",
        "query",
        "ensure_memory_graph_schema",
        "ensure_vector_index",
        "ensure_supporting_index",
        "ensure_unique_constraint",
        "merge_node",
        "merge_relationship",
        "set_node_vector",
        "get_entity_graph",
        "get_entity_neighbors",
        "vector_search",
        "ping",
    }
    missing = {name for name in expected_methods if not hasattr(FalkorClient, name)}
    assert missing == set()

    query_params = inspect.signature(FalkorClient.query).parameters
    assert list(query_params) == ["self", "cypher", "params"]
    assert query_params["params"].default is None

    merge_node_params = inspect.signature(FalkorClient.merge_node).parameters
    assert list(merge_node_params) == [
        "self",
        "entity_key",
        "name",
        "project_id",
        "labels",
        "properties",
    ]


async def test_close_delegates_to_async_falkordb_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should release the redis-asyncio connection owned by FalkorDB."""
    client = _client(monkeypatch)

    await client.close()

    fake_db = FakeFalkorDB.last_instance
    assert fake_db is not None
    assert fake_db.closed is True


async def test_query_maps_falkordb_result_set_to_flat_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query() should hide FalkorDB result_set rows behind dict rows."""
    client = _client(monkeypatch)

    rows = await client.query("MATCH (n) RETURN n.name AS name, n.score AS score")

    assert rows == [
        {"name": "Python", "score": 0.95},
        {"name": "FalkorDB", "score": 0.91},
    ]


async def test_query_maps_compact_header_pairs_to_column_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FalkorDB compact headers include column type plus alias."""
    client = _client(monkeypatch)

    fake_db = FakeFalkorDB.last_instance
    assert fake_db is not None
    fake_db.graph.next_result = types.SimpleNamespace(
        header=[(1, "name"), (1, b"score")],
        result_set=[["Python", 0.95]],
        statistics={},
    )

    rows = await client.query("MATCH (n) RETURN n.name AS name, n.score AS score")

    assert rows == [{"name": "Python", "score": 0.95}]


async def test_query_maps_redis_response_error_to_falkor_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cypher errors should not leak redis.exceptions.ResponseError."""
    client = _client(monkeypatch)

    import redis.exceptions

    FakeFalkorDB.last_instance.graph.next_error = redis.exceptions.ResponseError(
        "Invalid input near ')'"
    )

    from gobby.memory.falkor_client import FalkorQueryError

    with pytest.raises(FalkorQueryError) as exc_info:
        await client.query("MATCH (n:")

    assert "Invalid input" in str(exc_info.value)
    assert exc_info.value.response_body == ("Invalid input near ')'",)


async def test_query_maps_auth_response_error_to_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WRONGPASS and NOAUTH are connectivity/auth failures, not Cypher failures."""
    client = _client(monkeypatch)

    import redis.exceptions

    fake_db = FakeFalkorDB.last_instance
    assert fake_db is not None
    fake_db.graph.next_error = redis.exceptions.ResponseError("WRONGPASS invalid username-password")

    from gobby.memory.falkor_client import FalkorConnectionError

    with pytest.raises(FalkorConnectionError):
        await client.query("RETURN 1")


async def test_ensure_memory_graph_schema_gates_unique_constraints_with_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each unique constraint must be backed by an exact-match index first."""
    client = _client(monkeypatch)

    client.ensure_supporting_index = AsyncMock()
    client.ensure_unique_constraint = AsyncMock()
    client._ensure_index = AsyncMock()

    await client.ensure_memory_graph_schema()

    assert client.ensure_supporting_index.mock_calls == [
        call("_Entity", "entity_key"),
        call("Memory", "memory_id"),
    ]
    assert client.ensure_unique_constraint.mock_calls == [
        call("_Entity", "entity_key"),
        call("Memory", "memory_id"),
    ]
    client._ensure_index.assert_called_once_with("_Entity", ("project_id", "entity_type"))


async def test_ensure_supporting_index_swallows_existing_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FalkorDB raises when an index already exists; schema creation should stay idempotent."""
    client = _client(monkeypatch)

    from gobby.memory.falkor_client import FalkorQueryError

    client.query = AsyncMock(side_effect=FalkorQueryError("already indexed"))

    await client.ensure_supporting_index("_Entity", "entity_key")

    cypher = client.query.call_args.args[0]
    assert cypher == "CREATE INDEX FOR (n:_Entity) ON (n.entity_key)"


async def test_ensure_unique_constraint_uses_redis_command_and_polls_cypher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unique constraints are created out-of-band, then polled via db.constraints()."""
    client = _client(monkeypatch)

    fake_db = FakeFalkorDB.last_instance
    assert fake_db is not None
    fake_db.graph.next_result = types.SimpleNamespace(
        header=["type", "label", "properties", "entitytype", "status"],
        result_set=[["UNIQUE", "_Entity", ["entity_key"], "NODE", "OPERATIONAL"]],
        statistics={},
    )

    await client.ensure_unique_constraint("_Entity", "entity_key")

    assert fake_db.commands == [
        (
            "GRAPH.CONSTRAINT",
            "CREATE",
            "gobby_kg",
            "UNIQUE",
            "NODE",
            "_Entity",
            "PROPERTIES",
            1,
            "entity_key",
        )
    ]
    assert fake_db.graph.queries[-1] == ("CALL db.constraints()", None)


async def test_ensure_unique_constraint_raises_on_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED means existing data violates uniqueness and writes must remain gated."""
    client = _client(monkeypatch)

    fake_db = FakeFalkorDB.last_instance
    assert fake_db is not None
    fake_db.graph.next_result = types.SimpleNamespace(
        header=["type", "label", "properties", "entitytype", "status"],
        result_set=[["UNIQUE", "_Entity", ["entity_key"], "NODE", "FAILED"]],
        statistics={},
    )

    from gobby.memory.falkor_client import FalkorQueryError

    with pytest.raises(FalkorQueryError, match="unique constraint failed"):
        await client.ensure_unique_constraint("_Entity", "entity_key")


async def test_ping_returns_false_for_connection_or_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ping() should convert graph backend failures to a boolean status."""
    client = _client(monkeypatch)

    import redis.exceptions

    FakeFalkorDB.last_instance.graph.next_error = redis.exceptions.ConnectionError("refused")

    assert await client.ping() is False

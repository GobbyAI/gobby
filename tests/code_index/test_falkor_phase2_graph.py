"""Phase 2 FalkorDB contract tests for code graph wiring."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.graph import CodeGraph
from gobby.runner_init import services
from gobby.runner_lifecycle_shutdown import _close_managers_and_storage
from gobby.runner_lifecycle_subsystems import _check_external_services

pytestmark = pytest.mark.unit


def _compact(cypher: str) -> str:
    return " ".join(cypher.split())


class _FakeClosableGraph:
    available = True

    def __init__(self) -> None:
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class _FakeMemoryManager:
    def __init__(self) -> None:
        self.falkor_client = SimpleNamespace(ping=AsyncMock(return_value=False))
        self.cleared = False

    def clear_graph_clients(self) -> None:
        self.falkor_client = None
        self.cleared = True


def test_code_graph_constructor_accepts_falkor_client_keyword() -> None:
    """CodeGraph is constructed from FalkorClient, not Neo4jClient."""
    signature = inspect.signature(CodeGraph)

    assert "falkor_client" in signature.parameters
    assert "neo4j_client" not in signature.parameters
    assert CodeGraph(falkor_client=MagicMock()).available is True


@pytest.mark.asyncio
async def test_code_graph_write_queries_use_timestamp_for_falkor() -> None:
    """CodeGraph write Cypher uses timestamp() for FalkorDB epoch-ms timestamps."""
    client = AsyncMock()
    graph = CodeGraph(falkor_client=client)

    await graph.sync_file(
        project_id="proj-1",
        file_path="pkg/app.py",
        imports=[{"source_file": "pkg/app.py", "target_module": "sys"}],
        contains=[
            {
                "id": "proj-1:pkg/app.py:run",
                "name": "run",
                "kind": "function",
                "line_start": 10,
            }
        ],
        calls=[
            {
                "caller_symbol_id": "proj-1:pkg/app.py:run",
                "callee_target_kind": "external",
                "callee_name": "loads",
                "callee_external_module": "json",
                "file_path": "pkg/app.py",
                "line": 11,
            }
        ],
    )

    queries = [_compact(call.args[0]) for call in client.execute_write.await_args_list]
    assert queries
    assert any("timestamp()" in query for query in queries)
    assert all("datetime()" not in query for query in queries)


def test_runner_code_index_constructs_code_graph_with_falkor_client() -> None:
    """Runner init constructs a dedicated FalkorClient for the code graph."""
    falkor_client = MagicMock()
    db_cfg = SimpleNamespace(
        falkordb=SimpleNamespace(host="127.0.0.1", port=16379, requirepass="secret")
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(
            code_index=SimpleNamespace(
                enabled=True,
                embedding_enabled=False,
                graph_enabled=True,
            ),
            databases=db_cfg,
        ),
        database=MagicMock(),
        db_executor=SimpleNamespace(run=AsyncMock()),
        vector_store=MagicMock(),
        code_indexer=None,
    )

    with (
        patch("gobby.runner_init.services.is_falkordb_enabled") as enabled,
        patch("gobby.runner_init.services.FalkorClient") as falkor_cls,
        patch("gobby.code_index.storage.CodeIndexStorage") as storage_cls,
        patch("gobby.code_index.graph.CodeGraph") as code_graph_cls,
        patch("gobby.code_index.context.CodeIndexContext") as context_cls,
    ):
        storage_cls.return_value = MagicMock()
        enabled.return_value = True
        falkor_cls.return_value = falkor_client
        code_graph_cls.return_value = MagicMock()

        services._init_code_indexer(runner)

    enabled.assert_called_once_with(db_cfg)
    falkor_cls.assert_called_once_with(
        host="127.0.0.1",
        port=16379,
        password="secret",
        graph_name="gobby_code",
    )
    code_graph_cls.assert_called_once_with(falkor_client=falkor_client)
    context_kwargs = context_cls.call_args.kwargs
    assert context_kwargs["storage"] is storage_cls.return_value
    assert context_kwargs["graph"] is code_graph_cls.return_value
    assert context_kwargs["run_db"] is runner.db_executor.run
    assert runner.code_indexer is context_cls.return_value


def test_runner_code_index_leaves_graph_none_when_falkordb_disabled() -> None:
    """Runner init uses the canonical FalkorDB gate before constructing CodeGraph."""
    db_cfg = SimpleNamespace(falkordb=SimpleNamespace())
    runner = SimpleNamespace(
        config=SimpleNamespace(
            code_index=SimpleNamespace(
                enabled=True,
                embedding_enabled=False,
                graph_enabled=True,
            ),
            databases=db_cfg,
        ),
        database=MagicMock(),
        db_executor=SimpleNamespace(run=AsyncMock()),
        vector_store=MagicMock(),
        code_indexer=None,
    )

    with (
        patch("gobby.runner_init.services.is_falkordb_enabled") as enabled,
        patch("gobby.runner_init.services.FalkorClient") as falkor_cls,
        patch("gobby.code_index.graph.CodeGraph") as code_graph_cls,
    ):
        enabled.return_value = False

        services._init_code_indexer(runner)

    enabled.assert_called_once_with(db_cfg)
    falkor_cls.assert_not_called()
    code_graph_cls.assert_not_called()
    assert isinstance(runner.code_indexer, CodeIndexContext)
    assert runner.code_indexer.graph is None
    assert runner.code_indexer.storage.db is runner.database
    assert runner.code_indexer._run_db is runner.db_executor.run


@pytest.mark.asyncio
async def test_code_index_context_closes_and_clears_graph_client() -> None:
    """CodeIndexContext exposes an idempotent graph-client close hook."""
    graph = MagicMock()
    graph.close = AsyncMock()
    context = CodeIndexContext(storage=MagicMock(), graph=graph)

    await context.close_graph_client()
    await context.close_graph_client()

    graph.close.assert_awaited_once()
    assert context.graph is None


def test_code_index_context_clears_graph_client_without_await() -> None:
    """CodeIndexContext exposes a synchronous clear hook for health failures."""
    context = CodeIndexContext(storage=MagicMock(), graph=MagicMock())

    context.clear_graph_client()

    assert context.graph is None


@pytest.mark.asyncio
async def test_falkordb_health_failure_clears_code_graph_reference() -> None:
    """FalkorDB health failures disable both memory and code graph clients."""
    memory_manager = _FakeMemoryManager()
    graph = MagicMock()
    code_indexer = CodeIndexContext(storage=MagicMock(), graph=graph)
    runner = SimpleNamespace(
        config=SimpleNamespace(
            databases=SimpleNamespace(
                qdrant=SimpleNamespace(url=""),
                falkordb=SimpleNamespace(host="127.0.0.1", port=16379, requirepass="secret"),
            )
        ),
        memory_manager=memory_manager,
        code_indexer=code_indexer,
    )

    await _check_external_services(runner, tracker=None)

    assert memory_manager.cleared is True
    assert memory_manager.falkor_client is None
    assert code_indexer.graph is None


@pytest.mark.asyncio
async def test_shutdown_closes_code_graph_client() -> None:
    """Daemon shutdown closes the code graph client before storage cleanup completes."""
    graph = _FakeClosableGraph()
    code_indexer = CodeIndexContext(storage=MagicMock(), graph=graph)
    runner = SimpleNamespace(
        http_server=SimpleNamespace(_hook_manager=None),
        memory_manager=None,
        code_indexer=code_indexer,
        vector_store=None,
    )

    await _close_managers_and_storage(runner)

    assert graph.close_count == 1
    assert code_indexer.graph is None

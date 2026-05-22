"""Phase 2 FalkorDB contract tests for code graph wiring."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.code_index.graph import CodeGraph
from gobby.runner_init import services

pytestmark = pytest.mark.unit


def _compact(cypher: str) -> str:
    return " ".join(cypher.split())


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
    """Runner init passes the shared FalkorClient to CodeGraph."""
    falkor_client = MagicMock()
    runner = SimpleNamespace(
        config=SimpleNamespace(
            code_index=SimpleNamespace(
                enabled=True,
                embedding_enabled=False,
                graph_enabled=True,
            )
        ),
        database=MagicMock(),
        db_executor=SimpleNamespace(run=AsyncMock()),
        vector_store=MagicMock(),
        memory_manager=SimpleNamespace(falkor_client=falkor_client),
        code_indexer=None,
    )

    with (
        patch("gobby.code_index.storage.CodeIndexStorage") as storage_cls,
        patch("gobby.code_index.graph.CodeGraph") as code_graph_cls,
        patch("gobby.code_index.context.CodeIndexContext") as context_cls,
    ):
        storage_cls.return_value = MagicMock()
        code_graph_cls.return_value = MagicMock()

        services._init_code_indexer(runner)

    code_graph_cls.assert_called_once_with(falkor_client=falkor_client)
    context_kwargs = context_cls.call_args.kwargs
    assert context_kwargs["storage"] is storage_cls.return_value
    assert context_kwargs["graph"] is code_graph_cls.return_value
    assert context_kwargs["run_db"] is runner.db_executor.run
    assert runner.code_indexer is context_cls.return_value

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.graph import CodeGraph


def _compact(cypher: str) -> str:
    return " ".join(cypher.split())


@pytest.fixture
def mock_client():
    client = AsyncMock()
    return client


def test_available():
    graph = CodeGraph()
    assert graph.available is False

    graph = CodeGraph(falkor_client=MagicMock())
    assert graph.available is True


@pytest.mark.asyncio
async def test_close_delegates_to_falkor_client():
    client = MagicMock()
    client.close = AsyncMock()
    graph = CodeGraph(falkor_client=client)

    await graph.close()
    await graph.close()

    client.close.assert_awaited_once()
    assert graph.available is False


@pytest.mark.asyncio
async def test_close_ignores_already_closed_runtime_error():
    client = MagicMock()
    client.close = AsyncMock(side_effect=RuntimeError("Event loop is closed"))
    graph = CodeGraph(falkor_client=client)

    await graph.close()

    assert graph.available is False


@pytest.mark.asyncio
async def test_add_relationships_not_available():
    graph = CodeGraph()
    assert await graph.add_relationships("p1", "test.py") == 0


@pytest.mark.asyncio
async def test_add_relationships_success(mock_client):
    graph = CodeGraph(falkor_client=mock_client)

    imports = [{"source_file": "a.py", "target_module": "sys"}]
    calls = [{"caller_symbol_id": "sym1", "callee_name": "func", "file_path": "a.py", "line": 1}]
    contains = [{"id": "sym1", "name": "c", "kind": "func", "line_start": 1}]

    cnt = await graph.add_relationships("p1", "a.py", imports, calls, contains)
    assert cnt == 3
    assert mock_client.execute_write.call_count == 11
    queries = [_compact(call.args[0]) for call in mock_client.execute_write.await_args_list]
    assert any("timestamp()" in query for query in queries)
    assert all("datetime()" not in query for query in queries)
    assert all("CREATE CONSTRAINT" not in query for query in queries)
    assert all("db.idx.vector.createNodeIndex" not in query for query in queries)


@pytest.mark.asyncio
async def test_add_relationships_skips_incomplete_records(mock_client):
    graph = CodeGraph(falkor_client=mock_client)

    imports = [{"source_file": "a.py", "target_module": ""}]
    calls = [{"caller_symbol_id": "sym1", "callee_name": "", "file_path": "a.py", "line": 1}]
    contains = [{"id": "sym1", "name": "", "kind": "func", "line_start": 1}]

    cnt = await graph.add_relationships("p1", "a.py", imports, calls, contains)

    assert cnt == 0
    assert mock_client.execute_write.call_count == 8


@pytest.mark.asyncio
async def test_add_relationships_exception(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_write.side_effect = Exception("err")
    imports = [{"source_file": "start.py"}]
    with pytest.raises(Exception, match="err"):
        await graph.add_relationships("p1", "a.py", imports)


@pytest.mark.asyncio
async def test_methods_not_available():
    graph = CodeGraph()
    assert await graph.find_callers("q", "p") == []
    assert await graph.find_usages("q", "p") == []
    assert await graph.get_imports("f", "p") == []
    assert await graph.get_import_chain("m", "p") == []
    assert await graph.find_blast_radius("s", None, "p") == []
    res = await graph.get_file_graph("p")
    assert res == {"nodes": [], "links": []}
    res = await graph.get_file_symbols("f", "p")
    assert res == {"nodes": [], "links": []}
    res = await graph.get_symbol_neighbors("s", "p")
    assert res == {"nodes": [], "links": []}

    await graph.clear_project("p")
    await graph.delete_file("f", "p")


@pytest.mark.asyncio
async def test_find_callers(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.return_value = [
        {"caller_id": "c1", "caller_name": "cn", "file": "f", "line": 1}
    ]
    res = await graph.find_callers("n", "p1")
    assert len(res) == 1
    assert res[0]["caller_id"] == "c1"

    mock_client.execute_read.side_effect = Exception("e")
    res = await graph.find_callers("n", "p1")
    assert res == []


@pytest.mark.asyncio
async def test_find_usages(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.return_value = [{"source_id": "s1"}]
    assert await graph.find_usages("n", "p1") == [{"source_id": "s1"}]
    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.find_usages("n", "p1") == []


@pytest.mark.asyncio
async def test_get_imports(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.return_value = [{"module_name": "m1"}]
    assert await graph.get_imports("f", "p1") == [{"module_name": "m1"}]
    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.get_imports("f", "p1") == []


@pytest.mark.asyncio
async def test_get_import_chain(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.return_value = [{"name": "n1"}]
    assert await graph.get_import_chain("m", "p1") == [{"name": "n1"}]
    query, params = mock_client.execute_read.await_args.args
    assert "[:IMPORTS*1..3]" in query
    assert params == {"module": "m", "project": "p1"}
    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.get_import_chain("m", "p1") == []


@pytest.mark.asyncio
async def test_find_blast_radius(mock_client):
    graph = CodeGraph(falkor_client=mock_client)

    with pytest.raises(ValueError):
        await graph.find_blast_radius("s", "f", "p")

    with pytest.raises(ValueError):
        await graph.find_blast_radius(None, None, "p")

    # Path 1: symbol_id
    mock_client.execute_read.return_value = [{"node_id": "sym1", "distance": 1, "rel_type": "call"}]
    res = await graph.find_blast_radius("s", None, "p")
    assert len(res) == 1

    # Path 2: file_path
    mock_client.execute_read.side_effect = [
        [{"node_id": "sym2", "distance": 1, "rel_type": "call"}],  # call_records
        [{"node_id": "f2", "distance": 2, "rel_type": "import"}],  # import_records
    ]
    res = await graph.find_blast_radius(None, "f", "p")
    assert len(res) == 2

    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.find_blast_radius("s", None, "p") == []


@pytest.mark.asyncio
async def test_get_file_graph(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.side_effect = [
        [{"id": "f1", "path": "p1", "type": "file", "symbol_count": 1}],  # file_records
        [{"source": "f1", "target": "m1", "type": "IMPORTS"}],  # import_records
        [
            {
                "source": "f1",
                "target": "sym1",
                "type": "DEFINES",
                "symbol_name": "sym1",
                "symbol_kind": "function",
                "symbol_file_path": "f1",
                "line_start": 10,
            }
        ],
        [
            {
                "source": "sym1",
                "target": "sym2",
                "type": "CALLS",
                "target_name": "sym2",
                "target_type": "external",
                "target_kind": None,
                "target_file_path": None,
                "target_line_start": None,
            }
        ],
    ]
    res = await graph.get_file_graph("p1", limit=1)
    assert len(res["nodes"]) == 4  # f1 (file), m1 (module), sym1, sym2
    assert len(res["links"]) == 3

    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.get_file_graph("p1") == {"nodes": [], "links": []}


@pytest.mark.asyncio
async def test_get_file_symbols(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.side_effect = [
        [
            {
                "id": "sym1",
                "name": "sym1",
                "type": "function",
                "kind": "function",
                "file_path": "f",
                "line_start": 1,
                "signature": "def sym1(): ...",
            }
        ],
        [
            {
                "source_id": "sym1",
                "source_name": "sym1",
                "source_type": "function",
                "source_kind": "function",
                "source_file_path": "f",
                "source_line_start": 1,
                "source_signature": "def sym1(): ...",
                "target_id": "sym2",
                "target_name": "sym2",
                "target_type": "external",
                "target_kind": None,
                "target_file_path": None,
                "target_line_start": None,
                "target_signature": None,
                "line": 1,
            }
        ],
    ]
    res = await graph.get_file_symbols("f", "p")
    assert len(res["nodes"]) == 2  # sym1, sym2
    assert len(res["links"]) == 2  # 1 DEFINES, 1 CALLS

    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.get_file_symbols("f", "p") == {"nodes": [], "links": []}


@pytest.mark.asyncio
async def test_get_symbol_neighbors(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    mock_client.execute_read.return_value = [
        {
            "id": "sym_in",
            "name": "in",
            "type": "function",
            "kind": "func",
            "direction": "incoming",
            "file_path": "f",
            "line": 1,
        },
        {
            "id": "sym_out",
            "name": "out",
            "type": "external",
            "kind": "func",
            "direction": "outgoing",
            "file_path": "f",
            "line": 2,
        },
    ]
    res = await graph.get_symbol_neighbors("s", "p")
    assert len(res["nodes"]) == 2
    assert len(res["links"]) == 2

    mock_client.execute_read.side_effect = Exception("e")
    assert await graph.get_symbol_neighbors("s", "p") == {"nodes": [], "links": []}


@pytest.mark.asyncio
async def test_get_blast_radius_graph(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    graph.find_blast_radius = AsyncMock(
        return_value=[
            {
                "node_id": "sym1",
                "node_name": "nm",
                "kind": "func",
                "distance": 1,
                "rel_type": "call",
                "node_type": "function",
            },
            {
                "node_id": "f1",
                "node_name": "f1",
                "distance": 2,
                "rel_type": "import",
                "node_type": "file",
            },
        ]
    )
    mock_client.execute_read.return_value = [
        {"name": "target", "type": "function", "kind": "function", "file_path": "src/a.py"}
    ]
    res = await graph.get_blast_radius_graph("s", None, "p")
    assert res["center"] == "s"
    assert len(res["nodes"]) == 3  # center + sym1 + f1
    assert len(res["links"]) == 2

    with pytest.raises(ValueError):
        await graph.get_blast_radius_graph(None, None, "p")


@pytest.mark.asyncio
async def test_clear_project(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    await graph.clear_project("p1")
    mock_client.execute_write.assert_called_once()

    mock_client.execute_write.side_effect = Exception("e")
    with pytest.raises(Exception, match="e"):
        await graph.clear_project("p1")


@pytest.mark.asyncio
async def test_delete_file(mock_client):
    graph = CodeGraph(falkor_client=mock_client)
    await graph.delete_file("f", "p")
    assert mock_client.execute_write.call_count == 5

    mock_client.execute_write.side_effect = Exception("e")
    with pytest.raises(Exception, match="e"):
        await graph.delete_file("f", "p")

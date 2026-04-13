from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.models import Symbol
from gobby.code_index.searcher import CodeSearcher, _rrf_score
from gobby.config.code_index import CodeIndexConfig


def test_rrf_score():
    assert _rrf_score(0) == 1.0 / 60
    assert _rrf_score(1) == 1.0 / 61


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    return storage


@pytest.fixture
def mock_vector_store():
    store = AsyncMock()
    return store


@pytest.fixture
def mock_graph():
    graph = AsyncMock()
    graph.available = True
    return graph


@pytest.fixture
def base_symbol():
    return Symbol(
        id="sym1",
        project_id="p1",
        name="test_sym",
        qualified_name="test.test_sym",
        kind="function",
        language="python",
        file_path="test.py",
        byte_start=0,
        byte_end=10,
        line_start=1,
        line_end=10,
        content_hash="hash",
    )


@pytest.mark.asyncio
async def test_searcher_basic(mock_storage):
    searcher = CodeSearcher(storage=mock_storage)
    sym = Symbol(
        id="sym1",
        project_id="p1",
        name="test",
        qualified_name="test",
        kind="function",
        language="python",
        file_path="t.py",
        byte_start=0,
        byte_end=4,
        line_start=1,
        line_end=2,
        content_hash="h",
    )
    mock_storage.search_symbols_fts.return_value = [sym]

    res = await searcher.search("test", "p1")

    assert len(res) == 1
    assert res[0]["name"] == "test"
    assert "_score" in res[0]
    assert "name" in res[0]["_sources"]


@pytest.mark.asyncio
async def test_searcher_fallback_like(mock_storage):
    searcher = CodeSearcher(storage=mock_storage)
    mock_storage.search_symbols_fts.return_value = []

    sym = Symbol(
        id="sym2",
        project_id="p1",
        name="fallback",
        qualified_name="fallback",
        kind="class",
        language="python",
        file_path="t.py",
        byte_start=0,
        byte_end=4,
        line_start=1,
        line_end=2,
        content_hash="h",
    )
    mock_storage.search_symbols_by_name.return_value = [sym]

    res = await searcher.search("test", "p1")
    assert len(res) == 1
    assert res[0]["name"] == "fallback"
    mock_storage.search_symbols_by_name.assert_called_once()


@pytest.mark.asyncio
async def test_searcher_full_hybrid(mock_storage, mock_vector_store, mock_graph, base_symbol):
    async def mock_embed(*actions, **kwargs):
        return [0.1, 0.2]

    config = CodeIndexConfig(qdrant_collection_prefix="pre_")
    searcher = CodeSearcher(
        storage=mock_storage,
        vector_store=mock_vector_store,
        embed_fn=mock_embed,
        graph=mock_graph,
        config=config,
    )

    mock_storage.search_symbols_fts.return_value = [base_symbol]

    hit = MagicMock()
    hit.id = "sym1"
    hit.score = 0.9
    mock_vector_store.search.return_value = [hit]

    mock_graph.find_callers.return_value = [{"caller_id": "sym1"}]
    mock_graph.find_usages.return_value = [{"source_id": "sym3"}]  # sym3 not in cache

    mock_storage.get_symbol.return_value = Symbol(
        id="sym3",
        project_id="p1",
        name="other",
        qualified_name="other",
        kind="function",
        language="python",
        file_path="t.py",
        byte_start=0,
        byte_end=4,
        line_start=1,
        line_end=2,
        content_hash="h",
    )

    res = await searcher.search("query", "p1")
    assert len(res) == 2

    sym1_res = [r for r in res if r["name"] == "test_sym"][0]
    # RRF from 3 sources for sym1
    assert "name" in sym1_res["_sources"]
    assert "semantic" in sym1_res["_sources"]
    assert "graph" in sym1_res["_sources"]

    sym3_res = [r for r in res if r["name"] == "other"][0]
    assert "graph" in sym3_res["_sources"]


@pytest.mark.asyncio
async def test_semantic_search_none_embed(mock_storage, mock_vector_store):
    searcher = CodeSearcher(storage=mock_storage, vector_store=mock_vector_store)
    res = await searcher._semantic_search("t", "p", 10)
    assert res == []

    async def mock_embed_none(*actions, **kwargs):
        return None

    searcher._embed_fn = mock_embed_none
    res = await searcher._semantic_search("t", "p", 10)
    assert res == []


@pytest.mark.asyncio
async def test_searcher_exceptions(mock_storage, mock_vector_store, mock_graph, base_symbol):
    async def mock_embed(*actions, **kwargs):
        raise Exception("Embed fail")

    mock_graph.find_callers.side_effect = Exception("Graph fail")

    searcher = CodeSearcher(
        storage=mock_storage,
        vector_store=mock_vector_store,
        embed_fn=mock_embed,
        graph=mock_graph,
    )

    mock_storage.search_symbols_fts.return_value = [base_symbol]

    # Needs to degrade gracefully
    res = await searcher.search("test", "p1")
    assert len(res) == 1
    assert "name" in res[0]["_sources"]
    assert "semantic" not in res[0]["_sources"]
    assert "graph" not in res[0]["_sources"]


@pytest.mark.asyncio
async def test_graph_boost_none(mock_storage):
    searcher = CodeSearcher(storage=mock_storage)
    res = await searcher._graph_boost("q", "p")
    assert res == []


def test_search_text(mock_storage, base_symbol):
    searcher = CodeSearcher(storage=mock_storage)
    mock_storage.search_symbols_fts.return_value = [base_symbol]
    res = searcher.search_text("q", "p1")
    assert len(res) == 1
    assert res[0]["name"] == "test_sym"


def test_search_text_fallback(mock_storage, base_symbol):
    searcher = CodeSearcher(storage=mock_storage)
    mock_storage.search_symbols_fts.return_value = []
    mock_storage.search_symbols_by_name.return_value = [base_symbol]
    res = searcher.search_text("q", "p1")
    assert len(res) == 1


def test_search_content(mock_storage):
    searcher = CodeSearcher(storage=mock_storage)
    mock_storage.search_content_fts.return_value = [{"content": "hello"}]
    res = searcher.search_content("q", "p1")
    assert len(res) == 1
    assert res[0] == {"content": "hello"}


@pytest.mark.asyncio
async def test_get_symbol_none(mock_storage):
    searcher = CodeSearcher(storage=mock_storage)
    mock_storage.search_symbols_fts.return_value = []
    # Test path where get_symbol returns None
    # We need a sym ID in final scores that isn't in cache
    # Best way is graph boost returns ID
    mock_graph = AsyncMock()
    mock_graph.available = True
    mock_graph.find_callers.return_value = [{"caller_id": "missing_sym"}]
    mock_graph.find_usages.return_value = []

    searcher._graph = mock_graph

    mock_storage.get_symbol.return_value = None

    res = await searcher.search("query", "p1")
    assert len(res) == 0

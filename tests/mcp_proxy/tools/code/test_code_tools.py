"""Tests for MCP code tool registry (gobby-code)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.code_index.indexer import CodeIndexer
from gobby.code_index.parser import CodeParser
from gobby.code_index.searcher import CodeSearcher
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexConfig
from gobby.mcp_proxy.tools.code._factory import create_code_registry
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit

_CODE_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS code_indexed_projects (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    total_symbols INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    index_duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS code_indexed_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_cif_project ON code_indexed_files(project_id);
CREATE TABLE IF NOT EXISTS code_symbols (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT,
    docstring TEXT,
    parent_symbol_id TEXT,
    content_hash TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cs_project ON code_symbols(project_id);
CREATE INDEX IF NOT EXISTS idx_cs_file ON code_symbols(project_id, file_path);
CREATE INDEX IF NOT EXISTS idx_cs_name ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_cs_qualified ON code_symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_cs_kind ON code_symbols(kind);
CREATE INDEX IF NOT EXISTS idx_cs_parent ON code_symbols(parent_symbol_id);
"""


@pytest.fixture
def code_db(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "code-mcp-test.db")
    run_migrations(db)
    db.connection.executescript(_CODE_INDEX_SCHEMA)
    db.connection.commit()
    yield db  # type: ignore[misc]
    db.close()


@pytest.fixture
def code_storage(code_db: LocalDatabase) -> CodeIndexStorage:
    return CodeIndexStorage(code_db)


@pytest.fixture
def config() -> CodeIndexConfig:
    return CodeIndexConfig()


@pytest.fixture
def parser(config: CodeIndexConfig) -> CodeParser:
    return CodeParser(config)


@pytest.fixture
def indexer(code_storage: CodeIndexStorage, parser: CodeParser, config: CodeIndexConfig) -> CodeIndexer:
    return CodeIndexer(storage=code_storage, parser=parser, config=config)


@pytest.fixture
def searcher(code_storage: CodeIndexStorage) -> CodeSearcher:
    return CodeSearcher(storage=code_storage)


@pytest.fixture
def registry(
    code_storage: CodeIndexStorage,
    indexer: CodeIndexer,
    searcher: CodeSearcher,
    config: CodeIndexConfig,
):
    """Create the gobby-code registry."""
    return create_code_registry(
        storage=code_storage,
        indexer=indexer,
        searcher=searcher,
        config=config,
        project_id="test-proj",
    )


# ── Registry creation ──────────────────────────────────────────────────


def test_create_code_registry_returns_registry(registry) -> None:
    """create_code_registry returns an InternalToolRegistry."""
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry

    assert isinstance(registry, InternalToolRegistry)


def test_registry_name(registry) -> None:
    """Registry has the correct name."""
    assert registry.name == "gobby-code"


def test_registry_has_tools(registry) -> None:
    """Registry has tools registered."""
    assert len(registry) > 0


# ── Tool listing ────────────────────────────────────────────────────────


def test_list_tools_format(registry) -> None:
    """list_tools returns list of {name, brief} dicts."""
    tools = registry.list_tools()
    assert isinstance(tools, list)
    assert len(tools) > 0

    for tool in tools:
        assert "name" in tool
        assert "brief" in tool
        assert isinstance(tool["name"], str)


def test_expected_tools_present(registry) -> None:
    """Registry includes key tools from all sub-registries."""
    tool_names = {t["name"] for t in registry.list_tools()}

    # Indexing tools (index_folder moved to CLI)
    assert "index_folder" not in tool_names
    assert "list_indexed" in tool_names
    assert "invalidate_index" in tool_names

    # Query tools
    assert "get_file_tree" in tool_names
    assert "get_file_outline" in tool_names

    # Graph tools
    assert "find_callers" in tool_names
    assert "find_usages" in tool_names

    # Summary tools
    assert "get_summary" in tool_names


# ── Schema retrieval ────────────────────────────────────────────────────


def test_get_schema_for_known_tool(registry) -> None:
    """get_schema returns schema dict for a known tool."""
    schema = registry.get_schema("list_indexed")
    assert schema is not None
    assert schema["name"] == "list_indexed"
    assert "description" in schema
    assert "inputSchema" in schema


def test_get_schema_for_search_symbols(registry) -> None:
    """search_symbols schema has expected parameters."""
    schema = registry.get_schema("search_symbols")
    if schema is None:
        pytest.skip("search_symbols not in this build")
    props = schema["inputSchema"]["properties"]
    assert "query" in props


def test_get_schema_for_unknown_tool(registry) -> None:
    """get_schema returns None for an unknown tool."""
    assert registry.get_schema("nonexistent_tool") is None


# ── Tool calls ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_list_indexed(registry) -> None:
    """list_indexed tool returns a list (possibly empty)."""
    result = await registry.call("list_indexed", {})
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_call_get_file_tree(registry) -> None:
    """get_file_tree returns a list."""
    result = await registry.call("get_file_tree", {})
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_call_get_summary_not_found(registry) -> None:
    """get_summary for non-existent symbol returns error."""
    result = await registry.call(
        "get_summary", {"symbol_id": "nonexistent"}
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_call_find_callers_without_graph(registry) -> None:
    """find_callers without Neo4j returns graph unavailable error."""
    result = await registry.call(
        "find_callers", {"symbol_name": "foo"}
    )
    assert isinstance(result, list)
    assert len(result) == 1
    assert "error" in result[0]

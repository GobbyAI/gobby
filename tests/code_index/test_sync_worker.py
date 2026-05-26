"""Tests for code_index.sync_worker external store sync."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.models import IndexedFile, IndexedProject, Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.code_index.sync_worker import _sync_file, _sync_graph, _sync_pass, sync_worker_loop
from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


class MissingCollectionError(Exception):
    """Raised by the fake vector store when a collection was not ensured."""


class FakeEmbedModel:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class RecoveringVectorStore:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.calls: list[tuple[str, str, Any]] = []
        self.items: list[tuple[str, list[float], dict[str, Any]]] = []

    async def ensure_collection(
        self, collection_name: str, embedding_dim: int | None = None
    ) -> None:
        self.calls.append(("ensure_collection", collection_name, embedding_dim))
        self.collections.add(collection_name)

    async def delete(self, filters: dict[str, str], collection_name: str) -> None:
        self.calls.append(("delete", collection_name, filters))
        if collection_name not in self.collections:
            raise MissingCollectionError(collection_name)

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str,
    ) -> None:
        self.calls.append(("batch_upsert", collection_name, len(items)))
        if collection_name not in self.collections:
            raise MissingCollectionError(collection_name)
        self.items = items


class SyncWorkerVectorStore(RecoveringVectorStore):
    async def close(self) -> None:
        return None


class RecordingRunDb:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(
            getattr(func, "__name__", None) or getattr(func, "_mock_name", repr(func))
        )
        return func(*args, **kwargs)


def _indexed_project(root: Path) -> IndexedProject:
    return IndexedProject(id="proj-1", root_path=str(root), total_files=1, total_symbols=1)


def _indexed_file(
    *,
    vectors_synced: bool = False,
    graph_synced: bool = False,
) -> IndexedFile:
    return IndexedFile(
        id=IndexedFile.make_id("proj-1", "src/app.py"),
        project_id="proj-1",
        file_path="src/app.py",
        language="python",
        content_hash="abc123",
        symbol_count=1,
        vectors_synced=vectors_synced,
        graph_synced=graph_synced,
    )


def _write_source(root: Path) -> None:
    source_file = root / "src/app.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")


@pytest.mark.asyncio
async def test_sync_worker_keeps_vectors_live_when_graph_client_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cleared FalkorDB client must not starve vector syncing."""

    async def fake_generate_embeddings(
        texts: list[str],
        **_kwargs: Any,
    ) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr("gobby.search.embeddings.generate_embeddings", fake_generate_embeddings)
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=False)
    old_graph = SimpleNamespace(available=True, sync_file=AsyncMock())
    context = SimpleNamespace(graph=None)
    shutdown_flag = asyncio.Event()

    storage = MagicMock()
    storage.list_indexed_projects.return_value = [_indexed_project(tmp_path)]
    storage.get_file.return_value = pending_file
    storage.get_symbols_for_file.return_value = [
        SimpleNamespace(
            id="sym-1",
            name="greet",
            kind="function",
            qualified_name="greet",
            signature="greet(name: str) -> str",
            docstring=None,
            file_path="src/app.py",
        )
    ]

    def pending_once(*_args: Any, **_kwargs: Any) -> list[IndexedFile]:
        shutdown_flag.set()
        return [pending_file]

    storage.get_pending_sync_files.side_effect = pending_once
    vector_store = SyncWorkerVectorStore()

    await sync_worker_loop(
        storage=storage,
        vector_store=vector_store,
        context=context,
        config=CodeIndexConfig(
            embedding_enabled=True,
            graph_enabled=True,
            sync_worker_interval_seconds=0.01,
        ),
        embeddings_config=SimpleNamespace(
            model="test-model",
            api_base=None,
            api_key=None,
            dim=4,
        ),
        shutdown_flag=shutdown_flag,
        run_db=RecordingRunDb(),
    )

    old_graph.sync_file.assert_not_awaited()
    assert [call[0] for call in vector_store.calls] == [
        "ensure_collection",
        "delete",
        "batch_upsert",
    ]
    storage.mark_vectors_synced.assert_called_once_with(pending_file.id)


@pytest.mark.asyncio
async def test_sync_worker_uses_restored_graph_client_on_next_iteration(tmp_path: Path) -> None:
    """The loop re-reads CodeIndexContext.graph and resumes graph writes."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    new_graph = SimpleNamespace(available=True, sync_file=AsyncMock())
    context = SimpleNamespace(graph=None)
    shutdown_flag = asyncio.Event()

    storage = MagicMock()
    storage.list_indexed_projects.return_value = [_indexed_project(tmp_path)]
    storage.get_file.return_value = pending_file
    storage.get_imports_for_file.return_value = []
    storage.get_calls_for_file.return_value = []
    storage.get_symbols_for_file.return_value = []
    calls = 0

    def pending_across_recovery(*_args: Any, **_kwargs: Any) -> list[IndexedFile]:
        nonlocal calls
        calls += 1
        if calls == 1:
            context.graph = new_graph
            return []
        shutdown_flag.set()
        return [pending_file]

    storage.get_pending_sync_files.side_effect = pending_across_recovery

    await sync_worker_loop(
        storage=storage,
        vector_store=None,
        context=context,
        config=CodeIndexConfig(
            embedding_enabled=False,
            graph_enabled=True,
            sync_worker_interval_seconds=0.01,
        ),
        embeddings_config=SimpleNamespace(
            model="test-model",
            api_base=None,
            api_key=None,
            dim=4,
        ),
        shutdown_flag=shutdown_flag,
        run_db=RecordingRunDb(),
    )

    assert calls == 2
    new_graph.sync_file.assert_awaited_once()
    storage.mark_graph_synced.assert_called_once_with(pending_file.id)


@pytest.mark.asyncio
async def test_sync_file_ensures_missing_vector_collection_before_upsert(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
    tmp_path: Path,
) -> None:
    """A missing project-specific Qdrant collection is recreated before vector upsert."""
    project_id = "proj-1"
    file_path = "src/app.py"
    root = tmp_path
    source_file = root / file_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")

    indexed_file = IndexedFile(
        id=IndexedFile.make_id(project_id, file_path),
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="abc123",
        symbol_count=len(sample_symbols),
        graph_synced=True,
        vectors_synced=False,
    )
    code_storage.upsert_project_stats(
        IndexedProject(
            id=project_id,
            root_path=str(root),
            total_files=1,
            total_symbols=len(sample_symbols),
        )
    )
    code_storage.upsert_file(indexed_file)
    code_storage.upsert_symbols(sample_symbols)

    vector_store = RecoveringVectorStore()

    did_sync = await _sync_file(
        storage=code_storage,
        vector_store=vector_store,
        graph=None,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        embed_model=FakeEmbedModel(),
        project_id=project_id,
        root=root,
        file=indexed_file,
        embedding_dim=4,
    )

    collection = f"code_symbols_{project_id}"
    assert did_sync is True
    assert [call[0] for call in vector_store.calls] == [
        "ensure_collection",
        "delete",
        "batch_upsert",
    ]
    assert vector_store.calls[0] == ("ensure_collection", collection, 4)
    assert len(vector_store.items) == len(sample_symbols)

    synced_file = code_storage.get_file(project_id, file_path)
    assert synced_file is not None
    assert synced_file.vectors_synced is True


@pytest.mark.asyncio
async def test_sync_pass_polls_projects_and_files_through_run_db(tmp_path: Path) -> None:
    """Project and pending-file polling uses the injected DB runner."""
    project = IndexedProject(id="proj-1", root_path=str(tmp_path), total_files=0, total_symbols=0)
    storage = MagicMock()
    storage.list_indexed_projects.return_value = [project]
    storage.get_pending_sync_files.return_value = []
    run_db = RecordingRunDb()

    await _sync_pass(
        storage=storage,
        vector_store=None,
        graph=None,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=True),
        embed_model=None,
        batch_size=10,
        embedding_dim=4,
        run_db=run_db,
    )

    assert run_db.calls == ["list_indexed_projects", "get_pending_sync_files"]


@pytest.mark.asyncio
async def test_sync_file_routes_vector_storage_calls_through_run_db(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
    tmp_path: Path,
) -> None:
    """Vector sync reads and marker writes use the injected DB runner."""
    project_id = "proj-1"
    file_path = "src/app.py"
    source_file = tmp_path / file_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")

    indexed_file = IndexedFile(
        id=IndexedFile.make_id(project_id, file_path),
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="abc123",
        symbol_count=len(sample_symbols),
        graph_synced=True,
        vectors_synced=False,
    )
    code_storage.upsert_project_stats(
        IndexedProject(
            id=project_id,
            root_path=str(tmp_path),
            total_files=1,
            total_symbols=len(sample_symbols),
        )
    )
    code_storage.upsert_file(indexed_file)
    code_storage.upsert_symbols(sample_symbols)
    run_db = RecordingRunDb()

    did_sync = await _sync_file(
        storage=code_storage,
        vector_store=RecoveringVectorStore(),
        graph=None,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        embed_model=FakeEmbedModel(),
        project_id=project_id,
        root=tmp_path,
        file=indexed_file,
        embedding_dim=4,
        run_db=run_db,
    )

    assert did_sync is True
    assert run_db.calls == ["get_file", "get_symbols_for_file", "mark_vectors_synced"]


@pytest.mark.asyncio
async def test_sync_file_uses_current_row_for_sync_state_and_marker_id(tmp_path: Path) -> None:
    """A stale pending row should not overwrite current sync state or marker IDs."""
    project_id = "proj-1"
    file_path = "src/app.py"
    source_file = tmp_path / file_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")

    stale_file = IndexedFile(
        id="stale-file-id",
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="old",
        vectors_synced=False,
        graph_synced=False,
    )
    current_file = IndexedFile(
        id="current-file-id",
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="new",
        vectors_synced=True,
        graph_synced=False,
    )
    storage = MagicMock()
    storage.get_file.return_value = current_file
    storage.mark_graph_sync_attempted.return_value = None
    storage.mark_graph_synced.return_value = None
    storage.get_imports_for_file.return_value = []
    storage.get_calls_for_file.return_value = []
    storage.get_symbols_for_file.return_value = []
    vector_store = RecoveringVectorStore()
    graph = SimpleNamespace(available=True, sync_file=AsyncMock())
    run_db = RecordingRunDb()

    did_sync = await _sync_file(
        storage=storage,
        vector_store=vector_store,
        graph=graph,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=True),
        embed_model=FakeEmbedModel(),
        project_id=project_id,
        root=tmp_path,
        file=stale_file,
        embedding_dim=4,
        run_db=run_db,
    )

    assert did_sync is True
    assert vector_store.calls == []
    storage.mark_vectors_synced.assert_not_called()
    storage.mark_graph_sync_attempted.assert_called_once_with("current-file-id")
    storage.mark_graph_synced.assert_called_once_with("current-file-id")


@pytest.mark.asyncio
async def test_sync_graph_routes_relation_reads_through_run_db() -> None:
    """Graph relation reads use the injected DB runner before FalkorDB writes."""
    storage = MagicMock()
    storage.get_imports_for_file.return_value = [{"source_file": "a.py", "target_module": "b"}]
    storage.get_calls_for_file.return_value = []
    storage.get_symbols_for_file.return_value = [
        SimpleNamespace(id="sym-1", name="main", kind="function", line_start=1)
    ]
    graph = SimpleNamespace(sync_file=AsyncMock())
    file = IndexedFile(
        id="file-1",
        project_id="proj-1",
        file_path="a.py",
        language="python",
        content_hash="hash",
    )
    run_db = RecordingRunDb()

    await _sync_graph(storage, graph, "proj-1", file, run_db=run_db)

    assert run_db.calls == [
        "get_imports_for_file",
        "get_calls_for_file",
        "get_symbols_for_file",
    ]
    graph.sync_file.assert_awaited_once()

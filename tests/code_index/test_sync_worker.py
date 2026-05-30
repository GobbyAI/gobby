"""Tests for code_index.sync_worker external store sync."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeIndexedFileNotFoundError,
    GcodeProjectNotFoundError,
)
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


class RecordingGcodeGateway:
    def __init__(self, *, fail: bool = False, result: dict[str, Any] | None = None) -> None:
        self.fail = fail
        self.result = result or {"success": True}
        self.synced_files: list[tuple[Path, str]] = []

    async def graph_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        self.synced_files.append((project_root, file_path))
        if self.fail:
            raise RuntimeError("boom")
        return self.result


class IndexedFileNotFoundGcodeGateway:
    def __init__(self, *, remove_root: bool = False, remove_source: bool = False) -> None:
        self.remove_root = remove_root
        self.remove_source = remove_source
        self.synced_files: list[tuple[Path, str]] = []

    async def graph_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        self.synced_files.append((project_root, file_path))
        if self.remove_root:
            shutil.rmtree(project_root)
        if self.remove_source:
            (project_root / file_path).unlink()
        stderr = f"indexed file `{file_path}` was not found for project proj-1"
        raise GcodeIndexedFileNotFoundError(["gcode"], 2, stderr, file_path, "proj-1")


class CommandErrorGcodeGateway:
    async def graph_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        raise GcodeCommandError(
            ["gcode", "graph", "sync-file"],
            2,
            "real graph failure",
        )


class ProjectNotFoundGcodeGateway:
    """Gateway helper that simulates gcode losing access to a project root.

    When ``remove_root`` is true, ``graph_sync_file`` deliberately removes the
    provided ``project_root`` with ``shutil.rmtree`` before raising
    ``GcodeProjectNotFoundError``. Tests pass ``tmp_path`` roots, so that
    destructive side effect is isolated and intentional. ``synced_files``
    records attempted syncs for assertions.
    """

    def __init__(self, *, remove_root: bool = False) -> None:
        self.remove_root = remove_root
        self.synced_files: list[tuple[Path, str]] = []

    async def graph_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        self.synced_files.append((project_root, file_path))
        if self.remove_root:
            shutil.rmtree(project_root)
        stderr = f"Project '{project_root}' not found"
        raise GcodeProjectNotFoundError(["gcode"], 2, stderr, str(project_root))


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
async def test_sync_worker_keeps_vectors_live_when_graph_gateway_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing gcode graph sync must not starve vector syncing."""

    async def fake_generate_embeddings(
        texts: list[str],
        **_kwargs: Any,
    ) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr("gobby.search.embeddings.generate_embeddings", fake_generate_embeddings)
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=False)
    gcode_gateway = RecordingGcodeGateway(fail=True)
    context = SimpleNamespace(gcode_gateway=gcode_gateway, clear_graph=None)
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

    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)]
    assert [call[0] for call in vector_store.calls] == [
        "ensure_collection",
        "delete",
        "batch_upsert",
    ]
    storage.mark_vectors_synced.assert_called_once_with(pending_file.id)
    storage.mark_graph_sync_attempted.assert_not_called()
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_worker_delegates_graph_sync_to_gcode_gateway(tmp_path: Path) -> None:
    """The loop delegates graph projection sync to gcode."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    gcode_gateway = RecordingGcodeGateway()
    context = SimpleNamespace(gcode_gateway=gcode_gateway, clear_graph=None)
    shutdown_flag = asyncio.Event()

    storage = MagicMock()
    storage.list_indexed_projects.return_value = [_indexed_project(tmp_path)]
    storage.get_file.return_value = pending_file
    storage.get_imports_for_file.return_value = []
    storage.get_calls_for_file.return_value = []
    storage.get_symbols_for_file.return_value = []

    def pending_once(*_args: Any, **_kwargs: Any) -> list[IndexedFile]:
        shutdown_flag.set()
        return [pending_file]

    storage.get_pending_sync_files.side_effect = pending_once

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

    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)]
    storage.mark_graph_sync_attempted.assert_not_called()
    storage.mark_graph_synced.assert_called_once_with(pending_file.id)


@pytest.mark.asyncio
async def test_sync_pass_purges_missing_project_before_pending_files(tmp_path: Path) -> None:
    """Missing project roots are purged before polling pending graph work."""
    missing_root = tmp_path / "deleted-worktree"
    project = _indexed_project(missing_root)
    storage = MagicMock()
    storage.list_indexed_projects.return_value = [project]
    storage.delete_project_index.return_value = {
        "files": 1,
        "symbols": 2,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    clear_graph = AsyncMock(return_value={"success": True})
    vector_store = SimpleNamespace(delete_collection=AsyncMock())
    run_db = RecordingRunDb()

    await _sync_pass(
        storage=storage,
        vector_store=vector_store,
        gcode_gateway=RecordingGcodeGateway(),
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        embed_model=None,
        batch_size=10,
        embedding_dim=4,
        clear_graph=clear_graph,
        run_db=run_db,
    )

    assert not missing_root.exists()
    assert run_db.calls == ["list_indexed_projects", "delete_project_index"]
    storage.get_pending_sync_files.assert_not_called()
    storage.delete_project_index.assert_called_once_with(project.id)
    clear_graph.assert_awaited_once_with(project.id)
    vector_store.delete_collection.assert_awaited_once_with(f"code_symbols_{project.id}")


@pytest.mark.asyncio
async def test_sync_file_purges_when_gcode_project_missing_and_root_disappears(
    tmp_path: Path,
) -> None:
    """A stale gcode project error purges the index when the worktree vanished."""
    root = tmp_path / "repo"
    _write_source(root)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    storage.delete_project_index.return_value = {
        "files": 1,
        "symbols": 1,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    clear_graph = AsyncMock(return_value={"success": True})
    vector_store = SimpleNamespace(delete_collection=AsyncMock())

    did_sync = await _sync_file(
        storage=storage,
        vector_store=vector_store,
        gcode_gateway=ProjectNotFoundGcodeGateway(remove_root=True),
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        embed_model=None,
        project_id="proj-1",
        root=root,
        file=pending_file,
        embedding_dim=4,
        clear_graph=clear_graph,
        run_db=RecordingRunDb(),
    )

    assert did_sync is False
    assert not root.exists()
    storage.delete_project_index.assert_called_once_with("proj-1")
    clear_graph.assert_awaited_once_with("proj-1")
    vector_store.delete_collection.assert_awaited_once_with("code_symbols_proj-1")


@pytest.mark.asyncio
async def test_sync_file_warns_without_traceback_when_gcode_project_missing_but_root_exists(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A stale gcode project for an existing root is a warning without traceback noise."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            vector_store=None,
            gcode_gateway=ProjectNotFoundGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            embed_model=None,
            project_id="proj-1",
            root=tmp_path,
            file=pending_file,
            embedding_dim=4,
        )

    assert did_sync is False
    assert "gcode project missing for proj-1" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    storage.delete_project_index.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_skips_when_source_missing_before_graph_sync(tmp_path: Path) -> None:
    """A source file deleted after polling is skipped before calling gcode."""
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway()

    did_sync = await _sync_file(
        storage=storage,
        vector_store=None,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        embed_model=None,
        project_id="proj-1",
        root=tmp_path,
        file=pending_file,
        embedding_dim=4,
    )

    assert did_sync is False
    assert gcode_gateway.synced_files == []
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_leaves_graph_unsynced_when_indexed_row_disappears(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A row removed during graph sync is a stale race, not an ERROR."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.side_effect = [pending_file, None]
    gcode_gateway = IndexedFileNotFoundGcodeGateway()

    with caplog.at_level(logging.INFO, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            vector_store=None,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            embed_model=None,
            project_id="proj-1",
            root=tmp_path,
            file=pending_file,
            embedding_dim=4,
        )

    assert did_sync is False
    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)]
    assert "indexed file src/app.py disappeared from project proj-1" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_skips_when_source_disappears_during_graph_sync(
    tmp_path: Path,
) -> None:
    """A source file removed during graph sync reuses the normal missing-source skip."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.side_effect = [pending_file, pending_file]
    gcode_gateway = IndexedFileNotFoundGcodeGateway(remove_source=True)

    did_sync = await _sync_file(
        storage=storage,
        vector_store=None,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        embed_model=None,
        project_id="proj-1",
        root=tmp_path,
        file=pending_file,
        embedding_dim=4,
    )

    assert did_sync is False
    assert not (tmp_path / pending_file.file_path).exists()
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_warns_and_retries_when_indexed_row_still_exists(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A stale gcode file miss with a live row remains pending for retry."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.side_effect = [pending_file, pending_file]

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            vector_store=None,
            gcode_gateway=IndexedFileNotFoundGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            embed_model=None,
            project_id="proj-1",
            root=tmp_path,
            file=pending_file,
            embedding_dim=4,
        )

    assert did_sync is False
    assert any(
        record.levelno == logging.WARNING
        and "indexed file src/app.py missing in gcode project proj-1" in record.getMessage()
        and record.exc_info is None
        for record in caplog.records
    )
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_skipped_response_leaves_graph_unsynced(tmp_path: Path) -> None:
    """The new gcode skipped response is a no-op that remains retryable."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway(
        result={"status": "skipped", "reason": "indexed_file_not_found"}
    )

    did_sync = await _sync_file(
        storage=storage,
        vector_store=None,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        embed_model=None,
        project_id="proj-1",
        root=tmp_path,
        file=pending_file,
        embedding_dim=4,
    )

    assert did_sync is False
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_logs_unclassified_gcode_failures_as_errors(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Unclassified gcode command failures keep ERROR diagnostics with traceback."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file

    with caplog.at_level(logging.ERROR, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            vector_store=None,
            gcode_gateway=CommandErrorGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            embed_model=None,
            project_id="proj-1",
            root=tmp_path,
            file=pending_file,
            embedding_dim=4,
        )

    assert did_sync is False
    assert any(
        record.levelno == logging.ERROR
        and "Sync worker: graph sync failed for src/app.py" in record.getMessage()
        and record.exc_info
        for record in caplog.records
    )
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_logs_real_graph_failures_as_errors(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Ordinary graph gateway failures stay ERROR-level diagnostics."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file

    with caplog.at_level(logging.ERROR, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            vector_store=None,
            gcode_gateway=RecordingGcodeGateway(fail=True),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            embed_model=None,
            project_id="proj-1",
            root=tmp_path,
            file=pending_file,
            embedding_dim=4,
        )

    assert did_sync is False
    assert any(
        record.levelno == logging.ERROR
        and "Sync worker: graph sync failed for src/app.py: boom" in record.getMessage()
        and record.exc_info
        for record in caplog.records
    )


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
        gcode_gateway=None,
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
        gcode_gateway=SimpleNamespace(graph_sync_file=AsyncMock()),
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
        gcode_gateway=None,
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
    gcode_gateway = RecordingGcodeGateway()
    run_db = RecordingRunDb()

    did_sync = await _sync_file(
        storage=storage,
        vector_store=vector_store,
        gcode_gateway=gcode_gateway,
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
    storage.mark_graph_sync_attempted.assert_not_called()
    storage.mark_graph_synced.assert_called_once_with(current_file.id)
    assert gcode_gateway.synced_files == [(tmp_path, file_path)]


@pytest.mark.asyncio
async def test_sync_graph_delegates_to_gcode_without_python_relation_reads(tmp_path: Path) -> None:
    """Graph sync delegates to gcode instead of reading relation rows in Python."""
    gcode_gateway = RecordingGcodeGateway()
    file = IndexedFile(
        id="file-1",
        project_id="proj-1",
        file_path="a.py",
        language="python",
        content_hash="hash",
    )

    assert await _sync_graph(gcode_gateway, tmp_path, file) is True

    assert gcode_gateway.synced_files == [(tmp_path, "a.py")]

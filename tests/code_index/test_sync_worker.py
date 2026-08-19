"""Tests for code_index.sync_worker external store sync."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeFalkorTransportError,
    GcodeGateway,
    GcodeIndexedFileNotFoundError,
    GcodeProjectNotFoundError,
    GcodeTimeoutError,
)
from gobby.code_index.models import IndexedFile, IndexedProject
from gobby.code_index.storage import CodeIndexStorage
from gobby.code_index.sync_breaker import BreakerState, SyncCircuitBreaker
from gobby.code_index.sync_worker import _sync_file, _sync_graph, _sync_pass, sync_worker_loop
from gobby.config.code_index import CodeIndexConfig
from tests.code_index.conftest import PROJECT_ID

pytestmark = pytest.mark.unit

# Mock-only ids kept uuid-shaped to mirror the native uuid code_* columns.
STALE_FILE_ID = "00000000-0000-4000-8000-00000000aaaa"
CURRENT_FILE_ID = "00000000-0000-4000-8000-00000000bbbb"
FILE_1_ID = "00000000-0000-4000-8000-00000000cccc"


class RecordingRunDb:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        name = getattr(func, "__name__", None) or getattr(func, "_mock_name", None)
        self.calls.append(name if isinstance(name, str) else repr(func))
        return func(*args, **kwargs)


class RecordingGcodeGateway(GcodeGateway):
    def __init__(
        self,
        *,
        fail: bool = False,
        result: dict[str, Any] | None = None,
        vector_fail: bool = False,
        graph_timeout: bool = False,
        vector_timeout: bool = False,
        vector_result: dict[str, Any] | None = None,
        graph_errors: list[BaseException] | None = None,
    ) -> None:
        self.fail = fail
        self.result = result or {"success": True}
        self.vector_fail = vector_fail
        self.graph_timeout = graph_timeout
        self.vector_timeout = vector_timeout
        self.vector_result = vector_result or {"success": True}
        self.graph_errors = list(graph_errors or [])
        self.synced_files: list[tuple[Path, str]] = []
        self.vector_synced_files: list[tuple[Path, str]] = []
        self.graph_sync_timeouts: list[float | None] = []
        self.vector_sync_timeouts: list[float | None] = []

    async def graph_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.synced_files.append((project_root, file_path))
        self.graph_sync_timeouts.append(timeout)
        if self.graph_errors:
            raise self.graph_errors.pop(0)
        if self.graph_timeout:
            raise GcodeTimeoutError("gcode timed out: graph sync-file")
        if self.fail:
            raise RuntimeError("boom")
        return self.result

    async def vector_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.vector_synced_files.append((project_root, file_path))
        self.vector_sync_timeouts.append(timeout)
        if self.vector_timeout:
            raise GcodeTimeoutError("gcode timed out: vector sync-file")
        if self.vector_fail:
            raise RuntimeError("vector boom")
        return self.vector_result


class IndexedFileNotFoundGcodeGateway(GcodeGateway):
    def __init__(self, *, remove_root: bool = False, remove_source: bool = False) -> None:
        self.remove_root = remove_root
        self.remove_source = remove_source
        self.synced_files: list[tuple[Path, str]] = []
        self.vector_synced_files: list[tuple[Path, str]] = []

    async def graph_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.synced_files.append((project_root, file_path))
        if self.remove_root:
            shutil.rmtree(project_root)
        if self.remove_source:
            (project_root / file_path).unlink()
        stderr = f"indexed file `{file_path}` was not found for project {PROJECT_ID}"
        raise GcodeIndexedFileNotFoundError(["gcode"], 2, stderr, file_path, PROJECT_ID)

    async def vector_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.vector_synced_files.append((project_root, file_path))
        if self.remove_root:
            shutil.rmtree(project_root)
        if self.remove_source:
            (project_root / file_path).unlink()
        stderr = f"indexed file `{file_path}` was not found for project {PROJECT_ID}"
        raise GcodeIndexedFileNotFoundError(["gcode"], 2, stderr, file_path, PROJECT_ID)


class CommandErrorGcodeGateway(GcodeGateway):
    async def graph_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise GcodeCommandError(
            ["gcode", "graph", "sync-file"],
            2,
            "real graph failure",
        )


class ProjectNotFoundGcodeGateway(GcodeGateway):
    """Gateway helper that simulates gcode losing access to a project root.

    When ``remove_root`` is true, the first projection call deliberately removes
    the provided ``project_root`` with ``shutil.rmtree`` before raising
    ``GcodeProjectNotFoundError``. Tests pass ``tmp_path`` roots, so that
    destructive side effect is isolated and intentional.
    """

    def __init__(self, *, remove_root: bool = False) -> None:
        self.remove_root = remove_root
        self.synced_files: list[tuple[Path, str]] = []
        self.vector_synced_files: list[tuple[Path, str]] = []

    def _raise_project_not_found(self, project_root: Path) -> Never:
        if self.remove_root:
            shutil.rmtree(project_root)
        stderr = f"Project '{project_root}' not found"
        raise GcodeProjectNotFoundError(["gcode"], 2, stderr, str(project_root))

    async def graph_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.synced_files.append((project_root, file_path))
        self._raise_project_not_found(project_root)

    async def vector_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.vector_synced_files.append((project_root, file_path))
        self._raise_project_not_found(project_root)


def _indexed_project(root: Path) -> IndexedProject:
    return IndexedProject(id=PROJECT_ID, root_path=str(root), total_files=1, total_symbols=1)


def _indexed_file(
    *,
    vectors_synced: bool = False,
    graph_synced: bool = False,
    symbol_count: int = 1,
    language: str = "python",
) -> IndexedFile:
    return IndexedFile(
        id=IndexedFile.make_id(PROJECT_ID, "src/app.py", "abc123"),
        project_id=PROJECT_ID,
        file_path="src/app.py",
        language=language,
        content_hash="abc123",
        symbol_count=symbol_count,
        vectors_synced=vectors_synced,
        graph_synced=graph_synced,
    )


def _write_source(root: Path) -> None:
    source_file = root / "src/app.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")


@pytest.mark.asyncio
async def test_sync_worker_keeps_vectors_live_when_graph_gateway_fails(
    tmp_path: Path,
) -> None:
    """A failing gcode graph sync must not starve vector syncing."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=False)
    gcode_gateway = RecordingGcodeGateway(fail=True)
    context = cast(
        CodeIndexContext,
        SimpleNamespace(
            gcode_gateway=gcode_gateway,
            daemon_config_breaker=SyncCircuitBreaker(
                name="test",
                probe_target="daemon config",
                operation="sync",
            ),
        ),
    )
    shutdown_flag = asyncio.Event()

    storage = MagicMock()
    storage.list_indexed_projects.return_value = [_indexed_project(tmp_path)]
    storage.get_file.return_value = pending_file

    def pending_once(*_args: Any, **_kwargs: Any) -> list[IndexedFile]:
        shutdown_flag.set()
        return [pending_file]

    storage.get_pending_sync_files.side_effect = pending_once

    await sync_worker_loop(
        storage=storage,
        context=context,
        config=CodeIndexConfig(
            embedding_enabled=True,
            graph_enabled=True,
            sync_worker_interval_seconds=0.01,
        ),
        shutdown_flag=shutdown_flag,
        run_db=RecordingRunDb(),
    )

    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)]
    assert gcode_gateway.vector_synced_files == [(tmp_path, pending_file.file_path)]
    storage.mark_vectors_synced.assert_called_once_with(
        pending_file.id,
        pending_file.content_hash,
    )
    storage.clear_projection_cleanup_pending.assert_not_called()
    storage.mark_graph_sync_attempted.assert_called_once_with(pending_file.id)
    storage.mark_graph_synced.assert_not_called()


@pytest.mark.asyncio
async def test_sync_worker_delegates_graph_sync_to_gcode_gateway(tmp_path: Path) -> None:
    """The loop delegates graph projection sync to gcode."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    gcode_gateway = RecordingGcodeGateway()
    context = cast(
        CodeIndexContext,
        SimpleNamespace(
            gcode_gateway=gcode_gateway,
            daemon_config_breaker=SyncCircuitBreaker(
                name="test",
                probe_target="daemon config",
                operation="sync",
            ),
        ),
    )
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
        context=context,
        config=CodeIndexConfig(
            embedding_enabled=False,
            graph_enabled=True,
            sync_worker_interval_seconds=0.01,
        ),
        shutdown_flag=shutdown_flag,
        run_db=RecordingRunDb(),
    )

    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)]
    storage.mark_graph_sync_attempted.assert_called_once_with(pending_file.id)
    storage.mark_graph_synced.assert_called_once_with(
        pending_file.id,
        pending_file.content_hash,
    )
    storage.clear_projection_cleanup_pending.assert_not_called()


@pytest.mark.asyncio
async def test_sync_worker_logs_throttled_warning_on_pool_outage(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PoolTimeout during a sync pass logs a throttled WARNING and the loop continues."""
    from psycopg_pool import PoolTimeout

    import gobby.code_index.sync_worker as sync_worker_module

    sync_worker_module._pool_outage_log._last_logged.clear()
    context = cast(
        CodeIndexContext,
        SimpleNamespace(
            gcode_gateway=None,
            daemon_config_breaker=SyncCircuitBreaker(
                name="test",
                probe_target="daemon config",
                operation="sync",
            ),
        ),
    )
    shutdown_flag = asyncio.Event()

    storage = MagicMock()
    passes = 0

    def fail_then_stop(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal passes
        passes += 1
        if passes >= 3:
            shutdown_flag.set()
        raise PoolTimeout("couldn't get a connection after 5.00 sec")

    storage.list_indexed_projects.side_effect = fail_then_stop

    with caplog.at_level(logging.DEBUG, logger="gobby.code_index.sync_worker"):
        await sync_worker_loop(
            storage=storage,
            context=context,
            config=CodeIndexConfig(
                embedding_enabled=False,
                graph_enabled=False,
                sync_worker_interval_seconds=0.01,
            ),
            shutdown_flag=shutdown_flag,
            run_db=RecordingRunDb(),
        )

    assert passes == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "hub temporarily unavailable; skipping pass" in warnings[0].getMessage()
    assert warnings[0].exc_info is None


@pytest.mark.asyncio
async def test_sync_file_marks_zero_symbol_file_graph_synced_without_gcode(
    tmp_path: Path,
) -> None:
    """Content-only files have no graph projection work and should not call gcode."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False, symbol_count=0)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway()

    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        project_id=PROJECT_ID,
        root=tmp_path,
        file=pending_file,
        run_db=RecordingRunDb(),
    )

    assert did_sync is True
    assert gcode_gateway.synced_files == []
    storage.mark_graph_sync_attempted.assert_not_called()
    storage.mark_graph_synced.assert_called_once_with(
        pending_file.id,
        pending_file.content_hash,
    )
    storage.clear_projection_cleanup_pending.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_marks_non_graph_language_graph_synced_without_gcode(
    tmp_path: Path,
) -> None:
    """Content-search languages can have symbols but no call/import graph projection."""
    _write_source(tmp_path)
    pending_file = _indexed_file(
        vectors_synced=True,
        graph_synced=False,
        symbol_count=1,
        language="json",
    )
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway()

    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        project_id=PROJECT_ID,
        root=tmp_path,
        file=pending_file,
        run_db=RecordingRunDb(),
    )

    assert did_sync is True
    assert gcode_gateway.synced_files == []
    storage.mark_graph_sync_attempted.assert_not_called()
    storage.mark_graph_synced.assert_called_once_with(
        pending_file.id,
        pending_file.content_hash,
    )
    storage.clear_projection_cleanup_pending.assert_not_called()


@pytest.mark.asyncio
async def test_sync_pass_skips_missing_project_before_pending_files(tmp_path: Path) -> None:
    """Missing project roots are skipped before polling pending graph work."""
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
    run_db = RecordingRunDb()

    await _sync_pass(
        storage=storage,
        gcode_gateway=RecordingGcodeGateway(),
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        batch_size=10,
        run_db=run_db,
    )

    assert not missing_root.exists()
    assert run_db.calls == ["list_indexed_projects"]
    storage.get_pending_sync_files.assert_not_called()
    storage.delete_project_index.assert_not_called()


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
    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=ProjectNotFoundGcodeGateway(remove_root=True),
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        project_id=PROJECT_ID,
        root=root,
        file=pending_file,
        run_db=RecordingRunDb(),
    )

    assert did_sync is False
    assert not root.exists()
    storage.delete_project_index.assert_called_once_with(PROJECT_ID)


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
            gcode_gateway=ProjectNotFoundGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert f"gcode project missing for {PROJECT_ID}" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    storage.delete_project_index.assert_not_called()


@pytest.mark.asyncio
async def test_vector_project_missing_purges_vanished_root_once_and_skips_graph(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _write_source(root)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=False)
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
    gateway = ProjectNotFoundGcodeGateway(remove_root=True)
    breaker = SyncCircuitBreaker(
        name="Vector sync",
        probe_target="embedding endpoint",
        operation="vector sync",
    )

    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=gateway,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=True),
        project_id=PROJECT_ID,
        root=root,
        file=pending_file,
        run_db=RecordingRunDb(),
        vector_breaker=breaker,
    )

    assert did_sync is False
    assert not root.exists()
    assert gateway.vector_synced_files == [(root, pending_file.file_path)]
    assert gateway.synced_files == []
    storage.delete_project_index.assert_called_once_with(PROJECT_ID)
    storage.mark_vectors_synced.assert_not_called()
    storage.mark_graph_sync_attempted.assert_not_called()
    assert breaker.state is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_vector_project_missing_warns_existing_root_and_stays_pending(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gateway = ProjectNotFoundGcodeGateway()
    breaker = SyncCircuitBreaker(
        name="Vector sync",
        probe_target="embedding endpoint",
        operation="vector sync",
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=gateway,
            config=CodeIndexConfig(embedding_enabled=True, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
            vector_breaker=breaker,
        )

    assert did_sync is False
    assert gateway.vector_synced_files == [(tmp_path, pending_file.file_path)]
    assert gateway.synced_files == []
    storage.delete_project_index.assert_not_called()
    storage.mark_vectors_synced.assert_not_called()
    storage.mark_graph_sync_attempted.assert_not_called()
    assert breaker.state is BreakerState.CLOSED
    assert any(
        record.levelno == logging.WARNING
        and f"gcode project missing for {PROJECT_ID}" in record.getMessage()
        and "during vector sync" in record.getMessage()
        and not record.exc_info
        for record in caplog.records
    )
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_sync_file_skips_when_source_missing_before_graph_sync(tmp_path: Path) -> None:
    """A source file deleted after polling is skipped before calling gcode."""
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway()

    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        project_id=PROJECT_ID,
        root=tmp_path,
        file=pending_file,
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
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)]
    assert f"indexed file src/app.py disappeared from project {PROJECT_ID}" in caplog.text
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
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        project_id=PROJECT_ID,
        root=tmp_path,
        file=pending_file,
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
            gcode_gateway=IndexedFileNotFoundGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert any(
        record.levelno == logging.WARNING
        and f"indexed file src/app.py missing in gcode project {PROJECT_ID}" in record.getMessage()
        and record.exc_info is None
        for record in caplog.records
    )
    storage.mark_graph_synced.assert_not_called()
    storage.mark_graph_sync_attempted.assert_called_once_with(pending_file.id)


@pytest.mark.asyncio
async def test_sync_file_skipped_response_marks_graph_synced(tmp_path: Path) -> None:
    """The gcode skipped response is terminal for the daemon projection queue."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway(
        result={"status": "skipped", "reason": "indexed_file_not_found"}
    )

    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
        project_id=PROJECT_ID,
        root=tmp_path,
        file=pending_file,
    )

    assert did_sync is True
    storage.mark_graph_synced.assert_called_once_with(
        pending_file.id,
        pending_file.content_hash,
    )
    storage.clear_projection_cleanup_pending.assert_not_called()


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
            gcode_gateway=CommandErrorGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
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
            gcode_gateway=RecordingGcodeGateway(fail=True),
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert any(
        record.levelno == logging.ERROR
        and "Sync worker: graph sync failed for src/app.py: boom" in record.getMessage()
        and record.exc_info
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_sync_file_warns_and_retries_when_graph_sync_times_out(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Graph gcode timeouts exhaust bounded retries and stay pending."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway(graph_timeout=True)

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(
                embedding_enabled=False,
                graph_enabled=True,
                sync_worker_projection_timeout_seconds=124.0,
            ),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)] * 3
    assert gcode_gateway.graph_sync_timeouts == [124.0] * 3
    storage.mark_graph_synced.assert_not_called()
    storage.mark_graph_sync_attempted.assert_called_once_with(pending_file.id)
    assert any(
        record.levelno == logging.WARNING
        and "Sync worker: transient graph sync failure for src/app.py" in record.getMessage()
        and not record.exc_info
        for record in caplog.records
    )
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "graph sync retries exhausted for src/app.py" in errors[0].getMessage()
    assert errors[0].exc_info is None


def _falkor_transport_error() -> GcodeFalkorTransportError:
    return GcodeFalkorTransportError(
        ["gcode", "graph", "sync-file"],
        1,
        "Error: FalkorDB graph query failed: Resource temporarily unavailable (os error 35)",
    )


@pytest.mark.asyncio
async def test_sync_file_retries_transient_falkor_graph_error(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A one-shot Falkor EAGAIN is retried and then marked synced."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway(graph_errors=[_falkor_transport_error()])

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is True
    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)] * 2
    storage.mark_graph_synced.assert_called_once_with(pending_file.id, pending_file.content_hash)
    assert any(
        record.levelno == logging.WARNING
        and "transient graph sync failure for src/app.py" in record.getMessage()
        and not record.exc_info
        for record in caplog.records
    )
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_sync_file_exhausts_transient_falkor_graph_retries(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Persistent Falkor EAGAIN stays unsynced without a traceback."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=True, graph_synced=False)
    storage = MagicMock()
    storage.get_file.return_value = pending_file
    gcode_gateway = RecordingGcodeGateway(
        graph_errors=[
            _falkor_transport_error(),
            _falkor_transport_error(),
            _falkor_transport_error(),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(embedding_enabled=False, graph_enabled=True),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert gcode_gateway.synced_files == [(tmp_path, pending_file.file_path)] * 3
    storage.mark_graph_synced.assert_not_called()
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "graph sync retries exhausted for src/app.py" in errors[0].getMessage()
    assert errors[0].exc_info is None


@pytest.mark.asyncio
async def test_sync_file_delegates_vector_sync_to_gcode_gateway(
    code_storage: CodeIndexStorage,
    tmp_path: Path,
) -> None:
    """Vector projection work is delegated to gcode and marks the row on success."""
    project_id = PROJECT_ID
    file_path = "src/app.py"
    _write_source(tmp_path)
    indexed_file = _indexed_file(vectors_synced=False, graph_synced=True)
    code_storage.upsert_project_stats(
        IndexedProject(id=project_id, root_path=str(tmp_path), total_files=1, total_symbols=1)
    )
    code_storage.upsert_file(indexed_file)
    code_storage.record_projection_cleanup_failure(project_id, "vector", "stale vector drift")
    gcode_gateway = RecordingGcodeGateway()

    did_sync = await _sync_file(
        storage=code_storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        project_id=project_id,
        root=tmp_path,
        file=indexed_file,
    )

    assert did_sync is True
    assert gcode_gateway.vector_synced_files == [(tmp_path, file_path)]
    synced_file = code_storage.get_file(project_id, file_path)
    assert synced_file is not None
    assert synced_file.vectors_synced is True
    assert synced_file.vector_sync_attempted_at is not None
    assert [
        (pending.project_id, pending.store)
        for pending in code_storage.list_projection_cleanup_pending()
    ] == [(project_id, "vector")]


@pytest.mark.asyncio
async def test_sync_file_warns_and_retries_when_vector_sync_times_out(
    caplog: pytest.LogCaptureFixture,
    code_storage: CodeIndexStorage,
    tmp_path: Path,
) -> None:
    """Vector gcode timeouts exhaust bounded retries and stay pending."""
    project_id = PROJECT_ID
    file_path = "src/app.py"
    _write_source(tmp_path)
    indexed_file = _indexed_file(vectors_synced=False, graph_synced=True)
    code_storage.upsert_project_stats(
        IndexedProject(id=project_id, root_path=str(tmp_path), total_files=1, total_symbols=1)
    )
    code_storage.upsert_file(indexed_file)
    code_storage.record_projection_cleanup_failure(project_id, "vector", "stale vector drift")
    gcode_gateway = RecordingGcodeGateway(vector_timeout=True)

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=code_storage,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(
                embedding_enabled=True,
                graph_enabled=False,
                sync_worker_projection_timeout_seconds=123.0,
            ),
            project_id=project_id,
            root=tmp_path,
            file=indexed_file,
        )

    assert did_sync is False
    assert gcode_gateway.vector_synced_files == [(tmp_path, file_path)] * 3
    assert gcode_gateway.vector_sync_timeouts == [123.0] * 3
    synced_file = code_storage.get_file(project_id, file_path)
    assert synced_file is not None
    assert synced_file.vectors_synced is False
    assert synced_file.vector_sync_attempted_at is not None
    assert code_storage.list_projection_cleanup_pending()
    assert any(
        record.levelno == logging.WARNING
        and "Sync worker: transient vector sync failure for src/app.py" in record.getMessage()
        and not record.exc_info
        for record in caplog.records
    )
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "vector sync retries exhausted for src/app.py" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_sync_file_leaves_vectors_unsynced_when_indexed_row_disappears(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A row removed during vector sync is stale work, not an ERROR."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=True)
    storage = MagicMock()
    storage.get_file.side_effect = [pending_file, None]
    gcode_gateway = IndexedFileNotFoundGcodeGateway()

    with caplog.at_level(logging.INFO, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert gcode_gateway.vector_synced_files == [(tmp_path, pending_file.file_path)]
    assert f"indexed file src/app.py disappeared from project {PROJECT_ID}" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    storage.mark_vectors_synced.assert_not_called()
    storage.clear_projection_cleanup_pending.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_skips_when_source_disappears_during_vector_sync(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A source file removed during vector sync reuses the normal missing-source skip."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=True)
    storage = MagicMock()
    storage.get_file.side_effect = [pending_file, pending_file]
    gcode_gateway = IndexedFileNotFoundGcodeGateway(remove_source=True)

    with caplog.at_level(logging.ERROR, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=gcode_gateway,
            config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert not (tmp_path / pending_file.file_path).exists()
    assert not caplog.records
    storage.mark_vectors_synced.assert_not_called()
    storage.clear_projection_cleanup_pending.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_warns_and_retries_when_vector_indexed_row_still_exists(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A stale gcode vector file miss with a live row remains pending for retry."""
    _write_source(tmp_path)
    pending_file = _indexed_file(vectors_synced=False, graph_synced=True)
    storage = MagicMock()
    storage.get_file.side_effect = [pending_file, pending_file]

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.sync_worker"):
        did_sync = await _sync_file(
            storage=storage,
            gcode_gateway=IndexedFileNotFoundGcodeGateway(),
            config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
            project_id=PROJECT_ID,
            root=tmp_path,
            file=pending_file,
        )

    assert did_sync is False
    assert any(
        record.levelno == logging.WARNING
        and f"indexed file src/app.py missing in gcode project {PROJECT_ID}" in record.getMessage()
        and "vector sync; leaving vectors_synced=false" in record.getMessage()
        and record.exc_info is None
        for record in caplog.records
    )
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    storage.mark_vectors_synced.assert_not_called()
    storage.mark_vector_sync_attempted.assert_called_once_with(pending_file.id)
    storage.clear_projection_cleanup_pending.assert_not_called()


@pytest.mark.asyncio
async def test_sync_file_does_not_mark_vectors_synced_when_gcode_vector_sync_fails(
    code_storage: CodeIndexStorage,
    tmp_path: Path,
) -> None:
    """A failed gcode vector sync leaves the row pending for retry."""
    project_id = PROJECT_ID
    file_path = "src/app.py"
    _write_source(tmp_path)
    indexed_file = _indexed_file(vectors_synced=False, graph_synced=True)
    code_storage.upsert_project_stats(
        IndexedProject(id=project_id, root_path=str(tmp_path), total_files=1, total_symbols=1)
    )
    code_storage.upsert_file(indexed_file)
    gcode_gateway = RecordingGcodeGateway(
        vector_result={"success": False, "error": "gcode vector sync failed"}
    )

    did_sync = await _sync_file(
        storage=code_storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        project_id=project_id,
        root=tmp_path,
        file=indexed_file,
    )

    assert did_sync is False
    assert gcode_gateway.vector_synced_files == [(tmp_path, file_path)]
    synced_file = code_storage.get_file(project_id, file_path)
    assert synced_file is not None
    assert synced_file.vectors_synced is False
    assert synced_file.vector_sync_attempted_at is not None


@pytest.mark.asyncio
async def test_sync_pass_polls_projects_and_files_through_run_db(tmp_path: Path) -> None:
    """Project and pending-file polling uses the injected DB runner."""
    project = IndexedProject(id=PROJECT_ID, root_path=str(tmp_path), total_files=0, total_symbols=0)
    storage = MagicMock()
    storage.list_indexed_projects.return_value = [project]
    storage.get_pending_sync_files.return_value = []
    run_db = RecordingRunDb()

    await _sync_pass(
        storage=storage,
        gcode_gateway=cast(
            GcodeGateway,
            SimpleNamespace(
                graph_sync_file=AsyncMock(),
                vector_sync_file=AsyncMock(return_value={"success": True}),
            ),
        ),
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=True),
        batch_size=10,
        run_db=run_db,
    )

    assert run_db.calls == ["list_indexed_projects", "get_pending_sync_files"]


@pytest.mark.asyncio
async def test_sync_file_routes_vector_storage_calls_through_run_db(
    code_storage: CodeIndexStorage,
    tmp_path: Path,
) -> None:
    """Vector marker writes use the injected DB runner."""
    project_id = PROJECT_ID
    file_path = "src/app.py"
    source_file = tmp_path / file_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")

    indexed_file = IndexedFile(
        id=IndexedFile.make_id(project_id, file_path, "abc123"),
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="abc123",
        symbol_count=1,
        graph_synced=True,
        vectors_synced=False,
    )
    code_storage.upsert_project_stats(
        IndexedProject(
            id=project_id,
            root_path=str(tmp_path),
            total_files=1,
            total_symbols=1,
        )
    )
    code_storage.upsert_file(indexed_file)
    run_db = RecordingRunDb()
    gcode_gateway = RecordingGcodeGateway()

    did_sync = await _sync_file(
        storage=code_storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=False),
        project_id=project_id,
        root=tmp_path,
        file=indexed_file,
        run_db=run_db,
    )

    assert did_sync is True
    assert run_db.calls == [
        "get_file",
        "mark_vector_sync_attempted",
        "mark_vectors_synced",
    ]
    assert gcode_gateway.vector_synced_files == [(tmp_path, file_path)]


@pytest.mark.asyncio
async def test_sync_file_uses_current_row_for_sync_state_and_marker_id(tmp_path: Path) -> None:
    """A stale pending row should not overwrite current sync state or marker IDs."""
    project_id = PROJECT_ID
    file_path = "src/app.py"
    source_file = tmp_path / file_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def greet(name: str) -> str:\n    return name\n")

    stale_file = IndexedFile(
        id=STALE_FILE_ID,
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="old",
        vectors_synced=False,
        graph_synced=False,
    )
    current_file = IndexedFile(
        id=CURRENT_FILE_ID,
        project_id=project_id,
        file_path=file_path,
        language="python",
        content_hash="new",
        symbol_count=1,
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
    gcode_gateway = RecordingGcodeGateway()
    run_db = RecordingRunDb()

    did_sync = await _sync_file(
        storage=storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(embedding_enabled=True, graph_enabled=True),
        project_id=project_id,
        root=tmp_path,
        file=stale_file,
        run_db=run_db,
    )

    assert did_sync is True
    assert gcode_gateway.vector_synced_files == []
    storage.mark_vectors_synced.assert_not_called()
    storage.mark_graph_sync_attempted.assert_called_once_with(current_file.id)
    storage.mark_graph_synced.assert_called_once_with(current_file.id, current_file.content_hash)
    storage.clear_projection_cleanup_pending.assert_not_called()
    assert gcode_gateway.synced_files == [(tmp_path, file_path)]


@pytest.mark.asyncio
async def test_sync_graph_delegates_to_gcode_without_python_relation_reads(tmp_path: Path) -> None:
    """Graph sync delegates to gcode instead of reading relation rows in Python."""
    gcode_gateway = RecordingGcodeGateway()
    file = IndexedFile(
        id=FILE_1_ID,
        project_id=PROJECT_ID,
        file_path="a.py",
        language="python",
        content_hash="hash",
    )

    assert await _sync_graph(gcode_gateway, tmp_path, file) is True

    assert gcode_gateway.synced_files == [(tmp_path, "a.py")]

"""Tests for code index maintenance."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, TypeVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.code_index.maintenance import (
    _run_maintenance,
    _summarize_unsummarized,
    _update_symbol_summaries,
)
from gobby.code_index.models import IndexedProject

pytestmark = pytest.mark.unit

T = TypeVar("T")


class _MaintenanceConfig(Protocol):
    graph_enabled: bool
    embedding_enabled: bool
    missing_root_purge_observations: int


class _MaintenanceContext(Protocol):
    storage: Any
    gcode_gateway: Any | None
    config: _MaintenanceConfig

    async def run_db(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T: ...

    async def clear_graph(self, project_id: str) -> dict[str, Any]: ...


class _MaintenanceProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self.stderr


class RecordingGcodeGateway:
    def __init__(
        self,
        *,
        vector_sync_result: dict[str, Any] | None = None,
        vector_clear_result: dict[str, Any] | None = None,
    ) -> None:
        self.vector_sync_result = vector_sync_result or {"success": True}
        self.vector_clear_result = vector_clear_result or {"success": True}
        self.vector_synced_files: list[tuple[Path, str]] = []
        self.vector_cleared_roots: list[Path] = []

    async def vector_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        self.vector_synced_files.append((project_root, file_path))
        return self.vector_sync_result

    async def vector_clear(self, project_root: Path) -> dict[str, Any]:
        self.vector_cleared_roots.append(project_root)
        return self.vector_clear_result


@pytest.mark.asyncio
async def test_maintenance_purges_indexed_project_after_missing_threshold(
    tmp_path: Path,
) -> None:
    """Missing indexed roots are purged after consecutive maintenance observations."""
    missing_root = tmp_path / "missing"
    project = IndexedProject(
        id="proj-missing",
        root_path=str(missing_root),
        total_files=2,
        total_symbols=3,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    storage.delete_project_index.return_value = {
        "files": 2,
        "symbols": 3,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    clear_graph = AsyncMock(return_value={"success": True})
    gcode_gateway = RecordingGcodeGateway()
    run_db_calls: list[str] = []

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        func_name = getattr(func, "__name__", None)
        if not isinstance(func_name, str):
            mock_name = getattr(func, "_mock_name", None)
            func_name = mock_name if isinstance(mock_name, str) else repr(func)
        run_db_calls.append(func_name)
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=clear_graph,
        gcode_gateway=gcode_gateway,
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            missing_root_purge_observations=2,
        ),
        run_db=run_db,
    )
    missing_root_observations: dict[str, int] = {}

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec") as create_proc,
    ):
        await _run_maintenance(
            context,
            missing_root_observations=missing_root_observations,
        )

    storage.delete_project_index.assert_not_called()
    clear_graph.assert_not_awaited()
    assert gcode_gateway.vector_cleared_roots == []
    create_proc.assert_not_called()
    assert missing_root_observations == {"proj-missing": 1}

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec") as create_proc,
    ):
        await _run_maintenance(
            context,
            missing_root_observations=missing_root_observations,
        )

    storage.delete_project_index.assert_called_once_with("proj-missing")
    clear_graph.assert_awaited_once_with("proj-missing")
    assert gcode_gateway.vector_cleared_roots == []
    create_proc.assert_not_called()
    assert not missing_root.exists()
    assert missing_root_observations == {}
    assert run_db_calls == [
        "list_projection_cleanup_pending",
        "list_indexed_projects",
        "list_projection_cleanup_pending",
        "list_indexed_projects",
        "delete_project_index",
    ]


@pytest.mark.asyncio
async def test_maintenance_retries_pending_vector_projection_cleanup(tmp_path: Path) -> None:
    class Storage:
        def __init__(self) -> None:
            self.cleared: list[tuple[str, str]] = []
            self.failures: list[tuple[str, str, str]] = []

        def list_projection_cleanup_pending(self, _limit: int) -> list[Any]:
            return [SimpleNamespace(project_id="proj-retry", store="vector")]

        def list_indexed_projects(self) -> list[Any]:
            return []

        def get_project_stats(self, project_id: str) -> IndexedProject:
            assert project_id == "proj-retry"
            return IndexedProject(
                id="proj-retry",
                root_path=str(tmp_path),
                total_files=1,
                total_symbols=1,
            )

        def clear_projection_cleanup_pending(self, project_id: str, store: str) -> bool:
            self.cleared.append((project_id, store))
            return True

        def record_projection_cleanup_failure(
            self,
            project_id: str,
            store: str,
            error: str,
        ) -> None:
            self.failures.append((project_id, store, error))

    storage = Storage()
    gcode_gateway = RecordingGcodeGateway()

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=gcode_gateway,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=run_db,
    )

    with patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"):
        await _run_maintenance(context)

    assert gcode_gateway.vector_cleared_roots == [tmp_path]
    assert storage.cleared == [("proj-retry", "vector")]
    assert storage.failures == []


@pytest.mark.asyncio
async def test_maintenance_purges_indexed_project_when_gcode_rejects_existing_root(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = IndexedProject(
        id="proj-stale",
        root_path=str(root),
        total_files=2,
        total_symbols=3,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    storage.delete_project_index.return_value = {
        "files": 2,
        "symbols": 3,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    clear_graph = AsyncMock(return_value={"success": True})
    gcode_gateway = RecordingGcodeGateway()
    summarizer = SimpleNamespace(summarize_batch=AsyncMock())
    run_db_calls: list[str] = []
    subprocess_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        func_name = getattr(func, "__name__", None)
        if not isinstance(func_name, str):
            mock_name = getattr(func, "_mock_name", None)
            func_name = mock_name if isinstance(mock_name, str) else repr(func)
        run_db_calls.append(func_name)
        return func(*args, **kwargs)

    async def create_subprocess_exec(*args: Any, **kwargs: Any) -> _MaintenanceProcess:
        subprocess_calls.append((args, kwargs))
        return _MaintenanceProcess(
            returncode=1,
            stderr=b"No gcode project found. Run `gcode init` to initialize this directory.",
        )

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=clear_graph,
        gcode_gateway=gcode_gateway,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=run_db,
    )

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", side_effect=create_subprocess_exec),
        caplog.at_level(logging.WARNING, logger="gobby.code_index.maintenance"),
    ):
        await _run_maintenance(context, summarizer=summarizer)

    assert subprocess_calls == [
        (
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(root),
                "--quiet",
                "--sync-projections",
            ),
            {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.PIPE},
        )
    ]
    storage.delete_project_index.assert_called_once_with("proj-stale")
    storage.get_unsummarized_symbols.assert_not_called()
    clear_graph.assert_awaited_once_with("proj-stale")
    assert gcode_gateway.vector_cleared_roots == []
    summarizer.summarize_batch.assert_not_called()
    assert "Maintenance reindex failed" not in caplog.text
    assert run_db_calls == [
        "list_projection_cleanup_pending",
        "list_indexed_projects",
        "delete_project_index",
    ]


@pytest.mark.asyncio
async def test_maintenance_reconciles_deleted_and_renamed_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source_dir = root / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "new_name.py").write_text("def kept() -> None:\n    pass\n")
    (source_dir / "kept.py").write_text("def kept_too() -> None:\n    pass\n")

    project = IndexedProject(
        id="proj-orphans",
        root_path=str(root),
        total_files=4,
        total_symbols=8,
    )
    indexed_files = [
        SimpleNamespace(file_path="src/old_name.py"),
        SimpleNamespace(file_path="src/new_name.py"),
        SimpleNamespace(file_path="src/deleted.py"),
        SimpleNamespace(file_path="src/kept.py"),
    ]

    class Storage:
        def __init__(self) -> None:
            self.current_paths: list[set[str]] = []
            self.deleted: list[tuple[str, str]] = []
            self.graph_resets: list[str] = []
            self.prune_dirty: list[tuple[str, str, str]] = []

        def list_projection_cleanup_pending(self, _limit: int) -> list[Any]:
            return []

        def list_indexed_projects(self) -> list[IndexedProject]:
            return [project]

        def list_files(self, _project_id: str) -> list[Any]:
            return indexed_files

        def get_orphan_files(self, _project_id: str, current_paths: set[str]) -> list[str]:
            self.current_paths.append(current_paths)
            return [file.file_path for file in indexed_files if file.file_path not in current_paths]

        def delete_imports_for_file(self, project_id: str, file_path: str) -> int:
            self.deleted.append(("imports", file_path))
            assert project_id == project.id
            return 1

        def delete_calls_for_file(self, project_id: str, file_path: str) -> int:
            self.deleted.append(("calls", file_path))
            assert project_id == project.id
            return 1

        def delete_content_chunks_for_file(self, project_id: str, file_path: str) -> None:
            self.deleted.append(("content", file_path))
            assert project_id == project.id

        def delete_symbols_for_file(self, project_id: str, file_path: str) -> int:
            self.deleted.append(("symbols", file_path))
            assert project_id == project.id
            return 2

        def delete_file(self, project_id: str, file_path: str) -> None:
            self.deleted.append(("file", file_path))
            assert project_id == project.id

        def reset_graph_sync_for_project(self, project_id: str) -> int:
            self.graph_resets.append(project_id)
            return 2

        def mark_prune_dirty(self, project_id: str, root_path: str, reason: str) -> None:
            self.prune_dirty.append((project_id, root_path, reason))

        def get_unsummarized_symbols(
            self,
            _project_id: str,
            _kinds: list[str] | None = None,
            _limit: int = 20,
        ) -> list[Any]:
            return []

    storage = Storage()
    gcode_gateway = RecordingGcodeGateway()
    clear_graph = AsyncMock(return_value={"success": True})
    subprocess_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    async def create_subprocess_exec(*args: Any, **kwargs: Any) -> _MaintenanceProcess:
        subprocess_calls.append((args, kwargs))
        return _MaintenanceProcess()

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=clear_graph,
        gcode_gateway=gcode_gateway,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=run_db,
    )

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", side_effect=create_subprocess_exec),
    ):
        await _run_maintenance(context)

    assert subprocess_calls == [
        (
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(root),
                "--quiet",
                "--sync-projections",
            ),
            {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.PIPE},
        )
    ]
    assert storage.current_paths == [{"src/new_name.py", "src/kept.py"}]
    assert gcode_gateway.vector_synced_files == [
        (root, "src/old_name.py"),
        (root, "src/deleted.py"),
    ]
    assert storage.deleted == [
        ("imports", "src/old_name.py"),
        ("calls", "src/old_name.py"),
        ("content", "src/old_name.py"),
        ("symbols", "src/old_name.py"),
        ("file", "src/old_name.py"),
        ("imports", "src/deleted.py"),
        ("calls", "src/deleted.py"),
        ("content", "src/deleted.py"),
        ("symbols", "src/deleted.py"),
        ("file", "src/deleted.py"),
    ]
    assert storage.prune_dirty == [(project.id, str(root), "orphan_files")]
    clear_graph.assert_awaited_once_with(project.id)
    assert storage.graph_resets == [project.id]


@pytest.mark.asyncio
async def test_maintenance_keeps_orphan_row_when_vector_cleanup_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = IndexedProject(
        id="proj-orphans",
        root_path=str(root),
        total_files=1,
        total_symbols=2,
    )
    indexed_files = [SimpleNamespace(file_path="src/deleted.py")]

    class Storage:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str]] = []
            self.prune_dirty: list[tuple[str, str, str]] = []

        def list_projection_cleanup_pending(self, _limit: int) -> list[Any]:
            return []

        def list_indexed_projects(self) -> list[IndexedProject]:
            return [project]

        def list_files(self, _project_id: str) -> list[Any]:
            return indexed_files

        def get_orphan_files(self, _project_id: str, _current_paths: set[str]) -> list[str]:
            return ["src/deleted.py"]

        def delete_imports_for_file(self, _project_id: str, file_path: str) -> int:
            self.deleted.append(("imports", file_path))
            return 1

        def delete_calls_for_file(self, _project_id: str, file_path: str) -> int:
            self.deleted.append(("calls", file_path))
            return 1

        def delete_content_chunks_for_file(self, _project_id: str, file_path: str) -> None:
            self.deleted.append(("content", file_path))

        def delete_symbols_for_file(self, _project_id: str, file_path: str) -> int:
            self.deleted.append(("symbols", file_path))
            return 2

        def delete_file(self, _project_id: str, file_path: str) -> None:
            self.deleted.append(("file", file_path))

        def mark_prune_dirty(self, project_id: str, root_path: str, reason: str) -> None:
            self.prune_dirty.append((project_id, root_path, reason))

    storage = Storage()
    gcode_gateway = RecordingGcodeGateway(
        vector_sync_result={"success": False, "error": "vector cleanup failed"}
    )
    clear_graph = AsyncMock(return_value={"success": True})

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=clear_graph,
        gcode_gateway=gcode_gateway,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=run_db,
    )

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=_MaintenanceProcess()),
    ):
        await _run_maintenance(context)

    assert gcode_gateway.vector_synced_files == [(root, "src/deleted.py")]
    assert storage.deleted == []
    assert storage.prune_dirty == []
    clear_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintenance_logs_and_raises_on_unexpected_delete_counts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing_root = tmp_path / "missing"
    project = IndexedProject(
        id="proj-missing",
        root_path=str(missing_root),
        total_files=2,
        total_symbols=3,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    storage.delete_project_index.return_value = ["bad"]

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=None,
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            missing_root_purge_observations=1,
        ),
        run_db=run_db,
    )

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        caplog.at_level(logging.WARNING, logger="gobby.code_index.cleanup"),
        pytest.raises(TypeError, match="delete_project_index returned list"),
    ):
        await _run_maintenance(context)

    assert "delete_project_index returned unexpected list" in caplog.text
    assert "['bad']" in caplog.text


@pytest.mark.asyncio
async def test_summary_updates_are_concurrency_limited() -> None:
    """Summary DB writes stay bounded even when a batch contains many updates."""
    lock = threading.Lock()
    active = 0
    max_active = 0
    all_slots_busy = threading.Event()
    release_updates = threading.Event()
    waits_completed: list[bool] = []

    def update_symbol_summary(_symbol_id: str, _content_hash: str, _summary: str) -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                all_slots_busy.set()
        try:
            waits_completed.append(release_updates.wait(timeout=1))
        finally:
            with lock:
                active -= 1

    context: _MaintenanceContext = SimpleNamespace(
        storage=SimpleNamespace(update_symbol_summary=update_symbol_summary),
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=None,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=asyncio.to_thread,
    )
    results = {f"sym-{index}": f"summary-{index}" for index in range(12)}
    content_hashes = {symbol_id: f"hash-{index}" for index, symbol_id in enumerate(results)}

    update_task = asyncio.create_task(_update_symbol_summaries(context, results, content_hashes))
    assert await asyncio.to_thread(all_slots_busy.wait, 1)
    release_updates.set()
    await update_task

    assert max_active <= 4
    assert all(waits_completed)


@pytest.mark.asyncio
async def test_summarize_unsummarized_marks_failures_and_logs_aggregate(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Failed summary generations are cooled off with one aggregate warning."""
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def ok():\n    return 1\n\ndef fail():\n    return 2\n", encoding="utf-8")
    symbols = [
        SimpleNamespace(
            id="sym-ok",
            file_path="src/app.py",
            line_start=1,
            line_end=2,
            content_hash="hash-ok",
        ),
        SimpleNamespace(
            id="sym-fail",
            file_path="src/app.py",
            line_start=4,
            line_end=5,
            content_hash="hash-fail",
        ),
        SimpleNamespace(
            id="sym-missing",
            file_path="src/missing.py",
            line_start=1,
            line_end=1,
            content_hash="hash-missing",
        ),
    ]
    attempts: list[list[tuple[str, str]]] = []
    updates: dict[str, tuple[str, str]] = {}

    class Storage:
        def get_unsummarized_symbols(self, _project_id: str, limit: int) -> list[Any]:
            return symbols[:limit]

        def mark_symbol_summaries_attempted(self, symbols: list[tuple[str, str]]) -> int:
            attempts.append(symbols)
            return len(symbols)

        def update_symbol_summary(self, symbol_id: str, content_hash: str, summary: str) -> bool:
            updates[symbol_id] = (content_hash, summary)
            return True

    class Summarizer:
        async def summarize_batch(
            self,
            batch: list[Any],
            read_source: Callable[[Any], str | None],
        ) -> dict[str, str]:
            assert {symbol.id for symbol in batch if read_source(symbol)} == {
                "sym-ok",
                "sym-fail",
            }
            return {"sym-ok": "Returns one."}

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=Storage(),
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=None,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=run_db,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.maintenance"):
        await _summarize_unsummarized(
            context,
            SimpleNamespace(id="proj-1", root_path=str(tmp_path)),
            Summarizer(),  # type: ignore[arg-type]
            batch_size=3,
        )

    assert attempts == [[("sym-fail", "hash-fail")]]
    assert updates == {"sym-ok": ("hash-ok", "Returns one.")}
    assert "Summary generation failed for 1/2 symbol(s) in project proj-1" in caplog.text


@pytest.mark.asyncio
async def test_summary_update_logs_per_symbol_failures(caplog: pytest.LogCaptureFixture) -> None:
    """One summary write failure does not cancel the rest of the batch."""
    updated: list[str] = []

    def update_symbol_summary(symbol_id: str, _content_hash: str, _summary: str) -> None:
        updated.append(symbol_id)
        if symbol_id == "sym-bad":
            raise RuntimeError("write failed")

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=SimpleNamespace(update_symbol_summary=update_symbol_summary),
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=None,
        config=SimpleNamespace(graph_enabled=True, embedding_enabled=True),
        run_db=run_db,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.maintenance"):
        await _update_symbol_summaries(
            context,
            {"sym-ok": "ok", "sym-bad": "bad", "sym-later": "later"},
            {"sym-ok": "hash-ok", "sym-bad": "hash-bad", "sym-later": "hash-later"},
        )

    assert set(updated) == {"sym-ok", "sym-bad", "sym-later"}
    assert "Failed to persist summary for symbol sym-bad: write failed" in caplog.text

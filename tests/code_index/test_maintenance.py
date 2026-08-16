"""Tests for code index maintenance."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.gcode_gateway import (
    GcodeCommandResult,
    GcodeDaemonConfigUnavailableError,
)
from gobby.code_index.maintenance import (
    _run_maintenance,
    _summarize_unsummarized,
    _update_symbol_summaries,
)
from gobby.code_index.models import IndexedProject
from gobby.code_index.sync_breaker import BreakerState, SyncCircuitBreaker
from gobby.runtime_grants.launch import ManagedLaunch

pytestmark = pytest.mark.unit

T = TypeVar("T")
DAEMON_CONFIG_STDERR = (
    "Error: daemon effective config unavailable "
    "(timeout; url=http://127.0.0.1:60887/api/config/effective)"
)


class _MaintenanceConfig(Protocol):
    graph_enabled: bool
    embedding_enabled: bool
    missing_root_purge_observations: int
    maintenance_index_timeout_seconds: int


def _gcode_result(
    command: tuple[str, ...] = ("/tmp/gcode", "index", "--project", "/repo", "--quiet"),
    *,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    timeout_seconds: float | None = 120,
) -> GcodeCommandResult:
    return GcodeCommandResult(
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        duration_seconds=1.0,
        timeout_seconds=timeout_seconds,
        timed_out=timed_out,
    )


class _MaintenanceContext(Protocol):
    storage: Any
    gcode_gateway: Any | None
    daemon_config_breaker: SyncCircuitBreaker
    config: _MaintenanceConfig

    async def run_db(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T: ...

    async def clear_graph(self, project_id: str) -> dict[str, Any]: ...


@contextmanager
def _dummy_launch(project_id: str, *, timeout_seconds: float) -> Iterator[ManagedLaunch]:
    del project_id, timeout_seconds
    yield ManagedLaunch(
        grant_path=Path("/tmp/grant.json"),
        env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/tmp/grant.json"},
    )


class DummyLaunchFactory:
    def open(
        self, project_id: str, *, timeout_seconds: float
    ) -> AbstractContextManager[ManagedLaunch]:
        return _dummy_launch(project_id, timeout_seconds=timeout_seconds)


def _write_project_marker(root: Path, project_id: str) -> None:
    marker = root / ".gobby"
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "project.json").write_text(json.dumps({"id": project_id, "name": "test"}))


class RecordingGcodeGateway:
    def __init__(
        self,
        *,
        vector_sync_result: dict[str, Any] | None = None,
        vector_clear_result: dict[str, Any] | None = None,
        maintenance_result: GcodeCommandResult | None = None,
        maintenance_exception: BaseException | None = None,
    ) -> None:
        self.vector_sync_result = vector_sync_result or {"success": True}
        self.vector_clear_result = vector_clear_result or {"success": True}
        self.maintenance_result = maintenance_result
        self.maintenance_exception = maintenance_exception
        self.vector_synced_files: list[tuple[Path, str]] = []
        self.vector_cleared_roots: list[Path] = []
        self.graph_cleared: list[str] = []
        self.maintenance_calls: list[tuple[Path, float | None]] = []

    async def maintenance_index(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> GcodeCommandResult:
        del env
        self.maintenance_calls.append((project_root, timeout))
        if self.maintenance_exception is not None:
            raise self.maintenance_exception
        return self.maintenance_result or _gcode_result(
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(project_root),
                "--skip-if-locked",
            ),
            timeout_seconds=timeout,
        )

    async def vector_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        self.vector_synced_files.append((project_root, file_path))
        return self.vector_sync_result

    async def vector_clear(
        self,
        project_root: Path | None = None,
        *,
        project_id: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del env
        if project_root is not None:
            self.vector_cleared_roots.append(project_root)
        if project_id is not None:
            self.graph_cleared.append(f"vector:{project_id}")
        return self.vector_clear_result

    async def graph_clear(
        self, project_id: str, *, env: dict[str, str] | None = None
    ) -> dict[str, Any]:
        del env
        self.graph_cleared.append(project_id)
        return {"success": True}


@pytest.mark.asyncio
async def test_maintenance_purges_indexed_project_after_missing_threshold(
    tmp_path: Path,
) -> None:
    """Missing indexed roots reconcile immediately without deleting the path."""
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
    storage.get_registry_project.return_value = (True, False)
    storage.delete_project_index.return_value = {
        "files": 2,
        "symbols": 3,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    gcode_gateway = RecordingGcodeGateway()

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context = SimpleNamespace(
        storage=storage,
        gcode_gateway=gcode_gateway,
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="maintenance",
        ),
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            missing_root_purge_observations=2,
            maintenance_index_timeout_seconds=900,
        ),
        run_db=run_db,
    )

    await _run_maintenance(cast(CodeIndexContext, context))

    storage.delete_project_index.assert_called_once_with("proj-missing")
    assert gcode_gateway.maintenance_calls == []
    assert "proj-missing" in gcode_gateway.graph_cleared
    assert not missing_root.exists()


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
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="maintenance",
        ),
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            maintenance_index_timeout_seconds=900,
        ),
        run_db=run_db,
    )

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
    gcode_gateway = RecordingGcodeGateway(
        maintenance_result=_gcode_result(
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(root),
                "--quiet",
            ),
            returncode=1,
            stderr="No gcode project found. Run `gcode init` to initialize this directory.",
        )
    )
    summarizer = SimpleNamespace(summarize_batch=AsyncMock())
    run_db_calls: list[str] = []

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        func_name = getattr(func, "__name__", None)
        if not isinstance(func_name, str):
            mock_name = getattr(func, "_mock_name", None)
            func_name = mock_name if isinstance(mock_name, str) else repr(func)
        run_db_calls.append(func_name)
        return func(*args, **kwargs)

    _write_project_marker(root, "proj-stale")
    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=clear_graph,
        gcode_gateway=gcode_gateway,
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="maintenance",
        ),
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            maintenance_index_timeout_seconds=30,
        ),
        run_db=run_db,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.maintenance"):
        await _run_maintenance(context, summarizer=summarizer)

    assert gcode_gateway.maintenance_calls == [(root, 30)]
    assert gcode_gateway.maintenance_result is not None
    assert "--sync-projections" not in gcode_gateway.maintenance_result.command
    storage.delete_project_index.assert_called_once_with("proj-stale")
    storage.get_unsummarized_symbols.assert_not_called()
    clear_graph.assert_not_awaited()
    assert gcode_gateway.vector_cleared_roots == []
    summarizer.summarize_batch.assert_not_called()
    assert "Maintenance reindex failed" not in caplog.text
    assert run_db_calls == [
        "list_projection_cleanup_pending",
        "list_indexed_projects",
        "get_registry_project",
        "delete_project_index",
    ]


@pytest.mark.asyncio
async def test_maintenance_logs_unexpected_reindex_failure_at_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = IndexedProject(
        id="proj-failed",
        root_path=str(root),
        total_files=2,
        total_symbols=3,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    diagnostic = 'ERROR: invalid byte sequence for encoding "UTF8": 0x00'
    gcode_gateway = RecordingGcodeGateway(
        maintenance_result=_gcode_result(
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(root),
                "--quiet",
            ),
            returncode=1,
            stderr=diagnostic,
        )
    )

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    _write_project_marker(root, "proj-failed")
    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=gcode_gateway,
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="maintenance",
        ),
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            maintenance_index_timeout_seconds=30,
        ),
        run_db=run_db,
    )

    with caplog.at_level(logging.ERROR, logger="gobby.code_index.maintenance"):
        await _run_maintenance(cast(CodeIndexContext, context))

    assert "Maintenance reindex failed for proj-failed (exit code 1)" in caplog.text
    assert diagnostic in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    storage.delete_project_index.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_lock_busy_is_expected_and_continues_summaries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = IndexedProject(
        id="proj-busy",
        root_path=str(root),
        total_files=1,
        total_symbols=1,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    storage.get_unsummarized_symbols.return_value = []
    gateway = RecordingGcodeGateway(
        maintenance_result=_gcode_result(
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(root),
                "--skip-if-locked",
            ),
            returncode=3,
            stderr="index lock busy",
        )
    )
    breaker = SyncCircuitBreaker(
        name="test",
        probe_target="daemon config",
        operation="maintenance",
        failure_threshold=1,
        base_backoff_seconds=0.0,
    )
    breaker.record_failure()
    caplog.clear()
    _write_project_marker(root, "proj-busy")

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=gateway,
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=breaker,
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            maintenance_index_timeout_seconds=900,
        ),
        run_db=run_db,
    )

    with caplog.at_level(logging.WARNING):
        await _run_maintenance(
            context,
            summarizer=SimpleNamespace(summarize_batch=AsyncMock()),
        )

    assert gateway.maintenance_calls == [(root, 900)]
    assert breaker.state is BreakerState.CLOSED
    storage.get_unsummarized_symbols.assert_called_once()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_maintenance_rls_failure_is_logged_at_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project_marker(root, "proj-isolated")
    project = IndexedProject(
        id="proj-isolated",
        root_path=str(root),
        total_files=1,
        total_symbols=1,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    storage.get_unsummarized_symbols.return_value = []
    diagnostic = (
        'ERROR: new row violates row-level security policy for table "code_indexed_projects"'
    )
    gateway = RecordingGcodeGateway(
        maintenance_result=_gcode_result(
            (
                "/tmp/gcode",
                "index",
                "--project",
                str(root),
                "--skip-if-locked",
            ),
            returncode=1,
            stderr=diagnostic,
        )
    )

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=gateway,
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="maintenance",
        ),
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            maintenance_index_timeout_seconds=30,
        ),
        run_db=run_db,
    )

    with caplog.at_level(logging.ERROR, logger="gobby.code_index.maintenance"):
        await _run_maintenance(cast(CodeIndexContext, context))

    assert "Maintenance reindex failed for proj-isolated (exit code 1)" in caplog.text
    assert diagnostic in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    storage.delete_project_index.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_daemon_config_failure_opens_shared_breaker_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project_marker(root, "proj-config-down")
    project = IndexedProject(
        id="proj-config-down",
        root_path=str(root),
        total_files=1,
        total_symbols=1,
    )
    storage = MagicMock()
    storage.list_projection_cleanup_pending.return_value = []
    storage.list_indexed_projects.return_value = [project]
    gateway = RecordingGcodeGateway(
        maintenance_exception=GcodeDaemonConfigUnavailableError(
            ("gcode", "index"),
            1,
            DAEMON_CONFIG_STDERR,
        )
    )
    breaker = SyncCircuitBreaker(
        name="test",
        probe_target="daemon config",
        operation="maintenance",
        failure_threshold=1,
    )

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context: _MaintenanceContext = SimpleNamespace(
        storage=storage,
        clear_graph=AsyncMock(return_value={"success": True}),
        gcode_gateway=gateway,
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=breaker,
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            maintenance_index_timeout_seconds=900,
        ),
        run_db=run_db,
    )

    with caplog.at_level(logging.WARNING):
        await _run_maintenance(context)
        await _run_maintenance(context)

    assert breaker.state is BreakerState.OPEN
    assert gateway.maintenance_calls == [(root, 900)]
    assert caplog.text.count("breaker open") == 1
    assert "Maintenance reindex failed" not in caplog.text
    assert DAEMON_CONFIG_STDERR not in caplog.text


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
        launch_factory=DummyLaunchFactory(),
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="maintenance",
        ),
        config=SimpleNamespace(
            graph_enabled=True,
            embedding_enabled=True,
            missing_root_purge_observations=1,
            maintenance_index_timeout_seconds=900,
        ),
        run_db=run_db,
    )

    await _run_maintenance(context)

    storage.delete_project_index.assert_called_once_with("proj-missing")


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

    update_task = asyncio.create_task(
        _update_symbol_summaries(
            cast(CodeIndexContext, context),
            results,
            content_hashes,
        )
    )
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
            cast(CodeIndexContext, context),
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
            cast(CodeIndexContext, context),
            {"sym-ok": "ok", "sym-bad": "bad", "sym-later": "later"},
            {"sym-ok": "hash-ok", "sym-bad": "hash-bad", "sym-later": "hash-later"},
        )

    assert set(updated) == {"sym-ok", "sym-bad", "sym-later"}
    assert "Failed to persist summary for symbol sym-bad: write failed" in caplog.text

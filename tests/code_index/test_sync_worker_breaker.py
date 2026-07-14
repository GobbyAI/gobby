"""Tests for the vector-sync circuit breaker (incident #18196 disk churn)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeEmbeddingTransportError,
    GcodeIndexedFileNotFoundError,
    _classify_gcode_command_error,
)
from gobby.code_index.models import IndexedFile, IndexedProject
from gobby.code_index.sync_breaker import BreakerState, VectorSyncBreaker
from gobby.code_index.sync_worker import _sync_pass
from gobby.config.code_index import CodeIndexConfig
from tests.code_index.conftest import PROJECT_ID

pytestmark = pytest.mark.unit

INCIDENT_STDERR = (
    "Error: embedding response was invalid: AI transport failed: "
    "error sending request for url (http://localhost:1234/v1/embeddings)"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_breaker(clock: FakeClock | None = None, **kwargs: Any) -> VectorSyncBreaker:
    return VectorSyncBreaker(monotonic=clock or FakeClock(), **kwargs)


class TestVectorSyncBreakerUnit:
    def test_opens_after_threshold_and_backoff_gates(self) -> None:
        clock = FakeClock()
        breaker = make_breaker(clock, failure_threshold=5, base_backoff_seconds=30.0)
        for _ in range(4):
            breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.pending_allowed()

        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert not breaker.pending_allowed()
        assert not breaker.should_attempt()

        clock.now = 30.0
        assert breaker.pending_allowed()
        assert breaker.should_attempt()  # transitions to half-open probe
        assert breaker.state is BreakerState.HALF_OPEN
        assert not breaker.should_attempt()  # only one probe
        assert not breaker.pending_allowed()

        breaker.record_success()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.should_attempt()

    def test_failed_probe_doubles_backoff_to_cap(self) -> None:
        clock = FakeClock()
        breaker = make_breaker(
            clock,
            failure_threshold=1,
            base_backoff_seconds=30.0,
            max_backoff_seconds=100.0,
        )
        breaker.record_failure()  # open, backoff 30
        assert breaker.state is BreakerState.OPEN

        clock.now = 30.0
        assert breaker.should_attempt()
        breaker.record_failure()  # probe failed -> backoff 60
        assert breaker.state is BreakerState.OPEN
        clock.now = 89.0
        assert not breaker.should_attempt()
        clock.now = 90.0
        assert breaker.should_attempt()
        breaker.record_failure()  # probe failed -> backoff min(120, 100) = 100
        clock.now = 189.0
        assert not breaker.should_attempt()
        clock.now = 190.0
        assert breaker.should_attempt()

        breaker.record_success()  # backoff resets to base
        breaker.record_failure()
        clock.now = 190.0 + 30.0
        assert breaker.should_attempt()

    def test_success_resets_consecutive_count(self) -> None:
        breaker = make_breaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED

    def test_logs_once_per_state_transition(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        breaker = make_breaker(clock, failure_threshold=2)
        with caplog.at_level(logging.INFO, logger="gobby.code_index.sync_breaker"):
            breaker.record_failure()
            breaker.record_failure()  # opens
            breaker.record_failure()  # already open: no extra log
            breaker.record_failure()
            clock.now = 30.0
            breaker.should_attempt()  # half-open log
            breaker.record_success()  # closed log
        opens = [r for r in caplog.records if "breaker open" in r.message]
        probes = [r for r in caplog.records if "half-open" in r.message]
        closes = [r for r in caplog.records if "breaker closed" in r.message]
        assert len(opens) == 1
        assert len(probes) == 1
        assert len(closes) == 1


class TestErrorClassification:
    def test_incident_stderr_classifies_as_embedding_transport(self) -> None:
        error = _classify_gcode_command_error(("gcode", "vector", "sync-file"), 1, INCIDENT_STDERR)
        assert isinstance(error, GcodeEmbeddingTransportError)

    def test_generic_stderr_stays_generic(self) -> None:
        error = _classify_gcode_command_error(
            ("gcode", "vector", "sync-file"), 1, "Error: something unrelated broke"
        )
        assert type(error) is GcodeCommandError


def _indexed_project(root: Path) -> IndexedProject:
    return IndexedProject(id=PROJECT_ID, root_path=str(root), total_files=1, total_symbols=1)


def _indexed_file(path: str) -> IndexedFile:
    return IndexedFile(
        id=IndexedFile.make_id(PROJECT_ID, path),
        project_id=PROJECT_ID,
        file_path=path,
        language="python",
        content_hash="abc123",
        symbol_count=1,
        vectors_synced=False,
        graph_synced=True,
    )


class TransportFailGateway:
    def __init__(self, *, fail: bool = True) -> None:
        self.fail = fail
        self.vector_calls: list[str] = []

    async def vector_sync_file(
        self, project_root: Path, file_path: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.vector_calls.append(file_path)
        if self.fail:
            raise GcodeEmbeddingTransportError(
                ("gcode", "vector", "sync-file", file_path), 1, INCIDENT_STDERR
            )
        return {"success": True}


def _write_files(root: Path, paths: list[str]) -> None:
    for path in paths:
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("x = 1\n")


def _make_storage(root: Path, files: list[IndexedFile]) -> MagicMock:
    storage = MagicMock()
    storage.list_indexed_projects.return_value = [_indexed_project(root)]
    storage.get_pending_sync_files.return_value = files
    by_path = {f.file_path: f for f in files}
    storage.get_file.side_effect = lambda project_id, path: by_path.get(path)
    return storage


def _config() -> CodeIndexConfig:
    return CodeIndexConfig(embedding_enabled=True, graph_enabled=False)


@pytest.mark.asyncio
async def test_open_breaker_fetches_graph_only_batches(tmp_path: Path) -> None:
    paths = [f"src/f{i}.py" for i in range(5)]
    _write_files(tmp_path, paths)
    files = [_indexed_file(p) for p in paths]
    storage = _make_storage(tmp_path, files)
    gateway = TransportFailGateway()
    clock = FakeClock()
    breaker = make_breaker(clock, failure_threshold=5)

    await _sync_pass(
        storage=storage,
        gcode_gateway=gateway,  # type: ignore[arg-type]
        config=_config(),
        batch_size=50,
        breaker=breaker,
    )
    assert breaker.state is BreakerState.OPEN
    assert len(gateway.vector_calls) == 5  # threshold reached during the pass

    storage.get_pending_sync_files.reset_mock()
    gateway.vector_calls.clear()
    await _sync_pass(
        storage=storage,
        gcode_gateway=gateway,  # type: ignore[arg-type]
        config=_config(),
        batch_size=50,
        breaker=breaker,
    )
    # While open: vector-pending files are not even fetched, zero vector churn.
    assert storage.get_pending_sync_files.call_args.kwargs["vectors"] is False
    assert gateway.vector_calls == []


@pytest.mark.asyncio
async def test_half_open_probes_single_file_then_reopens(tmp_path: Path) -> None:
    paths = [f"src/f{i}.py" for i in range(3)]
    _write_files(tmp_path, paths)
    files = [_indexed_file(p) for p in paths]
    storage = _make_storage(tmp_path, files)
    gateway = TransportFailGateway()
    clock = FakeClock()
    breaker = make_breaker(clock, failure_threshold=1, base_backoff_seconds=30.0)

    breaker.record_failure()  # open
    clock.now = 30.0  # backoff elapsed -> probe allowed
    await _sync_pass(
        storage=storage,
        gcode_gateway=gateway,  # type: ignore[arg-type]
        config=_config(),
        batch_size=50,
        breaker=breaker,
    )
    assert gateway.vector_calls == ["src/f0.py"]  # exactly one probe
    assert breaker.state is BreakerState.OPEN  # failed probe reopened


@pytest.mark.asyncio
async def test_successful_probe_closes_and_pass_resumes(tmp_path: Path) -> None:
    paths = [f"src/f{i}.py" for i in range(3)]
    _write_files(tmp_path, paths)
    files = [_indexed_file(p) for p in paths]
    storage = _make_storage(tmp_path, files)
    gateway = TransportFailGateway(fail=False)
    clock = FakeClock()
    breaker = make_breaker(clock, failure_threshold=1, base_backoff_seconds=30.0)

    breaker.record_failure()
    clock.now = 30.0
    await _sync_pass(
        storage=storage,
        gcode_gateway=gateway,  # type: ignore[arg-type]
        config=_config(),
        batch_size=50,
        breaker=breaker,
    )
    assert breaker.state is BreakerState.CLOSED
    assert gateway.vector_calls == paths  # probe succeeded, rest of pass proceeded


@pytest.mark.asyncio
async def test_per_file_errors_do_not_trip_breaker(tmp_path: Path) -> None:
    paths = ["src/f0.py", "src/f1.py"]
    _write_files(tmp_path, paths)
    files = [_indexed_file(p) for p in paths]
    storage = _make_storage(tmp_path, files)
    breaker = make_breaker(failure_threshold=1)

    class PerFileErrorGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def vector_sync_file(
            self, project_root: Path, file_path: str, *, timeout: float | None = None
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise GcodeIndexedFileNotFoundError(
                    ("gcode",), 1, "not found", file_path, PROJECT_ID
                )
            raise GcodeCommandError(("gcode",), 1, "Error: unrelated per-file bug")

    gateway = PerFileErrorGateway()
    storage.resolve_index_state.return_value = MagicMock(indexed=True)
    await _sync_pass(
        storage=storage,
        gcode_gateway=gateway,  # type: ignore[arg-type]
        config=_config(),
        batch_size=50,
        breaker=breaker,
    )
    assert breaker.state is BreakerState.CLOSED
    assert gateway.calls == 2  # neither error paused vector work

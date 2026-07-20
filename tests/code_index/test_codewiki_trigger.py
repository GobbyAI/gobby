"""Tests for debounced post-commit codewiki refresh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.code_index.codewiki_trigger import (
    CodewikiRefreshRequest,
    CodewikiRefreshTrigger,
    codewiki_on_commit_enabled,
)
from gobby.code_index.gcode_gateway import GcodeGatewayError
from gobby.servers.routes.code_index import create_code_index_router

pytestmark = pytest.mark.unit


class FakeConfigStore:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def get(self, key: str) -> object | None:
        assert key == "wiki.codewiki_on_commit"
        return self._value


class ImmediateLoop:
    def call_soon_threadsafe(self, callback: Callable[..., object], *args: object) -> None:
        callback(*args)

    def call_later(
        self,
        _delay: float,
        _callback: Callable[..., object],
        *_args: object,
    ) -> FakeTimerHandle:
        return FakeTimerHandle()


class FakeTimerHandle:
    def cancel(self) -> None:
        pass


class FakeGcodeGateway:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[Path, Path, str | None, list[str] | None, bool]] = []

    async def codewiki(
        self,
        project_root: Path,
        out_dir: Path,
        *,
        ai: str | None = None,
        scopes: list[str] | None = None,
        complete_scope: bool = False,
    ) -> dict:
        self.calls.append((project_root, out_dir, ai, scopes, complete_scope))
        return self.result


class CancelledGcodeGateway:
    async def codewiki(
        self,
        _project_root: Path,
        _out_dir: Path,
        *,
        ai: str | None = None,
        scopes: list[str] | None = None,
        complete_scope: bool = False,
    ) -> dict:
        _ = ai, scopes, complete_scope
        raise asyncio.CancelledError


class FailingGcodeGateway:
    async def codewiki(
        self,
        _project_root: Path,
        _out_dir: Path,
        *,
        ai: str | None = None,
        scopes: list[str] | None = None,
        complete_scope: bool = False,
    ) -> dict:
        _ = ai, scopes, complete_scope
        raise RuntimeError("unexpected refresh failure")


class FakeGwikiGateway:
    def __init__(self) -> None:
        self.ingested: list[Path] = []
        self.index_count = 0

    async def ingest_file(self, path: str | Path) -> dict:
        self.ingested.append(Path(path))
        return {"status": "ok"}

    async def index(self) -> dict:
        self.index_count += 1
        return {"status": "ok"}


def test_codewiki_on_commit_enabled_reads_only_config_store() -> None:
    assert codewiki_on_commit_enabled(FakeConfigStore(True))
    assert not codewiki_on_commit_enabled(FakeConfigStore(False))


def test_codewiki_on_commit_enabled_defaults_off_when_unset() -> None:
    # No store, or a store that has no value, defaults to off (no snapshot fallback).
    assert not codewiki_on_commit_enabled(None)
    assert not codewiki_on_commit_enabled(FakeConfigStore(None))


def test_codewiki_on_commit_enabled_propagates_runtime_config_errors() -> None:
    class FailingConfigStore:
        def get(self, key: str) -> object | None:
            assert key == "wiki.codewiki_on_commit"
            raise RuntimeError("config store unavailable")

    with pytest.raises(RuntimeError, match="config store unavailable"):
        codewiki_on_commit_enabled(FailingConfigStore())


@pytest.mark.asyncio
async def test_refresh_runs_codewiki_and_indexes_changed_vault_docs(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md", "files/src/lib.rs.md"]})
    gwiki = FakeGwikiGateway()
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: gwiki,
        debounce_seconds=60,
    )

    trigger._schedule_request(
        CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1", ai="daemon")
    )
    await trigger._flush(trigger._root_key(str(tmp_path)))

    assert gcode.calls == [(tmp_path, tmp_path / "wiki", "daemon", None, False)]
    assert gwiki.ingested == []
    assert gwiki.index_count == 1


@pytest.mark.asyncio
async def test_request_refresh_passes_scopes_to_refresh_request(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": []})
    trigger = CodewikiRefreshTrigger(
        loop=cast(asyncio.AbstractEventLoop, ImmediateLoop()),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
        debounce_seconds=60,
    )

    accepted = trigger.request_refresh(
        root_path=str(tmp_path),
        project_id="proj-1",
        ai="daemon",
        scopes=["crates", "web", "src"],
    )
    await trigger._flush(trigger._root_key(str(tmp_path)))

    assert accepted is True
    assert gcode.calls == [(tmp_path, tmp_path / "wiki", "daemon", ["crates", "web", "src"], False)]


@pytest.mark.asyncio
async def test_refresh_with_external_out_dir_ingests_changed_docs(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md", "files/src/lib.rs.md"]})
    gwiki = FakeGwikiGateway()
    out_dir = tmp_path / "external-codewiki"
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: gwiki,
        debounce_seconds=60,
    )

    trigger._schedule_request(
        CodewikiRefreshRequest(
            root_path=str(tmp_path),
            project_id="proj-1",
            out_dir=str(out_dir),
            ai="daemon",
        )
    )
    await trigger._flush(trigger._root_key(str(tmp_path)))

    assert gcode.calls == [(tmp_path, out_dir, "daemon", None, False)]
    assert gwiki.ingested == [
        out_dir / "repo.md",
        out_dir / "files/src/lib.rs.md",
    ]
    assert gwiki.index_count == 1


@pytest.mark.asyncio
async def test_refresh_propagates_cancellation(tmp_path: Path) -> None:
    gwiki = FakeGwikiGateway()
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=CancelledGcodeGateway,
        gwiki_gateway_factory=lambda _root: gwiki,
        debounce_seconds=60,
    )

    with pytest.raises(asyncio.CancelledError):
        await trigger._run_refresh(CodewikiRefreshRequest(root_path=str(tmp_path)))

    assert gwiki.ingested == []
    assert gwiki.index_count == 0


@pytest.mark.asyncio
async def test_refresh_logs_gateway_factory_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_factory() -> FakeGcodeGateway:
        raise RuntimeError("constructor failed")

    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=fail_factory,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
        debounce_seconds=60,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.codewiki_trigger"):
        await trigger._run_refresh(
            CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1")
        )

    assert any(
        "codewiki refresh gateway construction failed" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_refresh_is_not_scheduled_when_disabled(tmp_path: Path) -> None:
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(False),
        debounce_seconds=0,
    )

    accepted = trigger.request_refresh(root_path=str(tmp_path))

    assert not accepted
    assert trigger._pending_by_root == {}


@pytest.mark.asyncio
async def test_flush_task_logs_unexpected_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=FailingGcodeGateway,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
        debounce_seconds=0,
    )
    root_key = trigger._root_key(str(tmp_path))
    trigger._pending_by_root[root_key] = CodewikiRefreshRequest(root_path=str(tmp_path))

    with caplog.at_level(logging.ERROR, logger="gobby.code_index.codewiki_trigger"):
        trigger._start_flush(root_key)
        tasks = list(trigger._flush_tasks)
        assert len(tasks) == 1
        await asyncio.gather(*tasks, return_exceptions=True)

    assert trigger._flush_tasks == set()
    assert any("codewiki refresh flush task failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_flush_does_not_stack_while_root_is_running(tmp_path: Path) -> None:
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        debounce_seconds=0,
    )
    root_key = trigger._root_key(str(tmp_path))
    request = CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1")
    trigger._schedule_request(request)
    trigger._running_roots.add(root_key)

    await trigger._flush(root_key)

    assert trigger._pending_by_root[root_key] == request


@pytest.mark.asyncio
async def test_status_reports_pending_and_running_state(tmp_path: Path) -> None:
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        debounce_seconds=60,
    )
    root_key = trigger._root_key(str(tmp_path))
    trigger._schedule_request(CodewikiRefreshRequest(root_path=str(tmp_path)))
    trigger._running_roots.add(root_key)

    snapshot = trigger.status()

    assert snapshot["pending_roots"] == [root_key]
    assert snapshot["running_roots"] == [root_key]
    assert snapshot["active_flush_tasks"] == 0
    assert snapshot["last_run"] is None


@pytest.mark.asyncio
async def test_status_records_last_run_success(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md"]})
    gwiki = FakeGwikiGateway()
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: gwiki,
        debounce_seconds=60,
    )
    trigger._schedule_request(
        CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1", ai="daemon")
    )
    await trigger._flush(trigger._root_key(str(tmp_path)))

    snapshot = trigger.status()

    assert snapshot["pending_roots"] == []
    last_run = snapshot["last_run"]
    assert last_run is not None
    assert last_run["outcome"] == "success"
    assert last_run["root_path"] == str(tmp_path)
    assert last_run["project_id"] == "proj-1"
    assert last_run["changed_count"] == 1
    assert last_run["indexed"] is True
    assert last_run["error"] is None
    assert last_run["started_at"] <= last_run["finished_at"]


@pytest.mark.asyncio
async def test_status_records_last_run_error(tmp_path: Path) -> None:
    class ErroringGcodeGateway:
        async def codewiki(
            self,
            _project_root: Path,
            _out_dir: Path,
            *,
            ai: str | None = None,
            scopes: list[str] | None = None,
            complete_scope: bool = False,
        ) -> dict:
            _ = ai, scopes, complete_scope
            raise GcodeGatewayError("gcode exploded")

    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=ErroringGcodeGateway,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
        debounce_seconds=60,
    )
    trigger._schedule_request(CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1"))
    await trigger._flush(trigger._root_key(str(tmp_path)))

    last_run = trigger.status()["last_run"]

    assert last_run is not None
    assert last_run["outcome"] == "error"
    assert last_run["error"] is not None
    assert "gcode exploded" in last_run["error"]
    assert last_run["root_path"] == str(tmp_path)
    assert last_run["started_at"] <= last_run["finished_at"]


@pytest.mark.asyncio
async def test_status_endpoint_snapshot(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md"]})
    trigger = CodewikiRefreshTrigger(
        loop=asyncio.get_running_loop(),
        config_store_provider=lambda: FakeConfigStore(True),
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
        debounce_seconds=60,
    )
    trigger._schedule_request(CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1"))
    await trigger._flush(trigger._root_key(str(tmp_path)))

    server = MagicMock()
    server.services.codewiki_trigger = trigger
    app = FastAPI()
    app.include_router(create_code_index_router(server))

    response = TestClient(app).get("/api/code-index/codewiki/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pending_roots"] == []
    assert payload["running_roots"] == []
    assert payload["active_flush_tasks"] == 0
    assert payload["last_run"]["outcome"] == "success"
    assert payload["last_run"]["changed_count"] == 1

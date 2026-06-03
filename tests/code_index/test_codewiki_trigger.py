"""Tests for debounced post-commit codewiki refresh."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.code_index.codewiki_trigger import (
    CodewikiRefreshRequest,
    CodewikiRefreshTrigger,
    codewiki_on_commit_enabled,
)

pytestmark = pytest.mark.unit


class FakeConfigStore:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def get(self, key: str) -> object | None:
        assert key == "wiki.codewiki_on_commit"
        return self._value


class FakeGcodeGateway:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[Path, Path, str | None]] = []

    async def codewiki(self, project_root: Path, out_dir: Path, *, ai: str | None = None) -> dict:
        self.calls.append((project_root, out_dir, ai))
        return self.result


class CancelledGcodeGateway:
    async def codewiki(
        self,
        _project_root: Path,
        _out_dir: Path,
        *,
        ai: str | None = None,
    ) -> dict:
        _ = ai
        raise asyncio.CancelledError


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


@pytest.mark.asyncio
async def test_refresh_runs_codewiki_and_ingests_changed_docs(tmp_path: Path) -> None:
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

    assert gcode.calls == [(tmp_path, tmp_path / "codewiki", "daemon")]
    assert gwiki.ingested == [
        tmp_path / "codewiki" / "repo.md",
        tmp_path / "codewiki" / "files/src/lib.rs.md",
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

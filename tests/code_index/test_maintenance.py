"""Tests for code index maintenance."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.maintenance import _run_maintenance, _update_symbol_summaries
from gobby.code_index.models import IndexedProject

pytestmark = pytest.mark.unit

T = TypeVar("T")


@pytest.mark.asyncio
async def test_maintenance_purges_indexed_project_when_root_is_missing(tmp_path: Path) -> None:
    """Missing indexed roots are purged instead of being sent to gcode."""
    missing_root = tmp_path / "missing"
    project = IndexedProject(
        id="proj-missing",
        root_path=str(missing_root),
        total_files=2,
        total_symbols=3,
    )
    storage = MagicMock()
    storage.list_indexed_projects.return_value = [project]
    storage.delete_project_index.return_value = {
        "files": 2,
        "symbols": 3,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    graph = SimpleNamespace(clear_project=AsyncMock())
    vector_store = SimpleNamespace(delete_collection=AsyncMock())
    run_db_calls: list[str] = []

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        func_name = getattr(func, "__name__", None)
        if not isinstance(func_name, str):
            mock_name = getattr(func, "_mock_name", None)
            func_name = mock_name if isinstance(mock_name, str) else repr(func)
        run_db_calls.append(func_name)
        return func(*args, **kwargs)

    context = SimpleNamespace(
        storage=storage,
        graph=graph,
        vector_store=vector_store,
        config=SimpleNamespace(qdrant_collection_prefix="code_symbols_"),
        run_db=run_db,
    )

    with (
        patch("gobby.code_index.maintenance.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec") as create_proc,
    ):
        await _run_maintenance(cast(CodeIndexContext, context))

    storage.delete_project_index.assert_called_once_with("proj-missing")
    graph.clear_project.assert_awaited_once_with("proj-missing")
    vector_store.delete_collection.assert_awaited_once_with("code_symbols_proj-missing")
    create_proc.assert_not_called()
    assert run_db_calls == ["list_indexed_projects", "delete_project_index"]


@pytest.mark.asyncio
async def test_summary_updates_are_concurrency_limited() -> None:
    """Summary DB writes stay bounded even when a batch contains many updates."""
    lock = threading.Lock()
    active = 0
    max_active = 0

    def update_symbol_summary(_symbol_id: str, _summary: str) -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.01)
        finally:
            with lock:
                active -= 1

    context = SimpleNamespace(
        storage=SimpleNamespace(update_symbol_summary=update_symbol_summary),
        run_db=asyncio.to_thread,
    )
    results = {f"sym-{index}": f"summary-{index}" for index in range(12)}

    await _update_symbol_summaries(cast(CodeIndexContext, context), results)

    assert max_active <= 4


@pytest.mark.asyncio
async def test_summary_update_logs_per_symbol_failures(caplog: pytest.LogCaptureFixture) -> None:
    """One summary write failure does not cancel the rest of the batch."""
    updated: list[str] = []

    def update_symbol_summary(symbol_id: str, _summary: str) -> None:
        updated.append(symbol_id)
        if symbol_id == "sym-bad":
            raise RuntimeError("write failed")

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return func(*args, **kwargs)

    context = SimpleNamespace(
        storage=SimpleNamespace(update_symbol_summary=update_symbol_summary),
        run_db=run_db,
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.maintenance"):
        await _update_symbol_summaries(
            cast(CodeIndexContext, context),
            {"sym-ok": "ok", "sym-bad": "bad", "sym-later": "later"},
        )

    assert set(updated) == {"sym-ok", "sym-bad", "sym-later"}
    assert "Failed to persist summary for symbol sym-bad: write failed" in caplog.text

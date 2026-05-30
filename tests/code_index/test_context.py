"""Tests for code-index daemon context graph delegation."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.context import (
    CodeIndexContext,
    CodeIndexGraphUnavailable,
    CodeIndexProjectNotFound,
)
from gobby.code_index.gcode_gateway import GcodeGatewayError
from gobby.code_index.models import IndexedProject
from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


def _project(root: Path) -> IndexedProject:
    return IndexedProject(id="proj-1", root_path=str(root), total_files=1, total_symbols=1)


@pytest.mark.asyncio
async def test_context_graph_overview_resolves_project_root_and_delegates(
    tmp_path: Path,
) -> None:
    storage = MagicMock()
    storage.get_project_stats.return_value = _project(tmp_path)
    gateway = MagicMock()
    gateway.graph_overview = AsyncMock(return_value={"nodes": []})
    context = CodeIndexContext(storage=storage, gcode_gateway=gateway)

    result = await context.graph_overview("proj-1", limit=5)

    assert result == {"nodes": []}
    storage.get_project_stats.assert_called_once_with("proj-1")
    gateway.graph_overview.assert_awaited_once_with(tmp_path, limit=5)


@pytest.mark.asyncio
async def test_context_clear_graph_uses_project_id_without_project_root() -> None:
    storage = MagicMock()
    gateway = MagicMock()
    gateway.graph_clear = AsyncMock(return_value={"success": True})
    context = CodeIndexContext(storage=storage, gcode_gateway=gateway)

    result = await context.clear_graph("proj-1")

    assert result == {"success": True}
    storage.get_project_stats.assert_not_called()
    gateway.graph_clear.assert_awaited_once_with("proj-1")


@pytest.mark.asyncio
async def test_context_rebuild_graph_uses_project_root_and_ignores_legacy_limit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = MagicMock()
    storage.get_project_stats.return_value = _project(tmp_path)
    gateway = MagicMock()
    gateway.graph_rebuild = AsyncMock(return_value={"success": True})
    context = CodeIndexContext(storage=storage, gcode_gateway=gateway)

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.context"):
        result = await context.rebuild_graph("proj-1", limit=1)

    assert result == {"success": True}
    gateway.graph_rebuild.assert_awaited_once_with(tmp_path)
    assert "deprecated and ignored" in caplog.text


@pytest.mark.asyncio
async def test_context_raises_when_graph_disabled(tmp_path: Path) -> None:
    storage = MagicMock()
    storage.get_project_stats.return_value = _project(tmp_path)
    context = CodeIndexContext(
        storage=storage,
        gcode_gateway=MagicMock(),
        config=CodeIndexConfig(graph_enabled=False),
    )

    with pytest.raises(CodeIndexGraphUnavailable):
        await context.graph_overview("proj-1")


@pytest.mark.asyncio
async def test_context_raises_when_project_root_missing() -> None:
    storage = MagicMock()
    storage.get_project_stats.return_value = None
    context = CodeIndexContext(storage=storage, gcode_gateway=MagicMock())

    with pytest.raises(CodeIndexProjectNotFound, match="proj-1"):
        await context.graph_file("proj-1", "src/app.py")


def test_context_does_not_create_gateway_when_graph_disabled() -> None:
    storage = MagicMock()
    context = CodeIndexContext(storage=storage, config=CodeIndexConfig(graph_enabled=False))

    assert context.gcode_gateway is None


def test_context_continues_when_gateway_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    storage = MagicMock()

    with (
        pytest.MonkeyPatch.context() as monkeypatch,
        caplog.at_level(logging.WARNING, logger="gobby.code_index.context"),
    ):
        monkeypatch.setattr(
            "gobby.code_index.context.GcodeGateway",
            MagicMock(side_effect=GcodeGatewayError("missing binary")),
        )
        context = CodeIndexContext(storage=storage, config=CodeIndexConfig(graph_enabled=True))

    assert context.gcode_gateway is None
    assert "Code graph gateway unavailable" in caplog.text


def test_context_propagates_unexpected_gateway_init_errors() -> None:
    storage = MagicMock()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "gobby.code_index.context.GcodeGateway",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError, match="boom"):
            CodeIndexContext(storage=storage, config=CodeIndexConfig(graph_enabled=True))

"""Tests for code-index daemon context graph delegation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.code_index.context import (
    CodeIndexContext,
    CodeIndexGraphUnavailable,
    CodeIndexProjectNotFound,
)
from gobby.code_index.models import IndexedProject
from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


def _project(root: Path) -> IndexedProject:
    return IndexedProject(id="proj-1", root_path=str(root), total_files=1, total_symbols=1)


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


async def test_context_clear_graph_uses_project_id_without_project_root() -> None:
    storage = MagicMock()
    gateway = MagicMock()
    gateway.graph_clear = AsyncMock(return_value={"success": True})
    context = CodeIndexContext(storage=storage, gcode_gateway=gateway)

    result = await context.clear_graph("proj-1")

    assert result == {"success": True}
    storage.get_project_stats.assert_not_called()
    gateway.graph_clear.assert_awaited_once_with("proj-1")


async def test_context_rebuild_graph_uses_project_root_and_ignores_legacy_limit(
    tmp_path: Path,
) -> None:
    storage = MagicMock()
    storage.get_project_stats.return_value = _project(tmp_path)
    gateway = MagicMock()
    gateway.graph_rebuild = AsyncMock(return_value={"success": True})
    context = CodeIndexContext(storage=storage, gcode_gateway=gateway)

    result = await context.rebuild_graph("proj-1", limit=1)

    assert result == {"success": True}
    gateway.graph_rebuild.assert_awaited_once_with(tmp_path)


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


async def test_context_raises_when_project_root_missing() -> None:
    storage = MagicMock()
    storage.get_project_stats.return_value = None
    context = CodeIndexContext(storage=storage, gcode_gateway=MagicMock())

    with pytest.raises(CodeIndexProjectNotFound, match="proj-1"):
        await context.graph_file("proj-1", "src/app.py")

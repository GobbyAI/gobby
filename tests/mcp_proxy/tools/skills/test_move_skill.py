"""Tests for skill move MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.mcp_proxy.tools.skills.move_skill import register

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_move_skill_tools_route_storage_calls_through_run_sqlite() -> None:
    """Move operations use the injected SQLite runner instead of the default executor."""
    storage = MagicMock()
    storage.move_to_project.return_value = SimpleNamespace(
        id="skl-1",
        name="demo",
        source="project",
        project_id="proj-1",
    )
    storage.move_to_installed.return_value = SimpleNamespace(
        id="skl-1",
        name="demo",
        source="installed",
    )
    run_db_calls: list[Callable[..., Any]] = []

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        run_db_calls.append(func)
        return func(*args, **kwargs)

    ctx = SkillsContext(
        db=MagicMock(),
        storage=storage,
        notifier=MagicMock(),
        session_manager=MagicMock(),
        search=MagicMock(),
        updater=MagicMock(),
        loader=MagicMock(),
        project_id="proj-1",
        hub_manager=None,
        run_db=run_db,
    )
    registry = InternalToolRegistry(name="gobby-skills")
    register(ctx, registry)

    move_to_project = registry.get_tool("move_skill_to_project")
    move_to_installed = registry.get_tool("move_skill_to_installed")
    assert move_to_project is not None
    assert move_to_installed is not None

    project_result = await move_to_project("skl-1", "proj-1")
    installed_result = await move_to_installed("skl-1")

    assert project_result["success"] is True
    assert project_result["project_id"] == "proj-1"
    assert installed_result["success"] is True
    assert run_db_calls == [storage.move_to_project, storage.move_to_installed]

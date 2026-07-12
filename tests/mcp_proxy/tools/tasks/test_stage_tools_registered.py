"""Task stage tools are discoverable only on their intended MCP server."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

pytestmark = pytest.mark.unit

READ_STAGE_TOOLS = {"get_task_stages", "list_stages_registry", "get_task_type_defaults"}
MUTATING_STAGE_TOOLS = {
    "update_stage",
    "restore_stage",
    "delete_stage",
    "set_task_type_defaults",
}


def _tool_names(registry: Any) -> set[str]:
    return {tool["name"] for tool in registry.list_tools()}


def test_mutating_tools_absent_from_gobby_tasks(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_registry(mock_task_manager, mock_sync_manager))

    assert names.isdisjoint(MUTATING_STAGE_TOOLS)


def test_mutating_tools_visible_on_gobby_tasks_ops(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_ops_registry(mock_task_manager, mock_sync_manager))

    assert MUTATING_STAGE_TOOLS <= names


def test_read_tools_absent_from_gobby_tasks_ops(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_ops_registry(mock_task_manager, mock_sync_manager))

    assert names.isdisjoint(READ_STAGE_TOOLS)


def test_read_tools_visible_on_gobby_tasks(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_registry(mock_task_manager, mock_sync_manager))

    assert READ_STAGE_TOOLS <= names

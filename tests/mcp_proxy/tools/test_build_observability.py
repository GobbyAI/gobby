from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _registry(temp_db: Any) -> Any:
    return create_task_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )


def test_build_observability_tools_are_registered_on_gobby_tasks(temp_db: Any) -> None:
    registry = _registry(temp_db)
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert {"get_build_status", "explain_dispatch", "list_build_history"}.issubset(tool_names)
    schema = registry.get_schema("get_build_status")
    assert schema is not None
    assert schema["inputSchema"]["required"] == ["input_ref"]
    assert schema["inputSchema"]["properties"]["history_limit"]["default"] == 5


def test_build_observability_tools_are_not_registered_on_gobby_tasks_ops(temp_db: Any) -> None:
    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert {"get_build_status", "explain_dispatch", "list_build_history"}.isdisjoint(tool_names)
    assert "build_task" in tool_names


def test_get_build_status_tool_calls_service(temp_db: Any) -> None:
    registry = _registry(temp_db)
    tool = registry.get_tool("get_build_status")

    with patch(
        "gobby.mcp_proxy.tools.tasks._build_observability.get_build_status",
        return_value={"ok": True},
    ) as service:
        result = tool(input_ref="#1", history_limit=2, project_id="project-1")

    assert result == {"ok": True}
    assert service.call_args.kwargs["project_id"] == "project-1"
    assert service.call_args.kwargs["history_limit"] == 2


def test_build_observability_missing_project_id_names_tool(temp_db: Any) -> None:
    registry = _registry(temp_db)
    tool = registry.get_tool("get_build_status")

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.get_project_context", return_value=None),
        pytest.raises(ValueError, match="get_build_status"),
    ):
        tool(input_ref="#1")


def test_explain_dispatch_tool_schema_and_call(temp_db: Any) -> None:
    registry = _registry(temp_db)
    tool = registry.get_tool("explain_dispatch")
    schema = registry.get_schema("explain_dispatch")

    assert schema is not None
    assert schema["inputSchema"]["required"] == ["task_id"]
    with patch(
        "gobby.mcp_proxy.tools.tasks._build_observability.explain_dispatch",
        return_value={"ok": True, "eligible": False},
    ) as service:
        result = tool(task_id="#1", max_active_agents=3, project_id="project-1")

    assert result["eligible"] is False
    assert service.call_args.kwargs["max_active_agents"] == 3


def test_list_build_history_tool_calls_service(temp_db: Any) -> None:
    registry = _registry(temp_db)
    tool = registry.get_tool("list_build_history")

    with patch(
        "gobby.mcp_proxy.tools.tasks._build_observability.list_build_history",
        return_value={"ok": True, "runs": [], "events": []},
    ) as service:
        result = tool(input_ref="#1", limit=7, project_id="project-1")

    assert result["ok"] is True
    assert service.call_args.kwargs["limit"] == 7

"""Review transition MCP tools live on gobby-tasks-ops."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit

REVIEW_TOOLS = ("submit_for_review", "approve_review", "reject_review")


@pytest.fixture
def registries():
    task_manager = MagicMock(spec=LocalTaskManager)
    task_manager.db = MagicMock()
    sync_manager = MagicMock(spec=TaskSyncManager)
    return (
        create_task_registry(task_manager, sync_manager),
        create_task_ops_registry(task_manager, sync_manager),
    )


def test_review_tools_removed_from_gobby_tasks(registries) -> None:
    task_registry, _ = registries

    for tool_name in REVIEW_TOOLS:
        assert task_registry.get_schema(tool_name) is None


def test_review_tools_registered_on_gobby_tasks_ops(registries) -> None:
    _, ops_registry = registries

    for tool_name in REVIEW_TOOLS:
        assert ops_registry.get_schema(tool_name) is not None


def test_review_tool_schemas_require_stage_name(registries) -> None:
    _, ops_registry = registries

    for tool_name in REVIEW_TOOLS:
        schema = ops_registry.get_schema(tool_name)
        assert schema is not None
        assert set(schema["inputSchema"]["required"]) == {"task_id", "stage_name"}

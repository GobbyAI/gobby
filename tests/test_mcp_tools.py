import pytest
import asyncio
import unittest
from unittest.mock import MagicMock, ANY

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager


@pytest.fixture
def mock_task_manager():
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager():
    return MagicMock(spec=TaskSyncManager)


@pytest.fixture
def task_registry(mock_task_manager, mock_sync_manager):
    return create_task_registry(mock_task_manager, mock_sync_manager)


def test_create_task_registry_returns_registry(task_registry):
    """Test that create_task_registry returns an InternalToolRegistry."""
    assert isinstance(task_registry, InternalToolRegistry)
    assert task_registry.name == "internal-tasks"


def test_create_task_registry_has_all_tools(task_registry):
    """Test that all expected tools are registered."""
    expected_tools = [
        "create_task",
        "get_task",
        "update_task",
        "close_task",
        "delete_task",
        "list_tasks",
        "add_dependency",
        "remove_dependency",
        "get_dependency_tree",
        "check_dependency_cycles",
        "list_ready_tasks",
        "list_blocked_tasks",
        "link_task_to_session",
        "get_session_tasks",
        "get_task_sessions",
        "sync_tasks",
        "get_sync_status",
    ]

    tools_list = task_registry.list_tools()
    tool_names = [t["name"] for t in tools_list]

    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Missing tool: {tool_name}"


def test_task_registry_get_schema(task_registry):
    """Test that schemas can be retrieved from the registry."""
    schema = task_registry.get_schema("create_task")

    assert schema is not None
    assert schema["name"] == "create_task"
    assert "description" in schema
    assert "inputSchema" in schema
    assert schema["inputSchema"]["type"] == "object"
    assert "title" in schema["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_create_task(mock_task_manager, mock_sync_manager):
    """Test create_task tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    # Mock return value
    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.to_dict.return_value = {"id": "t1", "title": "Test Task"}
    mock_task_manager.create_task.return_value = mock_task

    # Mock get_project_context
    with unittest.mock.patch("gobby.mcp_proxy.tools.tasks.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": "test-project-id"}

        result = await registry.call("create_task", {"title": "Test Task", "priority": 1})

        mock_task_manager.create_task.assert_called_with(
            project_id="test-project-id",
            title="Test Task",
            description=None,
            priority=1,
            task_type="task",
            parent_task_id=None,
            labels=None,
        )
    assert result == {"id": "t1", "title": "Test Task"}


@pytest.mark.asyncio
async def test_get_task_not_found(mock_task_manager, mock_sync_manager):
    """Test get_task returns error when task not found."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_task_manager.get_task.return_value = None

    result = await registry.call("get_task", {"task_id": "nonexistent"})

    assert "error" in result
    assert result["found"] is False


@pytest.mark.asyncio
async def test_list_ready_tasks(mock_task_manager, mock_sync_manager):
    """Test list_ready_tasks tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_t1 = MagicMock()
    mock_t1.to_dict.return_value = {"id": "t1"}
    mock_task_manager.list_ready_tasks.return_value = [mock_t1]

    result = await registry.call("list_ready_tasks", {"limit": 5})

    mock_task_manager.list_ready_tasks.assert_called_with(
        priority=None, task_type=None, assignee=None, limit=5
    )
    assert result["count"] == 1
    assert result["tasks"][0]["id"] == "t1"


@pytest.mark.asyncio
async def test_sync_tasks(mock_task_manager, mock_sync_manager):
    """Test sync_tasks tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    result = await registry.call("sync_tasks", {"direction": "both"})

    mock_sync_manager.import_from_jsonl.assert_called_once()
    mock_sync_manager.export_to_jsonl.assert_called_once()
    assert result["import"] == "completed"
    assert result["export"] == "completed"

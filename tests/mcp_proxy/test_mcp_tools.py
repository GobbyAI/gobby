from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


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


def test_create_task_registry_returns_registry(task_registry) -> None:
    """Test that create_task_registry returns an InternalToolRegistry."""
    assert isinstance(task_registry, InternalToolRegistry)
    assert task_registry.name == "gobby-tasks"


def test_create_task_registry_has_all_tools(task_registry) -> None:
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


def test_task_registry_get_schema(task_registry) -> None:
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
    # Mock return value for create_task_with_decomposition (returns dict with task key)
    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.seq_num = 123
    mock_task.to_dict.return_value = {"id": "t1", "title": "Test Task"}
    mock_task_manager.create_task_with_decomposition.return_value = {
        "task": {"id": "t1"},
    }
    mock_task_manager.get_task.return_value = mock_task

    # Mock get_project_context and LocalSessionManager
    with (
        patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
    ):
        mock_ctx.return_value = {"id": "test-project-id"}
        # Mock session manager to resolve session_id as-is
        mock_session_manager = MagicMock()
        mock_session_manager.resolve_session_reference.return_value = "test-session"
        MockSessionManager.return_value = mock_session_manager

        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "create_task",
            {
                "title": "Test Task",
                "priority": 1,
                "session_id": "test-session",
                "category": "research",
            },
        )

        mock_task_manager.create_task_with_decomposition.assert_called_with(
            project_id="test-project-id",
            title="Test Task",
            description=None,
            priority=1,
            task_type="task",
            parent_task_id=None,
            labels=None,
            category="research",
            validation_criteria=None,
            created_in_session_id="test-session",
        )
    assert result == {"id": "t1", "seq_num": 123, "ref": "#123"}


@pytest.mark.asyncio
async def test_create_task_with_session_id(mock_task_manager, mock_sync_manager):
    """Test create_task tool captures session_id as created_in_session_id."""
    # Mock return value for create_task_with_decomposition (returns dict with task key)
    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.seq_num = 456
    mock_task.to_dict.return_value = {"id": "t1", "title": "Test Task"}
    mock_task_manager.create_task_with_decomposition.return_value = {
        "task": {"id": "t1"},
    }
    mock_task_manager.get_task.return_value = mock_task

    # Mock get_project_context and LocalSessionManager
    with (
        patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
    ):
        mock_ctx.return_value = {"id": "test-project-id"}
        # Mock session manager to resolve session_id as-is
        mock_session_manager = MagicMock()
        mock_session_manager.resolve_session_reference.return_value = "session-abc123"
        MockSessionManager.return_value = mock_session_manager

        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "create_task",
            {"title": "Test Task", "session_id": "session-abc123", "category": "research"},
        )

        mock_task_manager.create_task_with_decomposition.assert_called_with(
            project_id="test-project-id",
            title="Test Task",
            description=None,
            priority=2,
            task_type="task",
            parent_task_id=None,
            labels=None,
            category="research",
            validation_criteria=None,
            created_in_session_id="session-abc123",
        )
    assert result == {"id": "t1", "seq_num": 456, "ref": "#456"}


@pytest.mark.asyncio
async def test_get_task_not_found(mock_task_manager, mock_sync_manager):
    """Test get_task returns error when task not found."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_task_manager.get_task.return_value = None

    result = await registry.call("get_task", {"task_id": "nonexistent"})

    mock_task_manager.get_task.assert_called_once_with("nonexistent")
    assert "error" in result
    assert result["found"] is False


@pytest.mark.asyncio
async def test_list_ready_tasks(mock_task_manager, mock_sync_manager):
    """Test list_ready_tasks tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_t1 = MagicMock()
    mock_t1.to_brief.return_value = {"id": "t1"}
    mock_task_manager.list_ready_tasks.return_value = [mock_t1]

    # Mock get_project_context for project filtering (in _context module used by standalone resolver)
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": "test-project-id"}

        result = await registry.call("list_ready_tasks", {"limit": 5})

        mock_task_manager.list_ready_tasks.assert_called_with(
            priority=None,
            task_type=None,
            assignee=None,
            parent_task_id=None,
            limit=5,
            project_id="test-project-id",
        )
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "t1"


@pytest.mark.asyncio
async def test_list_ready_tasks_all_projects(mock_task_manager, mock_sync_manager):
    """Test list_ready_tasks with all_projects=True ignores project filter."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_t1 = MagicMock()
    mock_t1.to_brief.return_value = {"id": "t1"}
    mock_task_manager.list_ready_tasks.return_value = [mock_t1]

    # Mock get_project_context (in _context module used by standalone resolver)
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": "test-project-id"}

        result = await registry.call("list_ready_tasks", {"limit": 5, "all_projects": True})

        mock_task_manager.list_ready_tasks.assert_called_with(
            priority=None,
            task_type=None,
            assignee=None,
            parent_task_id=None,
            limit=5,
            project_id=None,  # Should be None when all_projects=True
        )
        assert result["count"] == 1


@pytest.mark.asyncio
async def test_sync_tasks(mock_task_manager, mock_sync_manager):
    """Test sync_tasks tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    result = await registry.call("sync_tasks", {"direction": "both"})

    mock_sync_manager.import_from_jsonl.assert_called_once()
    mock_sync_manager.export_to_jsonl.assert_called_once()
    assert result["import"] == "completed"
    assert result["export"] == "completed"


# NOTE: test_expand_task_integration and test_expand_task_with_flags were removed
# because they tested the old expand_task tool API which has been replaced by
# save_expansion_spec/execute_expansion/get_expansion_spec.
# See tests/mcp_proxy/tools/test_task_expansion_new.py for current expansion tests.


# =============================================================================
# Commit Linking MCP Tools Tests
# =============================================================================


def test_task_registry_has_commit_linking_tools(task_registry) -> None:
    """Test that commit linking tools are registered."""
    expected_tools = [
        "link_commit",
        "unlink_commit",
        "auto_link_commits",
        "get_task_diff",
    ]

    tools_list = task_registry.list_tools()
    tool_names = [t["name"] for t in tools_list]

    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Missing commit linking tool: {tool_name}"


def test_link_commit_schema(task_registry) -> None:
    """Test link_commit tool schema."""
    schema = task_registry.get_schema("link_commit")

    assert schema is not None
    assert schema["name"] == "link_commit"
    assert "inputSchema" in schema

    properties = schema["inputSchema"]["properties"]
    assert "task_id" in properties
    assert "commit_sha" in properties


def test_unlink_commit_schema(task_registry) -> None:
    """Test unlink_commit tool schema."""
    schema = task_registry.get_schema("unlink_commit")

    assert schema is not None
    assert schema["name"] == "unlink_commit"
    assert "inputSchema" in schema

    properties = schema["inputSchema"]["properties"]
    assert "task_id" in properties
    assert "commit_sha" in properties


def test_auto_link_commits_schema(task_registry) -> None:
    """Test auto_link_commits tool schema."""
    schema = task_registry.get_schema("auto_link_commits")

    assert schema is not None
    assert schema["name"] == "auto_link_commits"
    assert "inputSchema" in schema

    properties = schema["inputSchema"]["properties"]
    # Optional parameters
    assert "task_id" in properties or "since" in properties


def test_get_task_diff_schema(task_registry) -> None:
    """Test get_task_diff tool schema."""
    schema = task_registry.get_schema("get_task_diff")

    assert schema is not None
    assert schema["name"] == "get_task_diff"
    assert "inputSchema" in schema

    properties = schema["inputSchema"]["properties"]
    assert "task_id" in properties


@pytest.mark.asyncio
async def test_link_commit_tool(mock_task_manager, mock_sync_manager):
    """Test link_commit tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.commits = ["abc123"]
    mock_task.to_dict.return_value = {"id": "t1", "commits": ["abc123"]}
    mock_task_manager.link_commit.return_value = mock_task

    with patch("gobby.mcp_proxy.tools.task_sync.get_project_context", return_value=None):
        result = await registry.call(
            "link_commit",
            {"task_id": "t1", "commit_sha": "abc123"},
        )

    mock_task_manager.link_commit.assert_called_with("t1", "abc123", cwd=None)
    assert result["task_id"] == "t1"
    assert "abc123" in result["commits"]


@pytest.mark.asyncio
async def test_unlink_commit_tool(mock_task_manager, mock_sync_manager):
    """Test unlink_commit tool execution."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.commits = []
    mock_task.to_dict.return_value = {"id": "t1", "commits": []}
    mock_task_manager.unlink_commit.return_value = mock_task

    with patch("gobby.mcp_proxy.tools.task_sync.get_project_context", return_value=None):
        result = await registry.call(
            "unlink_commit",
            {"task_id": "t1", "commit_sha": "abc123"},
        )

    mock_task_manager.unlink_commit.assert_called_with("t1", "abc123", cwd=None)
    assert result["task_id"] == "t1"


@pytest.mark.asyncio
async def test_get_task_diff_tool(mock_task_manager, mock_sync_manager):
    """Test get_task_diff tool execution."""
    from gobby.tasks.commits import TaskDiffResult

    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.commits = ["abc123"]
    mock_task_manager.get_task.return_value = mock_task

    # Patch before creating registry since functions are captured at creation time
    with patch("gobby.tasks.commits.get_task_diff") as mock_diff:
        mock_diff.return_value = TaskDiffResult(
            diff="diff content",
            commits=["abc123"],
            has_uncommitted_changes=False,
            file_count=2,
        )

        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        result = await registry.call(
            "get_task_diff",
            {"task_id": "t1", "include_uncommitted": False},
        )

        assert result["diff"] == "diff content"
        assert result["commits"] == ["abc123"]
        assert result["file_count"] == 2


@pytest.mark.asyncio
async def test_auto_link_commits_tool(mock_task_manager, mock_sync_manager):
    """Test auto_link_commits tool execution."""
    from gobby.tasks.commits import AutoLinkResult

    # Patch before creating registry since functions are captured at creation time
    with patch("gobby.tasks.commits.auto_link_commits") as mock_auto_link:
        mock_auto_link.return_value = AutoLinkResult(
            linked_tasks={"t1": ["abc123", "def456"]},
            total_linked=2,
            skipped=0,
        )

        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        result = await registry.call(
            "auto_link_commits",
            {"since": "1 week ago"},
        )

        assert result["total_linked"] == 2
        assert "t1" in result["linked_tasks"]


@pytest.mark.asyncio
async def test_link_commit_invalid_task(mock_task_manager, mock_sync_manager):
    """Test link_commit with non-existent task."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_task_manager.link_commit.side_effect = ValueError("Task not found")

    result = await registry.call(
        "link_commit",
        {"task_id": "nonexistent", "commit_sha": "abc123"},
    )

    assert "error" in result


@pytest.mark.asyncio
async def test_get_task_diff_no_commits(mock_task_manager, mock_sync_manager):
    """Test get_task_diff when task has no commits."""
    registry = create_task_registry(mock_task_manager, mock_sync_manager)

    mock_task = MagicMock()
    mock_task.id = "t1"
    mock_task.commits = []
    mock_task_manager.get_task.return_value = mock_task

    with patch("gobby.tasks.commits.get_task_diff") as mock_diff:
        from gobby.tasks.commits import TaskDiffResult

        mock_diff.return_value = TaskDiffResult(
            diff="",
            commits=[],
            has_uncommitted_changes=False,
            file_count=0,
        )

        result = await registry.call(
            "get_task_diff",
            {"task_id": "t1"},
        )

        assert result["diff"] == ""
        assert result["commits"] == []

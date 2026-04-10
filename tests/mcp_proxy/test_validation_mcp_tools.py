"""Tests for validation MCP tools.

TDD Red Phase: These tests define the expected behavior for
validation-related MCP tools which do not yet exist.

Tools tested:
- get_validation_history: Get full validation history for a task
- get_recurring_issues: Analyze validation history for recurring issues
- clear_validation_history: Clear validation history for fresh start
- de_escalate_task: Return an escalated task to open status

Task: gt-88c34e
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.task_validation import create_validation_registry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.validation import TaskValidator

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_task_validator():
    """Create a mock task validator."""
    validator = AsyncMock(spec=TaskValidator)
    return validator


@pytest.fixture
def registry_with_patches(mock_task_manager, mock_task_validator):
    """Create a validation registry directly (validation tools are internal-only)."""
    with patch(
        "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
        side_effect=lambda tm, tid, *a, **kw: tid,
    ):
        registry = create_validation_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )
        yield registry


@pytest.fixture
def task_registry_with_patches(mock_task_manager, mock_task_validator):
    """Create a full task registry for testing de_escalate_task (now in lifecycle)."""
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
    ):
        registry = create_task_registry(
            task_manager=mock_task_manager,
            sync_manager=MagicMock(),
            task_validator=mock_task_validator,
        )
        yield registry


# ============================================================================
# get_validation_history MCP Tool Tests
# ============================================================================


class TestGetValidationHistoryTool:
    """Tests for get_validation_history MCP tool."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_validation_history_returns_all_iterations(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that get_validation_history returns all validation iterations."""
        task = Task(
            id="t1",
            title="Task with history",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_validation_history", {"task_id": "t1"})

        assert "history" in result
        assert isinstance(result["history"], list)
        assert "task_id" in result
        assert result["task_id"] == "t1"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_validation_history_includes_iteration_details(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that history includes full iteration details."""
        task = Task(
            id="t1",
            title="Task with history",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_validation_history", {"task_id": "t1"})

        # If history exists, each item should have these fields
        if result["history"]:
            iteration = result["history"][0]
            assert "iteration" in iteration
            assert "status" in iteration
            assert "feedback" in iteration
            assert "issues" in iteration
            assert "context_type" in iteration
            assert "validator_type" in iteration
            assert "created_at" in iteration

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_validation_history_task_not_found(
        self, mock_task_manager, registry_with_patches
    ):
        """Test get_validation_history with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await registry_with_patches.call(
            "get_validation_history", {"task_id": "nonexistent"}
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_validation_history_empty_history(
        self, mock_task_manager, registry_with_patches
    ):
        """Test get_validation_history with task that has no history."""
        task = Task(
            id="t1",
            title="New task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_validation_history", {"task_id": "t1"})

        assert "history" in result
        assert result["history"] == []

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_validation_history_includes_issues_as_list(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that issues in history are returned as serializable list."""
        task = Task(
            id="t1",
            title="Task with issues",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_validation_history", {"task_id": "t1"})

        # Even empty history should be a list
        assert isinstance(result.get("history", []), list)


# ============================================================================
# get_recurring_issues MCP Tool Tests
# ============================================================================


class TestGetRecurringIssuesTool:
    """Tests for get_recurring_issues MCP tool."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_recurring_issues_returns_grouped_analysis(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that get_recurring_issues returns grouped issue analysis."""
        task = Task(
            id="t1",
            title="Task with recurring issues",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_recurring_issues", {"task_id": "t1"})

        assert "recurring_issues" in result
        assert "total_iterations" in result
        assert isinstance(result["recurring_issues"], list)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_recurring_issues_includes_occurrence_count(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that recurring issues include occurrence count."""
        task = Task(
            id="t1",
            title="Task with recurring issues",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_recurring_issues", {"task_id": "t1"})

        # If there are recurring issues, each should have count
        if result["recurring_issues"]:
            issue = result["recurring_issues"][0]
            assert "count" in issue
            assert "title" in issue

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_recurring_issues_respects_threshold_parameter(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that get_recurring_issues accepts threshold parameter."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        # Should accept threshold parameter without error
        result = await registry_with_patches.call(
            "get_recurring_issues", {"task_id": "t1", "threshold": 5}
        )

        assert "recurring_issues" in result

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_recurring_issues_task_not_found(
        self, mock_task_manager, registry_with_patches
    ):
        """Test get_recurring_issues with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await registry_with_patches.call(
            "get_recurring_issues", {"task_id": "nonexistent"}
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_recurring_issues_no_history(self, mock_task_manager, registry_with_patches):
        """Test get_recurring_issues with task that has no validation history."""
        task = Task(
            id="t1",
            title="New task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_recurring_issues", {"task_id": "t1"})

        assert result["recurring_issues"] == []
        assert result["total_iterations"] == 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_recurring_issues_includes_has_recurring_flag(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that result includes has_recurring boolean flag."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("get_recurring_issues", {"task_id": "t1"})

        assert "has_recurring" in result
        assert isinstance(result["has_recurring"], bool)


# ============================================================================
# clear_validation_history MCP Tool Tests
# ============================================================================


class TestClearValidationHistoryTool:
    """Tests for clear_validation_history MCP tool."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_clear_validation_history_removes_all_iterations(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that clear_validation_history removes all validation history."""
        task = Task(
            id="t1",
            title="Task with history",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("clear_validation_history", {"task_id": "t1"})

        assert "cleared" in result
        assert result["cleared"] is True
        assert result["task_id"] == "t1"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_clear_validation_history_task_not_found(
        self, mock_task_manager, registry_with_patches
    ):
        """Test clear_validation_history with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await registry_with_patches.call(
            "clear_validation_history", {"task_id": "nonexistent"}
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_clear_validation_history_resets_fail_count(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that clear_validation_history also resets validation_fail_count."""
        task = Task(
            id="t1",
            title="Failed task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="task",
            validation_fail_count=3,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("clear_validation_history", {"task_id": "t1"})

        # Should reset fail count as well
        assert result["cleared"] is True
        # Check that update_task was called with validation_fail_count=0
        mock_task_manager.update_task.assert_called()
        update_kwargs = mock_task_manager.update_task.call_args.kwargs
        assert update_kwargs.get("validation_fail_count") == 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_clear_validation_history_accepts_reason_parameter(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that clear_validation_history accepts optional reason parameter."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call(
            "clear_validation_history",
            {"task_id": "t1", "reason": "Major refactor, starting fresh"},
        )

        assert result["cleared"] is True

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_clear_validation_history_returns_items_cleared_count(
        self, mock_task_manager, registry_with_patches
    ):
        """Test that clear_validation_history returns count of cleared items."""
        task = Task(
            id="t1",
            title="Task with history",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await registry_with_patches.call("clear_validation_history", {"task_id": "t1"})

        assert "iterations_cleared" in result
        assert isinstance(result["iterations_cleared"], int)


# ============================================================================
# de_escalate_task MCP Tool Tests
# ============================================================================


class TestDeEscalateTaskTool:
    """Tests for de_escalate_task MCP tool (now in lifecycle registry via gobby-tasks)."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_returns_to_open_status(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task returns task to open status."""
        escalated_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="escalated",
            priority=2,
            task_type="task",
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="max_iterations",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = escalated_task

        reopened_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="task",
            escalated_at=None,
            escalation_reason=None,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.update_task.return_value = reopened_task

        result = await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Fixed manually"}
        )

        # Lifecycle version returns empty dict on success
        assert "error" not in result
        # Verify update_task was called with correct status
        mock_task_manager.update_task.assert_called_once()
        call_kwargs = mock_task_manager.update_task.call_args.kwargs
        assert call_kwargs["status"] == "open"
        assert call_kwargs["escalated_at"] is None
        assert call_kwargs["escalation_reason"] is None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_requires_reason(
        self, mock_task_manager, task_registry_with_patches
    ) -> None:
        """Test that de_escalate_task requires a reason."""
        escalated_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="escalated",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = escalated_task

        # Missing reason should raise an error (TypeError for missing required arg)
        with pytest.raises(TypeError):
            await task_registry_with_patches.call("de_escalate_task", {"task_id": "t1"})

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_not_escalated_error(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test de_escalate_task fails if task is not escalated."""
        non_escalated_task = Task(
            id="t1",
            title="Normal task",
            project_id="p1",
            status="open",  # Not escalated
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = non_escalated_task

        result = await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Trying to de-escalate"}
        )

        assert "error" in result
        assert "not escalated" in result["error"].lower()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_task_not_found(
        self, mock_task_manager, task_registry_with_patches
    ) -> None:
        """Test de_escalate_task with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "nonexistent", "reason": "Test"}
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_clears_escalation_fields(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task clears escalation-related fields."""
        escalated_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="escalated",
            priority=2,
            task_type="task",
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="recurring_issues",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = escalated_task

        await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Resolved manually"}
        )

        # Verify update_task was called with correct fields
        mock_task_manager.update_task.assert_called()
        update_kwargs = mock_task_manager.update_task.call_args.kwargs
        assert update_kwargs.get("status") == "open"
        assert update_kwargs.get("escalated_at") is None
        assert update_kwargs.get("escalation_reason") is None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_succeeds_with_reason_param(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task succeeds when given a reason."""
        escalated_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="escalated",
            priority=2,
            task_type="task",
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="max_iterations",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = escalated_task

        reopened_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.update_task.return_value = reopened_task

        result = await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Human fixed the issue"}
        )

        # Lifecycle version returns empty dict on success; verify the call went through
        assert "error" not in result
        mock_task_manager.update_task.assert_called_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_resets_validation_state(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task optionally resets validation state."""
        escalated_task = Task(
            id="t1",
            title="Escalated task",
            project_id="p1",
            status="escalated",
            priority=2,
            task_type="task",
            validation_fail_count=10,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="max_iterations",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = escalated_task

        await task_registry_with_patches.call(
            "de_escalate_task",
            {"task_id": "t1", "reason": "Fixed", "reset_validation": True},
        )

        # With reset_validation=True, should reset fail count
        mock_task_manager.update_task.assert_called()
        update_kwargs = mock_task_manager.update_task.call_args.kwargs
        assert update_kwargs.get("validation_fail_count") == 0


# ============================================================================
# Tool Registration Tests
# ============================================================================


class TestValidationToolsRegistration:
    """Tests verifying validation tools are properly registered."""

    @pytest.mark.integration
    def test_get_validation_history_tool_registered(self, registry_with_patches) -> None:
        """Test that get_validation_history is registered as an MCP tool."""
        tools = registry_with_patches.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_validation_history" in tool_names

    @pytest.mark.integration
    def test_get_recurring_issues_tool_registered(self, registry_with_patches) -> None:
        """Test that get_recurring_issues is registered as an MCP tool."""
        tools = registry_with_patches.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_recurring_issues" in tool_names

    @pytest.mark.integration
    def test_clear_validation_history_tool_registered(self, registry_with_patches) -> None:
        """Test that clear_validation_history is registered as an MCP tool."""
        tools = registry_with_patches.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "clear_validation_history" in tool_names

    @pytest.mark.integration
    def test_de_escalate_task_tool_registered(self, task_registry_with_patches) -> None:
        """Test that de_escalate_task is registered on gobby-tasks (lifecycle)."""
        tools = task_registry_with_patches.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "de_escalate_task" in tool_names

    @pytest.mark.integration
    def test_get_validation_history_tool_schema(self, registry_with_patches) -> None:
        """Test that get_validation_history has correct input schema."""
        schema = registry_with_patches.get_schema("get_validation_history")

        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "properties" in input_schema
        assert "task_id" in input_schema["properties"]
        assert "task_id" in input_schema.get("required", [])

    @pytest.mark.integration
    def test_get_recurring_issues_tool_schema(self, registry_with_patches) -> None:
        """Test that get_recurring_issues has correct input schema."""
        schema = registry_with_patches.get_schema("get_recurring_issues")

        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "properties" in input_schema
        assert "task_id" in input_schema["properties"]
        # threshold should be optional
        if "threshold" in input_schema["properties"]:
            assert input_schema["properties"]["threshold"]["type"] == "integer"

    @pytest.mark.integration
    def test_clear_validation_history_tool_schema(self, registry_with_patches) -> None:
        """Test that clear_validation_history has correct input schema."""
        schema = registry_with_patches.get_schema("clear_validation_history")

        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "properties" in input_schema
        assert "task_id" in input_schema["properties"]

    @pytest.mark.integration
    def test_de_escalate_task_tool_schema(self, task_registry_with_patches) -> None:
        """Test that de_escalate_task has correct input schema."""
        schema = task_registry_with_patches.get_schema("de_escalate_task")

        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "properties" in input_schema
        assert "task_id" in input_schema["properties"]
        assert "reason" in input_schema["properties"]
        # Both should be required
        assert "task_id" in input_schema.get("required", [])
        assert "reason" in input_schema.get("required", [])

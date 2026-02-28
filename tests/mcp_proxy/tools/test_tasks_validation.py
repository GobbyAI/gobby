"""Tests for task validation MCP tools module.

TDD Red Phase: These tests import from the NEW module location (tasks_validation)
which does not exist yet. Tests should fail with ImportError initially.

After extraction via Strangler Fig pattern:
- tasks_validation.py will contain validation-related tools
- tasks.py will re-export/delegate for backwards compatibility

Tools to be extracted:
- validate_task: Validate task completion via LLM
- get_validation_status: Get validation details
- reset_validation_count: Reset failure count
- get_validation_history: Full validation history
- get_recurring_issues: Analyze recurring issues
- clear_validation_history: Clear history for fresh start
- de_escalate_task: Return escalated task to open

Task: gt-3c4cf0
Parent: gt-30cebd (Decompose tasks.py)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import from NEW module location - will fail until extraction is complete
# This is intentional for TDD red phase
try:
    from gobby.mcp_proxy.tools.task_validation import (
        create_validation_registry,
    )

    IMPORT_SUCCEEDED = True
except ImportError:
    IMPORT_SUCCEEDED = False
    create_validation_registry = None

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.validation import TaskValidator, ValidationResult

# Skip all tests if module doesn't exist yet (TDD red phase)
pytestmark = pytest.mark.skipif(
    not IMPORT_SUCCEEDED,
    reason="tasks_validation module not yet extracted (TDD red phase)",
)


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
def validation_registry(mock_task_manager, mock_task_validator):
    """Create a validation tool registry with mocked dependencies."""
    if not IMPORT_SUCCEEDED:
        pytest.skip("Module not extracted yet")

    with patch("gobby.mcp_proxy.tools.task_validation.ValidationHistoryManager"):
        registry = create_validation_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )
        yield registry


# ============================================================================
# validate_task MCP Tool Tests
# ============================================================================


class TestValidateTaskTool:
    """Tests for validate_task MCP tool."""

    @pytest.mark.asyncio
    async def test_validate_task_leaf_task_valid(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test validate_task returns valid for a leaf task that passes validation."""
        task = Task(
            id="t1",
            title="Implement feature",
            description="Add the new feature",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_criteria="- Feature works correctly\n- Tests pass",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []  # No children (leaf task)

        mock_task_validator.validate_task.return_value = ValidationResult(
            status="valid",
            feedback="All criteria met. Feature works correctly and tests pass.",
        )

        result = await validation_registry.call(
            "validate_task",
            {"task_id": "t1", "changes_summary": "Added new feature with tests"},
        )

        assert result["is_valid"] is True
        assert result["status"] == "valid"
        assert "criteria met" in result["feedback"].lower()

    @pytest.mark.asyncio
    async def test_validate_task_leaf_task_invalid(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test validate_task returns invalid for a leaf task that fails validation."""
        task = Task(
            id="t1",
            title="Add tests",
            description="Write unit tests",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_criteria="- All functions have tests\n- Coverage >= 80%",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []  # No children

        mock_task_validator.validate_task.return_value = ValidationResult(
            status="invalid",
            feedback="Missing tests for helper functions. Coverage is only 60%.",
        )

        result = await validation_registry.call(
            "validate_task",
            {"task_id": "t1", "changes_summary": "Added some tests"},
        )

        assert result["is_valid"] is False
        assert result["status"] == "invalid"
        assert "missing tests" in result["feedback"].lower()

    @pytest.mark.asyncio
    async def test_validate_task_parent_all_children_closed(
        self, mock_task_manager, validation_registry
    ):
        """Test validate_task for parent task when all children are closed."""
        parent_task = Task(
            id="t1",
            title="Epic task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="epic",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = parent_task

        # All children closed
        children = [
            Task(
                id="c1",
                title="Subtask 1",
                project_id="p1",
                status="closed",
                priority=2,
                task_type="task",
                created_at="now",
                updated_at="now",
            ),
            Task(
                id="c2",
                title="Subtask 2",
                project_id="p1",
                status="closed",
                priority=2,
                task_type="task",
                created_at="now",
                updated_at="now",
            ),
        ]
        mock_task_manager.list_tasks.return_value = children

        result = await validation_registry.call("validate_task", {"task_id": "t1"})

        assert result["is_valid"] is True
        assert result["status"] == "valid"
        assert "child tasks" in result["feedback"].lower()

    @pytest.mark.asyncio
    async def test_validate_task_parent_open_children(self, mock_task_manager, validation_registry):
        """Test validate_task for parent task with open children."""
        parent_task = Task(
            id="t1",
            title="Epic task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="epic",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = parent_task

        # Some children open
        children = [
            Task(
                id="c1",
                title="Closed subtask",
                project_id="p1",
                status="closed",
                priority=2,
                task_type="task",
                created_at="now",
                updated_at="now",
            ),
            Task(
                id="c2",
                title="Open subtask",
                project_id="p1",
                status="open",
                priority=2,
                task_type="task",
                created_at="now",
                updated_at="now",
            ),
        ]
        mock_task_manager.list_tasks.return_value = children

        result = await validation_registry.call("validate_task", {"task_id": "t1"})

        assert result["is_valid"] is False
        assert result["status"] == "invalid"
        assert "still open" in result["feedback"].lower()

    @pytest.mark.asyncio
    async def test_validate_task_not_found(self, mock_task_manager, validation_registry):
        """Test validate_task with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await validation_registry.call("validate_task", {"task_id": "nonexistent"})
        assert "error" in result
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_validate_task_creates_fix_task_on_failure(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test that validate_task creates a fix task after validation failure."""
        task = Task(
            id="t1",
            title="Broken task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_criteria="Must work",
            validation_fail_count=0,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        mock_task_validator.validate_task.return_value = ValidationResult(
            status="invalid",
            feedback="Implementation is incorrect",
        )

        fix_task = Task(
            id="fix-t1",
            title="Fix validation failures for Broken task",
            project_id="p1",
            status="open",
            priority=1,
            task_type="bug",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.create_task.return_value = fix_task

        await validation_registry.call(
            "validate_task",
            {"task_id": "t1", "changes_summary": "Wrong implementation"},
        )

        # Should create a fix task
        mock_task_manager.create_task.assert_called_once()
        call_kwargs = mock_task_manager.create_task.call_args.kwargs
        assert "Fix validation failures" in call_kwargs["title"]
        assert call_kwargs["task_type"] == "bug"
        assert call_kwargs["parent_task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_validate_task_increments_fail_count(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test that validate_task increments fail count on invalid result."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_criteria="Must work",
            validation_fail_count=1,  # Already failed once
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        mock_task_validator.validate_task.return_value = ValidationResult(
            status="invalid",
            feedback="Still broken",
        )

        mock_task_manager.create_task.return_value = Task(
            id="fix-t1",
            title="Fix",
            project_id="p1",
            status="open",
            priority=1,
            task_type="bug",
            created_at="now",
            updated_at="now",
        )

        await validation_registry.call(
            "validate_task",
            {"task_id": "t1", "changes_summary": "Still wrong"},
        )

        # Should update task with incremented fail count
        mock_task_manager.update_task.assert_called()
        update_kwargs = mock_task_manager.update_task.call_args.kwargs
        assert update_kwargs.get("validation_fail_count") == 2

    @pytest.mark.asyncio
    async def test_validate_task_auto_gathers_context(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test that validate_task auto-gathers context when changes_summary not provided."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_criteria="Must work",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        mock_task_validator.validate_task.return_value = ValidationResult(
            status="valid",
            feedback="OK",
        )

        with patch("gobby.tasks.validation.get_validation_context_smart") as mock_context:
            mock_context.return_value = "Auto-gathered context from git"

            await validation_registry.call(
                "validate_task",
                {"task_id": "t1"},  # No changes_summary
            )

            # Should call smart context gathering
            mock_context.assert_called_once()


# ============================================================================
# get_validation_status MCP Tool Tests
# ============================================================================


class TestGetValidationStatusTool:
    """Tests for get_validation_status MCP tool."""

    @pytest.mark.asyncio
    async def test_get_validation_status_returns_all_fields(
        self, mock_task_manager, validation_registry
    ):
        """Test that get_validation_status returns all validation-related fields."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_status="invalid",
            validation_feedback="Missing tests",
            validation_criteria="- [ ] Tests pass",
            validation_fail_count=2,
            use_external_validator=False,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        result = await validation_registry.call("get_validation_status", {"task_id": "t1"})

        assert result["task_id"] == "t1"
        assert result["validation_status"] == "invalid"
        assert result["validation_feedback"] == "Missing tests"
        assert result["validation_criteria"] == "- [ ] Tests pass"
        assert result["validation_fail_count"] == 2
        assert result["use_external_validator"] is False

    @pytest.mark.asyncio
    async def test_get_validation_status_not_found(self, mock_task_manager, validation_registry):
        """Test get_validation_status with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await validation_registry.call("get_validation_status", {"task_id": "nonexistent"})
        assert "error" in result
        assert "not found" in result["error"].lower()


# ============================================================================
# reset_validation_count MCP Tool Tests
# ============================================================================


class TestResetValidationCountTool:
    """Tests for reset_validation_count MCP tool."""

    @pytest.mark.asyncio
    async def test_reset_validation_count_success(self, mock_task_manager, validation_registry):
        """Test that reset_validation_count resets to zero."""
        task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_fail_count=5,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        updated_task = Task(
            id="t1",
            title="Task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_fail_count=0,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.update_task.return_value = updated_task

        result = await validation_registry.call("reset_validation_count", {"task_id": "t1"})

        assert result["validation_fail_count"] == 0
        assert "reset" in result["message"].lower()
        mock_task_manager.update_task.assert_called_with("t1", validation_fail_count=0)

    @pytest.mark.asyncio
    async def test_reset_validation_count_not_found(self, mock_task_manager, validation_registry):
        """Test reset_validation_count with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await validation_registry.call(
            "reset_validation_count", {"task_id": "nonexistent"}
        )
        assert "error" in result
        assert "not found" in result["error"].lower()


# ============================================================================
# Tool Registration Tests
# ============================================================================


class TestValidationToolsRegistration:
    """Tests verifying validation tools are properly registered in the new module."""

    def test_validate_task_tool_registered(self, validation_registry) -> None:
        """Test that validate_task is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "validate_task" in tool_names

    def test_get_validation_status_tool_registered(self, validation_registry) -> None:
        """Test that get_validation_status is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_validation_status" in tool_names

    def test_reset_validation_count_tool_registered(self, validation_registry) -> None:
        """Test that reset_validation_count is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "reset_validation_count" in tool_names

    def test_get_validation_history_tool_registered(self, validation_registry) -> None:
        """Test that get_validation_history is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_validation_history" in tool_names

    def test_get_recurring_issues_tool_registered(self, validation_registry) -> None:
        """Test that get_recurring_issues is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "get_recurring_issues" in tool_names

    def test_clear_validation_history_tool_registered(self, validation_registry) -> None:
        """Test that clear_validation_history is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "clear_validation_history" in tool_names

    def test_de_escalate_task_tool_registered(self, validation_registry) -> None:
        """Test that de_escalate_task is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "de_escalate_task" in tool_names


# ============================================================================
# Schema Tests
# ============================================================================


class TestValidationToolSchemas:
    """Tests for tool input schemas."""

    def test_validate_task_schema(self, validation_registry) -> None:
        """Test validate_task has correct input schema."""
        schema = validation_registry.get_schema("validate_task")
        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "task_id" in input_schema["properties"]
        assert "changes_summary" in input_schema["properties"]
        assert "context_files" in input_schema["properties"]

# ============================================================================
# run_fix_attempt MCP Tool Tests
# ============================================================================


class TestRunFixAttemptTool:
    """Tests for run_fix_attempt MCP tool."""

    @pytest.mark.asyncio
    async def test_run_fix_attempt_no_agent_runner(self, mock_task_manager, validation_registry):
        """Test run_fix_attempt returns error when agent runner not configured."""
        # Default registry has no agent_runner
        result = await validation_registry.call("run_fix_attempt", {"task_id": "t1"})

        assert result["success"] is False
        assert "agent runner not configured" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_run_fix_attempt_task_not_found(self, mock_task_manager, mock_task_validator):
        """Test run_fix_attempt with non-existent task."""
        mock_task_manager.get_task.return_value = None

        mock_agent_runner = AsyncMock()

        with patch("gobby.mcp_proxy.tools.task_validation.ValidationHistoryManager"):
            registry = create_validation_registry(
                task_manager=mock_task_manager,
                task_validator=mock_task_validator,
                agent_runner=mock_agent_runner,
            )

            result = await registry.call("run_fix_attempt", {"task_id": "nonexistent"})

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_run_fix_attempt_no_issues(self, mock_task_manager, mock_task_validator):
        """Test run_fix_attempt with no issues and no validation feedback."""
        task = Task(
            id="t1",
            title="Task without feedback",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_feedback=None,
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task

        mock_agent_runner = AsyncMock()

        with patch("gobby.mcp_proxy.tools.task_validation.ValidationHistoryManager"):
            registry = create_validation_registry(
                task_manager=mock_task_manager,
                task_validator=mock_task_validator,
                agent_runner=mock_agent_runner,
            )

            result = await registry.call("run_fix_attempt", {"task_id": "t1"})

        assert result["success"] is False
        assert "no issues" in result["error"].lower()

    def test_run_fix_attempt_tool_registered(self, validation_registry) -> None:
        """Test that run_fix_attempt is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "run_fix_attempt" in tool_names


# ============================================================================
# validate_and_fix MCP Tool Tests
# ============================================================================


class TestValidateAndFixTool:
    """Tests for validate_and_fix MCP tool."""

    @pytest.mark.asyncio
    async def test_validate_and_fix_task_not_found(self, mock_task_manager, validation_registry):
        """Test validate_and_fix with non-existent task."""
        mock_task_manager.get_task.return_value = None

        result = await validation_registry.call("validate_and_fix", {"task_id": "nonexistent"})
        assert "error" in result
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_validate_and_fix_parent_task(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test validate_and_fix for parent task delegates to validate_task."""
        parent_task = Task(
            id="t1",
            title="Parent task",
            project_id="p1",
            status="open",
            priority=2,
            task_type="epic",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = parent_task

        # Has children
        child_task = Task(
            id="c1",
            title="Child",
            project_id="p1",
            status="closed",
            priority=2,
            task_type="task",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.list_tasks.return_value = [child_task]

        result = await validation_registry.call("validate_and_fix", {"task_id": "t1"})

        assert result["success"] is True
        assert result["is_parent_task"] is True

    @pytest.mark.asyncio
    async def test_validate_and_fix_valid_first_try(
        self, mock_task_manager, mock_task_validator, validation_registry
    ):
        """Test validate_and_fix succeeds on first validation."""
        task = Task(
            id="t1",
            title="Already correct task",
            project_id="p1",
            status="in_progress",
            priority=2,
            task_type="task",
            validation_criteria="- Tests pass",
            created_at="now",
            updated_at="now",
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []  # No children (leaf task)

        mock_task_validator.validate_task.return_value = ValidationResult(
            status="valid",
            feedback="All criteria met",
        )

        # Mock git context to avoid real git operations in tests
        # Patch where the function is defined (not where it's imported from)
        with patch("gobby.tasks.validation.get_validation_context_smart") as mock_context:
            mock_context.return_value = "Mocked validation context for test"
            result = await validation_registry.call("validate_and_fix", {"task_id": "t1"})

        assert result["success"] is True
        assert result["is_valid"] is True
        assert result["iterations"] == 1

    def test_validate_and_fix_tool_registered(self, validation_registry) -> None:
        """Test that validate_and_fix is registered."""
        tools = validation_registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "validate_and_fix" in tool_names

    def test_validate_and_fix_schema(self, validation_registry) -> None:
        """Test validate_and_fix has correct input schema."""
        schema = validation_registry.get_schema("validate_and_fix")
        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "task_id" in input_schema["properties"]
        assert "max_retries" in input_schema["properties"]
        assert "auto_fix" in input_schema["properties"]
        assert "fix_timeout" in input_schema["properties"]

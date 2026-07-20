"""Tests for task de-escalation MCP lifecycle behavior."""

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.validation import TaskValidator
from gobby.utils.session_context import session_context_for_test


# de_escalate_task requires an active session context after Change 4 — seed
# one for every test in this module; tests that need to exercise the guard
# itself live in tests/mcp_proxy/tools/test_task_lifecycle_coverage.py.
# Module-level pytestmark removed: per-test @pytest.mark.integration decorators
# conflicted with the old `pytestmark = pytest.mark.unit` annotation.
@pytest.fixture(autouse=True)
def _seed_session_context() -> Generator[None]:
    with session_context_for_test("validation-mcp-tools-session"):
        yield


def _task_like(
    *,
    task_id: str = "t1",
    title: str = "Task",
    project_id: str = "p1",
    is_escalated: bool = False,
    escalated_at: str | None = None,
    escalation_reason: str | None = None,
    validation_fail_count: int = 0,
):
    return SimpleNamespace(
        id=task_id,
        title=title,
        project_id=project_id,
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
        closed_at=None,
        closed_reason=None,
        closed_in_session_id=None,
        closed_commit_sha=None,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
        is_escalated=is_escalated,
        validation_fail_count=validation_fail_count,
        seq_num=None,
        current_stage=SimpleNamespace(name="development", state="ready"),
    )


class Task(SimpleNamespace):
    """Small stage-native stand-in for historical Task(status=...) test data."""

    def __init__(self, **kwargs):
        status = kwargs.pop("status", None)
        if "is_escalated" not in kwargs:
            kwargs["is_escalated"] = status == "escalated"
        if status == "escalated" and kwargs.get("escalated_at") is None:
            kwargs["escalated_at"] = "now"
        kwargs.setdefault("closed_at", "now" if status == "closed" else None)
        kwargs.setdefault("closed_reason", None)
        kwargs.setdefault("closed_in_session_id", None)
        kwargs.setdefault("closed_commit_sha", None)
        kwargs.setdefault("escalated_at", None)
        kwargs.setdefault("escalation_reason", None)
        kwargs.setdefault("validation_fail_count", 0)
        kwargs.setdefault("seq_num", None)
        kwargs.setdefault(
            "current_stage",
            SimpleNamespace(
                name="development",
                state={
                    "in_progress": "in_progress",
                    "needs_review": "needs_review",
                    "review_approved": "review_approved",
                }.get(status, "ready"),
            ),
        )
        super().__init__(**kwargs)


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
def task_registry_with_patches(mock_task_manager, mock_task_validator):
    """Create a full task registry for testing de_escalate_task (now in lifecycle)."""
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
    ):
        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )
        yield registry


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
        escalated_task = _task_like(
            title="Escalated task",
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="max_iterations",
        )
        mock_task_manager.get_task.return_value = escalated_task

        reopened_task = _task_like(
            title="Escalated task",
        )
        mock_task_manager.de_escalate_task.return_value = reopened_task

        result = await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Fixed manually"}
        )

        # Lifecycle version returns empty dict on success
        assert "error" not in result
        mock_task_manager.de_escalate_task.assert_called_once_with(
            "t1",
            reason="Fixed manually",
            reset_validation=False,
            reset_stage_attempts=False,
            restore_stage_from_history=False,
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_requires_reason(
        self, mock_task_manager, task_registry_with_patches
    ) -> None:
        """Test that de_escalate_task requires a reason."""
        escalated_task = _task_like(
            title="Escalated task",
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
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
        non_escalated_task = _task_like(
            title="Normal task",
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
        escalated_task = _task_like(
            title="Escalated task",
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="recurring_issues",
        )
        mock_task_manager.get_task.return_value = escalated_task

        await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Resolved manually"}
        )

        mock_task_manager.de_escalate_task.assert_called_once_with(
            "t1",
            reason="Resolved manually",
            reset_validation=False,
            reset_stage_attempts=False,
            restore_stage_from_history=False,
        )
        assert mock_task_manager.de_escalate_task.call_count == 1
        assert mock_task_manager.de_escalate_task.call_args is not None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_succeeds_with_reason_param(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task succeeds when given a reason."""
        escalated_task = _task_like(
            title="Escalated task",
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="max_iterations",
        )
        mock_task_manager.get_task.return_value = escalated_task

        reopened_task = _task_like(
            title="Escalated task",
        )
        mock_task_manager.update_task.return_value = reopened_task

        result = await task_registry_with_patches.call(
            "de_escalate_task", {"task_id": "t1", "reason": "Human fixed the issue"}
        )

        # Lifecycle version returns empty dict on success; verify the call went through
        assert "error" not in result
        mock_task_manager.de_escalate_task.assert_called_once_with(
            "t1",
            reason="Human fixed the issue",
            reset_validation=False,
            reset_stage_attempts=False,
            restore_stage_from_history=False,
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_resets_validation_state(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task optionally resets validation state."""
        escalated_task = _task_like(
            title="Escalated task",
            validation_fail_count=10,
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="max_iterations",
        )
        mock_task_manager.get_task.return_value = escalated_task

        await task_registry_with_patches.call(
            "de_escalate_task",
            {"task_id": "t1", "reason": "Fixed", "reset_validation": True},
        )

        mock_task_manager.de_escalate_task.assert_called_once_with(
            "t1",
            reason="Fixed",
            reset_validation=True,
            reset_stage_attempts=False,
            restore_stage_from_history=False,
        )
        assert mock_task_manager.de_escalate_task.call_count == 1
        assert mock_task_manager.de_escalate_task.call_args is not None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_resets_stage_attempts(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task optionally resets current stage attempts."""
        escalated_task = _task_like(
            title="Escalated task",
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="development_max_work_attempts",
        )
        mock_task_manager.get_task.return_value = escalated_task

        result = await task_registry_with_patches.call(
            "de_escalate_task",
            {"task_id": "t1", "reason": "Fixed", "reset_stage_attempts": True},
        )

        assert "error" not in result
        mock_task_manager.de_escalate_task.assert_called_once_with(
            "t1",
            reason="Fixed",
            reset_validation=False,
            reset_stage_attempts=True,
            restore_stage_from_history=False,
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_restores_stage_from_history(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task can request stage restoration from history."""
        escalated_task = _task_like(
            title="Escalated task",
            is_escalated=True,
            escalated_at="2024-01-01T00:00:00",
            escalation_reason="expansion_work_failed:max",
        )
        mock_task_manager.get_task.return_value = escalated_task

        result = await task_registry_with_patches.call(
            "de_escalate_task",
            {
                "task_id": "t1",
                "reason": "Fixed",
                "restore_stage_from_history": True,
            },
        )

        assert "error" not in result
        mock_task_manager.de_escalate_task.assert_called_once_with(
            "t1",
            reason="Fixed",
            reset_validation=False,
            reset_stage_attempts=False,
            restore_stage_from_history=True,
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_de_escalate_task_schema_has_no_explicit_stage_target(
        self, mock_task_manager, task_registry_with_patches
    ):
        """Test that de_escalate_task no longer accepts explicit state routing."""
        schema = task_registry_with_patches.get_schema("de_escalate_task")
        assert schema is not None

        properties = schema["inputSchema"]["properties"]
        assert "target_state" not in properties
        assert "restore_stage_from_history" in properties


# ============================================================================
# Tool Registration Tests
# ============================================================================


class TestValidationToolsRegistration:
    """Tests verifying validation tools are properly registered."""

    @pytest.mark.integration
    def test_de_escalate_task_tool_registered(self, task_registry_with_patches) -> None:
        """Test that de_escalate_task is registered on gobby-tasks (lifecycle)."""
        tools = task_registry_with_patches.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "de_escalate_task" in tool_names

    @pytest.mark.integration
    def test_de_escalate_task_tool_schema(self, task_registry_with_patches) -> None:
        """Test that de_escalate_task has correct input schema."""
        schema = task_registry_with_patches.get_schema("de_escalate_task")

        assert schema is not None
        input_schema = schema.get("inputSchema", schema)
        assert "properties" in input_schema
        assert "task_id" in input_schema["properties"]
        assert "reason" in input_schema["properties"]
        assert "reset_stage_attempts" in input_schema["properties"]
        # Both should be required
        assert "task_id" in input_schema.get("required", [])
        assert "reason" in input_schema.get("required", [])

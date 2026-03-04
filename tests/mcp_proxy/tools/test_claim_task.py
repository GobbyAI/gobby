"""
Tests for claim_task MCP tool.

The claim_task tool provides a semantic way to claim ownership of a task,
combining:
1. Setting the assignee to the current session
2. Marking the task as in_progress
3. Linking the task to the session

This follows the pattern established by claim_worktree in worktrees.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager():
    """Create a mock sync manager."""
    return MagicMock(spec=TaskSyncManager)


@pytest.fixture
def sample_task():
    """Create a sample unclaimed task."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Test Task",
        status="open",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Test description",
        labels=["test"],
        assignee=None,  # Unclaimed
    )


@pytest.fixture
def claimed_task():
    """Create a sample task already claimed by another session."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440001",
        project_id="proj-1",
        title="Claimed Task",
        status="in_progress",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Already claimed",
        labels=[],
        assignee="other-session-id",  # Claimed by another session
    )


class TestClaimTaskTool:
    """Tests for the claim_task MCP tool."""

    @pytest.mark.asyncio
    async def test_claim_task_success(self, mock_task_manager, mock_sync_manager, sample_task):
        """Test successfully claiming an unclaimed task."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "my-session-id"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = sample_task
            updated_task = MagicMock()
            updated_task.id = sample_task.id
            updated_task.status = "in_progress"
            updated_task.assignee = "my-session-id"
            mock_task_manager.update_task.return_value = updated_task

            result = await registry.call(
                "claim_task",
                {
                    "task_id": sample_task.id,
                    "session_id": "my-session-id",
                },
            )

            # Should succeed
            assert "error" not in result
            # Should update task with assignee and status
            mock_task_manager.update_task.assert_called_once_with(
                sample_task.id,
                assignee="my-session-id",
                status="in_progress",
            )
            # Should link task to session (best-effort)
            mock_st_instance.link_task.assert_called_once_with(
                "my-session-id", sample_task.id, "claimed"
            )

    @pytest.mark.asyncio
    async def test_claim_task_already_claimed_by_another_session(
        self, mock_task_manager, mock_sync_manager, claimed_task
    ):
        """Test claiming a task already claimed by another session fails without force."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.get_task.return_value = claimed_task

        result = await registry.call(
            "claim_task",
            {
                "task_id": claimed_task.id,
                "session_id": "my-session-id",
            },
        )

        # Should fail with error about existing claim
        assert "error" in result
        assert (
            "already claimed" in result["error"].lower()
            or "claimed by" in result.get("message", "").lower()
        )
        # Should include info about who claimed it
        assert result.get("claimed_by") == "other-session-id" or "other-session-id" in str(result)

    @pytest.mark.asyncio
    async def test_claim_task_force_override_existing_claim(
        self, mock_task_manager, mock_sync_manager, claimed_task
    ):
        """Test claiming a task with force=True overrides existing claim."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "my-session-id"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = claimed_task
            updated_task = MagicMock()
            updated_task.id = claimed_task.id
            updated_task.status = "in_progress"
            updated_task.assignee = "my-session-id"
            mock_task_manager.update_task.return_value = updated_task

            result = await registry.call(
                "claim_task",
                {
                    "task_id": claimed_task.id,
                    "session_id": "my-session-id",
                    "force": True,
                },
            )

            # Should succeed with force=True
            assert "error" not in result
            mock_task_manager.update_task.assert_called_once_with(
                claimed_task.id,
                assignee="my-session-id",
                status="in_progress",
            )

    @pytest.mark.asyncio
    async def test_claim_task_already_claimed_by_same_session(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test claiming a task already claimed by the same session succeeds (idempotent)."""
        task_claimed_by_self = Task(
            id="550e8400-e29b-41d4-a716-446655440002",
            project_id="proj-1",
            title="My Task",
            status="in_progress",
            priority=2,
            task_type="task",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            assignee="my-session-id",  # Same session
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "my-session-id"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = task_claimed_by_self
            mock_task_manager.update_task.return_value = task_claimed_by_self

            result = await registry.call(
                "claim_task",
                {
                    "task_id": task_claimed_by_self.id,
                    "session_id": "my-session-id",
                },
            )

            # Should succeed (idempotent operation)
            assert "error" not in result

    @pytest.mark.asyncio
    async def test_claim_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test claiming a non-existent task returns error."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.get_task.return_value = None

        result = await registry.call(
            "claim_task",
            {
                "task_id": "00000000-0000-0000-0000-000000000000",
                "session_id": "my-session-id",
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_claim_task_resolves_task_reference(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test claim_task resolves #N format task references."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            # Mock get_task to return the sample task when called with resolved UUID
            mock_task_manager.get_task.return_value = sample_task
            mock_task_manager.update_task.return_value = sample_task

            # Mock the task resolution to return a UUID from #42 format
            with patch("gobby.mcp_proxy.tools.tasks._crud.resolve_task_id_for_mcp") as mock_resolve:
                mock_resolve.return_value = sample_task.id

                result = await registry.call(
                    "claim_task",
                    {
                        "task_id": "#42",  # Reference format
                        "session_id": "my-session-id",
                    },
                )

                # Should succeed with reference format
                assert "error" not in result

    @pytest.mark.asyncio
    async def test_claim_task_missing_session_id(self, mock_task_manager, mock_sync_manager):
        """Test claim_task requires session_id parameter."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        # Call without session_id - should error or fail validation
        # The tool should require session_id in its schema
        tools = registry.list_tools()
        claim_tool_schema = next(
            (t for t in tools if t["name"] == "claim_task"),
            None,
        )

        # Verify claim_task tool exists
        assert claim_tool_schema is not None, "claim_task tool not registered"

        # Verify session_id is in required fields
        schema = registry.get_schema("claim_task")
        assert "session_id" in schema["inputSchema"]["required"]

    @pytest.mark.asyncio
    async def test_claim_task_session_link_failure_does_not_fail_claim(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test that session link failure doesn't fail the overall claim (best-effort linking)."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            # Session linking fails
            mock_st_instance.link_task.side_effect = Exception("Session link failed")
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = sample_task
            mock_task_manager.update_task.return_value = sample_task

            result = await registry.call(
                "claim_task",
                {
                    "task_id": sample_task.id,
                    "session_id": "my-session-id",
                },
            )

            # Should still succeed even though session link failed
            assert "error" not in result
            # Task update should still happen
            mock_task_manager.update_task.assert_called_once()


class TestClaimTaskSchema:
    """Tests for claim_task tool schema."""

    def test_claim_task_registered_in_registry(self, mock_task_manager, mock_sync_manager) -> None:
        """Test that claim_task is registered in the task registry."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "claim_task" in tool_names, "claim_task tool not registered"

    def test_claim_task_schema_has_required_fields(
        self, mock_task_manager, mock_sync_manager
    ) -> None:
        """Test claim_task schema includes required fields."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        schema = registry.get_schema("claim_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        # Required parameters
        assert "task_id" in props
        assert "session_id" in props

        # Optional parameters
        assert "force" in props
        assert props["force"].get("default") is False

        # Required fields
        assert "task_id" in schema["inputSchema"]["required"]
        assert "session_id" in schema["inputSchema"]["required"]

    def test_claim_task_schema_has_description(self, mock_task_manager, mock_sync_manager) -> None:
        """Test claim_task has helpful description."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        schema = registry.get_schema("claim_task")

        assert schema is not None
        # Should have a description that explains the tool's purpose
        assert "description" in schema
        description = schema["description"].lower()
        # Description should mention key behaviors
        assert "claim" in description or "assignee" in description or "in_progress" in description


class TestClaimTaskSessionVariables:
    """Tests for claim_task setting task_claimed session variable."""

    @pytest.mark.asyncio
    async def test_claim_task_sets_task_claimed_via_session_variables(
        self, mock_task_manager, mock_sync_manager, sample_task
    ) -> None:
        """claim_task must set task_claimed via session_var_manager.merge_variables.

        Regression test for #8642: PreToolUse hook blocked edits despite claimed task
        because task_claimed was never persisted.
        """
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "my-session-id"
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {}
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            sample_task.seq_num = 42
            mock_task_manager.get_task.return_value = sample_task
            mock_task_manager.update_task.return_value = sample_task

            result = await registry.call(
                "claim_task",
                {"task_id": sample_task.id, "session_id": "my-session-id"},
            )

            assert "error" not in result

            # merge_variables must have been called with task_claimed
            mock_sv_manager.merge_variables.assert_called_once()
            call_args = mock_sv_manager.merge_variables.call_args
            assert call_args[0][0] == "my-session-id"
            merged_vars = call_args[0][1]
            assert merged_vars["task_claimed"] is True
            assert merged_vars["claimed_tasks"].get(sample_task.id) == "#42"


class TestClaimTaskVsUpdateTask:
    """Tests demonstrating why claim_task provides value over update_task."""

    @pytest.mark.asyncio
    async def test_claim_task_is_atomic_operation(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test that claim_task atomically sets assignee and status together."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "my-session-id"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = sample_task
            mock_task_manager.update_task.return_value = sample_task

            await registry.call(
                "claim_task",
                {
                    "task_id": sample_task.id,
                    "session_id": "my-session-id",
                },
            )

            # Both assignee and status should be set in a single update call
            # (atomic operation, not two separate calls)
            mock_task_manager.update_task.assert_called_once()
            call_kwargs = mock_task_manager.update_task.call_args.kwargs
            assert "assignee" in call_kwargs
            assert "status" in call_kwargs
            assert call_kwargs["assignee"] == "my-session-id"
            assert call_kwargs["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_claim_task_detects_conflicts(
        self, mock_task_manager, mock_sync_manager, claimed_task
    ):
        """Test that claim_task detects conflicts before modifying (unlike raw update_task)."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.get_task.return_value = claimed_task

        result = await registry.call(
            "claim_task",
            {
                "task_id": claimed_task.id,
                "session_id": "my-session-id",
            },
        )

        # Should detect conflict and not proceed with update
        assert "error" in result
        # update_task should NOT have been called because conflict was detected first
        mock_task_manager.update_task.assert_not_called()

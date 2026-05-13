"""Focused coverage tests for task MCP tools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry

pytestmark = pytest.mark.unit


class TestGetTaskTool:
    """Tests for get_task MCP tool."""

    @pytest.mark.asyncio
    async def test_get_task_found(self, mock_task_manager, mock_sync_manager, sample_task):
        """Test get_task returns task with dependencies."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            mock_dep_instance.get_blockers.return_value = []
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call("get_task", {"task_id": sample_task.id})

            assert result["id"] == sample_task.id
            assert result["title"] == "Test Task"
            assert "state" in result
            assert result["state"]["is_blocked"] is False
            assert "dependencies" in result
            assert "blocked_by" in result["dependencies"]
            assert "blocking" in result["dependencies"]

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test get_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.get_task.return_value = None

        result = await registry.call(
            "get_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_get_task_with_dependencies(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test get_task with brief=False includes full dependency information."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()

            # Create mock blocker and blocking dependencies
            mock_blocker = MagicMock()
            mock_blocker.to_dict.return_value = {
                "from_task": "550e8400-e29b-41d4-a716-446655440001",
                "type": "blocks",
            }

            mock_blocking = MagicMock()
            mock_blocking.to_dict.return_value = {
                "from_task": "550e8400-e29b-41d4-a716-446655440000",
                "type": "blocks",
            }

            mock_dep_instance.get_blockers.return_value = [mock_blocker]
            mock_dep_instance.get_blocking.return_value = [mock_blocking]
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call(
                "get_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000", "brief": False},
            )

            assert len(result["dependencies"]["blocked_by"]) == 1
            assert len(result["dependencies"]["blocking"]) == 1

    @pytest.mark.asyncio
    async def test_get_task_brief_excludes_description(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test get_task with brief=True (default) excludes heavy fields."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            mock_dep_instance.get_blockers.return_value = []
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)
            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call("get_task", {"task_id": sample_task.id})

            assert result["id"] == sample_task.id
            assert result["title"] == "Test Task"
            assert "description" not in result
            assert "validation_criteria" not in result
            assert "expansion_context" not in result
            assert "dependencies" in result

    @pytest.mark.asyncio
    async def test_get_task_brief_false_returns_full(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test get_task with brief=False returns full format."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            mock_dep_instance.get_blockers.return_value = []
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)
            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call("get_task", {"task_id": sample_task.id, "brief": False})

            assert result["id"] == sample_task.id
            assert "state" in result
            assert "description" in result
            assert "validation_criteria" in result
            assert "dependencies" in result

    @pytest.mark.asyncio
    async def test_get_task_brief_resolves_dependencies(
        self, mock_task_manager, mock_sync_manager, sample_task
    ):
        """Test get_task brief mode resolves deps to ref+title+state."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()

            mock_blocker = MagicMock()
            mock_blocker.depends_on = "550e8400-e29b-41d4-a716-446655440001"
            mock_blocker.dep_type = "blocks"

            mock_dep_instance.get_blockers.return_value = [mock_blocker]
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            # Create a linked task that the blocker resolves to
            linked_task = MagicMock()
            linked_task.seq_num = 42
            linked_task.title = "Blocking Task"
            linked_task.id = "550e8400-e29b-41d4-a716-446655440001"
            linked_state = {"current_stage": {"name": "implementation", "state": "in_progress"}}
            linked_task.to_brief.return_value = {"state": linked_state}

            def get_task_side_effect(task_id):
                if task_id == sample_task.id:
                    return sample_task
                if task_id == "550e8400-e29b-41d4-a716-446655440001":
                    return linked_task
                return None

            mock_task_manager.get_task.side_effect = get_task_side_effect

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call("get_task", {"task_id": sample_task.id})

            blocked_by = result["dependencies"]["blocked_by"]
            assert len(blocked_by) == 1
            assert blocked_by[0]["ref"] == "#42"
            assert blocked_by[0]["title"] == "Blocking Task"
            assert blocked_by[0]["state"] == linked_state
            assert blocked_by[0]["dep_type"] == "blocks"


# =============================================================================
# update_task Tool Tests
# =============================================================================


class TestUpdateTaskTool:
    """Tests for update_task MCP tool."""

    @pytest.mark.asyncio
    async def test_update_task_title(self, mock_task_manager, mock_sync_manager, sample_task):
        """Test update_task updates title."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "title": "Updated Title"},
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", title="Updated Title"
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test update_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.update_task.return_value = None

        result = await registry.call(
            "update_task", {"task_id": "00000000-0000-0000-0000-000000000000", "title": "New Title"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_task_all_fields(self, mock_task_manager, mock_sync_manager):
        """Test update_task with all updatable fields.

        Note: All status transitions are blocked by production code.
        Must use claim_task, close_task, reopen_task, or submit_for_review.
        """
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        updated_task.to_brief.return_value = {"id": "550e8400-e29b-41d4-a716-446655440000"}
        mock_task_manager.update_task.return_value = updated_task

        await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "title": "New Title",
                "description": "New Description",
                # "status" is blocked for all values - must use lifecycle tools
                "priority": 1,
                # "assignee" is blocked - must use claim_task
                "labels": ["urgent"],
                "validation_criteria": "Must pass",
                "parent_task_id": "550e8400-e29b-41d4-a716-446655440010",
                "category": "automated",
                "task_type": "epic",
                "workflow_name": "dev-flow",
                "verification": "Run tests",
                "sequence_order": 5,
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000",
            title="New Title",
            description="New Description",
            priority=1,
            labels=["urgent"],
            validation_criteria="Must pass",
            parent_task_id="550e8400-e29b-41d4-a716-446655440010",
            category="automated",
            task_type="epic",
            workflow_name="dev-flow",
            verification="Run tests",
            sequence_order=5,
        )
        assert mock_task_manager.update_task.call_count >= 1
        assert mock_task_manager.update_task.call_args is not None

    @pytest.mark.asyncio
    async def test_update_task_task_type(self, mock_task_manager, mock_sync_manager):
        """update_task forwards task_type to the storage layer."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "task_type": "epic"},
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", task_type="epic"
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_allow_automation(self, mock_task_manager, mock_sync_manager):
        """update_task forwards allow_automation to the storage layer."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "allow_automation": False,
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", allow_automation=False
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_isolation(self, mock_task_manager, mock_sync_manager):
        """update_task forwards validated isolation to the storage layer."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "isolation": "none",
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", isolation="none"
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_rejects_isolation_artifact_conflict(
        self, mock_task_manager, mock_sync_manager
    ):
        """update_task rejects retargeting to a conflicting isolation artifact family."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        mock_task_manager.artifacts.get_artifacts.return_value = MagicMock(
            worktree_path="/tmp/gobby-worktree",
            clone_path=None,
        )

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "isolation": "clone",
            },
        )

        assert result == {
            "error": (
                "task already has a worktree artifact; clear existing build artifacts "
                "before switching to clone isolation"
            )
        }
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_assigned_agent(self, mock_task_manager, mock_sync_manager):
        """update_task forwards assigned_agent to the storage layer."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "assigned_agent": "backend-developer",
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", assigned_agent="backend-developer"
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_additional_skills(self, mock_task_manager, mock_sync_manager):
        """update_task forwards additional_skills to the storage layer."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "additional_skills": ["tech-writer"],
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", additional_skills=["tech-writer"]
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_assignment_fields_combined(
        self, mock_task_manager, mock_sync_manager
    ):
        """update_task forwards both assignment fields together."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.update_task.return_value = updated_task

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "assigned_agent": "backend-developer",
                "additional_skills": ["tech-writer", "code-index"],
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000",
            assigned_agent="backend-developer",
            additional_skills=["tech-writer", "code-index"],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_blocks_open_status(self, mock_task_manager, mock_sync_manager):
        """Test update_task blocks 'open' status changes."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "status": "open"},
        )

        assert "error" in result
        assert "Cannot set status to 'open'" in result["error"]
        assert "reopen_task" in result["error"]
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_blocks_review_status(self, mock_task_manager, mock_sync_manager):
        """Test update_task blocks 'review' status changes."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "status": "review"},
        )

        assert "error" in result
        assert "Cannot set status to 'needs_review'" in result["error"]
        assert "submit_for_review" in result["error"]
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_blocks_needs_review_status(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test update_task blocks 'needs_review' status changes."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "status": "needs_review"},
        )

        assert "error" in result
        assert "Cannot set status to 'needs_review'" in result["error"]
        assert "submit_for_review" in result["error"]
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_blocks_escalated_status(self, mock_task_manager, mock_sync_manager):
        """Test update_task blocks 'escalated' status changes."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "status": "escalated"},
        )

        assert "error" in result
        assert "Cannot set status to 'escalated'" in result["error"]
        assert "escalate_task" in result["error"]
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_partial_update(self, mock_task_manager, mock_sync_manager):
        """Test update_task only includes provided fields.

        Note: status='closed' is blocked (must use close_task),
        and status='in_progress' is blocked (must use claim_task).
        """
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        updated_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "title": "Updated Title",
        }
        mock_task_manager.update_task.return_value = updated_task

        await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "title": "Updated Title"},
        )

        # Should only include title, not other None values
        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", title="Updated Title"
        )
        assert mock_task_manager.update_task.call_count >= 1
        assert mock_task_manager.update_task.call_args is not None


# =============================================================================
# add_label and remove_label Tool Tests
# =============================================================================


class TestLabelTools:
    """Tests for add_label and remove_label MCP tools."""

    @pytest.mark.asyncio
    async def test_add_label_success(self, mock_task_manager, mock_sync_manager, sample_task):
        """Test add_label adds a label to task."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.add_label.return_value = updated_task

        result = await registry.call(
            "add_label", {"task_id": "550e8400-e29b-41d4-a716-446655440000", "label": "new"}
        )

        mock_task_manager.add_label.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", "new"
        )
        assert result == {"success": True, "task_id": "550e8400-e29b-41d4-a716-446655440000"}

    @pytest.mark.asyncio
    async def test_add_label_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test add_label returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.add_label.return_value = None

        result = await registry.call(
            "add_label", {"task_id": "00000000-0000-0000-0000-000000000000", "label": "new"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_remove_label_success(self, mock_task_manager, mock_sync_manager):
        """Test remove_label removes a label from task."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        updated_task = MagicMock()
        mock_task_manager.remove_label.return_value = updated_task

        result = await registry.call(
            "remove_label", {"task_id": "550e8400-e29b-41d4-a716-446655440000", "label": "old"}
        )

        mock_task_manager.remove_label.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", "old"
        )
        assert result == {"success": True, "task_id": "550e8400-e29b-41d4-a716-446655440000"}

    @pytest.mark.asyncio
    async def test_remove_label_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test remove_label returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.remove_label.return_value = None

        result = await registry.call(
            "remove_label", {"task_id": "00000000-0000-0000-0000-000000000000", "label": "old"}
        )

        assert "error" in result


# =============================================================================
# close_task Tool Tests
# =============================================================================


class TestDeleteTaskTool:
    """Tests for delete_task MCP tool."""

    @pytest.mark.asyncio
    async def test_delete_task_success(self, mock_task_manager, mock_sync_manager):
        """Test delete_task successfully deletes a task."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.delete_task.return_value = True

        result = await registry.call(
            "delete_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )

        mock_task_manager.delete_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", cascade=True, unlink=False
        )
        assert "error" not in result
        assert result["deleted_task_id"] == "550e8400-e29b-41d4-a716-446655440000"

    @pytest.mark.asyncio
    async def test_delete_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test delete_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.delete_task.return_value = False

        result = await registry.call(
            "delete_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_task_without_cascade(self, mock_task_manager, mock_sync_manager):
        """Test delete_task without cascade option."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.delete_task.return_value = True

        await registry.call(
            "delete_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000", "cascade": False}
        )

        mock_task_manager.delete_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", cascade=False, unlink=False
        )
        assert mock_task_manager.delete_task.call_count >= 1
        assert mock_task_manager.delete_task.call_args is not None

    @pytest.mark.asyncio
    async def test_delete_task_with_unlink(self, mock_task_manager, mock_sync_manager):
        """Test delete_task with unlink option preserves dependents."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.delete_task.return_value = True

        result = await registry.call(
            "delete_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "cascade": False, "unlink": True},
        )

        mock_task_manager.delete_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", cascade=False, unlink=True
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_delete_task_dependents_error(self, mock_task_manager, mock_sync_manager):
        """Test delete_task returns structured error when task has dependents."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        from gobby.storage.tasks._models import TaskHasDependentsError

        mock_task_manager.delete_task.side_effect = TaskHasDependentsError(
            "Task abc has 2 dependent task(s): #1, #2. Use cascade or unlink."
        )

        result = await registry.call(
            "delete_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000", "cascade": False}
        )

        assert result["error"] == "has_dependents"
        assert "suggestion" in result


# =============================================================================
# list_tasks Tool Tests
# =============================================================================


class TestListTasksTool:
    """Tests for list_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_list_tasks_basic(self, mock_task_manager, mock_sync_manager):
        """Test list_tasks returns tasks with count."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task1 = MagicMock()
        mock_task1.to_brief.return_value = {"id": "t1", "title": "Task 1"}
        mock_task2 = MagicMock()
        mock_task2.to_brief.return_value = {"id": "t2", "title": "Task 2"}

        mock_task_manager.list_tasks.return_value = [mock_task1, mock_task2]

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call("list_tasks", {})

            assert result["count"] == 2
            assert len(result["tasks"]) == 2

    @pytest.mark.asyncio
    async def test_list_tasks_with_filters(self, mock_task_manager, mock_sync_manager):
        """Test list_tasks applies filters correctly."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "list_tasks",
                {
                    "current_stage_state": "ready",
                    "priority": 1,
                    "task_type": "bug",
                    "assignee": "dev",
                    "label": "urgent",
                    "parent_task_id": "550e8400-e29b-41d4-a716-446655440010",
                    "title_like": "feature",
                    "limit": 10,
                },
            )

            mock_task_manager.list_tasks.assert_called_with(
                current_stage_state="ready",
                priority=1,
                task_type="bug",
                assignee="dev",
                label="urgent",
                parent_task_id="550e8400-e29b-41d4-a716-446655440010",
                title_like="feature",
                limit=10,
                project_id="proj-1",
            )
            assert mock_task_manager.list_tasks.call_count >= 1
            assert mock_task_manager.list_tasks.call_args is not None

    @pytest.mark.asyncio
    async def test_list_tasks_all_projects(self, mock_task_manager, mock_sync_manager):
        """Test list_tasks with all_projects=True ignores project filter."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call("list_tasks", {"all_projects": True})

            call_kwargs = mock_task_manager.list_tasks.call_args.kwargs
            assert call_kwargs["project_id"] is None

    @pytest.mark.asyncio
    async def test_list_tasks_comma_separated_status(self, mock_task_manager, mock_sync_manager):
        """Test list_tasks handles comma-separated current_stage_state strings."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call("list_tasks", {"current_stage_state": "ready,in_progress"})

            call_kwargs = mock_task_manager.list_tasks.call_args.kwargs
            assert call_kwargs["current_stage_state"] == ["ready", "in_progress"]


# =============================================================================
# Session Integration Tool Tests
# =============================================================================

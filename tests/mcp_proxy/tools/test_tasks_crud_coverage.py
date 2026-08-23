"""Focused coverage tests for task MCP tools."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit

COMPACT_STATE_KEYS = {
    "current_stage",
    "is_closed",
    "closed_at",
    "is_claimed",
    "is_blocked",
    "is_escalated",
}

DISCOVERY_TASK_KEYS = {
    "ref",
    "id",
    "seq_num",
    "title",
    "task_type",
    "category",
    "priority",
    "path_cache",
    "updated_at",
    "state",
}

SUMMARY_TASK_KEYS = {
    "ref",
    "id",
    "seq_num",
    "title",
    "task_type",
    "category",
    "priority",
    "path_cache",
    "description",
    "validation_criteria",
    "labels",
    "parent_task_id",
    "created_at",
    "updated_at",
    "state",
    "dependencies",
    "allow_automation",
    "unattended",
    "isolation",
    "assigned_agent",
    "implementation_domain",
    "additional_skills",
}

NOISY_TASK_KEYS = {
    "claimed_by_session_id",
    "closed_in_session_id",
    "closed_commit_sha",
    "validation_status",
    "validation_feedback",
    "validation_fail_count",
    "dispatch_failure_count",
    "validation_override_reason",
    "merge_in_progress",
    "blocked_by_merge",
    "commits",
    "github_issue_number",
    "github_pr_number",
    "github_repo",
    "linear_issue_id",
    "linear_team_id",
}


class TestGetTaskTool:
    """Tests for get_task MCP tool."""

    @pytest.mark.asyncio
    async def test_get_task_found(self, mock_task_manager: MagicMock, sample_task: Task) -> None:
        """Test get_task returns task with dependencies."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            mock_dep_instance.get_blockers.return_value = []
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager)

            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call("get_task", {"task_id": sample_task.id})

            assert result["id"] == sample_task.id
            assert result["title"] == "Test Task"
            assert result["description"] == "Test description"
            assert "state" in result
            assert result["state"]["is_blocked"] is False
            assert "dependencies" in result
            assert "blocked_by" in result["dependencies"]
            assert "blocking" in result["dependencies"]

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, mock_task_manager: MagicMock) -> None:
        """Test get_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.get_task.return_value = None

        result = await registry.call(
            "get_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_get_task_with_dependencies(
        self, mock_task_manager: MagicMock, sample_task: Task
    ) -> None:
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

            registry = create_task_registry(mock_task_manager)

            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call(
                "get_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000", "brief": False},
            )

            assert len(result["dependencies"]["blocked_by"]) == 1
            assert len(result["dependencies"]["blocking"]) == 1
            assert result["dependencies"]["blocked_by"][0] == {
                "from_task": "550e8400-e29b-41d4-a716-446655440001",
                "type": "blocks",
            }
            assert result["dependencies"]["blocking"][0] == {
                "from_task": "550e8400-e29b-41d4-a716-446655440000",
                "type": "blocks",
            }

    @pytest.mark.asyncio
    async def test_get_task_brief_returns_agent_task_card(
        self, mock_task_manager: MagicMock, sample_task: Task
    ) -> None:
        """Test get_task with brief=True returns concise actionable fields."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            mock_dep_instance.get_blockers.return_value = []
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager)
            sample_task.validation_criteria = "Run focused task MCP validation"
            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call("get_task", {"task_id": sample_task.id})

            assert set(result) == SUMMARY_TASK_KEYS
            assert result["id"] == sample_task.id
            assert result["title"] == "Test Task"
            assert result["description"] == "Test description"
            assert result["validation_criteria"] == "Run focused task MCP validation"
            assert set(result["state"]) == COMPACT_STATE_KEYS
            assert NOISY_TASK_KEYS.isdisjoint(result)
            assert "expansion_context" not in result
            assert "dependencies" in result

    @pytest.mark.asyncio
    async def test_get_task_brief_false_returns_full(
        self, mock_task_manager: MagicMock, sample_task: Task
    ) -> None:
        """Test get_task with brief=False returns full format."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            mock_dep_instance.get_blockers.return_value = []
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager)
            mock_task_manager.get_task.return_value = sample_task

            result = await registry.call("get_task", {"task_id": sample_task.id, "brief": False})

            assert result["id"] == sample_task.id
            assert "state" in result
            assert "description" in result
            assert "validation_criteria" in result
            assert "dependencies" in result

    @pytest.mark.asyncio
    async def test_get_task_brief_resolves_dependencies(
        self, mock_task_manager: MagicMock, sample_task: Task
    ) -> None:
        """Test get_task brief mode resolves deps to ref+title+state."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()

            mock_blocker = MagicMock()
            mock_blocker.depends_on = "550e8400-e29b-41d4-a716-446655440001"
            mock_blocker.dep_type = "blocks"

            mock_dep_instance.get_blockers.return_value = [mock_blocker]
            mock_dep_instance.get_blocking.return_value = []
            MockDepManager.return_value = mock_dep_instance

            # Create a linked task that the blocker resolves to.
            linked_stage = {"name": "implementation", "state": "in_progress"}
            linked_task = SimpleNamespace(
                id="550e8400-e29b-41d4-a716-446655440001",
                seq_num=42,
                title="Blocking Task",
                current_stage=linked_stage,
            )

            def get_task_side_effect(task_id):
                if task_id == sample_task.id:
                    return sample_task
                if task_id == "550e8400-e29b-41d4-a716-446655440001":
                    return linked_task
                return None

            mock_task_manager.get_task.side_effect = get_task_side_effect

            registry = create_task_registry(mock_task_manager)

            result = await registry.call("get_task", {"task_id": sample_task.id})

            blocked_by = result["dependencies"]["blocked_by"]
            assert len(blocked_by) == 1
            assert blocked_by[0]["ref"] == "#42"
            assert blocked_by[0]["id"] == "550e8400-e29b-41d4-a716-446655440001"
            assert blocked_by[0]["title"] == "Blocking Task"
            assert blocked_by[0]["state"]["current_stage"] == linked_stage
            assert set(blocked_by[0]["state"]) == COMPACT_STATE_KEYS
            assert blocked_by[0]["dep_type"] == "blocks"


# =============================================================================
# update_task Tool Tests
# =============================================================================


class TestUpdateTaskTool:
    """Tests for update_task MCP tool."""

    @pytest.fixture(autouse=True)
    def _existing_task_has_contract(self, mock_task_manager: MagicMock) -> None:
        mock_task_manager.get_task.return_value = SimpleNamespace(
            task_type="task",
            category="research",
            validation_criteria="The requested task metadata is stored.",
            implementation_domain=None,
            is_escalated=False,
        )

    @pytest.mark.asyncio
    async def test_update_task_title(self, mock_task_manager: MagicMock, sample_task: Task) -> None:
        """Test update_task updates title."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_update_task_not_found(self, mock_task_manager: MagicMock) -> None:
        """Test update_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.update_task.return_value = None

        result = await registry.call(
            "update_task", {"task_id": "00000000-0000-0000-0000-000000000000", "title": "New Title"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_task_rejects_code_category_without_criteria(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A category flip must satisfy the effective code-task invariant."""
        registry = create_task_registry(mock_task_manager)
        mock_task_manager.get_task.return_value = SimpleNamespace(
            task_type="task",
            category="manual",
            validation_criteria=None,
            implementation_domain=None,
        )

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "category": "code"},
        )

        assert "Every non-epic task requires nonempty validation_criteria" in result["error"]
        assert mock_task_manager.get_task.call_count == 2
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_rejects_code_category_without_domain(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A category flip with criteria must still provide an implementation domain."""
        registry = create_task_registry(mock_task_manager)
        mock_task_manager.get_task.return_value = SimpleNamespace(
            task_type="task",
            category="manual",
            validation_criteria=None,
            implementation_domain=None,
        )

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "category": "code",
                "validation_criteria": "Focused tests pass",
            },
        )

        assert result == {
            "error": "Code tasks require implementation_domain "
            "('backend', 'frontend', or 'fullstack')."
        }
        assert mock_task_manager.get_task.call_count == 2
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_rejects_clearing_code_task_criteria(
        self, mock_task_manager: MagicMock
    ) -> None:
        """An existing code task cannot clear its validation criteria."""
        registry = create_task_registry(mock_task_manager)
        mock_task_manager.get_task.return_value = SimpleNamespace(
            task_type="task",
            category="code",
            validation_criteria="Focused tests pass",
            implementation_domain="backend",
        )

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "validation_criteria": "",
            },
        )

        assert "Every non-epic task requires nonempty validation_criteria" in result["error"]
        assert mock_task_manager.get_task.call_count == 2
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_accepts_valid_code_category_flip(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A category flip succeeds when the effective code-task state is valid."""
        registry = create_task_registry(mock_task_manager)
        mock_task_manager.get_task.return_value = SimpleNamespace(
            task_type="task",
            category="manual",
            validation_criteria=None,
            implementation_domain=None,
        )
        mock_task_manager.update_task.return_value = MagicMock()

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "category": "code",
                "validation_criteria": "Focused tests pass",
                "implementation_domain": "backend",
            },
        )

        assert result == {}
        assert mock_task_manager.get_task.call_count == 2
        mock_task_manager.update_task.assert_called_once_with(
            "550e8400-e29b-41d4-a716-446655440000",
            category="code",
            validation_criteria="Focused tests pass",
            implementation_domain="backend",
        )

    @pytest.mark.asyncio
    async def test_update_task_metadata_fields(self, mock_task_manager: MagicMock) -> None:
        """Test update_task with representative metadata fields.

        Note: All status transitions are blocked by production code.
        Must use claim_task, close_task, reopen_task, or submit_for_review.
        """
        registry = create_task_registry(mock_task_manager)

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
                "labels": ["urgent"],
                "validation_criteria": "Must pass",
                "parent_task_id": "550e8400-e29b-41d4-a716-446655440010",
                "category": "automated",
                "task_type": "epic",
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
        )
        assert mock_task_manager.update_task.call_count >= 1
        assert mock_task_manager.update_task.call_args is not None

    @pytest.mark.asyncio
    async def test_update_task_task_type(self, mock_task_manager: MagicMock) -> None:
        """update_task forwards task_type to the storage layer."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_update_task_allow_automation(self, mock_task_manager: MagicMock) -> None:
        """update_task forwards allow_automation to the storage layer."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_update_task_escalation_reason_without_transition(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Updating an escalation reason leaves lifecycle state untouched."""
        registry = create_task_registry(mock_task_manager)
        escalated_at = object()
        mock_task_manager.get_task.return_value = SimpleNamespace(
            task_type="task",
            category="research",
            validation_criteria="The requested task metadata is stored.",
            implementation_domain=None,
            is_escalated=True,
            escalated_at=escalated_at,
        )
        mock_task_manager.update_task.return_value = SimpleNamespace(
            escalation_reason="Migration emergency resolved; design review remains.",
            escalated_at=escalated_at,
        )

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "escalation_reason": "Migration emergency resolved; design review remains.",
            },
        )

        assert result == {}
        mock_task_manager.update_task.assert_called_once_with(
            "550e8400-e29b-41d4-a716-446655440000",
            escalation_reason="Migration emergency resolved; design review remains.",
        )
        mock_task_manager.escalate_task.assert_not_called()
        mock_task_manager.de_escalate_task.assert_not_called()
        assert mock_task_manager.update_task.return_value.escalated_at is escalated_at

    @pytest.mark.asyncio
    async def test_update_task_rejects_escalation_reason_when_not_escalated(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A non-escalated task cannot retain orphan escalation text."""
        registry = create_task_registry(mock_task_manager)

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "escalation_reason": "Orphan reason",
            },
        )

        assert result == {
            "error": "Cannot update escalation_reason for a task that is not escalated."
        }
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_task_isolation(self, mock_task_manager: MagicMock) -> None:
        """update_task forwards validated isolation to the storage layer."""
        registry = create_task_registry(mock_task_manager)

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
        self, mock_task_manager: MagicMock
    ) -> None:
        """update_task rejects retargeting to a conflicting isolation artifact family."""
        registry = create_task_registry(mock_task_manager)
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
    async def test_update_task_assigned_agent(self, mock_task_manager: MagicMock) -> None:
        """update_task forwards assigned_agent to the storage layer."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_update_task_additional_skills(self, mock_task_manager: MagicMock) -> None:
        """update_task forwards additional_skills to the storage layer."""
        registry = create_task_registry(mock_task_manager)

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
        self, mock_task_manager: MagicMock
    ) -> None:
        """update_task forwards both assignment fields together."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_update_task_replaces_affected_files(self, mock_task_manager: MagicMock) -> None:
        registry = create_task_registry(mock_task_manager)

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "affected_files": ["src/a.py", "src/b.py"],
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000",
            affected_files=["src/a.py", "src/b.py"],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_clears_affected_files(self, mock_task_manager: MagicMock) -> None:
        registry = create_task_registry(mock_task_manager)

        result = await registry.call(
            "update_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "affected_files": [],
            },
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000",
            affected_files=[],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_omits_affected_files(self, mock_task_manager: MagicMock) -> None:
        registry = create_task_registry(mock_task_manager)

        result = await registry.call(
            "update_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "title": "Updated Title"},
        )

        mock_task_manager.update_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000",
            title="Updated Title",
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_task_partial_update(self, mock_task_manager: MagicMock) -> None:
        """Test update_task only includes provided metadata fields."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_add_label_success(self, mock_task_manager: MagicMock, sample_task: Task) -> None:
        """Test add_label adds a label to task."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_add_label_task_not_found(self, mock_task_manager: MagicMock) -> None:
        """Test add_label returns error when task not found."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.add_label.return_value = None

        result = await registry.call(
            "add_label", {"task_id": "00000000-0000-0000-0000-000000000000", "label": "new"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_remove_label_success(self, mock_task_manager: MagicMock) -> None:
        """Test remove_label removes a label from task."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_remove_label_task_not_found(self, mock_task_manager: MagicMock) -> None:
        """Test remove_label returns error when task not found."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_delete_task_success(self, mock_task_manager: MagicMock) -> None:
        """Test delete_task successfully deletes a task."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.delete_task.return_value = True

        result = await registry.call(
            "delete_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )

        mock_task_manager.delete_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", cascade=False, unlink=False
        )
        assert "error" not in result
        assert result["deleted_task_id"] == "550e8400-e29b-41d4-a716-446655440000"

    @pytest.mark.asyncio
    async def test_delete_task_not_found(self, mock_task_manager: MagicMock) -> None:
        """Test delete_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.delete_task.return_value = False

        result = await registry.call(
            "delete_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_task_without_cascade(self, mock_task_manager: MagicMock) -> None:
        """Test delete_task without cascade option."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_delete_task_with_unlink(self, mock_task_manager: MagicMock) -> None:
        """Test delete_task with unlink option preserves dependents."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_delete_task_dependents_error(self, mock_task_manager: MagicMock) -> None:
        """Test delete_task returns structured error when task has dependents."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_list_tasks_basic(self, mock_task_manager: MagicMock) -> None:
        """Test list_tasks returns tasks with count."""
        registry = create_task_registry(mock_task_manager)

        mock_task1 = SimpleNamespace(
            id="t1",
            seq_num=1,
            title="Task 1",
            task_type="task",
            category="code",
            priority=2,
            path_cache="1",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_task2 = SimpleNamespace(
            id="t2",
            seq_num=2,
            title="Task 2",
            task_type="bug",
            category="code",
            priority=1,
            path_cache="2",
            updated_at="2024-01-02T00:00:00Z",
        )

        mock_task_manager.list_tasks.return_value = [mock_task1, mock_task2]

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "11111111-1111-4111-8111-111111110001"}

            result = await registry.call("list_tasks", {})

            assert result["count"] == 2
            assert len(result["tasks"]) == 2
            for task in result["tasks"]:
                assert set(task) == DISCOVERY_TASK_KEYS
                assert set(task["state"]) == COMPACT_STATE_KEYS
                assert NOISY_TASK_KEYS.isdisjoint(task)

    @pytest.mark.asyncio
    async def test_list_tasks_with_filters(self, mock_task_manager: MagicMock) -> None:
        """Test list_tasks applies filters correctly."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "11111111-1111-4111-8111-111111110001"}

            await registry.call(
                "list_tasks",
                {
                    "current_stage_state": "ready",
                    "priority": 1,
                    "task_type": "bug",
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
                label="urgent",
                parent_task_id="550e8400-e29b-41d4-a716-446655440010",
                title_like="feature",
                limit=10,
                project_id="11111111-1111-4111-8111-111111110001",
            )
            assert mock_task_manager.list_tasks.call_count >= 1
            assert mock_task_manager.list_tasks.call_args is not None

    @pytest.mark.asyncio
    async def test_list_tasks_all_projects(self, mock_task_manager: MagicMock) -> None:
        """Test list_tasks with all_projects=True ignores project filter."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "11111111-1111-4111-8111-111111110001"}

            await registry.call("list_tasks", {"all_projects": True})

            call_kwargs = mock_task_manager.list_tasks.call_args.kwargs
            assert call_kwargs["project_id"] is None

    @pytest.mark.asyncio
    async def test_list_tasks_comma_separated_status(self, mock_task_manager: MagicMock) -> None:
        """Test list_tasks handles comma-separated current_stage_state strings."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "11111111-1111-4111-8111-111111110001"}

            await registry.call("list_tasks", {"current_stage_state": "ready,in_progress"})

            call_kwargs = mock_task_manager.list_tasks.call_args.kwargs
            assert call_kwargs["current_stage_state"] == ["ready", "in_progress"]


# =============================================================================
# Session Integration Tool Tests
# =============================================================================

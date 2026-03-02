"""
Comprehensive unit tests for tasks.py MCP tools to improve coverage.

Tests focus on:
1. Task CRUD operations (create, get, update, close, delete, list)
2. Task validation and error handling
3. Label management
4. Session integration
5. Edge cases and error paths

Uses pytest with unittest.mock following existing test patterns.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import (
    SKIP_REASONS,
    create_task_registry,
)
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit

# =============================================================================
# Fixtures
# =============================================================================


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
def mock_task_validator():
    """Create a mock task validator."""
    validator = AsyncMock()
    validator.validate_task = AsyncMock()
    return validator


@pytest.fixture
def mock_config():
    """Create a mock daemon config."""
    config = MagicMock()
    tasks_config = MagicMock()
    tasks_config.show_result_on_create = False
    validation_config = MagicMock()
    validation_config.auto_generate_on_create = False
    validation_config.auto_generate_on_expand = False
    validation_config.use_external_validator = False
    tasks_config.validation = validation_config
    config.get_gobby_tasks_config.return_value = tasks_config
    return config


@pytest.fixture
def task_registry(mock_task_manager, mock_sync_manager):
    """Create a task registry with mocked dependencies."""
    return create_task_registry(mock_task_manager, mock_sync_manager)


@pytest.fixture
def sample_task():
    """Create a sample task for testing."""
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
    )


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestSkipReasons:
    """Tests for SKIP_REASONS constant."""

    def test_skip_reasons_contains_expected_values(self) -> None:
        """Test that SKIP_REASONS contains all expected values."""
        assert "duplicate" in SKIP_REASONS
        assert "already_implemented" in SKIP_REASONS
        assert "wont_fix" in SKIP_REASONS
        assert "obsolete" in SKIP_REASONS

    def test_skip_reasons_is_frozenset(self) -> None:
        """Test that SKIP_REASONS is immutable."""
        assert isinstance(SKIP_REASONS, frozenset)


# =============================================================================
# create_task Tool Tests
# =============================================================================


class TestCreateTaskTool:
    """Tests for create_task MCP tool."""

    @pytest.mark.asyncio
    async def test_create_task_minimal(self, mock_task_manager, mock_sync_manager):
        """Test create_task with minimal arguments."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440001"
        mock_task.seq_num = 42
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "title": "New Task",
        }
        # Mock create_task_with_decomposition to return non-decomposed result
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440001", "title": "New Task"},
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "New Task", "session_id": "test-session", "category": "research"},
            )

            assert result == {
                "id": "550e8400-e29b-41d4-a716-446655440001",
                "seq_num": 42,
                "ref": "#42",
            }
            mock_task_manager.create_task_with_decomposition.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_task_with_blocks(self, mock_task_manager, mock_sync_manager):
        """Test create_task with blocks argument creates dependencies."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440002"
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440002"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440002"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "Blocker Task",
                        "session_id": "test-session",
                        "category": "research",
                        "blocks": [
                            "550e8400-e29b-41d4-a716-446655440003",
                            "550e8400-e29b-41d4-a716-446655440004",
                        ],
                    },
                )

                assert result["id"] == "550e8400-e29b-41d4-a716-446655440002"
                # Verify dependencies were added
                assert mock_dep_instance.add_dependency.call_count == 2
                mock_dep_instance.add_dependency.assert_any_call(
                    "550e8400-e29b-41d4-a716-446655440003",
                    "550e8400-e29b-41d4-a716-446655440002",
                    "blocks",
                )
                mock_dep_instance.add_dependency.assert_any_call(
                    "550e8400-e29b-41d4-a716-446655440004",
                    "550e8400-e29b-41d4-a716-446655440002",
                    "blocks",
                )

    @pytest.mark.asyncio
    async def test_create_task_with_depends_on(self, mock_task_manager, mock_sync_manager):
        """Test create_task with depends_on argument creates dependencies."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440010"
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440010"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440010"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}
                with patch(
                    "gobby.mcp_proxy.tools.tasks._crud.resolve_task_id_for_mcp"
                ) as mock_resolve:
                    mock_resolve.side_effect = lambda mgr, ref, pid: ref  # Pass through

                    result = await registry.call(
                        "create_task",
                        {
                            "title": "Dependent Task",
                            "session_id": "test-session",
                            "category": "research",
                            "depends_on": ["blocker-1", "blocker-2"],
                        },
                    )

                    assert result["id"] == "550e8400-e29b-41d4-a716-446655440010"
                    # Verify dependencies were added (blocker blocks the new task)
                    assert mock_dep_instance.add_dependency.call_count == 2
                    mock_dep_instance.add_dependency.assert_any_call(
                        "550e8400-e29b-41d4-a716-446655440010",
                        "blocker-1",
                        "blocks",
                    )
                    mock_dep_instance.add_dependency.assert_any_call(
                        "550e8400-e29b-41d4-a716-446655440010",
                        "blocker-2",
                        "blocks",
                    )

    @pytest.mark.asyncio
    async def test_create_task_depends_on_with_errors(self, mock_task_manager, mock_sync_manager):
        """Test create_task with depends_on handles invalid refs gracefully."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440011"
            mock_task.seq_num = 1
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440011"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440011"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}
                with patch(
                    "gobby.mcp_proxy.tools.tasks._crud.resolve_task_id_for_mcp"
                ) as mock_resolve:
                    # First blocker found, second not found
                    mock_resolve.side_effect = [
                        "valid-blocker",
                        TaskNotFoundError("not found"),
                    ]

                    result = await registry.call(
                        "create_task",
                        {
                            "title": "Partial Deps Task",
                            "session_id": "test-session",
                            "category": "research",
                            "depends_on": ["valid-ref", "invalid-ref"],
                        },
                    )

                    # Task should still be created
                    assert result["id"] == "550e8400-e29b-41d4-a716-446655440011"
                    # But with warning about failed dependencies
                    assert "dependency_errors" in result
                    assert len(result["dependency_errors"]) == 1
                    assert "warning" in result

    @pytest.mark.asyncio
    async def test_create_task_with_labels(self, mock_task_manager, mock_sync_manager):
        """Test create_task with labels argument."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440005"
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440005",
            "labels": ["urgent", "bug"],
        }
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440005", "labels": ["urgent", "bug"]},
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "create_task",
                {
                    "title": "Labeled Task",
                    "session_id": "test-session",
                    "category": "research",
                    "labels": ["urgent", "bug"],
                },
            )

            mock_task_manager.create_task_with_decomposition.assert_called_once()
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["labels"] == ["urgent", "bug"]

    @pytest.mark.asyncio
    async def test_create_code_task_requires_validation_criteria(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test that code tasks are rejected without validation_criteria."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {
                    "title": "Implement new feature",
                    "session_id": "test-session",
                    "category": "code",
                },
            )

            assert "error" in result
            assert "validation_criteria" in result["error"]
            mock_task_manager.create_task_with_decomposition.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_code_task_with_validation_criteria_succeeds(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test that code tasks succeed when validation_criteria is provided."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440007"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440007"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440007"},
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "create_task",
                {
                    "title": "Implement new feature",
                    "session_id": "test-session",
                    "category": "code",
                    "validation_criteria": "Tests pass and feature works",
                },
            )

            mock_task_manager.create_task_with_decomposition.assert_called_once()
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["category"] == "code"

    @pytest.mark.asyncio
    async def test_create_non_code_task_without_validation_criteria(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test that non-code tasks succeed without validation_criteria."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440006"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440006"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440006"},
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "create_task",
                {
                    "title": "Research auth options",
                    "session_id": "test-session",
                    "category": "research",
                },
            )

            mock_task_manager.create_task_with_decomposition.assert_called_once()
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["category"] == "research"

    @pytest.mark.asyncio
    async def test_create_task_with_all_optional_fields(self, mock_task_manager, mock_sync_manager):
        """Test create_task with all optional fields."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.LocalSessionManager"
        ) as MockSessionManager:
            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "sess-123"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440008"
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440008"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440008"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                await registry.call(
                    "create_task",
                    {
                        "title": "Full Task",
                        "description": "Detailed description",
                        "priority": 1,
                        "task_type": "feature",
                        "parent_task_id": "550e8400-e29b-41d4-a716-446655440009",
                        "labels": ["important"],
                        "category": "automated",
                        "validation_criteria": "Must pass tests",
                        "session_id": "sess-123",
                    },
                )

                call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
                assert call_kwargs["title"] == "Full Task"
                assert call_kwargs["description"] == "Detailed description"
                assert call_kwargs["priority"] == 1
                assert call_kwargs["task_type"] == "feature"
                assert call_kwargs["parent_task_id"] == "550e8400-e29b-41d4-a716-446655440009"
                assert call_kwargs["labels"] == ["important"]
                assert call_kwargs["category"] == "automated"
                assert call_kwargs["validation_criteria"] == "Must pass tests"
                assert call_kwargs["created_in_session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_create_task_uses_personal_project(self, mock_task_manager, mock_sync_manager):
        """Test create_task uses personal workspace when no project context exists."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440010"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440010"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440010"},
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = None  # No project context

            await registry.call(
                "create_task",
                {"title": "Task", "session_id": "test-session", "category": "research"},
            )

            # When no project context, should fall back to PERSONAL_PROJECT_ID
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["project_id"] == PERSONAL_PROJECT_ID

    @pytest.mark.asyncio
    async def test_create_task_with_show_result_on_create(
        self, mock_task_manager, mock_sync_manager, mock_config
    ):
        """Test create_task returns full result when show_result_on_create is True."""
        mock_config.get_gobby_tasks_config.return_value.show_result_on_create = True

        registry = create_task_registry(mock_task_manager, mock_sync_manager, config=mock_config)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440011"
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440011",
            "title": "Full Task",
            "status": "open",
        }
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {
                "id": "550e8400-e29b-41d4-a716-446655440011",
                "title": "Full Task",
                "status": "open",
            },
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "Full Task", "session_id": "test-session", "category": "research"},
            )

            # Should return full task dict, not minimal
            assert result == {
                "id": "550e8400-e29b-41d4-a716-446655440011",
                "title": "Full Task",
                "status": "open",
            }

    @pytest.mark.asyncio
    async def test_create_task_auto_generates_validation(
        self, mock_task_manager, mock_sync_manager, mock_task_validator, mock_config
    ):
        """Test create_task auto-generates validation criteria when enabled."""
        mock_config.get_gobby_tasks_config.return_value.validation.auto_generate_on_create = True

        registry = create_task_registry(
            mock_task_manager,
            mock_sync_manager,
            task_validator=mock_task_validator,
            config=mock_config,
        )

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440012"
        mock_task.task_type = "task"  # Not epic
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440012"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440012"},
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "Task", "session_id": "test-session", "category": "research"},
            )

            # Without claim=True, update_task should NOT be called (no auto-claim)
            mock_task_manager.update_task.assert_not_called()
            assert "validation_generated" not in result

    @pytest.mark.asyncio
    async def test_create_task_default_no_claim(self, mock_task_manager, mock_sync_manager):
        """Test create_task without claim parameter does NOT auto-claim."""
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
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440020"
            mock_task.seq_num = 100
            mock_task.status = "open"
            mock_task.assignee = None
            mock_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440020",
                "status": "open",
                "assignee": None,
            }
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440020"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {"title": "New Task", "session_id": "test-session", "category": "research"},
                )

                # Task should be created
                assert result["id"] == "550e8400-e29b-41d4-a716-446655440020"

                # update_task should NOT be called (no auto-claim)
                mock_task_manager.update_task.assert_not_called()

                # Session link should be "created", not "claimed"
                mock_st_instance.link_task.assert_called_once_with(
                    "test-session", "550e8400-e29b-41d4-a716-446655440020", "created"
                )

    @pytest.mark.asyncio
    async def test_create_task_with_claim_true(self, mock_task_manager, mock_sync_manager):
        """Test create_task with claim=True auto-claims the task."""
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
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440021"
            mock_task.seq_num = 101
            mock_task.status = "in_progress"
            mock_task.assignee = "test-session"
            mock_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440021",
                "status": "in_progress",
                "assignee": "test-session",
            }
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440021"},
            }
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.update_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
                        "session_id": "test-session",
                        "category": "research",
                        "claim": True,
                    },
                )

                # Task should be created
                assert result["id"] == "550e8400-e29b-41d4-a716-446655440021"

                # update_task should be called with assignee and status
                mock_task_manager.update_task.assert_called_once_with(
                    "550e8400-e29b-41d4-a716-446655440021",
                    assignee="test-session",
                    status="in_progress",
                )

                # Session links should include both "created" and "claimed"
                assert mock_st_instance.link_task.call_count == 2
                mock_st_instance.link_task.assert_any_call(
                    "test-session", "550e8400-e29b-41d4-a716-446655440021", "created"
                )
                mock_st_instance.link_task.assert_any_call(
                    "test-session", "550e8400-e29b-41d4-a716-446655440021", "claimed"
                )

    @pytest.mark.asyncio
    async def test_create_task_with_claim_sets_task_claimed_via_session_variables(
        self, mock_task_manager, mock_sync_manager
    ) -> None:
        """create_task(claim=True) must set task_claimed via session_var_manager.

        Regression test for #8642.
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
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {}
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440021"
            mock_task.seq_num = 101
            mock_task.status = "in_progress"
            mock_task.assignee = "test-session"
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440021"},
            }
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.update_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
                        "session_id": "test-session",
                        "category": "research",
                        "claim": True,
                    },
                )

                assert result["id"] == "550e8400-e29b-41d4-a716-446655440021"

                # merge_variables must have been called with task_claimed
                mock_sv_manager.merge_variables.assert_called_once()
                call_args = mock_sv_manager.merge_variables.call_args
                assert call_args[0][0] == "test-session"
                merged_vars = call_args[0][1]
                assert merged_vars["task_claimed"] is True
                assert merged_vars["claimed_task_id"] == mock_task.id


# =============================================================================
# get_task Tool Tests
# =============================================================================


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
        """Test get_task includes dependency information."""
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
                "get_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

            assert len(result["dependencies"]["blocked_by"]) == 1
            assert len(result["dependencies"]["blocking"]) == 1


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
        Must use claim_task, close_task, reopen_task, or mark_task_needs_review.
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
            workflow_name="dev-flow",
            verification="Run tests",
            sequence_order=5,
        )

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
        assert "mark_task_needs_review" in result["error"]
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
        assert "mark_task_needs_review" in result["error"]
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
        assert result == {}

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
        assert result == {}

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


class TestCloseTaskTool:
    """Tests for close_task MCP tool."""

    @pytest.mark.asyncio
    async def test_close_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test close_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.get_task.return_value = None

        result = await registry.call(
            "close_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_close_task_no_commits_error(self, mock_task_manager, mock_sync_manager):
        """Test close_task requires commits to be linked."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager:
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance

            result = await registry.call(
                "close_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

            assert "error" in result
            assert result["error"] == "no_commits_linked"

    @pytest.mark.asyncio
    async def test_close_task_with_skip_reason_skips_commit_check(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test close_task with skip reason bypasses commit check."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task.requires_user_review = False  # Avoid review routing
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No children

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            result = await registry.call(
                "close_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000", "reason": "duplicate"},
            )

            assert "error" not in result
            mock_task_manager.close_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_task_parent_with_open_children(self, mock_task_manager, mock_sync_manager):
        """Test close_task fails for parent with open children."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440020"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task_manager.get_task.return_value = mock_task

        # Create open child tasks
        child1 = MagicMock()
        child1.id = "550e8400-e29b-41d4-a716-446655440021"
        child1.title = "Open Child 1"
        child1.status = "open"

        child2 = MagicMock()
        child2.id = "550e8400-e29b-41d4-a716-446655440022"
        child2.title = "Open Child 2"
        child2.status = "in_progress"

        mock_task_manager.list_tasks.return_value = [child1, child2]

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager:
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance

            result = await registry.call(
                "close_task", {"task_id": "550e8400-e29b-41d4-a716-446655440020"}
            )

            assert "error" in result
            assert result["error"] == "validation_failed"
            assert "open_children" in result

    @pytest.mark.asyncio
    async def test_close_task_success_with_commits(self, mock_task_manager, mock_sync_manager):
        """Test close_task succeeds when commits are linked."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task.requires_user_review = False  # Explicitly set to avoid review routing
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No children

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            result = await registry.call(
                "close_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

            assert result == {}

    @pytest.mark.asyncio
    async def test_close_task_with_commit_sha_links_first(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test close_task with commit_sha links the commit first."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.link_commit.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            await registry.call(
                "close_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000", "commit_sha": "new-commit"},
            )

            mock_task_manager.link_commit.assert_called_with(
                "550e8400-e29b-41d4-a716-446655440000", "new-commit"
            )

    @pytest.mark.asyncio
    async def test_close_task_with_skip_validation(self, mock_task_manager, mock_sync_manager):
        """Test close_task with skip_validation bypasses LLM validation."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = "Must pass tests"
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "skip_validation": True,
                    "override_justification": "Manually verified",
                },
            )

            # When override_justification is provided, task routes to review
            assert result.get("routed_to_review") is True

    @pytest.mark.asyncio
    async def test_close_task_out_of_repo_blocked_when_session_had_edits(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test out_of_repo reason still enforces commit check when session had edits."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task_manager.get_task.return_value = mock_task

        # Mock session with had_edits=True
        mock_session = MagicMock()
        mock_session.had_edits = True

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch("gobby.storage.sessions.LocalSessionManager") as MockSessionManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            mock_session_instance = MagicMock()
            mock_session_instance.get.return_value = mock_session
            MockSessionManager.return_value = mock_session_instance

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "out_of_repo",
                    "session_id": "550e8400-e29b-41d4-a716-446655440000",
                },
            )

            assert result.get("error") == "missing_commits_for_edits"
            mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_clears_task_claimed_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """close_task must clear task_claimed/claimed_task_id session variables.

        Regression test for #9064: after successful close, the workflow state
        variables were not cleared due to scoping issues, causing stale
        'Task (unknown) is still in_progress' blocks after compaction.
        """
        task_uuid = "550e8400-e29b-41d4-a716-446655440000"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            # Session variables with task_claimed=True for this task
            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {
                "task_claimed": True,
                "claimed_task_id": task_uuid,
                "task_ref": "#42",
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "proj-1"
            mock_task.validation_criteria = None
            mock_task.requires_user_review = False
            mock_task.to_brief.return_value = {"id": task_uuid, "status": "closed"}
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {"task_id": task_uuid, "session_id": "test-session"},
            )

            assert "error" not in result
            # Variables must be cleared after successful close
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": False,
                    "claimed_task_id": None,
                    "task_ref": "",
                },
            )


# =============================================================================
# reopen_task Tool Tests
# =============================================================================


class TestReopenTaskTool:
    """Tests for reopen_task MCP tool."""

    @pytest.mark.asyncio
    async def test_reopen_task_success(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task successfully reopens a closed task."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        reopened_task = MagicMock()
        mock_task_manager.reopen_task.return_value = reopened_task

        result = await registry.call(
            "reopen_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )

        mock_task_manager.reopen_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", reason=None
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_reopen_task_with_reason(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task with a reason."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        reopened_task = MagicMock()
        reopened_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "open",
        }
        mock_task_manager.reopen_task.return_value = reopened_task

        await registry.call(
            "reopen_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "reason": "Needs more work"},
        )

        mock_task_manager.reopen_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", reason="Needs more work"
        )

    @pytest.mark.asyncio
    async def test_reopen_task_error(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task returns error on failure."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.reopen_task.side_effect = ValueError("Task not found")

        result = await registry.call(
            "reopen_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_reopen_task_reactivates_worktree(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task reactivates associated worktrees."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle.LocalWorktreeManager"
        ) as MockWorktreeManager:
            mock_wt_instance = MagicMock()
            mock_worktree = MagicMock()
            mock_worktree.id = "wt-123"
            mock_worktree.status = "merged"
            mock_wt_instance.get_by_task.return_value = mock_worktree
            MockWorktreeManager.return_value = mock_wt_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            reopened_task = MagicMock()
            reopened_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "open",
            }
            mock_task_manager.reopen_task.return_value = reopened_task

            await registry.call("reopen_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"})

            mock_wt_instance.update.assert_called()


# =============================================================================
# delete_task Tool Tests
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

        mock_task_manager.delete_task.side_effect = ValueError(
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

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
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
                    "status": "open",
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
                status="open",
                priority=1,
                task_type="bug",
                assignee="dev",
                label="urgent",
                parent_task_id="550e8400-e29b-41d4-a716-446655440010",
                title_like="feature",
                limit=10,
                project_id="proj-1",
            )

    @pytest.mark.asyncio
    async def test_list_tasks_all_projects(self, mock_task_manager, mock_sync_manager):
        """Test list_tasks with all_projects=True ignores project filter."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call("list_tasks", {"all_projects": True})

            call_kwargs = mock_task_manager.list_tasks.call_args.kwargs
            assert call_kwargs["project_id"] is None

    @pytest.mark.asyncio
    async def test_list_tasks_comma_separated_status(self, mock_task_manager, mock_sync_manager):
        """Test list_tasks handles comma-separated status strings."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call("list_tasks", {"status": "open,in_progress"})

            call_kwargs = mock_task_manager.list_tasks.call_args.kwargs
            assert call_kwargs["status"] == ["open", "in_progress"]


# =============================================================================
# Session Integration Tool Tests
# =============================================================================


class TestSessionIntegrationTools:
    """Tests for session integration MCP tools."""

    @pytest.mark.asyncio
    async def test_link_task_to_session_success(self, mock_task_manager, mock_sync_manager):
        """Test link_task_to_session creates a link."""
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
            mock_session_manager.resolve_session_reference.return_value = "sess-123"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "link_task_to_session",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "session_id": "sess-123",
                    "action": "worked_on",
                },
            )

            mock_st_instance.link_task.assert_called_with(
                "sess-123", "550e8400-e29b-41d4-a716-446655440000", "worked_on"
            )
            assert result == {}

    @pytest.mark.asyncio
    async def test_link_task_to_session_missing_session_id(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test link_task_to_session requires session_id."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "link_task_to_session", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_link_task_to_session_error(self, mock_task_manager, mock_sync_manager):
        """Test link_task_to_session handles errors."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            mock_st_instance.link_task.side_effect = ValueError("Invalid task")
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "link_task_to_session",
                {"task_id": "00000000-0000-0000-0000-000000000000", "session_id": "sess-123"},
            )

            assert "error" in result

    @pytest.mark.asyncio
    async def test_get_session_tasks(self, mock_task_manager, mock_sync_manager):
        """Test get_session_tasks returns tasks for a session."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            mock_st_instance.get_session_tasks.return_value = [
                {"task_id": "t1", "action": "worked_on"}
            ]
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "sess-123"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call("get_session_tasks", {"session_id": "sess-123"})

            assert result["session_id"] == "sess-123"
            assert len(result["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_get_task_sessions(self, mock_task_manager, mock_sync_manager):
        """Test get_task_sessions returns sessions for a task."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            mock_st_instance.get_task_sessions.return_value = [
                {"session_id": "sess-1", "action": "created"}
            ]
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "get_task_sessions", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

            assert result["task_id"] == "550e8400-e29b-41d4-a716-446655440000"
            assert len(result["sessions"]) == 1


# =============================================================================
# Registry Integration Tests
# =============================================================================


class TestRegistryIntegration:
    """Tests for registry merging and tool availability."""

    def test_registry_name_and_description(self, task_registry) -> None:
        """Test registry has correct name and description."""
        assert task_registry.name == "gobby-tasks"
        assert "Task management" in task_registry.description

    def test_crud_tools_registered(self, task_registry) -> None:
        """Test all CRUD tools are registered."""
        crud_tools = [
            "create_task",
            "get_task",
            "update_task",
            "close_task",
            "delete_task",
            "list_tasks",
        ]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in crud_tools:
            assert tool_name in tool_names, f"Missing CRUD tool: {tool_name}"

    def test_label_tools_registered(self, task_registry) -> None:
        """Test label tools are registered."""
        label_tools = ["add_label", "remove_label"]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in label_tools:
            assert tool_name in tool_names, f"Missing label tool: {tool_name}"

    def test_session_tools_registered(self, task_registry) -> None:
        """Test session integration tools are registered."""
        session_tools = ["link_task_to_session", "get_session_tasks", "get_task_sessions"]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in session_tools:
            assert tool_name in tool_names, f"Missing session tool: {tool_name}"

    def test_reopen_task_registered(self, task_registry) -> None:
        """Test reopen_task is registered."""
        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        assert "reopen_task" in tool_names

    def test_merged_registries_available(self, task_registry) -> None:
        """Test tools from merged registries are available."""
        merged_tools = [
            # From task_validation
            "validate_task",
            # From task_expansion (skill-based)
            "save_expansion_spec",
            "execute_expansion",
            "get_expansion_spec",
            # From task_dependencies
            "add_dependency",
            "remove_dependency",
            # From task_readiness
            "list_ready_tasks",
            "list_blocked_tasks",
            # From task_sync
            "sync_tasks",
        ]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in merged_tools:
            assert tool_name in tool_names, f"Missing merged tool: {tool_name}"


# =============================================================================
# Schema Tests
# =============================================================================


class TestToolSchemas:
    """Tests for tool input schemas."""

    def test_create_task_schema_has_required_fields(self, task_registry) -> None:
        """Test create_task schema has required title field."""
        schema = task_registry.get_schema("create_task")

        assert schema is not None
        assert "title" in schema["inputSchema"]["properties"]
        assert "title" in schema["inputSchema"]["required"]

    def test_create_task_schema_has_claim_parameter(self, task_registry) -> None:
        """Test create_task schema includes optional claim parameter."""
        schema = task_registry.get_schema("create_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        assert "claim" in props, "Missing claim parameter in create_task schema"
        assert props["claim"]["type"] == "boolean"
        assert props["claim"]["default"] is False
        # claim should NOT be in required
        assert "claim" not in schema["inputSchema"]["required"]

    def test_update_task_schema_has_all_fields(self, task_registry) -> None:
        """Test update_task schema includes all updatable fields."""
        schema = task_registry.get_schema("update_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        expected_props = [
            "task_id",
            "title",
            "description",
            "status",
            "priority",
            "assignee",
            "labels",
            "validation_criteria",
            "parent_task_id",
            "category",
            "workflow_name",
            "verification",
            "sequence_order",
        ]

        for prop in expected_props:
            assert prop in props, f"Missing property: {prop}"

    def test_close_task_schema_has_all_fields(self, task_registry) -> None:
        """Test close_task schema includes all options."""
        schema = task_registry.get_schema("close_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        expected_props = [
            "task_id",
            "reason",
            "changes_summary",
            "skip_validation",
            "session_id",
            "override_justification",
            "commit_sha",
        ]

        for prop in expected_props:
            assert prop in props, f"Missing property: {prop}"

    def test_close_task_schema_requires_changes_summary(self, task_registry) -> None:
        """Test close_task schema requires changes_summary."""
        schema = task_registry.get_schema("close_task")

        assert schema is not None
        required = schema["inputSchema"]["required"]
        assert "changes_summary" in required, "changes_summary must be in required list"

    def test_list_tasks_schema_has_filters(self, task_registry) -> None:
        """Test list_tasks schema includes filter options."""
        schema = task_registry.get_schema("list_tasks")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        expected_props = [
            "status",
            "priority",
            "task_type",
            "assignee",
            "label",
            "parent_task_id",
            "title_like",
            "limit",
            "all_projects",
        ]

        for prop in expected_props:
            assert prop in props, f"Missing property: {prop}"


# =============================================================================
# Session Variable Mirroring Tests (#9071)
# =============================================================================


class TestSessionVariableMirroring:
    """Tests that task lifecycle operations mirror state to session_variables.

    The rule engine evaluates session_variables (higher precedence) over
    workflow_states. If task tools only write to workflow_states, the
    require-task-close rule never blocks the stop hook.
    """

    @pytest.mark.asyncio
    async def test_claim_task_mirrors_to_session_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """claim_task must mirror task_claimed=True to session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

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
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.seq_num = 200
            mock_task.status = "open"
            mock_task.assignee = None
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.update_task.return_value = mock_task

            result = await registry.call(
                "claim_task",
                {"task_id": task_uuid, "session_id": "test-session"},
            )

            assert "error" not in result
            # session_variables must be written with task_claimed=True and full UUID
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": True,
                    "claimed_task_id": task_uuid,
                    "task_ref": "#200",
                },
            )

    @pytest.mark.asyncio
    async def test_close_task_mirrors_clear_to_session_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """close_task must mirror task_claimed=False to session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {
                "task_claimed": True,
                "claimed_task_id": task_uuid,
                "task_ref": "#200",
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "proj-1"
            mock_task.validation_criteria = None
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {"task_id": task_uuid, "session_id": "test-session", "changes_summary": "done"},
            )

            assert "error" not in result
            # session_variables must be cleared
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": False,
                    "claimed_task_id": None,
                    "task_ref": "",
                },
            )

    @pytest.mark.asyncio
    async def test_create_task_with_claim_mirrors_to_session_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """create_task(claim=True) must mirror task_claimed=True to session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

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
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.seq_num = 300
            mock_task.status = "in_progress"
            mock_task.assignee = "test-session"
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": task_uuid},
            }
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.update_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
                        "session_id": "test-session",
                        "category": "research",
                        "claim": True,
                    },
                )

                assert result["id"] == task_uuid
                # session_variables must be written with full UUID (not truncated)
                mock_sv_manager.merge_variables.assert_called_once_with(
                    "test-session",
                    {
                        "task_claimed": True,
                        "claimed_task_id": task_uuid,
                        "task_ref": "#300",
                    },
                )

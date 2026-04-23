"""Focused coverage tests for task MCP tools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.tasks import TaskNotFoundError
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


class TestCreateTaskTool:
    """Tests for create_task MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("test-session"):
            yield

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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "New Task", "category": "research"},
            )

            assert result == {
                "id": "550e8400-e29b-41d4-a716-446655440001",
                "seq_num": 42,
                "ref": "#42",
            }
            mock_task_manager.create_task_with_decomposition.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_task_accepts_refactor_category(
        self, mock_task_manager, mock_sync_manager
    ) -> None:
        """Happy-path: create_task(category='refactor') succeeds.

        Expansion produces refactor tasks (expansion_service.py:566). Before this was a
        canonical category, the MCP enum rejected those payloads. This locks in the fix.
        """
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440099"
        mock_task.seq_num = 99
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440099",
            "title": "Refactor extraction",
        }
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {
                "id": "550e8400-e29b-41d4-a716-446655440099",
                "title": "Refactor extraction",
            },
        }
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "Refactor extraction", "category": "refactor"},
            )

            assert result["id"] == "550e8400-e29b-41d4-a716-446655440099"
            # Confirm the category made it through the call unchanged
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["category"] == "refactor"

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

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "Blocker Task",
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

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}
                with patch(
                    "gobby.mcp_proxy.tools.tasks._crud.resolve_task_id_for_mcp"
                ) as mock_resolve:
                    mock_resolve.side_effect = lambda mgr, ref, pid: ref  # Pass through

                    result = await registry.call(
                        "create_task",
                        {
                            "title": "Dependent Task",
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

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "create_task",
                {
                    "title": "Labeled Task",
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {
                    "title": "Implement new feature",
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "create_task",
                {
                    "title": "Implement new feature",
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            await registry.call(
                "create_task",
                {
                    "title": "Research auth options",
                    "category": "research",
                },
            )

            mock_task_manager.create_task_with_decomposition.assert_called_once()
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["category"] == "research"

    @pytest.mark.asyncio
    async def test_create_task_with_all_optional_fields(self, mock_task_manager, mock_sync_manager):
        """Test create_task with all optional fields."""
        with patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager:
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

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = None  # No project context

            await registry.call(
                "create_task",
                {"title": "Task", "category": "research"},
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "Full Task", "category": "research"},
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

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            result = await registry.call(
                "create_task",
                {"title": "Task", "category": "research"},
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
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
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

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {"title": "New Task", "category": "research"},
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
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(project_id="proj-1")
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
            mock_task_manager.claim_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
                        "category": "research",
                        "claim": True,
                    },
                )

                # Task should be created
                assert result["id"] == "550e8400-e29b-41d4-a716-446655440021"

                mock_task_manager.claim_task.assert_called_once_with(
                    "550e8400-e29b-41d4-a716-446655440021",
                    "test-session",
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
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(project_id="proj-1")
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
            mock_task_manager.claim_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
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
                assert mock_task.id in merged_vars["claimed_tasks"]


# =============================================================================
# Cross-Project Claim Blocking Tests (create_task)
# =============================================================================


class TestCreateTaskCrossProjectClaimBlocking:
    """Tests for cross-project claim blocking in create_task."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("test-session"):
            yield

    @pytest.mark.asyncio
    async def test_create_task_claim_skipped_when_cross_project(
        self, mock_task_manager, mock_sync_manager
    ):
        """create_task(claim=True) creates the task but skips claiming when cross-project."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            # Session is in proj-2, but task will be created in proj-1
            mock_session_manager.get.return_value = MagicMock(project_id="proj-2")
            MockSessionManager.return_value = mock_session_manager

            # Mock project resolution so explicit project="proj-1" resolves
            mock_proj_instance = MagicMock()
            mock_proj_instance.resolve_ref.return_value = MagicMock(id="proj-1")
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440099"
            mock_task.seq_num = 500
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": mock_task.id},
            }
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "Cross-project task",
                    "category": "research",
                    "claim": True,
                    "project": "proj-1",
                },
            )

            # Task should be created
            assert result["id"] == mock_task.id
            # Warning about skipped claim
            assert "warning" in result
            assert "different project" in result["warning"].lower()
            # update_task should NOT have been called (claim skipped)
            mock_task_manager.update_task.assert_not_called()
            # Session link for "created" should still exist, but NOT "claimed"
            mock_st_instance.link_task.assert_called_once_with(
                "test-session", mock_task.id, "created"
            )

    @pytest.mark.asyncio
    async def test_create_task_claim_allowed_when_same_project(
        self, mock_task_manager, mock_sync_manager
    ):
        """create_task(claim=True) claims normally when projects match."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(project_id="proj-1")
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440099"
            mock_task.seq_num = 500
            mock_task.status = "in_progress"
            mock_task.assignee = "test-session"
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": mock_task.id},
            }
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.claim_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "Same-project task",
                        "category": "research",
                        "claim": True,
                    },
                )

                # Task should be created and claimed
                assert result["id"] == mock_task.id
                assert "warning" not in result
                mock_task_manager.claim_task.assert_called_once_with(
                    mock_task.id,
                    "test-session",
                )


# =============================================================================
# get_task Tool Tests
# =============================================================================

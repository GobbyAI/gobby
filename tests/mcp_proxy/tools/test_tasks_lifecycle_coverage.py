"""Task lifecycle contracts through the public MCP registry."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry as _create_task_registry
from gobby.storage.tasks import Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit
TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])
_CHECKOUT_MACHINE = "21000000-0000-4000-8000-000000000001"


def _checkout_session_manager() -> MagicMock:
    manager = MagicMock()
    manager.get.return_value = MagicMock(
        id="test-session",
        machine_id=_CHECKOUT_MACHINE,
        project_id="11111111-1111-4111-8111-111111110001",
    )
    manager.resolve_session_reference.side_effect = lambda ref, _project_id=None: ref
    return manager


@pytest.fixture(autouse=True)
def _stub_checkout_resolvers() -> Iterator[None]:
    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._context.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.require_local_machine_id",
            return_value=_CHECKOUT_MACHINE,
        ),
        patch(
            "gobby.mcp_proxy.tools.task_repo_paths.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.task_repo_paths.require_local_machine_id",
            return_value=_CHECKOUT_MACHINE,
        ),
        patch(
            "gobby.storage.project_checkouts.require_local_machine_id",
            return_value=_CHECKOUT_MACHINE,
        ),
        patch("gobby.sessions.machine_scope.get_machine_id", return_value=_CHECKOUT_MACHINE),
        patch(
            "gobby.storage.project_checkouts.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionManager",
            return_value=_checkout_session_manager(),
        ),
    ):
        yield


def _contract_task() -> Any:
    now = datetime.now(UTC)
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="11111111-1111-4111-8111-111111110001",
        title="Contract task",
        priority=2,
        task_type="task",
        created_at=now,
        updated_at=now,
        category="research",
        validation_criteria="The requested lifecycle behavior is observable.",
    )


def create_task_registry(task_manager: Any, *args: Any, **kwargs: Any) -> Any:
    with patch(
        "gobby.mcp_proxy.tools.tasks._context.SessionManager",
        return_value=_checkout_session_manager(),
    ):
        return _create_task_registry(task_manager, *args, **kwargs)


class TestReopenTaskTool:
    """Tests for reopen_task MCP tool."""

    @pytest.mark.asyncio
    async def test_reopen_task_success(self, mock_task_manager: MagicMock) -> None:
        """Test reopen_task successfully reopens a closed task."""
        registry = create_task_registry(mock_task_manager)

        reopened_task = MagicMock()
        mock_task_manager.reopen_task.return_value = reopened_task

        with session_context_for_test("test-session"):
            result = await registry.call(
                "reopen_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

        mock_task_manager.reopen_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", reason=None
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_reopen_task_with_reason(self, mock_task_manager: MagicMock) -> None:
        """Test reopen_task with a reason."""
        registry = create_task_registry(mock_task_manager)

        reopened_task = MagicMock()
        reopened_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "open",
        }
        mock_task_manager.reopen_task.return_value = reopened_task

        with session_context_for_test("test-session"):
            await registry.call(
                "reopen_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000", "reason": "Needs more work"},
            )

        mock_task_manager.reopen_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", reason="Needs more work"
        )
        assert mock_task_manager.reopen_task.call_count >= 1
        assert mock_task_manager.reopen_task.call_args is not None

    @pytest.mark.asyncio
    async def test_reopen_task_error(self, mock_task_manager: MagicMock) -> None:
        """Test reopen_task returns error on failure."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.reopen_task.side_effect = ValueError("Task not found")

        with session_context_for_test("test-session"):
            result = await registry.call(
                "reopen_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
            )

        assert "error" in result
        assert "Task not found" in result["error"]

    @pytest.mark.asyncio
    async def test_reopen_task_leaves_worktree_status_unchanged(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test reopen_task does not mutate associated worktrees."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.LocalWorktreeManager"
        ) as MockWorktreeManager:
            mock_wt_instance = MagicMock()
            mock_worktree = MagicMock()
            mock_worktree.id = "wt-123"
            mock_worktree.status = "merged"
            mock_wt_instance.get_by_task.return_value = mock_worktree
            MockWorktreeManager.return_value = mock_wt_instance

            registry = create_task_registry(mock_task_manager)

            reopened_task = MagicMock()
            reopened_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "open",
            }
            mock_task_manager.reopen_task.return_value = reopened_task

            await registry.call("reopen_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"})

            mock_wt_instance.get_by_task.assert_not_called()
            assert mock_wt_instance.get_by_task.call_count == 0
            assert not mock_wt_instance.get_by_task.called
            mock_wt_instance.update.assert_not_called()
            assert mock_wt_instance.update.call_count == 0
            assert not mock_wt_instance.update.called


class TestSessionVariableMirroring:
    """Tests that task lifecycle operations mirror state to session_variables.

    The rule engine evaluates session_variables (higher precedence) over
    workflow_states. If task tools only write to workflow_states, the
    require-task-close rule never blocks the stop hook.
    """

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with session_context_for_test("test-session"):
            yield

    @pytest.mark.asyncio
    async def test_claim_task_leaves_claim_mirroring_to_storage(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Storage selects the claim in its transaction (#23751); the tool merges only skills."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

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
            mock_session_manager.get.return_value = MagicMock(
                project_id="11111111-1111-4111-8111-111111110001"
            )
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager)

            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.seq_num = 200
            mock_task.project_id = "11111111-1111-4111-8111-111111110001"
            mock_task.status = "open"
            mock_task.claimed_by_session_id = None
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.update_task.return_value = mock_task
            mock_task_manager.claim_task_for_agent.return_value = mock_task

            result = await registry.call(
                "claim_task",
                {"task_id": task_uuid},
            )

            assert "error" not in result
            mock_task_manager.claim_task_for_agent.assert_called_once_with(
                task_uuid, session_id="test-session", force=False
            )
            mock_sv_manager.merge_variables.assert_called_once()
            session_id, merged = mock_sv_manager.merge_variables.call_args[0]
            assert session_id == "test-session"
            assert "task_claimed" not in merged
            assert "claimed_tasks" not in merged
            assert "active_task_id" not in merged

    @pytest.mark.asyncio
    async def test_create_task_with_claim_leaves_claim_mirroring_to_storage(
        self, mock_task_manager: MagicMock
    ) -> None:
        """create_task(claim=True) claims in storage (#23751); the tool merges only skills."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

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
            mock_session_manager.get.return_value = MagicMock(
                project_id="11111111-1111-4111-8111-111111110001"
            )
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager)

            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.seq_num = 300
            mock_task.status = "in_progress"
            mock_task.claimed_by_session_id = "test-session"
            mock_task_manager.create_task_for_agent.return_value = mock_task
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.claim_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "11111111-1111-4111-8111-111111110001"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
                        "category": "research",
                        "claim": True,
                        "validation_criteria": "Test task completion is observable.",
                    },
                )

                assert result["id"] == task_uuid
                mock_task_manager.create_task_for_agent.assert_called_once()
                create_kwargs = mock_task_manager.create_task_for_agent.call_args.kwargs
                assert create_kwargs["session_id"] == "test-session"
                mock_sv_manager.merge_variables.assert_called_once()
                session_id, merged = mock_sv_manager.merge_variables.call_args[0]
                assert session_id == "test-session"
                assert "task_claimed" not in merged
                assert "claimed_tasks" not in merged
                assert "active_task_id" not in merged

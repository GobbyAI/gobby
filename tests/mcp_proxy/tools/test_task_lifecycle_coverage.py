"""Tests for tasks/_lifecycle.py — targeting uncovered lines."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import _is_uuid
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit
TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])
_CHECKOUT_MACHINE = "21000000-0000-4000-8000-000000000001"


def _checkout_session_manager(session_id: str = "test-session") -> MagicMock:
    manager = MagicMock()
    manager.get.return_value = MagicMock(
        id=session_id,
        machine_id=_CHECKOUT_MACHINE,
        project_id="11111111-1111-4111-8111-111111110001",
    )
    manager.resolve_session_reference.return_value = session_id
    return manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "550e8400-e29b-41d4-a716-446655440000",
    project_id: str = "11111111-1111-4111-8111-111111110001",
    title: str = "Test Task",
    status: str = "open",
    priority: int = 2,
    task_type: str = "task",
    claimed_by_session_id: str | None = None,
    labels: list[str] | None = None,
    validation_criteria: str | None = "Focused tests pass.",
    commits: list[str] | None = None,
    seq_num: int | None = 42,
    description: str | None = "Test desc",
) -> Task:
    created_at = datetime(2024, 1, 1, tzinfo=UTC)
    terminal_at = datetime(2024, 1, 2, tzinfo=UTC)
    return Task(
        id=id,
        project_id=project_id,
        title=title,
        priority=priority,
        task_type=task_type,
        created_at=created_at,
        updated_at=created_at,
        closed_at=terminal_at if status == "closed" else None,
        claimed_by_session_id=claimed_by_session_id,
        labels=labels or [],
        validation_criteria=validation_criteria,
        commits=commits,
        seq_num=seq_num,
        description=description,
        escalated_at=terminal_at if status == "escalated" else None,
        is_escalated=status == "escalated",
    )


@pytest.fixture(autouse=True)
def _stub_project_manager(tmp_path: Path) -> Iterator[None]:
    """Resolve the test checkout during repo-path resolution.

    RegistryContext.__post_init__ builds a real LocalProjectManager over the
    MagicMock db used here, so close_task's resolve_task_repo_path would parse
    MagicMock rows as datetimes (fromisoformat TypeError). Stubbing the class
    provides the explicit repository required by close_task Git operations.
    """
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch(
            "gobby.mcp_proxy.tools.tasks._context.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.require_local_machine_id",
            return_value="21000000-0000-4000-8000-000000000001",
        ),
        patch(
            "gobby.mcp_proxy.tools.task_repo_paths.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.task_repo_paths.require_local_machine_id",
            return_value="21000000-0000-4000-8000-000000000001",
        ),
        patch(
            "gobby.storage.project_checkouts.require_local_machine_id",
            return_value=_CHECKOUT_MACHINE,
        ),
        patch(
            "gobby.sessions.machine_scope.get_machine_id",
            return_value=_CHECKOUT_MACHINE,
        ),
        patch(
            "gobby.storage.project_checkouts.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionManager",
            return_value=_checkout_session_manager(),
        ),
    ):
        mock_pm.return_value.get.return_value = MagicMock()
        mock_pm.return_value.list.return_value = []
        mock_pm.return_value.db = MagicMock()
        mock_pm.return_value.db.fetchone.return_value = None
        mock_pm.return_value.db.fetchall.return_value = []
        yield


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    manager.db.fetchone.return_value = None
    manager.list_tasks.return_value = []
    return manager


def _create_registry(task_manager: MagicMock) -> Any:
    """Create a task registry with isolated session lookup."""
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionManager",
            return_value=_checkout_session_manager("resolved-session"),
        ),
    ):
        return create_task_registry(task_manager)


# ---------------------------------------------------------------------------
# close_task tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reopen_task tests
# ---------------------------------------------------------------------------


class TestReopenTask:
    """Tests for reopen_task tool."""

    @pytest.mark.asyncio
    async def test_reopen_success(self, mock_task_manager: MagicMock) -> None:
        """Reopen resolves task and calls reopen."""
        mock_task_manager.get_task.return_value = _make_task(status="in_progress")
        registry = _create_registry(mock_task_manager)

        with session_context_for_test():
            result = await registry.call(
                "reopen_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
            )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_reopen_clears_claimed_tasks_variable(self, mock_task_manager: MagicMock) -> None:
        """Reopen removes task from claimed_tasks session variable for prior claimed_by_session_id."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        mock_task_manager.get_task.return_value = _make_task(
            status="in_progress", claimed_by_session_id=session_id
        )

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as MockSTM,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSM,
            patch("gobby.workflows.task_claim_state.release_claimed_task") as mock_release,
        ):
            mock_sm = MagicMock()
            mock_sm.resolve_session_reference.return_value = "resolved-session"
            MockSM.return_value = mock_sm

            mock_stm = MagicMock()
            MockSTM.return_value = mock_stm

            registry = create_task_registry(mock_task_manager)

            # Mock session_var_manager on the context
            mock_svm = MagicMock()
            mock_svm.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_id: "#42"},
            }
            mock_release.return_value = {
                "task_claimed": False,
                "claimed_tasks": {},
                "active_task_id": None,
            }

            # Patch session_var_manager on the registry context
            with patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionVariableManager",
                return_value=mock_svm,
            ):
                registry = create_task_registry(mock_task_manager)
                with session_context_for_test(session_id):
                    result = await registry.call("reopen_task", {"task_id": task_id})

            assert "error" not in result
            assert result == {}
            mock_release.assert_called_once_with(mock_svm.get_variables.return_value, task_id)

    @pytest.mark.asyncio
    async def test_reopen_value_error(self, mock_task_manager: MagicMock) -> None:
        """Returns error when reopen raises ValueError."""
        mock_task_manager.get_task.return_value = _make_task(status="in_progress")
        mock_task_manager.reopen_task.side_effect = ValueError("cannot reopen")
        registry = _create_registry(mock_task_manager)

        with session_context_for_test():
            result = await registry.call(
                "reopen_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
            )
        assert "error" in result
        assert "cannot reopen" in result["error"]


# ---------------------------------------------------------------------------
# delete_task tests
# ---------------------------------------------------------------------------


class TestDeleteTask:
    """Tests for delete_task tool."""

    @pytest.mark.asyncio
    async def test_delete_success(self, mock_task_manager: MagicMock) -> None:
        """Delete resolves task and deletes."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.delete_task.return_value = True
        registry = _create_registry(mock_task_manager)

        result = await registry.call("delete_task", {"task_id": task.id})
        assert "error" not in result
        assert result["ref"] == "#42"

    @pytest.mark.asyncio
    async def test_delete_not_found(self, mock_task_manager: MagicMock) -> None:
        """Returns error when task not found."""
        mock_task_manager.get_task.return_value = None
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "delete_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_has_dependents_error(self, mock_task_manager: MagicMock) -> None:
        """Returns specific error when task has dependent task(s)."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        from gobby.storage.tasks._models import TaskHasDependentsError

        mock_task_manager.delete_task.side_effect = TaskHasDependentsError(
            "Cannot delete: has dependent task(s)"
        )
        registry = _create_registry(mock_task_manager)

        result = await registry.call("delete_task", {"task_id": task.id, "cascade": False})
        assert result["error"] == "has_dependents"
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_delete_has_children_error(self, mock_task_manager: MagicMock) -> None:
        """Returns specific error when task has children."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        from gobby.storage.tasks._models import TaskHasChildrenError

        mock_task_manager.delete_task.side_effect = TaskHasChildrenError(
            "Cannot delete: has children"
        )
        registry = _create_registry(mock_task_manager)

        result = await registry.call("delete_task", {"task_id": task.id, "cascade": False})
        assert result["error"] == "has_children"

    @pytest.mark.asyncio
    async def test_delete_returns_false(self, mock_task_manager: MagicMock) -> None:
        """Returns error when delete returns False."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.delete_task.return_value = False
        registry = _create_registry(mock_task_manager)

        result = await registry.call("delete_task", {"task_id": task.id})
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# add_label / remove_label tests
# ---------------------------------------------------------------------------


class TestLabels:
    """Tests for add_label and remove_label tools."""

    @pytest.mark.asyncio
    async def test_add_label_success(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(labels=["existing"])
        mock_task_manager.add_label.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call("add_label", {"task_id": task.id, "label": "new"})
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_add_label_not_found(self, mock_task_manager: MagicMock) -> None:
        mock_task_manager.add_label.return_value = None
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "add_label",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "label": "x"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_remove_label_success(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(labels=[])
        mock_task_manager.remove_label.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call("remove_label", {"task_id": task.id, "label": "old"})
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_remove_label_not_found(self, mock_task_manager: MagicMock) -> None:
        mock_task_manager.remove_label.return_value = None
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "remove_label",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "label": "x"},
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# escalate_task tests
# ---------------------------------------------------------------------------


class TestEscalateTask:
    """Tests for escalate_task tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with session_context_for_test("my-session"):
            yield

    @pytest.mark.asyncio
    async def test_escalate_success(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "blocked by external dep"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_escalate_already_escalated(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="escalated")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "still blocked"},
        )
        assert "error" in result
        assert "escalated" in result["error"]

    @pytest.mark.asyncio
    async def test_escalate_closed_task(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "oops"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_escalate_with_session_id(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "escalate_task",
            {
                "task_id": task.id,
                "reason": "blocked",
            },
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_escalate_clears_claimed_tasks_variable(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Escalation removes the task from the prior owner's claimed_tasks."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        task = _make_task(id=task_id, status="in_progress", claimed_by_session_id=session_id)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.escalate_task.return_value = _make_task(
            id=task_id,
            status="escalated",
        )

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.workflows.task_claim_state.release_claimed_task") as mock_release,
        ):
            mock_svm = MagicMock()
            mock_svm.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_id: "#42"},
            }
            MockSVM.return_value = mock_svm
            mock_release.return_value = {
                "task_claimed": False,
                "claimed_tasks": {},
                "active_task_id": None,
            }

            registry = _create_registry(mock_task_manager)
            with session_context_for_test(session_id):
                result = await registry.call(
                    "escalate_task",
                    {"task_id": task_id, "reason": "blocked"},
                )

        assert "error" not in result
        mock_release.assert_called_once_with(mock_svm.get_variables.return_value, task_id)
        mock_svm.merge_variables.assert_called_once_with(
            session_id,
            {
                "task_claimed": False,
                "claimed_tasks": {},
                "active_task_id": None,
            },
        )


# ---------------------------------------------------------------------------
# approve_review tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# submit_for_review tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_uuid tests
# ---------------------------------------------------------------------------


class TestIsUuid:
    """Tests for the _is_uuid helper."""

    def test_valid_uuid(self) -> None:
        assert _is_uuid("550e8400-e29b-41d4-a716-446655440000") is True

    def test_invalid_uuid(self) -> None:
        assert _is_uuid("#123") is False

    def test_none_value(self) -> None:
        assert _is_uuid(None) is False


# ---------------------------------------------------------------------------
# Session-context guards (Change 4)
# ---------------------------------------------------------------------------


class TestCloseTaskSessionContextGuard:
    @pytest.mark.asyncio
    async def test_close_task_without_session_context_cannot_impersonate_the_claimant(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(claimed_by_session_id="claimed-session", commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager)
        result = await registry.call("close_task", {"task_id": task.id})
        assert result.get("error") == "no_session_context"
        mock_task_manager.close_task.assert_not_called()
        mock_task_manager.close_task_with_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_without_session_context_or_claimed_by_errors(
        self, mock_task_manager: MagicMock
    ) -> None:
        """No SessionContext and no claimed_by → explicit no_session_context error."""
        task = _make_task(claimed_by_session_id=None, commits=None)
        mock_task_manager.get_task.return_value = task

        registry = _create_registry(mock_task_manager)
        result = await registry.call(
            "close_task",
            {"task_id": task.id, "changes_summary": "done"},
        )

        assert result.get("error") == "no_session_context"
        assert "active session" in result.get("message", "")
        mock_task_manager.close_task.assert_not_called()


class TestEscalateTaskSessionContextGuard:
    """escalate_task / de_escalate_task must error without an active session context."""

    @pytest.mark.asyncio
    async def test_escalate_task_without_session_context_errors(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task

        registry = _create_registry(mock_task_manager)
        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "blocked"},
        )

        assert "No session context available" in result.get("error", "")
        mock_task_manager.escalate_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_de_escalate_task_without_session_context_errors(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(status="escalated")
        mock_task_manager.get_task.return_value = task

        registry = _create_registry(mock_task_manager)
        result = await registry.call(
            "de_escalate_task",
            {"task_id": task.id, "reason": "unblocked"},
        )

        assert "No session context available" in result.get("error", "")
        mock_task_manager.de_escalate_task.assert_not_called()

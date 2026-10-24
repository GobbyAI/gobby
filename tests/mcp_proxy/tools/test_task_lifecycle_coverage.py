"""Tests for tasks/_lifecycle.py — targeting uncovered lines."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.mcp_proxy.tools.tasks import _lifecycle_close as lifecycle_close
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._close_context import CloseAttributionSnapshot
from gobby.mcp_proxy.tools.tasks._lifecycle import _is_uuid
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git
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
        # Close reads its step-record policy from the primary checkout; an empty
        # checkout carries the default policy.
        patch("gobby.tasks.step_records.require_root", return_value=str(tmp_path)),
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


class _CloseTaskGitIsolation:
    """Exercise close lifecycle logic with isolated Git and attribution boundaries."""

    @pytest.fixture(autouse=True)
    def _isolated_close_boundaries(self) -> Iterator[None]:
        async def git_read(argv: tuple[str, ...], **_kwargs: object) -> GitOk | GitFailed:
            if argv[0] == "rev-parse":
                ref = argv[-1].removesuffix("^{commit}")
                if ref == "deadbeef":
                    return GitFailed("failed", argv, 1, "", "Unknown revision")
                return GitOk("ok", argv, ref, "")
            assert argv[:2] == ("merge-base", "--is-ancestor"), argv
            return GitOk("ok", argv, "", "")

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.daemon_git.run", side_effect=git_read
            ),
            patch.object(
                lifecycle_close,
                "capture_attribution",
                return_value=CloseAttributionSnapshot({}, True),
            ),
            patch.object(
                lifecycle_close,
                "main_checkout_target",
                return_value=(Path(TEST_REPO_PATH), "0.5.0"),
            ),
            patch.object(
                lifecycle_close,
                "resolve_close_worktree_root_async",
                return_value=SimpleNamespace(repo_path=None),
            ),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as variables,
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization._queue_close_memory_review"
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization.notify_parent_on_task_state_change"
            ),
        ):
            variables.return_value.get_variables.return_value = {}
            yield


class TestCloseTask(_CloseTaskGitIsolation):
    @pytest.fixture(autouse=True)
    def _seed_session_context(self) -> Iterator[None]:
        with session_context_for_test("test-session"):
            yield

    @pytest.mark.asyncio
    async def test_close_task_get_returns_none(self, mock_task_manager: MagicMock) -> None:
        mock_task_manager.get_task.return_value = None
        result = await _create_registry(mock_task_manager).call(
            "close_task", {"task_id": "missing"}
        )
        assert result["error"] == "task_not_found"
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("task_type", ["task", "epic"])
    async def test_completed_task_requires_linked_commits(
        self, mock_task_manager: MagicMock, task_type: str
    ) -> None:
        task = _make_task(task_type=task_type)
        mock_task_manager.get_task.return_value = task
        result = await _create_registry(mock_task_manager).call("close_task", {"task_id": task.id})
        assert result["error"] == "linked_commits"
        assert result["closed"] is False
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_epic_with_closed_children_is_read_only(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(task_type="epic", commits=["abc1234"])
        child = _make_task(id="child", status="closed")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = [child]
        with patch("gobby.hooks.event_handlers._plan.on_epic_terminal") as archive:
            result = await _create_registry(mock_task_manager).call(
                "close_task", {"task_id": task.id, "preview": True, "response_detail": "diagnostic"}
            )
        assert result["success"] is True
        assert result["can_close"] is True
        assert result["closed"] is False
        assert [gate["name"] for gate in result["gates"]] == [
            "children_closed",
            "linked_commits",
            "uncommitted_task_edits",
            "integration_ancestry",
            "step_records",
        ]
        mock_task_manager.close_task.assert_not_called()
        archive.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preview", [False, True])
    async def test_close_epic_open_children_blocked(
        self, mock_task_manager: MagicMock, preview: bool
    ) -> None:
        task = _make_task(task_type="epic", commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = [_make_task(id="child")]
        result = await _create_registry(mock_task_manager).call(
            "close_task", {"task_id": task.id, "preview": preview}
        )
        assert result["error"] == "children_closed"
        assert result["preview"] is preview
        assert result["can_close"] is False
        assert result["closed"] is False
        mock_task_manager.close_task_with_commit.assert_not_called()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_moot_empty_epic_closes_without_commits(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(task_type="epic")
        mock_task_manager.get_task.return_value = task
        with patch("gobby.hooks.event_handlers._plan.on_epic_terminal") as archive:
            result = await _create_registry(mock_task_manager).call(
                "close_task", {"task_id": task.id, "reason": "moot"}
            )
        assert result["closed"] is True
        mock_task_manager.close_task.assert_called_once()
        archive.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_task_invalid_commit_sha_returns_error(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        result = await _create_registry(mock_task_manager).call(
            "close_task", {"task_id": task.id, "commit_sha": "deadbeef"}
        )
        assert result["error"] == "invalid_commit_sha"
        mock_task_manager.close_task_with_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_links_candidate_atomically(self, mock_task_manager: MagicMock) -> None:
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        result = await _create_registry(mock_task_manager).call(
            "close_task", {"task_id": task.id, "commit_sha": "abc1234"}
        )
        assert result["closed"] is True
        mock_task_manager.close_task_with_commit.assert_called_once_with(
            task.id,
            "abc1234",
            reason="done",
            closed_in_session_id="resolved-session",
            cwd=TEST_REPO_PATH,
            expected_updated_at=task.updated_at,
            closed_ancestors=[],
        )
        mock_task_manager.link_commit.assert_not_called()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("criteria", [None, "Focused tests pass."])
    async def test_close_needs_no_criteria_review_or_validation_reset(
        self, mock_task_manager: MagicMock, criteria: str | None
    ) -> None:
        task = _make_task(validation_criteria=criteria, commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        result = await _create_registry(mock_task_manager).call("close_task", {"task_id": task.id})
        assert result["closed"] is True
        assert mock_task_manager.close_task.call_args.kwargs["closed_commit_sha"] == "abc1234"
        assert "reset_review_fail_count" not in mock_task_manager.close_task.call_args.kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("external_worktree", [False, True])
    async def test_close_uses_registered_external_checkout(
        self, mock_task_manager: MagicMock, tmp_path: Path, external_worktree: bool
    ) -> None:
        repo = tmp_path / "external-repo"
        repo.mkdir()
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.artifacts.get_artifacts.return_value = SimpleNamespace(
            worktree_path=None, clone_path=None
        )
        root = TEST_REPO_PATH if external_worktree else str(repo)
        with (
            patch("gobby.mcp_proxy.tools.task_repo_paths.require_root", return_value=root),
            patch("gobby.mcp_proxy.tools.task_repo_paths.LocalWorktreeManager") as worktrees,
            patch.object(daemon_git, "run", wraps=daemon_git.run) as git,
        ):
            worktrees.return_value.list_worktrees.return_value = (
                [SimpleNamespace(task_id="sibling-task", worktree_path=str(repo))]
                if external_worktree
                else []
            )
            result = await _create_registry(mock_task_manager).call(
                "close_task",
                {"task_id": task.id, "project_path": str(repo), "commit_sha": "abc1234"},
            )
        assert result.get("error") is None, result.get("message")
        assert result["closed"] is True
        git.assert_any_await(
            ("rev-parse", "--verify", "--end-of-options", "abc1234^{commit}"), cwd=str(repo)
        )
        assert mock_task_manager.close_task_with_commit.call_args.kwargs["cwd"] == str(repo)

    @pytest.mark.parametrize(
        "archive_error",
        [
            PermissionError("archive denied"),
            OSError("archive filesystem unavailable"),
            psycopg.DatabaseError("archive database unavailable"),
        ],
        ids=["permission", "os", "database"],
    )
    @pytest.mark.asyncio
    async def test_archive_failure_after_epic_close_preserves_notification_and_claim_cleanup(
        self,
        mock_task_manager: MagicMock,
        archive_error: Exception,
    ) -> None:
        session_id = "session-archive-failure"
        epic = _make_task(
            task_type="epic",
            commits=["abc1234"],
            claimed_by_session_id=session_id,
        )
        mock_task_manager.get_task.return_value = epic
        mock_task_manager.list_tasks.return_value = []
        order: list[str] = []

        def close_committed(*_args: Any, **_kwargs: Any) -> Task:
            order.append("close")
            return epic

        def archive_failed(*_args: Any, **_kwargs: Any) -> None:
            order.append("archive")
            raise archive_error

        def notify_parent(*_args: Any, **_kwargs: Any) -> None:
            order.append("notify")

        mock_task_manager.close_task.side_effect = close_committed
        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {epic.id: "#42"},
            "active_task_id": epic.id,
            "task_edited_files": {},
        }
        mock_svm.merge_variables.side_effect = lambda *_args, **_kwargs: order.append("cleanup")

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionVariableManager",
                return_value=mock_svm,
            ),
            patch(
                "gobby.hooks.event_handlers._plan.LocalPlanManager.archive_plan",
                side_effect=archive_failed,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization.notify_parent_on_task_state_change",
                side_effect=notify_parent,
            ) as mock_notify_parent,
        ):
            registry = _create_registry(mock_task_manager)
            result = await registry.call("close_task", {"task_id": epic.id})

        assert result["success"] is True
        assert result["closed"] is True
        assert result["can_close"] is True
        assert order == ["close", "archive", "notify", "cleanup"]
        mock_notify_parent.assert_called_once()
        mock_svm.merge_variables.assert_called_once()
        merged_claim_state = mock_svm.merge_variables.call_args.args[1]
        assert merged_claim_state["task_claimed"] is False
        assert merged_claim_state["claimed_tasks"] == {}
        assert merged_claim_state["active_task_id"] is None

    @pytest.mark.asyncio
    async def test_close_task_rejects_missing_project_path_before_git(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        task = _make_task(commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockPM,
            patch.object(daemon_git, "run", wraps=daemon_git.run) as git,
        ):
            MockPM.return_value.get.return_value = MagicMock()
            registry = _create_registry(mock_task_manager)
            result = await registry.call(
                "close_task",
                {
                    "task_id": task.id,
                    "changes_summary": "done",
                    "commit_sha": "abc1234",
                    "project_path": str(tmp_path / "missing"),
                },
            )

        assert result["error"] == "invalid_project_path"
        assert result["message"].startswith("project_path does not exist:")
        git.assert_not_awaited()
        mock_task_manager.link_commit.assert_not_called()
        mock_task_manager.close_task_with_commit.assert_not_called()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_rejects_non_directory_project_path_before_git(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        task = _make_task(commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        project_path = tmp_path / "not-a-directory"
        project_path.write_text("not a repo")

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockPM,
            patch.object(daemon_git, "run", wraps=daemon_git.run) as git,
        ):
            MockPM.return_value.get.return_value = MagicMock()
            registry = _create_registry(mock_task_manager)
            result = await registry.call(
                "close_task",
                {
                    "task_id": task.id,
                    "changes_summary": "done",
                    "commit_sha": "abc1234",
                    "project_path": str(project_path),
                },
            )

        assert result["error"] == "invalid_project_path"
        assert result["message"].startswith("project_path is not a directory:")
        git.assert_not_awaited()
        mock_task_manager.link_commit.assert_not_called()
        mock_task_manager.close_task_with_commit.assert_not_called()
        mock_task_manager.close_task.assert_not_called()


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


class TestCloseTaskSessionContextGuard(_CloseTaskGitIsolation):
    @pytest.mark.asyncio
    async def test_close_task_without_session_context_falls_back_to_claimed_by_session_id(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(claimed_by_session_id="claimed-session", commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager)
        result = await registry.call("close_task", {"task_id": task.id})
        assert result["closed"] is True
        assert (
            mock_task_manager.close_task.call_args.kwargs["closed_in_session_id"]
            == "resolved-session"
        )

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

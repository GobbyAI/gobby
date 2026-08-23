"""Tests for tasks/_lifecycle.py — targeting uncovered lines."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import psycopg
import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import _is_uuid
from gobby.mcp_proxy.tools.tasks._task_scope import TaskScopeEvaluation
from gobby.storage.tasks import LocalTaskManager, Task, TaskAlreadyEscalatedError
from gobby.storage.tasks._stage_states import StageState
from gobby.tasks.close_checklist import CloseGateResult
from gobby.tasks.close_verdict import CloseCriterionVerdict, CloseVerdict
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit
TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])


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
    stage_state = {
        "open": "ready",
        "escalated": "ready",
        "closed": "done",
    }.get(status, status)
    return Task(
        id=id,
        project_id=project_id,
        title=title,
        priority=priority,
        task_type=task_type,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        closed_at="2024-01-02T00:00:00Z" if status == "closed" else None,
        claimed_by_session_id=claimed_by_session_id,
        labels=labels or [],
        validation_criteria=validation_criteria,
        commits=commits,
        seq_num=seq_num,
        description=description,
        escalated_at="2024-01-02T00:00:00Z" if status == "escalated" else None,
        is_escalated=status == "escalated",
        stages=(
            {
                "stage_name": "development",
                "position": 0,
                "state": stage_state,
            },
        ),
    )


def _make_stage_state(
    *,
    task_id: str = "550e8400-e29b-41d4-a716-446655440000",
    stage_name: str = "planning",
    state: str = "needs_review",
) -> StageState:
    return StageState(
        task_id=task_id,
        stage_name=stage_name,
        position=0,
        state=state,
        review_policy="required",
        reviewer_agent="plan-adversary",
        entered_at=None,
        entered_by_session_id=None,
        completed_at=None,
        completed_by_session_id=None,
        completed_commit_sha=None,
        work_attempt_count=1,
        review_round_count=0,
        max_work_attempts=None,
        max_review_rounds=None,
        artifact_refs=None,
        notes=None,
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture(autouse=True)
def _stub_project_manager() -> Iterator[None]:
    """Resolve the test checkout during repo-path resolution.

    RegistryContext.__post_init__ builds a real LocalProjectManager over the
    MagicMock db used here, so close_task's resolve_task_repo_path would parse
    MagicMock rows as datetimes (fromisoformat TypeError). Stubbing the class
    provides the explicit repository required by close_task Git operations.
    """
    with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm:
        mock_pm.return_value.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
        mock_pm.return_value.list.return_value = []
        yield


@pytest.fixture
def mock_task_manager() -> MagicMock:
    mgr = MagicMock(spec=LocalTaskManager)
    mgr.db = MagicMock()
    mgr.db.fetchone.return_value = None
    # Validation backoff lookups must read as "no active backoff" (fetchone -> None),
    # otherwise TaskValidationBackoffStore builds a state from MagicMock rows.
    _backoff_conn = MagicMock()
    _backoff_conn.execute.return_value.fetchone.return_value = None
    _backoff_conn.execute.return_value.rowcount = 0
    mgr.db.transaction.return_value.__enter__.return_value = _backoff_conn
    mgr.db.transaction.return_value.__exit__.return_value = False
    mgr.increment_validation_failure.return_value = (1, False)
    mgr.stage_states = MagicMock()
    mgr.stage_states.get.return_value = _make_stage_state()
    return mgr


def _create_registry(
    task_manager: MagicMock,
    task_validator: AsyncMock | None = None,
) -> Any:
    """Create registry with patches for context managers."""
    if task_validator is None:
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = CloseVerdict(
            status="valid",
            criteria=(CloseCriterionVerdict(1, "Focused tests pass.", True, None),),
            feedback="Focused validation passed.",
        )
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSM,
    ):
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "resolved-session"
        MockSM.return_value = mock_sm
        return create_task_registry(task_manager, task_validator_resolver=lambda: task_validator)


def _create_stage_ops_registry(task_manager: MagicMock) -> Any:
    """Create the gobby-tasks-ops stage registry with patched context managers."""
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSM,
    ):
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "resolved-session"
        mock_sm.get.return_value = None
        MockSM.return_value = mock_sm
        ctx = RegistryContext(task_manager=task_manager)
        return create_stage_ops_registry(ctx)


# ---------------------------------------------------------------------------
# close_task tests
# ---------------------------------------------------------------------------


class TestCloseTask:
    """Tests for the close_task lifecycle tool."""

    # close_task now requires an active session context or task.claimed_by_session_id
    # (Change 4). The original tests in this class predate that guard and don't
    # exercise it — seed a SessionContext so they continue to test the
    # non-audit-guard paths.
    @pytest.fixture(autouse=True)
    def _seed_session_context(self) -> Iterator[None]:
        with session_context_for_test("legacy-test-session"):
            yield

    @pytest.mark.asyncio
    async def test_close_task_get_returns_none(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Returns error when get_task returns None after resolve."""
        mock_task_manager.get_task.return_value = None
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "close_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "changes_summary": "done"},
        )
        assert "error" in result
        assert result["error"] == "task_not_found"
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_close_epic_all_children_closed_no_commit_needed(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Closing a parent task (epic) with all children closed succeeds without commits."""
        parent = _make_task(task_type="epic", commits=None)
        child = _make_task(
            id="child-0000-0000-0000-000000000001",
            title="Child Task",
            status="closed",
            seq_num=43,
        )
        mock_task_manager.get_task.return_value = parent
        # First list_tasks call (limit=1) returns a child -> is a parent
        # Second list_tasks call (limit=1000) returns all children (all closed)
        mock_task_manager.list_tasks.return_value = [child]
        mock_task_manager.close_task.return_value = parent

        registry = _create_registry(mock_task_manager)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            # close_task archives the linked plan when an epic closes as
            # completed/obsolete; patch at the source module (close_task imports
            # it locally) so LocalPlanManager doesn't run against the mock db.
            patch("gobby.hooks.event_handlers._plan.on_epic_terminal") as mock_epic_terminal,
        ):
            result = await registry.call(
                "close_task",
                {
                    "task_id": parent.id,
                    "changes_summary": "All subtasks completed",
                    "preview": True,
                    "response_detail": "diagnostic",
                },
            )
            assert result["success"] is True
            assert result["preview"] is True
            assert result["can_close"] is True
            assert result["closed"] is True
            assert any(
                gate["name"] == "children_closed" and gate["passed"] for gate in result["checklist"]
            )
            # commit check should NOT have been called
            mock_vcr.assert_not_called()

        assert "error" not in result
        assert result.get("success", True) is not False
        mock_task_manager.close_task.assert_called_once()
        mock_epic_terminal.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_epic_open_children_blocked(self, mock_task_manager: MagicMock) -> None:
        """Closing a parent task with open children is blocked."""
        parent = _make_task(task_type="epic", commits=None)
        open_child = _make_task(
            id="child-0000-0000-0000-000000000002",
            title="Open Child",
            status="in_progress",
            seq_num=44,
        )
        mock_task_manager.get_task.return_value = parent
        mock_task_manager.list_tasks.return_value = [open_child]

        registry = _create_registry(mock_task_manager)
        result = await registry.call(
            "close_task",
            {
                "task_id": parent.id,
                "changes_summary": "Trying to close",
                "preview": True,
            },
        )

        assert result["success"] is False
        assert result["preview"] is True
        assert result["can_close"] is False
        assert result["closed"] is False
        assert result["error"] == "children_open"
        assert "open" in result["blocking_reasons"][0].lower()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_epic_no_children_no_commit_succeeds(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Closing an epic with no children succeeds without commits or changes_summary."""
        epic = _make_task(task_type="epic", commits=None)
        mock_task_manager.get_task.return_value = epic
        mock_task_manager.list_tasks.return_value = []  # no children
        mock_task_manager.close_task.return_value = epic

        registry = _create_registry(mock_task_manager)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            # close_task archives the linked plan when an epic closes as
            # completed/obsolete; patch at the source module (close_task imports
            # it locally) so LocalPlanManager doesn't run against the mock db.
            patch("gobby.hooks.event_handlers._plan.on_epic_terminal") as mock_epic_terminal,
        ):
            result = await registry.call(
                "close_task",
                {"task_id": epic.id},
            )
            # commit check should NOT have been called — epics skip leaf validation
            mock_vcr.assert_not_called()

        assert "error" not in result
        assert result.get("success", True) is not False
        assert result["closed"] is True
        mock_task_manager.close_task.assert_called_once()
        mock_epic_terminal.assert_called_once()

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
            commits=None,
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
    async def test_close_commit_requirements_fail(self, mock_task_manager: MagicMock) -> None:
        """Returns error when commit requirements fail."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSM,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
        ):
            mock_sm = MagicMock()
            mock_sm.resolve_session_reference.return_value = "resolved-session"
            MockSM.return_value = mock_sm
            MockSVM.return_value.get_variables.return_value = {
                "task_edited_files": {task.id: ["src/owned.py"]},
            }

            mock_vcr.return_value = MagicMock(
                can_close=False,
                error_type="missing_commits",
                message="no commits linked",
            )

            registry = create_task_registry(mock_task_manager)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "done"},
            )
        assert result["success"] is False
        assert result["error"] == "missing_commits"

    @pytest.mark.asyncio
    async def test_close_task_invalid_commit_sha_returns_error(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Returns error when commit_sha cannot be resolved (nonexistent or non-commit)."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        registry = _create_registry(mock_task_manager)
        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.resolve_close_commit_shas",
            return_value=(
                [],
                {
                    "error": "invalid_commit_sha",
                    "message": "Commit SHA 'deadbeef' could not be resolved.",
                },
            ),
        ):
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "done", "commit_sha": "deadbeef"},
            )

        assert "error" in result
        assert result["error"] == "invalid_commit_sha"
        assert "could not be resolved" in result["message"]

    @pytest.mark.asyncio
    async def test_close_task_passes_cwd_to_link_commit(self, mock_task_manager: MagicMock) -> None:
        """Verifies link_commit receives the project repo_path as cwd."""
        task = _make_task(commits=[])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.link_commit.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task

        registry = _create_registry(mock_task_manager)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.collect_commit_diff_text",
                return_value=None,
            ),
            patch("gobby.utils.git.normalize_commit_sha", return_value="abc1234"),
        ):
            mock_vcr.return_value = MagicMock(can_close=True)
            await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "done", "commit_sha": "abc1234"},
            )

        # link_commit should have been called with cwd keyword arg
        call_kwargs = mock_task_manager.link_commit.call_args
        assert call_kwargs is not None
        assert "cwd" in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_close_task_uses_project_path_override_for_commit_checks(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        """Cross-repo close_task calls must use a registered repo path."""
        repo_path = tmp_path / "external" / "repo"
        repo_path.mkdir(parents=True)
        task = _make_task(commits=[])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.link_commit.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockPM,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.collect_commit_diff_text",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close._has_committable_edits",
                return_value=False,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization._has_committable_edits",
                return_value=False,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.evaluate_validation_commands",
                return_value=CloseGateResult(
                    item=10,
                    name="validation_commands",
                    status="skipped",
                    message="Validation commands skipped for this test.",
                ),
            ),
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ) as mock_norm,
        ):
            MockPM.return_value.get.return_value = MagicMock(repo_path=str(repo_path))
            MockSVM.return_value.get_variables.return_value = {
                "task_edited_files": {task.id: ["src/owned.py"]},
            }
            registry = _create_registry(mock_task_manager)
            mock_vcr.return_value = MagicMock(can_close=True)
            await registry.call(
                "close_task",
                {
                    "task_id": task.id,
                    "changes_summary": "done",
                    "commit_sha": "abc1234",
                    "project_path": str(repo_path),
                },
            )

        expected_cwd = str(repo_path.resolve())
        mock_task_manager.link_commit.assert_called_with(
            task.id,
            "abc1234",
            cwd=expected_cwd,
        )
        mock_norm.assert_called_with("abc1234", cwd=expected_cwd)
        validation_task = mock_vcr.call_args.args[0]
        assert validation_task.commits == ["abc1234"]
        mock_vcr.assert_called_with(validation_task, "completed", expected_cwd)
        close_call = mock_task_manager.close_task.call_args
        assert close_call is not None
        assert close_call.kwargs["closed_commit_sha"] == "abc1234"

    @pytest.mark.asyncio
    async def test_close_task_accepts_active_external_project_worktree(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        """An active worktree may belong to a different project and sibling task."""
        task_repo = tmp_path / "task-repo"
        external_worktree = tmp_path / "external-project" / "worktree"
        task_repo.mkdir()
        external_worktree.mkdir(parents=True)
        task = _make_task(commits=[])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.link_commit.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task
        mock_task_manager.artifacts.get_artifacts.return_value = MagicMock(
            worktree_path=None,
            clone_path=None,
        )
        worktree = MagicMock(task_id="sibling-task", worktree_path=str(external_worktree))

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockPM,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.mcp_proxy.tools.task_repo_paths.LocalWorktreeManager") as worktree_manager,
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.collect_commit_diff_text",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close._has_committable_edits",
                return_value=False,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization._has_committable_edits",
                return_value=False,
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.evaluate_validation_commands",
                return_value=CloseGateResult(
                    item=10,
                    name="validation_commands",
                    status="skipped",
                    message="Validation commands skipped for this test.",
                ),
            ),
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ) as mock_norm,
        ):
            MockPM.return_value.get.return_value = MagicMock(repo_path=str(task_repo))
            MockSVM.return_value.get_variables.return_value = {
                "task_edited_files": {task.id: ["src/owned.py"]},
            }
            worktree_manager.return_value.list_worktrees.return_value = [worktree]
            registry = _create_registry(mock_task_manager)
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {
                    "task_id": task.id,
                    "changes_summary": "done",
                    "commit_sha": "abc1234",
                    "project_path": str(external_worktree),
                },
            )

        expected_cwd = str(external_worktree)
        assert "error" not in result
        assert result.get("success", True) is not False
        mock_task_manager.link_commit.assert_called_with(
            task.id,
            "abc1234",
            cwd=expected_cwd,
        )
        mock_norm.assert_called_with("abc1234", cwd=expected_cwd)
        validation_task = mock_vcr.call_args.args[0]
        assert validation_task.commits == ["abc1234"]
        mock_vcr.assert_called_with(validation_task, "completed", expected_cwd)

    @pytest.mark.asyncio
    async def test_close_task_rejects_missing_project_path_before_git(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        task = _make_task(commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockPM:
            MockPM.return_value.get.return_value = MagicMock(repo_path=str(tmp_path))
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
        mock_task_manager.link_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_rejects_non_directory_project_path_before_git(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        task = _make_task(commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        project_path = tmp_path / "not-a-directory"
        project_path.write_text("not a repo")

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockPM:
            MockPM.return_value.get.return_value = MagicMock(repo_path=str(tmp_path))
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
        mock_task_manager.link_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_code_leaf_without_criteria_is_rejected_before_validation(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(
            description="Implement the required behavior",
            validation_criteria=None,
        )
        task.category = "code"
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = MagicMock(
            status="invalid",
            feedback="The implementation does not satisfy the task description.",
            blocking_reasons=["Required behavior is missing"],
        )

        registry = _create_registry(mock_task_manager, task_validator)

        result = await registry.call(
            "close_task",
            {"task_id": task.id, "changes_summary": "Implementation attempted"},
        )

        assert result["error"] == "missing_validation_criteria"
        task_validator.validate_task.assert_not_awaited()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_diff_close_resets_validation_failure_count(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(commits=None)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task
        registry = _create_registry(mock_task_manager)

        result = await registry.call(
            "close_task",
            {"task_id": task.id, "changes_summary": "No repository diff was required"},
        )

        assert result["success"] is True
        assert mock_task_manager.close_task.call_args.kwargs["reset_validation_fail_count"] is True

    @pytest.mark.asyncio
    async def test_close_task_valid_llm_result_closes_when_feedback_satisfies_criteria(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A clean valid validator result allows close_task to close."""
        task = _make_task(validation_criteria="Strict mypy and focused tests are clean")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = CloseVerdict(
            status="valid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "Strict mypy and focused tests are clean",
                    True,
                    None,
                ),
            ),
            feedback="All criteria satisfied. Strict mypy and focused tests are clean.",
        )

        registry = _create_registry(mock_task_manager, task_validator)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "Implemented and verified"},
            )

        assert result["success"] is True
        task_validator.validate_task.assert_awaited_once()
        validation_kwargs = task_validator.validate_task.await_args.kwargs
        assert validation_kwargs["task_id"] == task.id
        assert validation_kwargs["closure_reason"] == "completed"
        assert "Implemented and verified" in validation_kwargs["changes_summary"]
        mock_task_manager.update_task.assert_not_called()
        mock_task_manager.close_task.assert_called_once()
        close_kwargs = mock_task_manager.close_task.call_args.kwargs
        assert close_kwargs["reset_validation_fail_count"] is True
        assert close_kwargs["validation_status"] == "valid"
        assert close_kwargs["validation_feedback"] == (
            "All criteria satisfied. Strict mypy and focused tests are clean."
        )

    @pytest.mark.asyncio
    async def test_close_task_obsolete_reason_reaches_criteria_review(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A no-work closure reason is forwarded to the criteria reviewer."""
        task = _make_task(validation_criteria="The wiki gap is corrected")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = CloseVerdict(
            status="valid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "The wiki gap is corrected",
                    True,
                    None,
                ),
            ),
            feedback="Obsolescence justification is coherent.",
        )

        registry = _create_registry(mock_task_manager, task_validator)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {
                    "task_id": task.id,
                    "reason": "obsolete",
                    "changes_summary": "Superseded by the vault cutover.",
                },
            )

        assert result["success"] is True
        task_validator.validate_task.assert_awaited_once()
        validation_kwargs = task_validator.validate_task.await_args.kwargs
        assert validation_kwargs["closure_reason"] == "obsolete"
        mock_task_manager.close_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_task_normalized_valid_result_closes(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A validator result normalized to valid permits closure."""
        task = _make_task(validation_criteria="Focused tests and lint pass")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = CloseVerdict(
            status="valid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "Focused tests and lint pass",
                    True,
                    None,
                ),
            ),
            feedback=(
                "Verified all three validation criteria are satisfied: tests pass and lint passes."
            ),
        )

        registry = _create_registry(mock_task_manager, task_validator)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "Implemented and verified"},
            )

        assert result["success"] is True
        task_validator.validate_task.assert_awaited_once()
        validation_kwargs = task_validator.validate_task.await_args.kwargs
        assert validation_kwargs["task_id"] == task.id
        assert "Implemented and verified" in validation_kwargs["changes_summary"]
        mock_task_manager.update_task.assert_not_called()
        close_kwargs = mock_task_manager.close_task.call_args.kwargs
        assert close_kwargs["reset_validation_fail_count"] is True
        assert close_kwargs["validation_status"] == "valid"
        mock_task_manager.close_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_task_normalized_invalid_result_is_rejected(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A validator result normalized to invalid blocks closure."""
        task = _make_task(validation_criteria="Strict mypy on touched tests is clean")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        task_validator = AsyncMock()
        feedback = (
            "All migration behavior passed. The only gap is the mypy criterion: "
            "typing errors prevented a clean mypy gate."
        )
        task_validator.validate_task.return_value = CloseVerdict(
            status="invalid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "Strict mypy on touched tests is clean",
                    False,
                    "Strict mypy criterion failed",
                ),
            ),
            feedback=feedback,
        )

        registry = _create_registry(mock_task_manager, task_validator)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "Implemented and verified"},
            )

        assert result["success"] is False
        assert result["error"] == "validation_failed"
        assert result["validation_status"] == "invalid"
        assert "mypy criterion" in result["message"]
        mock_task_manager.update_task.assert_not_called()
        mock_task_manager.increment_validation_failure.assert_called_once()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_invalid_llm_result_remains_rejected(
        self, mock_task_manager: MagicMock
    ) -> None:
        """An invalid criteria verdict blocks close_task."""
        task = _make_task(validation_criteria="Focused tests pass")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = CloseVerdict(
            status="invalid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "Focused tests pass",
                    False,
                    "Run focused tests clean.",
                ),
            ),
            feedback="invalid feedback",
        )

        registry = _create_registry(mock_task_manager, task_validator)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "Implemented and verified"},
            )

        assert result["success"] is False
        assert result["error"] == "validation_failed"
        assert result["validation_status"] == "invalid"
        mock_task_manager.update_task.assert_not_called()
        mock_task_manager.increment_validation_failure.assert_called_once()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_already_escalated_review_race_is_not_stale(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(validation_criteria="Focused tests pass")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.increment_validation_failure.side_effect = TaskAlreadyEscalatedError(
            task.id,
            "Repeated invalid review",
        )
        task_validator = AsyncMock()
        task_validator.validate_task.return_value = CloseVerdict(
            status="invalid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "Focused tests pass",
                    False,
                    "Run focused tests clean.",
                ),
            ),
            feedback="invalid feedback",
        )
        registry = _create_registry(mock_task_manager, task_validator)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "Implemented and verified"},
            )

        assert result["error"] == "validation_failed"
        assert result["escalated"] is True
        assert result["already_escalated"] is True
        assert "stale_state" not in result
        mock_task_manager.close_task.assert_not_called()


# ---------------------------------------------------------------------------
# validate_commit_requirements stale SHA tests
# ---------------------------------------------------------------------------


class TestValidateCommitRequirementsStale:
    """Tests for stale SHA detection in validate_commit_requirements."""

    def test_stale_commits_detected(self) -> None:
        """Returns stale_commits error when linked SHAs don't exist in repo."""
        from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
            validate_commit_requirements,
        )

        task = _make_task(commits=["abc1234", "def5678"])

        with patch("gobby.utils.git.normalize_commit_sha") as mock_norm:
            # First SHA resolves, second doesn't
            mock_norm.side_effect = ["abc1234", None]
            result = validate_commit_requirements(task, reason="completed", repo_path="/repo")

        assert not result.can_close
        assert result.error_type == "stale_commits"
        assert result.extra is not None
        assert "def5678" in result.extra["stale_shas"]

    def test_all_commits_valid(self) -> None:
        """Passes when all linked SHAs exist in repo."""
        from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
            validate_commit_requirements,
        )

        task = _make_task(commits=["abc1234"])

        with patch("gobby.utils.git.normalize_commit_sha") as mock_norm:
            mock_norm.return_value = "abc1234"
            result = validate_commit_requirements(task, reason="completed", repo_path="/repo")

        assert result.can_close

    def test_skips_verification_without_repo_path(self) -> None:
        """Degrades gracefully when no repo_path is available."""
        from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
            validate_commit_requirements,
        )

        task = _make_task(commits=["abc1234"])
        result = validate_commit_requirements(task, reason="completed", repo_path=None)

        assert result.can_close


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


class TestMarkTaskReviewApproved:
    """Tests for approve_review tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with session_context_for_test("sess-1"):
            yield

    @pytest.mark.asyncio
    async def test_approve_needs_review(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="needs_review")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.approve_review.return_value = task
        registry = _create_stage_ops_registry(mock_task_manager)

        result = await registry.call(
            "approve_review",
            {"task_id": task.id, "stage_name": "development"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_approve_wrong_status(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.approve_review.side_effect = ValueError("No current stage")
        registry = _create_stage_ops_registry(mock_task_manager)

        result = await registry.call(
            "approve_review",
            {"task_id": task.id, "stage_name": "development"},
        )
        assert "error" in result
        assert "No current stage" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_with_notes(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="needs_review", description="Original desc")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.approve_review.return_value = task
        registry = _create_stage_ops_registry(mock_task_manager)

        result = await registry.call(
            "approve_review",
            {
                "task_id": task.id,
                "stage_name": "development",
                "approval_notes": "Looks good",
            },
        )
        assert "error" not in result
        mock_task_manager.approve_review.assert_called_once_with(
            task.id,
            "development",
            approval_notes="Looks good",
            by_session_id=ANY,
        )

    @pytest.mark.asyncio
    async def test_approve_update_fails(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="needs_review")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.approve_review.return_value = None
        registry = _create_stage_ops_registry(mock_task_manager)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review._auto_link_session_commits"
            ) as auto_link,
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review._release_current_agent_dispatch_mutex"
            ) as release,
        ):
            result = await registry.call(
                "approve_review",
                {"task_id": task.id, "stage_name": "development"},
            )
        assert "error" in result
        assert "Failed to approve" in result["error"]
        auto_link.assert_not_called()
        release.assert_not_called()

    @pytest.mark.asyncio
    async def test_approve_clears_claimed_tasks_variable(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Review approval removes the task from the prior owner's claimed_tasks."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        task = _make_task(id=task_id, status="needs_review", claimed_by_session_id=session_id)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.approve_review.return_value = task

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

            registry = _create_stage_ops_registry(mock_task_manager)
            result = await registry.call(
                "approve_review",
                {"task_id": task_id, "stage_name": "development"},
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
# submit_for_review tests
# ---------------------------------------------------------------------------


class TestMarkTaskNeedsReview:
    """Tests for submit_for_review tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with session_context_for_test("sess-1"):
            yield

    @pytest.mark.asyncio
    async def test_mark_needs_review_success(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.submit_for_review.return_value = task
        registry = _create_stage_ops_registry(mock_task_manager)

        result = await registry.call(
            "submit_for_review",
            {"task_id": task.id, "stage_name": "planning"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_mark_needs_review_with_notes(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="in_progress", description="Original")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.submit_for_review.return_value = task
        registry = _create_stage_ops_registry(mock_task_manager)

        result = await registry.call(
            "submit_for_review",
            {
                "task_id": task.id,
                "stage_name": "planning",
                "review_notes": "Please check the output",
            },
        )
        assert "error" not in result
        # a2b779f60 (#19368) dropped repair_submission with the rest of the
        # repair-proof gating; no production caller passes it any more.
        mock_task_manager.submit_for_review.assert_called_once_with(
            task.id,
            "planning",
            review_notes="Please check the output",
            by_session_id=ANY,
        )

    @pytest.mark.asyncio
    async def test_mark_needs_review_update_fails(self, mock_task_manager: MagicMock) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.submit_for_review.return_value = None
        registry = _create_stage_ops_registry(mock_task_manager)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review._auto_link_session_commits"
            ) as auto_link,
            patch(
                "gobby.mcp_proxy.tools.tasks._stage_review._release_current_agent_dispatch_mutex"
            ) as release,
        ):
            result = await registry.call(
                "submit_for_review",
                {"task_id": task.id, "stage_name": "planning"},
            )
        assert "error" in result
        assert "Failed to submit" in result["error"]
        auto_link.assert_called_once()
        release.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_needs_review_blocks_scope_mismatch(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        registry = _create_stage_ops_registry(mock_task_manager)
        scope = TaskScopeEvaluation(
            declared_paths=("tests/",),
            actual_paths=("src/gobby/service.py",),
            out_of_scope_paths=("src/gobby/service.py",),
            justification_error="A scope_justification is required for out-of-scope paths.",
        )

        with patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.evaluate_task_scope",
            return_value=scope,
        ):
            result = await registry.call(
                "submit_for_review",
                {"task_id": task.id, "stage_name": "planning"},
            )

        assert result["success"] is False
        assert result["error"] == "task_scope_mismatch"
        assert result["out_of_scope_paths"] == ["src/gobby/service.py"]
        mock_task_manager.submit_for_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_needs_review_records_scope_justification(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.submit_for_review.return_value = task
        registry = _create_stage_ops_registry(mock_task_manager)
        justification = "The shared implementation path is required by these scoped tests."
        scope = TaskScopeEvaluation(
            declared_paths=("tests/",),
            actual_paths=("src/gobby/service.py",),
            out_of_scope_paths=("src/gobby/service.py",),
            scope_justification=justification,
        )

        with patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.evaluate_task_scope",
            return_value=scope,
        ):
            result = await registry.call(
                "submit_for_review",
                {
                    "task_id": task.id,
                    "stage_name": "planning",
                    "review_notes": "Review implementation.",
                    "scope_justification": justification,
                },
            )

        assert "error" not in result
        mock_task_manager.submit_for_review.assert_called_once_with(
            task.id,
            "planning",
            review_notes=(f"Review implementation.\n\n[Task Scope Justification]\n{justification}"),
            by_session_id=ANY,
        )

    @pytest.mark.asyncio
    async def test_mark_needs_review_not_found(self, mock_task_manager: MagicMock) -> None:
        mock_task_manager.get_task.return_value = None
        registry = _create_stage_ops_registry(mock_task_manager)

        result = await registry.call(
            "submit_for_review",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "stage_name": "planning"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_mark_needs_review_clears_claimed_tasks_variable(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Needs-review transition removes the task from the prior owner's claimed_tasks."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        task = _make_task(id=task_id, status="in_progress", claimed_by_session_id=session_id)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.submit_for_review.return_value = task

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

            registry = _create_stage_ops_registry(mock_task_manager)
            result = await registry.call(
                "submit_for_review",
                {"task_id": task_id, "stage_name": "planning"},
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
    """close_task fallback to task.claimed_by_session_id / error when no ContextVar."""

    @pytest.mark.asyncio
    async def test_close_task_without_session_context_falls_back_to_claimed_by_session_id(
        self, mock_task_manager: MagicMock
    ) -> None:
        """No SessionContext → uses task.claimed_by_session_id for the audit write."""
        claimed_session = "claimed-session-uuid"
        task = _make_task(claimed_by_session_id=claimed_session, commits=None)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            registry = _create_registry(mock_task_manager)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "done"},
            )

        assert "error" not in result
        close_kwargs = mock_task_manager.close_task.call_args.kwargs
        assert close_kwargs.get("closed_in_session_id") == "resolved-session"

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


def test_close_task_git_helper_calls_follow_repo_path_resolution() -> None:
    """close_task must resolve project_path before commit/Git helper cwd use."""
    import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle_close

    evaluation_source = inspect.getsource(lifecycle_close._evaluate_close)
    evaluation = ast.parse(evaluation_source)
    evaluation_lines = _call_line_numbers(evaluation)

    resolver_line = evaluation_lines["resolve_task_repo_path"]
    assert resolver_line < evaluation_lines["resolve_close_commit_shas"]
    assert resolver_line < evaluation_lines["validate_commit_requirements"]
    assert evaluation_source.index("resolve_task_repo_path(") < evaluation_source.index(
        "collect_commit_diff_text,"
    )

    commit = ast.parse(inspect.getsource(lifecycle_close._commit_close))
    commit_lines = _call_line_numbers(commit)
    assert commit_lines["resolve_close_commit_shas"] < commit_lines["link_close_commit_shas"]


def _call_line_numbers(function: ast.AST) -> dict[str, int]:
    lines: dict[str, int] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node)
        if call_name and call_name not in lines:
            lines[call_name] = node.lineno
    return lines


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        value = call.func.value
        if isinstance(value, ast.Name):
            return f"{value.id}.{call.func.attr}"
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return f"{value.value.id}.{value.attr}.{call.func.attr}"
        return call.func.attr
    return None

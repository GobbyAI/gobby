"""Focused coverage tests for task MCP tools."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry as _create_task_registry
from gobby.storage.tasks import Task
from gobby.tasks.close_verdict import CloseVerdict
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit
TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])


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


def create_task_registry(
    task_manager: Any,
    *args: Any,
    with_passing_validator: bool = False,
    **kwargs: Any,
) -> Any:
    if with_passing_validator:
        assert not args
        assert "task_validator_resolver" not in kwargs
        validator = AsyncMock()
        validator.validate_task.return_value = CloseVerdict(
            status="valid",
            criteria=(),
            feedback="Every criterion is satisfied by admissible evidence.",
        )
        kwargs["task_validator_resolver"] = lambda: validator
    return _create_task_registry(task_manager, *args, **kwargs)


@pytest.fixture(autouse=True)
def _disable_validation_backoff_storage() -> Iterator[None]:
    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_validation.TaskValidationBackoffStore.get",
            return_value=None,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_validation._record_validation_iteration",
            return_value=1,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration"
            ".TaskCloseReviewStore.get_active_for_task",
            return_value=None,
        ),
    ):
        yield


class TestCloseTaskTool:
    """Tests for close_task MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with (
            session_context_for_test("test-session"),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.collect_commit_diff_text",
                return_value=(
                    "diff --git a/src/example.py b/src/example.py\n"
                    "--- a/src/example.py\n"
                    "+++ b/src/example.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+example\n"
                ),
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_close_task_not_found(self, mock_task_manager: MagicMock) -> None:
        """Test close_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.get_task.return_value = None

        result = await registry.call(
            "close_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result
        assert result["error"] == "task_not_found"

    @pytest.mark.asyncio
    async def test_close_task_no_commits_error(self, mock_task_manager: MagicMock) -> None:
        """Test close_task requires commits to be linked."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            MockSessionManager.return_value.resolve_session_reference.return_value = "test-session"
            MockSVManager.return_value.get_variables.return_value = {
                "task_edited_files": {"550e8400-e29b-41d4-a716-446655440000": ["src/owned.py"]},
            }
            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "test changes",
                    "response_detail": "diagnostic",
                },
            )

            assert "error" in result
            assert result["error"] == "no_commits_linked"
            assert result["attributed_paths"] == ["src/owned.py"]

    @pytest.mark.asyncio
    async def test_close_task_with_skip_reason_skips_commit_check(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test close_task with skip reason bypasses commit check."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.requires_user_review = False  # Avoid review routing
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No children

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "duplicate",
                    "changes_summary": "test changes",
                },
            )

            assert "error" not in result
            mock_task_manager.close_task.assert_called_once()
            assert mock_task_manager.close_task.call_count == 1
            assert mock_task_manager.close_task.call_args is not None

    @pytest.mark.asyncio
    async def test_close_task_parent_with_open_children(self, mock_task_manager: MagicMock) -> None:
        """Test close_task fails for parent with open children."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440020"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
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
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task", {"task_id": "550e8400-e29b-41d4-a716-446655440020"}
            )

            assert "error" in result
            assert result["error"] == "children_open"
            assert "open_children" in result

    @pytest.mark.asyncio
    async def test_close_task_success_with_commits(self, mock_task_manager: MagicMock) -> None:
        """Test close_task succeeds when commits are linked."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.requires_user_review = False  # Explicitly set to avoid review routing
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No children

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "test changes",
                },
            )

            assert result["success"] is True
            assert mock_task_manager.close_task.call_count == 1

    @pytest.mark.asyncio
    async def test_close_task_uses_latest_linked_commit_for_closed_commit_sha(
        self, mock_task_manager: MagicMock
    ) -> None:
        """A repaired task should close against its linked repair commit, not ambient HEAD."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["old-commit", "repair-commit"]
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "ambient-head"

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "Closed after coordinator repair.",
                },
            )

            assert result["success"] is True
            close_call = mock_task_manager.close_task.call_args
            assert close_call.kwargs["closed_commit_sha"] == "repair-commit"
            mock_git.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_reports_auto_closed_ancestors(
        self, mock_task_manager: MagicMock
    ) -> None:
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.task_type = "epic"
        mock_task.seq_num = 20557
        mock_task.requires_user_review = False
        ancestor = _contract_task()
        ancestor.id = "ancestor-epic-id"
        ancestor.title = "Phase epic"
        ancestor.task_type = "epic"
        ancestor.seq_num = 20555
        ancestor.project_id = mock_task.project_id

        def _close_task(*_args: object, **kwargs: object) -> Any:
            collected = kwargs.get("closed_ancestors")
            if isinstance(collected, list):
                collected.append(ancestor.id)
            return mock_task

        def _get_task(task_id: str) -> Any:
            if task_id == ancestor.id:
                return ancestor
            return mock_task

        mock_task_manager.get_task.side_effect = _get_task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.side_effect = _close_task

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command", return_value="abc123"),
            patch("gobby.hooks.event_handlers._plan.on_epic_terminal") as archive,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "Landed the leaf.",
                },
            )

        assert result["success"] is True
        assert result["closed_ancestors"] == [
            {"id": ancestor.id, "ref": "#20555", "title": "Phase epic"}
        ]
        archive.assert_called_once()
        event = archive.call_args.args[0]
        assert event["task_ref"] == "#20557"

    @pytest.mark.asyncio
    async def test_close_task_with_commit_sha_links_after_evaluation(
        self, mock_task_manager: MagicMock, tmp_path: Path
    ) -> None:
        """A prospective commit is linked only after the checklist passes."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.link_commit.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            expected_repo_path = str((tmp_path / "repo").resolve())
            (tmp_path / "repo").mkdir()
            mock_proj_instance = MagicMock()
            mock_project = MagicMock()
            mock_project.repo_path = expected_repo_path
            mock_proj_instance.get.return_value = mock_project
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            # Build the registry inside the patch so RegistryContext picks up
            # the mocked LocalProjectManager.
            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "commit_sha": "new-commit",
                    "changes_summary": "Closed with the explicitly linked commit.",
                },
            )

            assert result["closed"] is True
            call_args = mock_task_manager.link_commit.call_args
            assert call_args[0] == ("550e8400-e29b-41d4-a716-446655440000", "new-commit")
            assert call_args.kwargs["cwd"] == expected_repo_path

            close_call = mock_task_manager.close_task.call_args
            assert close_call[0] == ("550e8400-e29b-41d4-a716-446655440000",)
            assert close_call.kwargs["closed_commit_sha"] == "new-commit"

    @pytest.mark.asyncio
    async def test_close_task_fails_when_commit_sha_cannot_be_resolved(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test close_task surfaces explicit commit SHA resolution failures."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.requires_user_review = False
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.link_commit.side_effect = ValueError(
            "Invalid or unresolved commit SHA: bad-sha"
        )

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager:
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "commit_sha": "bad-sha",
                    "changes_summary": "test changes",
                },
            )

        assert result["success"] is False
        assert result["error"] == "invalid_commit_sha"
        mock_task_manager.link_commit.assert_not_called()
        assert mock_task_manager.close_task.call_count == 0

    @pytest.mark.asyncio
    async def test_close_task_rejects_commit_when_repo_path_is_unresolved(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Commit operations fail closed when the task repository cannot be resolved."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.claimed_by_session_id = None
        mock_task_manager.get_task.return_value = mock_task

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.tasks.commits.auto_link_commits") as mock_autolink,
            patch("gobby.utils.git.normalize_commit_sha") as mock_normalize,
            patch("gobby.utils.git.run_git_command") as mock_run_git,
        ):
            MockProjManager.return_value.get.return_value = None
            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": mock_task.id,
                    "commit_sha": "abc1234",
                    "changes_summary": "test changes",
                },
            )

        assert result["success"] is False
        assert result["error"] == "task_repo_path_unavailable"
        assert result["message"] == "close_task requires a registered repository path."
        assert mock_task_manager.link_commit.call_count == 0
        assert mock_run_git.call_count == 0
        mock_task_manager.link_commit.assert_not_called()
        mock_autolink.assert_not_called()
        mock_normalize.assert_not_called()
        mock_run_git.assert_not_called()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_rejects_linked_commit_when_repo_path_is_unresolved(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Linked commits are not normalized against the daemon working directory."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc1234"]
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.claimed_by_session_id = None
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.tasks.commits.auto_link_commits") as mock_autolink,
            patch("gobby.utils.git.normalize_commit_sha") as mock_normalize,
            patch("gobby.utils.git.run_git_command") as mock_run_git,
        ):
            MockProjManager.return_value.get.return_value = None
            MockSVM.return_value.get_variables.return_value = {}
            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": mock_task.id,
                    "changes_summary": "test changes",
                },
            )

        assert result["error"] == "task_repo_path_unavailable"
        assert result["success"] is False
        mock_autolink.assert_not_called()
        mock_normalize.assert_not_called()
        mock_run_git.assert_not_called()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_rejects_claim_autolink_when_repo_path_is_unresolved(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Claim-window commit discovery requires the task repository path."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.claimed_by_session_id = "test-session"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as MockSTM,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.tasks.commits.auto_link_commits") as mock_autolink,
        ):
            MockProjManager.return_value.get.return_value = None
            MockSessionManager.return_value.resolve_session_reference.return_value = "test-session"
            MockSTM.return_value.get_task_sessions.return_value = [
                {
                    "action": "claimed",
                    "session_id": "test-session",
                    "created_at": "2026-07-12T10:00:00+00:00",
                }
            ]
            MockSVM.return_value.get_variables.return_value = {}
            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": mock_task.id,
                    "changes_summary": "test changes",
                },
            )

        assert result["error"] == "task_repo_path_unavailable"
        assert result["success"] is False
        mock_autolink.assert_not_called()
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_out_of_repo_blocked_when_target_task_has_edits(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test out_of_repo reason still enforces commit check for target-task edits."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        def fake_run_git_command(
            command: list[str], cwd: str | None = None, timeout: int = 5
        ) -> str | None:
            del cwd, timeout
            if command[:2] == ["git", "check-ignore"]:
                return None  # exit 1 => path is NOT gitignored (a real tracked file)
            return "abc123"

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch(
                "gobby.utils.git.run_git_command",
                side_effect=fake_run_git_command,
            ),
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            mock_session_instance = MagicMock()
            mock_session_instance.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_instance
            MockSVManager.return_value.get_variables.return_value = {
                "task_edited_files": {"550e8400-e29b-41d4-a716-446655440000": ["src/owned.py"]},
            }
            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "out_of_repo",
                    "changes_summary": "test changes",
                },
            )

            assert result.get("error") == "no_commits_linked"
            mock_task_manager.close_task.assert_not_called()
            assert mock_task_manager.close_task.call_count == 0
            assert not mock_task_manager.close_task.called

    @pytest.mark.asyncio
    async def test_close_task_succeeds_when_only_gitignored_paths_edited(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Gitignored-only edits (e.g. a vault under wiki/) never need a commit."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.requires_user_review = False
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        def fake_run_git_command(
            command: list[str], cwd: str | None = None, timeout: int = 5
        ) -> str:
            del cwd, timeout
            if command[:2] == ["git", "check-ignore"]:
                return ""  # exit 0 => path IS gitignored
            return "abc123"

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch(
                "gobby.utils.git.run_git_command",
                side_effect=fake_run_git_command,
            ),
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            mock_session_instance = MagicMock()
            mock_session_instance.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_instance
            MockSVManager.return_value.get_variables.return_value = {
                "task_edited_files": {
                    "550e8400-e29b-41d4-a716-446655440000": ["wiki/knowledge/topics/x.md"],
                },
            }
            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "vault-only research notes",
                },
            )

            assert result["success"] is True
            assert mock_task_manager.close_task.call_count == 1
            assert mock_task_manager.link_commit.call_count == 0
            mock_task_manager.close_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_task_out_of_repo_succeeds_with_unrelated_task_edits(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test out_of_repo reason succeeds when only another task has edits."""
        mock_task = _contract_task()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = "Test task completion is observable."
        mock_task.requires_user_review = False
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            mock_session_instance = MagicMock()
            mock_session_instance.resolve_session_reference.return_value = "test-session"
            MockSessionManager.return_value = mock_session_instance
            MockSVManager.return_value.get_variables.return_value = {
                "task_edited_files": {"other-task": ["src/other.py"]},
            }
            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "out_of_repo",
                    "changes_summary": "test changes",
                },
            )

            assert result["success"] is True
            mock_task_manager.close_task.assert_called_once()
            assert mock_task_manager.close_task.call_count == 1
            assert mock_task_manager.close_task.call_args is not None

    @pytest.mark.asyncio
    async def test_close_task_clears_task_claimed_variables(
        self, mock_task_manager: MagicMock
    ) -> None:
        """close_task must remove task from claimed_tasks session variables.

        Regression test for #9064: after successful close, the workflow state
        variables were not cleared due to scoping issues, causing stale
        'Task (unknown) is still in_progress' blocks after compaction.
        """
        task_uuid = "550e8400-e29b-41d4-a716-446655440000"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            # Session variables with only this task claimed
            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_uuid: "#42"},
                "active_task_id": task_uuid,
                "task_has_commits": True,
                "task_edited_files": {task_uuid: ["src/owned.py"]},
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "11111111-1111-4111-8111-111111110001"
            mock_task.validation_criteria = "Test task completion is observable."
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {
                    "task_id": task_uuid,
                    "changes_summary": "test changes",
                },
            )

            assert "error" not in result
            # Variables must show task removed from claimed_tasks
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": False,
                    "claimed_tasks": {},
                    "active_task_id": None,
                    "task_has_commits": False,
                    "task_edited_files": {},
                },
            )
            assert mock_sv_manager.merge_variables.call_count == 1
            assert mock_sv_manager.merge_variables.call_args is not None

    @pytest.mark.asyncio
    async def test_close_task_prunes_only_target_task_edit_state(
        self, mock_task_manager: MagicMock
    ) -> None:
        """close_task must preserve edit state for other claimed tasks."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440000"
        other_task_uuid = "550e8400-e29b-41d4-a716-446655440001"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_session_task_manager = MagicMock()
            MockSessionTaskManager.return_value = mock_session_task_manager

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_uuid: "#42", other_task_uuid: "#43"},
                "active_task_id": task_uuid,
                "task_edited_files": {
                    task_uuid: ["src/owned.py"],
                    other_task_uuid: ["src/other.py"],
                },
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "11111111-1111-4111-8111-111111110001"
            mock_task.validation_criteria = "Test task completion is observable."
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {
                    "task_id": task_uuid,
                    "changes_summary": "test changes",
                },
            )

            assert "error" not in result
            mock_task_manager.close_task.assert_called_once()
            close_call = mock_task_manager.close_task.call_args
            assert close_call.args == (task_uuid,)
            assert close_call.kwargs["closed_in_session_id"] == "test-session"
            assert close_call.kwargs["closed_commit_sha"] == "abc123"
            mock_session_task_manager.link_task.assert_called_once_with(
                "test-session",
                task_uuid,
                "closed",
            )
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": True,
                    "claimed_tasks": {other_task_uuid: "#43"},
                    "active_task_id": other_task_uuid,
                    "task_edited_files": {other_task_uuid: ["src/other.py"]},
                },
            )
            mock_session_manager.clear_had_edits.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("lookup_error", "is_expected_lookup_failure"),
        [
            (KeyError("vars unavailable"), True),
            (ValueError("vars unavailable"), True),
            (TypeError("vars unavailable"), True),
            (RuntimeError("programming error"), False),
        ],
    )
    async def test_close_task_fails_closed_when_owner_variables_cannot_load(
        self,
        mock_task_manager: MagicMock,
        lookup_error: Exception,
        is_expected_lookup_failure: bool,
    ) -> None:
        task_uuid = "550e8400-e29b-41d4-a716-446655440000"

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
        ):
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "closer-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.side_effect = lookup_error
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(mock_task_manager)
            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.claimed_by_session_id = "owner-session"
            mock_task.commits = []
            mock_task.project_id = "11111111-1111-4111-8111-111111110001"
            mock_task.validation_criteria = "Test task completion is observable."
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            close_arguments = {
                "task_id": task_uuid,
                "changes_summary": "test changes",
            }
            if not is_expected_lookup_failure:
                with pytest.raises(RuntimeError, match="programming error"):
                    await registry.call("close_task", close_arguments)
                mock_task_manager.close_task.assert_not_called()
                return
            result = await registry.call("close_task", close_arguments)

        assert result["success"] is False
        assert result["error"] == "session_variable_lookup_failed"
        mock_sv_manager.get_variables.assert_called_once_with("owner-session")
        mock_task_manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_task_uses_owner_variables_and_refetches_before_merge(
        self, mock_task_manager: MagicMock
    ) -> None:
        task_uuid = "550e8400-e29b-41d4-a716-446655440000"
        concurrent_uuid = "550e8400-e29b-41d4-a716-446655440099"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command", return_value="abc123"),
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            MockSessionTaskManager.return_value = MagicMock()

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "closer-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            initial_owner_vars = {
                "task_claimed": True,
                "claimed_tasks": {task_uuid: "#42"},
                "active_task_id": task_uuid,
                "task_edited_files": {task_uuid: ["src/owned.py"]},
            }
            fresh_owner_vars = {
                "task_claimed": True,
                "claimed_tasks": {task_uuid: "#42", concurrent_uuid: "#99"},
                "active_task_id": task_uuid,
                "task_edited_files": {
                    task_uuid: ["src/owned.py"],
                    concurrent_uuid: ["src/concurrent.py"],
                },
            }
            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.side_effect = [
                initial_owner_vars,
                fresh_owner_vars,
                fresh_owner_vars,
            ]
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )
            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.claimed_by_session_id = "owner-session"
            mock_task.commits = ["abc123"]
            mock_task.project_id = "11111111-1111-4111-8111-111111110001"
            mock_task.validation_criteria = "Test task completion is observable."
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {
                    "task_id": task_uuid,
                    "changes_summary": "test changes",
                },
            )

        assert "error" not in result
        assert mock_sv_manager.get_variables.call_args_list == [
            call("owner-session"),
            call("owner-session"),
            call("owner-session"),
        ]
        mock_sv_manager.merge_variables.assert_called_once_with(
            "owner-session",
            {
                "task_claimed": True,
                "claimed_tasks": {concurrent_uuid: "#99"},
                "active_task_id": concurrent_uuid,
                "task_edited_files": {concurrent_uuid: ["src/concurrent.py"]},
            },
        )


# =============================================================================
# reopen_task Tool Tests
# =============================================================================


class TestReopenTaskTool:
    """Tests for reopen_task MCP tool."""

    @pytest.mark.asyncio
    async def test_reopen_task_success(self, mock_task_manager: MagicMock) -> None:
        """Test reopen_task successfully reopens a closed task."""
        registry = create_task_registry(mock_task_manager)

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
    async def test_reopen_task_with_reason(self, mock_task_manager: MagicMock) -> None:
        """Test reopen_task with a reason."""
        registry = create_task_registry(mock_task_manager)

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
        assert mock_task_manager.reopen_task.call_count >= 1
        assert mock_task_manager.reopen_task.call_args is not None

    @pytest.mark.asyncio
    async def test_reopen_task_error(self, mock_task_manager: MagicMock) -> None:
        """Test reopen_task returns error on failure."""
        registry = create_task_registry(mock_task_manager)

        mock_task_manager.reopen_task.side_effect = ValueError("Task not found")

        result = await registry.call(
            "reopen_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result

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


# =============================================================================
# delete_task Tool Tests
# =============================================================================


class TestSessionVariableMirroring:
    """Tests that task lifecycle operations mirror state to session_variables.

    The rule engine evaluates session_variables (higher precedence) over
    workflow_states. If task tools only write to workflow_states, the
    require-task-close rule never blocks the stop hook.
    """

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with (
            session_context_for_test("test-session"),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.collect_commit_diff_text",
                return_value=(
                    "diff --git a/src/example.py b/src/example.py\n"
                    "--- a/src/example.py\n"
                    "+++ b/src/example.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+example\n"
                ),
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_claim_task_mirrors_to_session_variables(
        self, mock_task_manager: MagicMock
    ) -> None:
        """claim_task must mirror task_claimed=True to session_variables."""
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

            result = await registry.call(
                "claim_task",
                {"task_id": task_uuid},
            )

            assert "error" not in result
            # session_variables must be written with task in claimed_tasks dict
            mock_sv_manager.merge_variables.assert_called_once()
            call_args = mock_sv_manager.merge_variables.call_args
            merged = call_args[0][1]
            assert merged["task_claimed"] is True
            assert task_uuid in merged["claimed_tasks"]
            assert merged["claimed_tasks"][task_uuid] == "#200"
            assert merged["active_task_id"] == task_uuid

    @pytest.mark.asyncio
    async def test_close_task_mirrors_clear_to_session_variables(
        self, mock_task_manager: MagicMock
    ) -> None:
        """close_task must clear claim and commit state in session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
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
                "claimed_tasks": {task_uuid: "#200"},
                "active_task_id": task_uuid,
                "task_edited_files": {task_uuid: ["src/owned.py"]},
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(
                mock_task_manager,
                with_passing_validator=True,
            )

            mock_task = _contract_task()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "11111111-1111-4111-8111-111111110001"
            mock_task.validation_criteria = "Test task completion is observable."
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {"task_id": task_uuid, "changes_summary": "done"},
            )

            assert "error" not in result
            # session_variables must show task removed
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": False,
                    "claimed_tasks": {},
                    "active_task_id": None,
                    "task_edited_files": {},
                    "task_has_commits": False,
                },
            )
            assert mock_sv_manager.merge_variables.call_count == 1
            assert mock_sv_manager.merge_variables.call_args is not None

    @pytest.mark.asyncio
    async def test_create_task_with_claim_mirrors_to_session_variables(
        self, mock_task_manager: MagicMock
    ) -> None:
        """create_task(claim=True) must mirror task_claimed=True to session_variables."""
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
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": task_uuid},
            }
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
                # session_variables must be written with task in claimed_tasks dict
                mock_sv_manager.merge_variables.assert_called_once()
                call_args = mock_sv_manager.merge_variables.call_args
                merged = call_args[0][1]
                assert merged["task_claimed"] is True
                assert task_uuid in merged["claimed_tasks"]
                assert merged["claimed_tasks"][task_uuid] == "#300"
                assert merged["active_task_id"] == task_uuid

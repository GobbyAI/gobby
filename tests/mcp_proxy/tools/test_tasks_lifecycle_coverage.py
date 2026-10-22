"""Task lifecycle contracts through the public MCP registry."""

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry as _create_task_registry
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.tasks import Task
from gobby.utils.daemon_git import daemon_git
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


@dataclass
class CloseCase:
    registry: InternalToolRegistry
    manager: MagicMock
    sessions: MagicMock
    variables: MagicMock
    session_tasks: MagicMock
    task: Task
    repo: Path
    sha: str

    async def close(self, **kwargs: Any) -> dict[str, Any]:
        result = await self.registry.call("close_task", {"task_id": self.task.id, **kwargs})
        assert isinstance(result, dict)
        return result

    def attribute(self, paths: list[str]) -> dict[str, Any]:
        return {
            "task_edited_files": {self.task.id: paths},
            "task_edited_file_checkouts": {self.task.id: {str(self.repo): paths}},
        }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def close_case(tmp_path: Path, mock_task_manager: MagicMock) -> Iterator[CloseCase]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "owned.py").write_text("owned = 1\n")
    (repo / "other.py").write_text("other = 1\n")
    (repo / ".gitignore").write_text("scratch/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    sha = _git(repo, "rev-parse", "HEAD")
    task = _contract_task()
    task.seq_num = 42
    task.commits = [sha]
    task.claimed_by_session_id = "owner-session"
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_manager.close_task.return_value = task
    mock_task_manager.close_task_with_commit.return_value = task
    sessions = _checkout_session_manager()
    variables = MagicMock()
    variables.get_variables.return_value = {}
    session_tasks = MagicMock()
    projects = MagicMock()
    projects.get.return_value = MagicMock()
    projects.db.fetchone.return_value = None
    projects.db.fetchall.return_value = []
    with (
        session_context_for_test("closer-session"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager", return_value=sessions),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionVariableManager", return_value=variables
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager", return_value=session_tasks
        ),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager", return_value=projects),
        patch("gobby.mcp_proxy.tools.task_repo_paths.require_root", return_value=str(repo)),
        patch("gobby.storage.project_checkouts.require_root", return_value=str(repo)),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.resolve_close_worktree_root_async",
            new=AsyncMock(return_value=SimpleNamespace(repo_path=None)),
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.main_checkout_target",
            new=AsyncMock(return_value=(repo, "main")),
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._close_context.foreign_owned_dirty_paths_async",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization._queue_close_memory_review"
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization.notify_parent_on_task_state_change"
        ),
        patch("gobby.sessions.title_lifecycle.recompute_automatic_title"),
    ):
        yield CloseCase(
            _create_task_registry(mock_task_manager),
            mock_task_manager,
            sessions,
            variables,
            session_tasks,
            task,
            repo,
            sha,
        )


class TestCloseTaskTool:
    @pytest.mark.asyncio
    async def test_close_task_not_found(self, close_case: CloseCase) -> None:
        close_case.manager.get_task.side_effect = ValueError("Task not found")
        result = await close_case.close()
        assert result["error"] == "task_not_found"
        close_case.manager.close_task.assert_not_called()
        close_case.manager.close_task_with_commit.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason,expected", [("completed", False), ("duplicate", True)])
    async def test_commitless_close_requires_no_work_reason(
        self, close_case: CloseCase, reason: str, expected: bool
    ) -> None:
        close_case.task.commits = []
        result = await close_case.close(reason=reason)
        assert result["success"] is expected
        assert result["closed"] is expected
        assert result.get("error") == (None if expected else "linked_commits")
        assert close_case.manager.close_task.call_count == int(expected)
        close_case.manager.link_commit.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preview", [False, True])
    async def test_parent_with_open_children(self, close_case: CloseCase, preview: bool) -> None:
        child = _contract_task()
        child.id = "child-id"
        close_case.manager.list_tasks.return_value = [child]
        result = await close_case.close(preview=preview)
        assert result["success"] is False
        assert result["can_close"] is False
        assert result["closed"] is False
        assert result["error"] == "children_closed"
        close_case.manager.close_task.assert_not_called()
        close_case.manager.close_task_with_commit.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("explicit_candidate", [False, True])
    async def test_close_records_candidate_atomically(
        self, close_case: CloseCase, explicit_candidate: bool
    ) -> None:
        if explicit_candidate:
            (close_case.repo / "other.py").write_text("other = 2\n")
            _git(close_case.repo, "add", "other.py")
            _git(close_case.repo, "commit", "-m", "later commit")
            newer = _git(close_case.repo, "rev-parse", "HEAD")
            assert newer != close_case.sha
            close_case.task.commits = [close_case.sha, newer]
        arguments = {"commit_sha": close_case.sha[:12]} if explicit_candidate else {}
        result = await close_case.close(**arguments)
        assert result["closed"] is True
        close_case.manager.link_commit.assert_not_called()
        if explicit_candidate:
            close_case.manager.close_task.assert_not_called()
            method = close_case.manager.close_task_with_commit
            assert method.call_args.args == (close_case.task.id, close_case.sha)
            assert method.call_args.kwargs["cwd"] == str(close_case.repo)
        else:
            close_case.manager.close_task_with_commit.assert_not_called()
            method = close_case.manager.close_task
            assert method.call_args.args == (close_case.task.id,)
            assert method.call_args.kwargs["closed_commit_sha"] == close_case.sha
        method.assert_called_once()
        assert method.call_args.kwargs["closed_in_session_id"] == "closer-session"

    @pytest.mark.asyncio
    async def test_new_candidate_is_linked_only_after_clean_evaluation(
        self, close_case: CloseCase
    ) -> None:
        close_case.task.commits = []
        close_case.variables.get_variables.return_value = close_case.attribute(["owned.py"])
        (close_case.repo / "owned.py").write_text("owned = 2\n")
        refused = await close_case.close(commit_sha=close_case.sha)
        assert refused["error"] == "uncommitted_task_edits"
        close_case.manager.link_commit.assert_not_called()
        close_case.manager.close_task_with_commit.assert_not_called()
        _git(close_case.repo, "add", "owned.py")
        _git(close_case.repo, "commit", "-m", "task change")
        candidate = _git(close_case.repo, "rev-parse", "HEAD")
        closed = await close_case.close(commit_sha=candidate)
        assert closed["closed"] is True
        assert close_case.manager.close_task_with_commit.call_args.args == (
            close_case.task.id,
            candidate,
        )
        close_case.manager.link_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_reports_auto_closed_ancestors(self, close_case: CloseCase) -> None:
        close_case.task.task_type = "epic"
        ancestor = _contract_task()
        ancestor.id, ancestor.seq_num, ancestor.title = "ancestor-id", 20555, "Phase epic"

        def close(*_args: object, closed_ancestors: list[str], **_kwargs: object) -> Task:
            closed_ancestors.append(ancestor.id)
            return close_case.task

        close_case.manager.get_task.side_effect = lambda task_id: (
            ancestor if task_id == ancestor.id else close_case.task
        )
        close_case.manager.close_task.side_effect = close
        with patch("gobby.hooks.event_handlers._plan.on_epic_terminal") as archive:
            result = await close_case.close()
        assert result["success"] is True
        assert result["closed_ancestors"] == [
            {"id": ancestor.id, "ref": "#20555", "title": "Phase epic"}
        ]
        archive.assert_called_once()
        assert archive.call_args.args[0]["task_ref"] == "#42"

    @pytest.mark.asyncio
    async def test_unresolved_candidate_never_links(self, close_case: CloseCase) -> None:
        result = await close_case.close(commit_sha="bad-sha")
        assert result["success"] is False
        assert result["error"] == "invalid_commit_sha"
        close_case.manager.link_commit.assert_not_called()
        close_case.manager.close_task.assert_not_called()
        close_case.manager.close_task_with_commit.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("commit_mode", ["explicit", "linked", "claimed"])
    async def test_unresolved_repository_refuses_before_git(
        self, close_case: CloseCase, commit_mode: str
    ) -> None:
        if commit_mode != "linked":
            close_case.task.commits = []
        arguments = {"commit_sha": close_case.sha} if commit_mode == "explicit" else {}
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.resolve_task_repo_path",
                side_effect=CheckoutNotFoundError("no checkout"),
            ),
            patch.object(daemon_git, "run", new_callable=AsyncMock) as git,
        ):
            result = await close_case.close(**arguments)
        assert result["success"] is False
        assert result["error"] == "invalid_project_path"
        git.assert_not_awaited()
        close_case.manager.link_commit.assert_not_called()
        close_case.manager.close_task.assert_not_called()
        close_case.manager.close_task_with_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_work_reason_still_refuses_target_dirt(self, close_case: CloseCase) -> None:
        close_case.task.commits = []
        close_case.variables.get_variables.return_value = close_case.attribute(["owned.py"])
        (close_case.repo / "owned.py").write_text("owned = 2\n")
        result = await close_case.close(reason="out_of_repo")
        assert result["error"] == "uncommitted_task_edits"
        close_case.manager.close_task.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scoped_ledger", [False, True])
    async def test_ignored_edits_need_checkout_attribution(
        self, close_case: CloseCase, scoped_ledger: bool
    ) -> None:
        close_case.task.commits = []
        (close_case.repo / "scratch").mkdir()
        (close_case.repo / "scratch/notes.md").write_text("notes\n")
        variables = close_case.attribute(["scratch/notes.md"])
        if not scoped_ledger:
            variables.pop("task_edited_file_checkouts")
        close_case.variables.get_variables.return_value = variables
        result = await close_case.close(reason="out_of_repo")
        assert result["success"] is scoped_ledger
        assert result.get("error") == (None if scoped_ledger else "uncommitted_task_edits")
        assert close_case.manager.close_task.call_count == int(scoped_ledger)
        close_case.manager.link_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_work_close_ignores_other_task_dirt(self, close_case: CloseCase) -> None:
        close_case.task.commits = []
        (close_case.repo / "other.py").write_text("other = 2\n")
        close_case.variables.get_variables.return_value = {
            "task_edited_files": {"other-task": ["other.py"]},
            "task_edited_file_checkouts": {"other-task": {str(close_case.repo): ["other.py"]}},
        }
        result = await close_case.close(reason="out_of_repo")
        assert result["closed"] is True
        close_case.manager.close_task.assert_called_once()
        assert close_case.variables.merge_variables.call_args.args[1]["task_edited_files"] == {
            "other-task": ["other.py"]
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("other_claim", [False, True])
    async def test_close_cleans_only_target_claim_and_attribution(
        self, close_case: CloseCase, other_claim: bool
    ) -> None:
        variables = close_case.attribute(["owned.py"])
        variables.update(
            task_claimed=True,
            claimed_tasks={close_case.task.id: "#42"},
            active_task_id=close_case.task.id,
        )
        if other_claim:
            variables["claimed_tasks"]["other-task"] = "#43"
            variables["task_edited_files"]["other-task"] = ["other.py"]
            variables["task_edited_file_checkouts"]["other-task"] = {
                str(close_case.repo): ["other.py"]
            }
        close_case.variables.get_variables.return_value = variables
        result = await close_case.close(commit_sha=close_case.sha)
        assert result["closed"] is True
        close_case.session_tasks.link_task.assert_called_once_with(
            "closer-session", close_case.task.id, "closed"
        )
        close_case.variables.merge_variables.assert_called_once_with(
            "owner-session",
            {
                "task_claimed": other_claim,
                "claimed_tasks": {"other-task": "#43"} if other_claim else {},
                "active_task_id": None,
                **({"task_has_commits": False} if not other_claim else {}),
                "task_edited_files": {"other-task": ["other.py"]} if other_claim else {},
                "task_edited_file_checkouts": (
                    {"other-task": {str(close_case.repo): ["other.py"]}} if other_claim else {}
                ),
            },
        )
        assert close_case.sessions.clear_had_edits.call_count == int(not other_claim)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("evaluation_read", [1, 2])
    @pytest.mark.parametrize(
        "lookup_error",
        [
            KeyError("vars unavailable"),
            ValueError("vars unavailable"),
            TypeError("vars unavailable"),
            RuntimeError("programming error"),
        ],
    )
    async def test_owner_variable_failure_never_closes(
        self, close_case: CloseCase, lookup_error: Exception, evaluation_read: int
    ) -> None:
        close_case.variables.get_variables.side_effect = [{}] * (evaluation_read - 1) + [
            lookup_error
        ]
        if isinstance(lookup_error, RuntimeError):
            with pytest.raises(RuntimeError, match="programming error"):
                await close_case.close()
        else:
            result = await close_case.close()
            assert result["success"] is False
            assert result["error"] == "session_variable_lookup_failed"
        assert close_case.variables.get_variables.call_count == evaluation_read
        assert {c.args for c in close_case.variables.get_variables.call_args_list} == {
            ("owner-session",)
        }
        close_case.manager.close_task.assert_not_called()
        close_case.manager.close_task_with_commit.assert_not_called()
        close_case.variables.merge_variables.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_refetches_owner_after_close(self, close_case: CloseCase) -> None:
        initial = close_case.attribute(["owned.py"])
        initial.update(
            task_claimed=True,
            claimed_tasks={close_case.task.id: "#42"},
            active_task_id=close_case.task.id,
        )
        fresh = close_case.attribute(["owned.py"])
        fresh.update(
            task_claimed=True,
            claimed_tasks={close_case.task.id: "#42", "concurrent": "#99"},
            active_task_id=close_case.task.id,
        )
        fresh["task_edited_files"]["concurrent"] = ["other.py"]
        fresh["task_edited_file_checkouts"]["concurrent"] = {str(close_case.repo): ["other.py"]}
        close_case.variables.get_variables.return_value = initial

        def close(*_args: object, **_kwargs: object) -> Task:
            close_case.variables.get_variables.return_value = fresh
            return close_case.task

        close_case.manager.close_task.side_effect = close
        result = await close_case.close()
        assert result["closed"] is True
        assert close_case.variables.get_variables.call_count >= 3
        assert {c.args for c in close_case.variables.get_variables.call_args_list} == {
            ("owner-session",)
        }
        close_case.variables.merge_variables.assert_called_once_with(
            "owner-session",
            {
                "task_claimed": True,
                "claimed_tasks": {"concurrent": "#99"},
                "active_task_id": None,
                "task_edited_files": {"concurrent": ["other.py"]},
                "task_edited_file_checkouts": {"concurrent": {str(close_case.repo): ["other.py"]}},
            },
        )


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

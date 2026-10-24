"""MCP task lifecycle regressions for task/worktree decoupling."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import StepRequirements
from gobby.utils.daemon_git import GitOk
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])
_CHECKOUT_MACHINE = "21000000-0000-4000-8000-00000000000b"


async def _resolving_git(argv: tuple[str, ...], **_kwargs: object) -> GitOk:
    """Resolve every linked commit and report it as landed on the integration tip."""
    stdout = argv[-1].removesuffix("^{commit}") if argv[0] == "rev-parse" else ""
    return GitOk("ok", argv, stdout, "")


def _checkout_session_manager() -> MagicMock:
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(
        id="test-session",
        machine_id=_CHECKOUT_MACHINE,
        project_id="11111111-1111-4111-8111-111111110001",
        parent_session_id=None,
        agent_run_id=None,
        agent_depth=0,
    )
    manager.resolve_session_reference.side_effect = lambda ref, _project_id=None: ref
    return manager


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    # Close-review and backoff lookups read as "no row" instead of MagicMock rows.
    _conn = MagicMock()
    _conn.execute.return_value.fetchone.return_value = None
    _conn.execute.return_value.rowcount = 0
    manager.db.transaction.return_value.__enter__.return_value = _conn
    manager.db.transaction.return_value.__exit__.return_value = False
    return manager


@pytest.fixture(autouse=True)
def _set_session_context() -> Iterator[None]:
    with (
        session_context_for_test("test-session"),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.require_local_machine_id",
            return_value="21000000-0000-4000-8000-00000000000b",
        ),
        patch(
            "gobby.mcp_proxy.tools.task_repo_paths.require_root",
            return_value=TEST_REPO_PATH,
        ),
        patch(
            "gobby.mcp_proxy.tools.task_repo_paths.require_local_machine_id",
            return_value="21000000-0000-4000-8000-00000000000b",
        ),
        patch(
            "gobby.storage.project_checkouts.require_local_machine_id",
            return_value="21000000-0000-4000-8000-00000000000b",
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
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["done", "superseded"])
async def test_close_task_does_not_mutate_worktree_status(
    mock_task_manager: MagicMock,
    reason: str,
) -> None:
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.LocalWorktreeManager") as MockWorktreeManager,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.daemon_git.run",
            side_effect=_resolving_git,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.main_checkout_target",
            return_value=(Path(TEST_REPO_PATH), "0.5.0"),
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.step_requirements",
            AsyncMock(return_value=StepRequirements((), ())),
        ),
        patch("gobby.hooks.event_handlers._plan.on_epic_terminal"),
    ):
        mock_wt_instance = MagicMock()
        MockWorktreeManager.return_value = mock_wt_instance
        mock_proj_instance = MagicMock()
        mock_proj_instance.get.return_value = MagicMock()
        MockProjManager.return_value = mock_proj_instance

        registry: InternalToolRegistry = create_task_registry(mock_task_manager)

        now = datetime.now(UTC)
        mock_task = Task(
            id="550e8400-e29b-41d4-a716-446655440000",
            project_id="11111111-1111-4111-8111-111111110001",
            title="Worktree lifecycle",
            priority=2,
            task_type="epic",
            created_at=now,
            updated_at=now,
            commits=["abc123"],
            seq_num=123,
        )
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        result = await registry.call(
            "close_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "reason": reason,
                "changes_summary": "test changes",
            },
        )

    gates = result.pop("gates")
    assert gates and all(gate["status"] != "failed" for gate in gates)
    assert result == {
        "success": True,
        "preview": False,
        "can_close": True,
        "closed": True,
        "task_id": mock_task.id,
        "commit_shas": ["abc123"],
        "candidate_commit_sha": "abc123",
    }
    mock_task_manager.close_task.assert_called_once()
    assert mock_task_manager.close_task.call_args.args == (mock_task.id,)
    assert mock_task_manager.close_task.call_args.kwargs["reason"] == reason
    mock_wt_instance.get_by_task.assert_not_called()
    mock_wt_instance.mark_merged.assert_not_called()
    mock_wt_instance.mark_abandoned.assert_not_called()

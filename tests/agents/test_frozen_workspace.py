"""A live run cannot carry its frozen overlay and SRT write scope to a new workspace."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.frozen_workspace import FROZEN_WORKSPACE_ERROR_CODE, frozen_workspace_refusal
from gobby.mcp_proxy.tools.clones import create_clones_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.session_context import session_context_for_test

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _session(session_manager: SessionManager, project_id: str, name: str) -> str:
    return session_manager.register(
        external_id=f"frozen-workspace-{name}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
    ).id


def _live_run(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    *,
    worktree_id: str | None = None,
    clone_id: str | None = None,
) -> str:
    parent = _session(session_manager, project_id, "parent")
    child = _session(session_manager, project_id, "child")
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=parent,
        child_session_id=child,
        provider="claude",
        prompt="frozen workspace test",
        worktree_id=worktree_id,
        clone_id=clone_id,
    )
    runs.start(run.id)
    return child


def _worktree(db: HubDatabase, project_id: str, name: str, tmp_path: Path) -> tuple[str, str]:
    path = str(tmp_path / name)
    worktree = LocalWorktreeManager(db).create(
        project_id=project_id, branch_name=name, worktree_path=path
    )
    return worktree.id, path


def _clone(db: HubDatabase, project_id: str, name: str, tmp_path: Path) -> tuple[str, str]:
    path = str(tmp_path / name)
    clone = LocalCloneManager(db).create(project_id=project_id, branch_name=name, clone_path=path)
    return clone.id, path


def _claimed_task(db: HubDatabase, project_id: str, session_id: str) -> str:
    tasks = LocalTaskManager(db)
    task = tasks.create_task(
        project_id, "Frozen workspace task", validation_criteria="Workspace stays usable."
    )
    tasks.claim_task(task.id, session_id)
    return task.id


def _git_stops_after_guard() -> MagicMock:
    """A git manager whose creates fail, so a create that passes the guard has no side effects."""
    git_manager = MagicMock()
    git_manager.run_git_command = AsyncMock(return_value=MagicMock(returncode=0))
    git_manager.get_local_commit = AsyncMock(return_value="abc123")
    stopped = MagicMock(success=False, error="stopped after guard", message="")
    git_manager.create_worktree = AsyncMock(return_value=stopped)
    git_manager.shallow_clone = AsyncMock(return_value=stopped)
    return git_manager


def _worktree_tools(
    db: HubDatabase, git_manager: MagicMock, project_id: str
) -> InternalToolRegistry:
    return create_worktrees_registry(
        worktree_storage=LocalWorktreeManager(db),
        git_manager=git_manager,
        project_id=project_id,
    )


def _assert_frozen(result: dict[str, Any], action: str, bound_path: str) -> None:
    assert result["success"] is False
    assert result["error_code"] == FROZEN_WORKSPACE_ERROR_CODE
    error = result["error"]
    assert error.startswith(f"{action} refused:")
    # Both frozen scopes are named, with the workspace they are frozen to.
    assert "gcode code-index overlay" in error
    assert "SRT write scope" in error
    assert f"frozen at launch to {bound_path}." in error


@pytest.mark.asyncio
async def test_bound_run_create_worktree_for_own_task_refused_before_git(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    bound_id, bound_path = _worktree(temp_db, project_id, "lane-bound", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id, worktree_id=bound_id)
    own_task = _claimed_task(temp_db, project_id, caller)
    git_manager = _git_stops_after_guard()
    tools = _worktree_tools(temp_db, git_manager, project_id)

    with session_context_for_test(caller):
        result = await tools.call(
            "create_worktree",
            {"branch_name": "mid-run", "base_branch": "0.5.0", "task_id": own_task},
        )

    _assert_frozen(result, "create_worktree", bound_path)
    git_manager.create_worktree.assert_not_called()
    assert LocalWorktreeManager(temp_db).get_by_branch(project_id, "mid-run") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("for_task", ["none", "other-seat"])
async def test_bound_run_creates_worktree_for_another_seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    for_task: str,
) -> None:
    project_id = sample_project["id"]
    bound_id, _ = _worktree(temp_db, project_id, "lane-bound", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id, worktree_id=bound_id)
    arguments: dict[str, Any] = {
        "branch_name": "sub-lane",
        "base_branch": "0.5.0",
        "worktree_path": str(tmp_path / "sub-lane"),
    }
    if for_task == "other-seat":
        seat = _session(session_manager, project_id, "seat")
        arguments["task_id"] = _claimed_task(temp_db, project_id, seat)
    git_manager = _git_stops_after_guard()
    tools = _worktree_tools(temp_db, git_manager, project_id)

    with session_context_for_test(caller):
        result = await tools.call("create_worktree", arguments)

    assert result["error_code"] == "git_create_failed"
    git_manager.create_worktree.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_worktree_for_self_refused_unless_bound(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    bound_id, bound_path = _worktree(temp_db, project_id, "lane-bound", tmp_path)
    other_id, _ = _worktree(temp_db, project_id, "lane-other", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id, worktree_id=bound_id)
    tools = _worktree_tools(temp_db, MagicMock(), project_id)
    worktrees = LocalWorktreeManager(temp_db)

    with session_context_for_test(caller):
        refused = await tools.call(
            "claim_worktree", {"worktree_id": other_id, "session_id": caller}
        )
        own = await tools.call("claim_worktree", {"worktree_id": bound_id, "session_id": caller})

    _assert_frozen(refused, "claim_worktree", bound_path)
    other = worktrees.get(other_id)
    assert other is not None and other.agent_session_id is None
    assert own["success"] is True


@pytest.mark.asyncio
async def test_claim_worktree_for_another_session_is_not_guarded(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    bound_id, _ = _worktree(temp_db, project_id, "lane-bound", tmp_path)
    other_id, _ = _worktree(temp_db, project_id, "lane-other", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id, worktree_id=bound_id)
    seat = _session(session_manager, project_id, "seat")
    tools = _worktree_tools(temp_db, MagicMock(), project_id)

    with session_context_for_test(caller):
        result = await tools.call("claim_worktree", {"worktree_id": other_id, "session_id": seat})

    assert result["success"] is True
    claimed = LocalWorktreeManager(temp_db).get(other_id)
    assert claimed is not None and claimed.agent_session_id == seat


def test_unbound_run_may_create_for_others_but_not_for_itself(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    other_id, _ = _worktree(temp_db, project_id, "lane-other", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id)
    own_task = _claimed_task(temp_db, project_id, caller)

    with session_context_for_test(caller):
        create = frozen_workspace_refusal(temp_db, "create_worktree")
        unresolved = frozen_workspace_refusal(temp_db, "create_worktree", task_id="#999999")
        own_create = frozen_workspace_refusal(temp_db, "create_worktree", task_id=own_task)
        claim = frozen_workspace_refusal(temp_db, "claim_worktree", claimed_worktree_id=other_id)

    assert create is None
    assert unresolved is None
    assert own_create is not None
    _assert_frozen(own_create, "create_worktree", "the checkout it launched in")
    assert claim is not None
    _assert_frozen(claim, "claim_worktree", "the checkout it launched in")


def test_callers_outside_a_live_run_are_not_guarded(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    other_id, _ = _worktree(temp_db, project_id, "lane-other", tmp_path)
    operator = _session(session_manager, project_id, "operator")

    assert frozen_workspace_refusal(temp_db, "create_worktree") is None
    with session_context_for_test(operator):
        assert frozen_workspace_refusal(temp_db, "create_worktree") is None
        assert (
            frozen_workspace_refusal(temp_db, "claim_worktree", claimed_worktree_id=other_id)
            is None
        )


@pytest.mark.asyncio
async def test_clone_bound_run_create_for_own_task_and_claim_clone_refused(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    bound_id, bound_path = _clone(temp_db, project_id, "clone-bound", tmp_path)
    other_id, _ = _clone(temp_db, project_id, "clone-other", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id, clone_id=bound_id)
    own_task = _claimed_task(temp_db, project_id, caller)
    git_manager = _git_stops_after_guard()
    tools = create_clones_registry(
        clone_storage=LocalCloneManager(temp_db), git_manager=git_manager, project_id=project_id
    )

    with session_context_for_test(caller):
        created = await tools.call(
            "create_clone",
            {
                "branch_name": "mid-run",
                "clone_path": str(tmp_path / "mid-run"),
                "remote_url": "https://example.invalid/repo.git",
                "task_id": own_task,
            },
        )
        claimed = await tools.call("claim_clone", {"clone_id": other_id})

    _assert_frozen(created, "create_clone", bound_path)
    _assert_frozen(claimed, "claim_clone", bound_path)
    git_manager.shallow_clone.assert_not_called()
    other = LocalCloneManager(temp_db).get(other_id)
    assert other is not None and other.agent_session_id is None


@pytest.mark.asyncio
async def test_clone_bound_run_creates_clone_for_another_seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    project_id = sample_project["id"]
    bound_id, _ = _clone(temp_db, project_id, "clone-bound", tmp_path)
    caller = _live_run(temp_db, session_manager, project_id, clone_id=bound_id)
    git_manager = _git_stops_after_guard()
    tools = create_clones_registry(
        clone_storage=LocalCloneManager(temp_db), git_manager=git_manager, project_id=project_id
    )

    with session_context_for_test(caller):
        created = await tools.call(
            "create_clone",
            {
                "branch_name": "sub-lane",
                "clone_path": str(tmp_path / "sub-lane"),
                "remote_url": "https://example.invalid/repo.git",
            },
        )

    assert created == {"success": False, "error": "Clone failed: stopped after guard"}
    git_manager.shallow_clone.assert_awaited_once()

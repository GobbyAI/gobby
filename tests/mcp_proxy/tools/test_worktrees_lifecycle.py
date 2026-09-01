import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.storage.worktrees import Worktree, WorktreeStatus
from gobby.worktrees.executor import WorktreeDeleteExecutor
from gobby.worktrees.git._models import WorktreeInfo

pytestmark = pytest.mark.unit

_VALID_TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _detached_worktree() -> Worktree:
    return Worktree(
        id="worktree-detached",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name=None,
        worktree_path="/tmp/detached",
        base_branch="main",
        task_id=None,
        agent_session_id=None,
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
    )


@pytest.fixture
def mock_worktree_storage():
    storage = MagicMock()
    storage.resolve_reference.side_effect = lambda ref: ref
    return storage


@pytest.fixture
def mock_git_manager():
    manager = MagicMock()
    manager.repo_path = "/tmp/repo"
    manager.run_git_command.side_effect = (
        lambda args, cwd=None, timeout=30, check=False, env=None: manager._run_git(
            args, cwd=cwd, timeout=timeout, check=check, env=env
        )
    )

    def get_unmerged_files(cwd=None):
        result = manager._run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd, timeout=10)
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    manager.get_unmerged_files.side_effect = get_unmerged_files
    return manager


@pytest.fixture
def registry(mock_worktree_storage, mock_git_manager):
    executor = WorktreeDeleteExecutor(thread_name_prefix="test-mcp-worktree-delete")
    try:
        yield create_worktrees_registry(
            worktree_storage=mock_worktree_storage,
            git_manager=mock_git_manager,
            project_id="11111111-1111-4111-8111-111111110001",
            worktree_delete_executor=executor,
        )
    finally:
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
async def test_foreign_worktree_id_fails_before_side_effects(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    worktree_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee99"
    mock_worktree_storage.get.side_effect = MachineOwnershipMismatchError(
        resource_kind="worktree",
        resource_id=worktree_id,
        owner_machine_id="21000000-0000-4000-8000-000000000002",
        current_machine_id="21000000-0000-4000-8000-000000000001",
    )
    mock_worktree_storage.claim_if_available.side_effect = mock_worktree_storage.get.side_effect

    with (
        patch("pathlib.Path.exists") as path_exists,
        patch("gobby.mcp_proxy.tools.worktrees._lifecycle.emit_worktree_event") as emit_event,
    ):
        results = [
            await registry.call("claim_worktree", {"worktree_id": worktree_id, "session_id": "s1"}),
            await registry.call("sync_worktree", {"worktree_id": worktree_id}),
            await registry.call("delete_worktree", {"worktree_id": worktree_id}),
        ]

    assert all(result["error_code"] == "machine_ownership_mismatch" for result in results)
    path_exists.assert_not_called()
    emit_event.assert_not_called()
    mock_git_manager.assert_not_called()
    mock_worktree_storage.claim.assert_not_called()
    mock_worktree_storage.claim_if_available.assert_called_once()
    mock_worktree_storage.update.assert_not_called()
    mock_worktree_storage.delete.assert_not_called()


def _local_merge_git_side_effect(
    *,
    source: str = "feature/test",
    target: str = "main",
    current_branch: str | None = None,
    merge_returncode: int = 0,
    unmerged_stdout: str = "",
    merge_stderr: str = "",
):
    current = current_branch or target
    stash_list_calls = 0

    def _run_git(args, cwd=None, timeout=30, check=False, env=None):
        nonlocal stash_list_calls
        if args == ["show-ref", "--verify", "--quiet", f"refs/heads/{target}"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["show-ref", "--verify", "--quiet", f"refs/heads/{source}"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["status", "--porcelain"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return MagicMock(returncode=0, stdout=current, stderr="")
        if args in (["rev-parse", "HEAD"], ["rev-parse", f"refs/heads/{target}"]):
            return MagicMock(returncode=0, stdout="abc123def456\n", stderr="")
        if args == ["stash", "list"]:
            stash_list_calls += 1
            stdout = "" if stash_list_calls == 1 else "stash@{0}"
            return MagicMock(returncode=0, stdout=stdout, stderr="")
        if args[:2] == ["stash", "push"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["stash", "pop"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["merge", f"refs/heads/{source}", "--no-ff", "--no-edit"]:
            return MagicMock(returncode=merge_returncode, stdout="", stderr=merge_stderr)
        if args == ["diff", "--name-only", "--diff-filter=U"]:
            return MagicMock(returncode=0, stdout=unmerged_stdout, stderr="")
        if args == ["merge", "--abort"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["commit", "--no-edit"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["merge-base", "--is-ancestor", source, target]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if args == ["checkout", target] or args == ["checkout", current]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return _run_git


@pytest.mark.asyncio
async def test_get_worktree_found(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feat/1",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        last_activity_at=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_status = MagicMock()
    mock_status.has_uncommitted_changes = True
    mock_status.ahead = 1
    mock_status.behind = 2
    mock_status.branch = "feat/1"
    mock_git_manager.get_worktree_status.return_value = mock_status
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})
        assert result["success"] is True
        assert result["worktree"]["id"] == "wt-123"
        assert result["git_status"]["has_uncommitted_changes"] is True
        mock_git_manager.get_worktree_status.assert_called_once_with("/tmp/wt1", "main")


@pytest.mark.asyncio
async def test_get_worktree_not_found(registry, mock_worktree_storage) -> None:
    mock_worktree_storage.get.return_value = None
    result = await registry.call("get_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_get_worktree_path_not_exists(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test get_worktree when path doesn't exist on disk."""
    wt = Worktree(
        id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feat/1",
        worktree_path="/nonexistent/path",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})
        assert result["success"] is True
        assert result.get("git_status") is None or "git_status" not in result


@pytest.mark.asyncio
async def test_get_worktree_downgrades_stale_merged_status(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.MERGED.value,
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at="2026-04-22T00:00:00+00:00",
        cleanup_after="2026-04-29T00:00:00+00:00",
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=1, stdout="", stderr="")

    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})

    assert result["success"] is True
    assert result["worktree"]["status"] == WorktreeStatus.ACTIVE.value
    assert result["worktree"]["stored_status"] == WorktreeStatus.MERGED.value
    assert result["worktree"]["merged_at"] is None
    assert result["worktree"]["git_merge_state"]["git_merged"] is False


@pytest.mark.asyncio
async def test_list_worktrees(registry, mock_worktree_storage) -> None:
    wt1 = Worktree(
        id="1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.list_worktrees.return_value = [wt1]
    result = await registry.call("list_worktrees", {"status": "active"})
    assert result["success"] is True
    assert len(result["worktrees"]) == 1
    mock_worktree_storage.list_worktrees.assert_called_with(
        project_id="11111111-1111-4111-8111-111111110001",
        status="active",
        agent_session_id=None,
        limit=50,
    )


@pytest.mark.asyncio
async def test_list_worktrees_accepts_explicit_project_id(registry, mock_worktree_storage) -> None:
    wt1 = Worktree(
        id="1",
        project_id="target-proj",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.list_worktrees.return_value = [wt1]

    result = await registry.call(
        "list_worktrees", {"status": "active", "project_id": "target-proj"}
    )

    assert result["success"] is True
    assert result["count"] == 1
    mock_worktree_storage.list_worktrees.assert_called_with(
        project_id="target-proj", status="active", agent_session_id=None, limit=50
    )


@pytest.mark.asyncio
async def test_list_worktrees_resolves_project_path(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt1 = Worktree(
        id="1",
        project_id="target-proj",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.list_worktrees.return_value = [wt1]
    mock_worktree_storage.count_by_status.return_value = {"active": 1}
    mock_worktree_storage.get_by_task.return_value = wt1

    with patch(
        "gobby.mcp_proxy.tools.worktrees._crud.resolve_project_context",
        return_value=(mock_git_manager, "target-proj", None),
    ) as resolve_project_context:
        list_result = await registry.call(
            "list_worktrees", {"status": "active", "project_path": "/tmp/target-project"}
        )
        stats_result = await registry.call(
            "get_worktree_stats", {"project_path": "/tmp/target-project"}
        )
        task_result = await registry.call("get_worktree_by_task", {"task_id": "task-1"})

    assert list_result["success"] is True
    assert list_result["count"] == 1
    assert list_result["worktrees"][0]["id"] == task_result["worktree"]["id"]
    assert list_result["count"] == stats_result["counts"]["active"]
    assert resolve_project_context.call_count == 2
    resolve_project_context.assert_any_call(
        "/tmp/target-project", mock_git_manager, "11111111-1111-4111-8111-111111110001"
    )
    mock_worktree_storage.count_by_status.assert_called_once_with("target-proj")
    mock_worktree_storage.get_by_task.assert_called_once_with("task-1")
    mock_worktree_storage.list_worktrees.assert_called_with(
        project_id="target-proj", status="active", agent_session_id=None, limit=50
    )


@pytest.mark.asyncio
async def test_claim_worktree_success(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    claimed = replace(wt, agent_session_id="sess-1")
    mock_worktree_storage.claim_if_available.return_value = claimed
    with patch(
        "gobby.mcp_proxy.tools.worktrees._lifecycle.emit_worktree_event",
        return_value={
            "event_type": "worktree_claimed",
            "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        },
    ) as emit_event:
        result = await registry.call(
            "claim_worktree",
            {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "session_id": "sess-1"},
        )
    assert result["success"] is True
    assert result["event"]["event_type"] == "worktree_claimed"
    mock_worktree_storage.claim_if_available.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        "sess-1",
        allowed_existing_session_ids=(None, "sess-1"),
    )
    mock_worktree_storage.claim.assert_not_called()
    emit_event.assert_called_once_with(
        "worktree_claimed",
        worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_claim_worktree_already_claimed(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id="other-session",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.claim_if_available.return_value = None
    mock_worktree_storage.get.return_value = wt
    result = await registry.call(
        "claim_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "session_id": "sess-1"},
    )
    assert result["success"] is False
    assert "already claimed" in result["error"]


@pytest.mark.asyncio
async def test_claim_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test claim_worktree when worktree not found."""
    mock_worktree_storage.claim_if_available.return_value = None
    mock_worktree_storage.get.return_value = None
    result = await registry.call(
        "claim_worktree", {"worktree_id": "nonexistent", "session_id": "sess-1"}
    )
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_claim_worktree_same_session_is_idempotent(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id="sess-1",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.claim_if_available.return_value = wt

    result = await registry.call(
        "claim_worktree",
        {"worktree_id": wt.id, "session_id": "sess-1"},
    )

    assert result["success"] is True
    mock_worktree_storage.claim_if_available.assert_called_once_with(
        wt.id,
        "sess-1",
        allowed_existing_session_ids=(None, "sess-1"),
    )


@pytest.mark.asyncio
async def test_concurrent_claim_worktree_has_exactly_one_winner(
    registry, mock_worktree_storage
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    owner: str | None = None

    def claim_if_available(
        worktree_id: str,
        session_id: str,
        *,
        allowed_existing_session_ids: tuple[str | None, ...],
    ) -> Worktree | None:
        nonlocal owner
        assert worktree_id == wt.id
        if owner not in allowed_existing_session_ids:
            return None
        owner = session_id
        return replace(wt, agent_session_id=session_id)

    def get_worktree(worktree_id: str) -> Worktree:
        assert worktree_id == wt.id
        return replace(wt, agent_session_id=owner)

    mock_worktree_storage.claim_if_available.side_effect = claim_if_available
    mock_worktree_storage.get.side_effect = get_worktree

    results = await asyncio.gather(
        registry.call("claim_worktree", {"worktree_id": wt.id, "session_id": "sess-1"}),
        registry.call("claim_worktree", {"worktree_id": wt.id, "session_id": "sess-2"}),
    )

    assert sum(result["success"] is True for result in results) == 1
    assert sum(result["success"] is False for result in results) == 1
    assert owner in {"sess-1", "sess-2"}
    assert "already claimed" in next(
        result["error"] for result in results if result["success"] is False
    )
    mock_worktree_storage.claim.assert_not_called()


@pytest.mark.asyncio
async def test_release_worktree(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id="sess-1",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.release.return_value = True
    with patch(
        "gobby.mcp_proxy.tools.worktrees._lifecycle.emit_worktree_event",
        return_value={
            "event_type": "worktree_released",
            "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        },
    ) as emit_event:
        result = await registry.call(
            "release_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
        )
    assert result["success"] is True
    mock_worktree_storage.release.assert_called_with("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")
    assert result["event"]["event_type"] == "worktree_released"
    emit_event.assert_called_once_with(
        "worktree_released",
        worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        session_id="sess-1",
    )


@pytest.mark.asyncio
async def test_release_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test release_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("release_worktree", {"worktree_id": "nonexistent"})
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_abandon_worktree(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status=WorktreeStatus.ACTIVE.value,
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.mark_abandoned.return_value = wt

    result = await registry.call(
        "abandon_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
    )

    assert result["success"] is True
    mock_worktree_storage.mark_abandoned.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
    )


@pytest.mark.asyncio
async def test_abandon_worktree_not_found(registry, mock_worktree_storage) -> None:
    mock_worktree_storage.get.return_value = None
    result = await registry.call("abandon_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]
    mock_worktree_storage.mark_abandoned.assert_not_called()


@pytest.mark.asyncio
async def test_reactivate_worktree(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status=WorktreeStatus.MERGED.value,
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id=None,
        task_id=None,
        merged_at="2026-04-22T00:00:00+00:00",
        cleanup_after="2026-04-29T00:00:00+00:00",
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.update.return_value = wt

    result = await registry.call(
        "reactivate_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
    )

    assert result["success"] is True
    mock_worktree_storage.update.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        status=WorktreeStatus.ACTIVE.value,
        merged_at=None,
        cleanup_after=None,
    )


@pytest.mark.asyncio
async def test_reactivate_worktree_not_found(registry, mock_worktree_storage) -> None:
    mock_worktree_storage.get.return_value = None
    result = await registry.call("reactivate_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]
    mock_worktree_storage.update.assert_not_called()


@pytest.mark.asyncio
async def test_mark_worktree_merged_requires_git_ancestry(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.ACTIVE.value,
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=1, stdout="", stderr="")

    result = await registry.call(
        "mark_worktree_merged", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
    )

    assert result["success"] is False
    assert "not merged" in result["error"]
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.parametrize(
    ("tool_name", "arguments", "operation"),
    [
        (
            "link_task_to_worktree",
            {"worktree_id": "worktree-detached", "task_id": "#1"},
            "linked to a task",
        ),
        ("sync_worktree", {"worktree_id": "worktree-detached"}, "synced"),
        ("merge_worktree", {"worktree_id": "worktree-detached"}, "merged"),
        (
            "mark_worktree_merged",
            {"worktree_id": "worktree-detached"},
            "marked as merged",
        ),
    ],
)
@pytest.mark.asyncio
async def test_detached_worktree_rejects_branch_dependent_operations(
    registry,
    mock_worktree_storage,
    mock_git_manager,
    tool_name: str,
    arguments: dict[str, str],
    operation: str,
) -> None:
    mock_worktree_storage.get.return_value = _detached_worktree()

    result = await registry.call(tool_name, arguments)

    assert result == {
        "success": False,
        "error": f"Detached worktree 'worktree-detached' cannot be {operation}",
    }
    mock_git_manager.sync_from_main.assert_not_called()
    mock_git_manager.run_git_command.assert_not_called()
    mock_worktree_storage.update.assert_not_called()
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_mark_worktree_merged_success(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="feature/merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.ACTIVE.value,
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_worktree_storage.mark_merged.return_value = wt

    result = await registry.call(
        "mark_worktree_merged", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
    )

    assert result["success"] is True
    mock_worktree_storage.mark_merged.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
    )


@pytest.mark.asyncio
async def test_delete_worktree_success(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True
    mock_worktree_storage.delete.return_value = True
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "gobby.worktrees.deletion.emit_worktree_event",
            return_value={
                "event_type": "worktree_deleted",
                "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            },
        ) as emit_event,
    ):
        result = await registry.call(
            "delete_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
        )
        assert result["success"] is True
        assert result["event"]["event_type"] == "worktree_deleted"
        mock_git_manager.delete_worktree.assert_called_with(
            "/tmp/p1",
            force=False,
            delete_branch=True,
            force_delete_branch=False,
            branch_name="b1",
            base_branch="main",
        )
        mock_worktree_storage.delete.assert_called_with("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")
        emit_event.assert_called_once_with(
            "worktree_deleted",
            worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            project_id="p1",
            branch_name="b1",
            worktree_path="/tmp/p1",
            artifact_refs_cleared=0,
        )


@pytest.mark.asyncio
async def test_delete_worktree_uncommitted_changes(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = True
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call(
            "delete_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
        )
        assert result["success"] is False
        assert "uncommitted changes" in result["error"]

        mock_git_manager.delete_worktree.return_value.success = True
        mock_worktree_storage.delete.return_value = True
        result_force = await registry.call(
            "delete_worktree",
            {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "force": True},
        )
        assert result_force["success"] is True
        mock_git_manager.delete_worktree.assert_called_with(
            "/tmp/p1",
            force=True,
            delete_branch=True,
            force_delete_branch=False,
            branch_name="b1",
            base_branch="main",
        )


@pytest.mark.asyncio
async def test_delete_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test delete_worktree is idempotent when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("delete_worktree", {"worktree_id": "nonexistent"})
    assert result["success"] is True
    assert result["already_deleted"] is True


@pytest.mark.asyncio
async def test_delete_worktree_path_not_exists(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test delete_worktree when path doesn't exist (orphaned DB record)."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/nonexistent",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    mock_git_manager.delete_worktree.return_value.success = True
    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call(
            "delete_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
        )
        assert result["success"] is True
        mock_git_manager.delete_worktree.assert_called_once_with(
            wt.worktree_path,
            force=False,
            delete_branch=True,
            force_delete_branch=False,
            branch_name=wt.branch_name,
            base_branch=wt.base_branch,
        )
        mock_worktree_storage.delete.assert_called_once_with("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")


@pytest.mark.asyncio
async def test_delete_worktree_missing_path_prune_failure_preserves_record(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/nonexistent",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.delete_worktree.return_value.success = False
    mock_git_manager.delete_worktree.return_value.error = "Git delete failed"
    mock_git_manager.prune_worktrees.return_value.success = False
    mock_git_manager.prune_worktrees.return_value.error = "Prune failed"

    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("delete_worktree", {"worktree_id": wt.id})

    assert result["success"] is False
    assert result["error"] == "Prune failed"
    mock_git_manager.prune_worktrees.assert_called_once_with()
    mock_worktree_storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_worktree_existing_path_without_git_manager_preserves_record(
    mock_worktree_storage,
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    registry = create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=None,
        project_id="11111111-1111-4111-8111-111111110001",
    )

    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": wt.id})

    assert result["success"] is False
    assert "without a resolved git manager" in result["error"]
    mock_worktree_storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_worktree_missing_path_without_git_manager_removes_stale_record(
    mock_worktree_storage,
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/nonexistent",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    registry = create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=None,
        project_id="11111111-1111-4111-8111-111111110001",
    )

    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("delete_worktree", {"worktree_id": wt.id})

    assert result["success"] is True
    mock_worktree_storage.delete.assert_called_once_with(wt.id)


@pytest.mark.asyncio
async def test_delete_worktree_git_failure(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test delete_worktree when git delete fails."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = False
    mock_git_manager.delete_worktree.return_value.error = "Git delete failed"
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call(
            "delete_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
        )
        assert result["success"] is False
        assert "Git delete failed" in result["error"]


@pytest.mark.asyncio
async def test_delete_worktree_continues_when_git_failure_removed_path(
    registry,
    mock_worktree_storage,
    mock_git_manager,
) -> None:
    """If git returns failure after deleting the path, DB cleanup still completes."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = False
    mock_git_manager.delete_worktree.return_value.error = "Git delete failed after unlink"

    with patch("pathlib.Path.exists", side_effect=[True, False]):
        result = await registry.call(
            "delete_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
        )

    assert result["success"] is True
    mock_git_manager.prune_worktrees.assert_called_once()
    mock_worktree_storage.delete.assert_called_once_with("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")


@pytest.mark.asyncio
async def test_delete_worktree_clears_task_artifact_references(
    mock_worktree_storage,
    mock_git_manager,
) -> None:
    """delete_worktree clears stale task artifact references before reporting success."""
    task_manager = MagicMock()
    task_manager.artifacts.clear_worktree_references.return_value = 2
    registry = create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=mock_git_manager,
        project_id="11111111-1111-4111-8111-111111110001",
        task_manager=task_manager,
    )
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id="task-1",
        agent_session_id=None,
        merged_at=None,
        workspace_role="integration",
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True

    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call(
            "delete_worktree",
            {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "force": True},
        )

    assert result["success"] is True
    assert result["artifact_refs_cleared"] == 2
    assert result["event"]["event_type"] == "worktree_deleted"
    assert result["event"]["artifact_refs_cleared"] == 2
    task_manager.artifacts.clear_worktree_references.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
    )


@pytest.mark.asyncio
async def test_delete_worktree_artifact_cleanup_failure_is_best_effort(
    mock_worktree_storage,
    mock_git_manager,
    caplog,
) -> None:
    """delete_worktree still succeeds if post-delete artifact cleanup fails."""
    task_manager = MagicMock()
    task_manager.artifacts.clear_worktree_references.side_effect = RuntimeError("cleanup failed")
    registry = create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=mock_git_manager,
        project_id="11111111-1111-4111-8111-111111110001",
        task_manager=task_manager,
    )
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id="task-1",
        agent_session_id=None,
        merged_at=None,
        workspace_role="integration",
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True

    with (
        patch("pathlib.Path.exists", return_value=True),
        caplog.at_level(logging.WARNING),
    ):
        result = await registry.call(
            "delete_worktree",
            {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "force": True},
        )

    assert result["success"] is True
    assert result["artifact_refs_cleared"] == 0
    assert result["event"]["event_type"] == "worktree_deleted"
    assert result["event"]["artifact_refs_cleared"] == 0
    mock_worktree_storage.delete.assert_called_once_with("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01")
    task_manager.artifacts.clear_worktree_references.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
    )
    assert any(
        record.getMessage() == "Failed to clear task artifact worktree references after deletion"
        and getattr(record, "operation", None) == "delete_worktree"
        and getattr(record, "worktree_id", None) == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_sync_worktree(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = True
    mock_git_manager.sync_from_main.return_value.message = "Synced"
    result = await registry.call(
        "sync_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "strategy": "merge"},
    )
    assert result["success"] is True
    assert result["source_branch"] == "main"
    mock_git_manager.sync_from_main.assert_called_with(
        "/tmp/p1", base_branch="main", strategy="merge", source_branch=None
    )
    mock_worktree_storage.touch.assert_called_once_with(wt.id)
    mock_worktree_storage.update.assert_not_called()


async def test_sync_worktree_explicit_source_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = True
    mock_git_manager.sync_from_main.return_value.message = "Synced"
    result = await registry.call(
        "sync_worktree",
        {
            "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            "strategy": "merge",
            "source_branch": "origin/main",
        },
    )
    assert result["success"] is True
    assert result["source_branch"] == "origin/main"
    mock_git_manager.sync_from_main.assert_called_with(
        "/tmp/p1",
        base_branch="main",
        strategy="merge",
        source_branch="origin/main",
    )


@pytest.mark.asyncio
async def test_sync_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test sync_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("sync_worktree", {"worktree_id": "nonexistent"})
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_sync_worktree_failure(registry, mock_worktree_storage, mock_git_manager) -> None:
    """Test sync_worktree when sync fails."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = False
    mock_git_manager.sync_from_main.return_value.error = "Sync failed"
    result = await registry.call(
        "sync_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
    )
    assert result["success"] is False
    assert "Sync failed" in result["error"]
    mock_worktree_storage.touch.assert_not_called()


@pytest.mark.asyncio
async def test_detect_stale_worktrees(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.find_stale.return_value = [wt]
    result = await registry.call("detect_stale_worktrees", {"hours": 48})
    assert result["success"] is True
    assert result["count"] == 1
    mock_worktree_storage.find_stale.assert_called_with(
        project_id="11111111-1111-4111-8111-111111110001", hours=48, limit=50
    )


@pytest.mark.asyncio
async def test_cleanup_stale_worktrees(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.cleanup_stale.return_value = [wt]
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True

    result = await registry.call("cleanup_stale_worktrees", {"hours": 24, "dry_run": True})
    assert result["success"] is True
    assert result["count"] == 1
    assert result["cleaned"][0]["marked_abandoned"] is False
    mock_worktree_storage.cleanup_stale.assert_called_with(
        project_id="11111111-1111-4111-8111-111111110001", hours=24, dry_run=True
    )

    result = await registry.call(
        "cleanup_stale_worktrees", {"hours": 24, "dry_run": False, "delete_git": True}
    )
    assert result["success"] is True
    assert result["cleaned"][0]["marked_abandoned"] is True
    assert result["cleaned"][0]["git_deleted"] is True
    mock_git_manager.delete_worktree.assert_called_with(
        "/tmp/p1",
        force=False,
        delete_branch=True,
        force_delete_branch=False,
        branch_name="b1",
        base_branch="main",
    )


@pytest.mark.asyncio
async def test_cleanup_stale_worktrees_skips_dirty_git_worktree(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.cleanup_stale.return_value = [wt]
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = True

    result = await registry.call(
        "cleanup_stale_worktrees",
        {"hours": 24, "dry_run": False, "delete_git": True},
    )

    assert result["success"] is True
    assert result["cleaned"][0]["git_deleted"] is False
    assert result["cleaned"][0]["git_skipped"] is True
    assert result["cleaned"][0]["git_skip_reason"] == "Worktree has uncommitted changes"
    mock_git_manager.delete_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_expired_worktree_rechecks_git_merge_state(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee02",
        project_id="p1",
        branch_name="b2",
        worktree_path="/tmp/p2",
        base_branch="main",
        status="merged",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=_VALID_TIMESTAMP,
        cleanup_after=_VALID_TIMESTAMP,
    )
    mock_worktree_storage.cleanup_stale.return_value = []
    mock_worktree_storage.find_expired.return_value = [wt]

    with patch(
        "gobby.mcp_proxy.tools.worktrees._cleanup.is_worktree_git_merged",
        return_value=False,
    ):
        result = await registry.call(
            "cleanup_stale_worktrees",
            {"hours": 24, "dry_run": False, "delete_git": True},
        )

    assert result["success"] is True
    assert result["cleaned"][0]["git_deleted"] is False
    assert result["cleaned"][0]["git_skipped"] is True
    assert "no longer reports" in result["cleaned"][0]["git_skip_reason"]
    mock_git_manager.delete_worktree.assert_not_called()
    mock_worktree_storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_success(registry, mock_worktree_storage, mock_git_manager) -> None:
    """Merge worktree successfully into the local target branch."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.side_effect = _local_merge_git_side_effect()
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "target_branch": "main"},
    )

    assert result["success"] is True
    assert result["source_branch"] == "feature/test"
    assert result["target_branch"] == "main"
    assert result["merge_sha"] == "abc123def456"
    assert result["target_head_sha"] == "abc123def456"
    mock_worktree_storage.mark_merged.assert_called_once_with(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"
    )
    calls = mock_git_manager._run_git.call_args_list
    merge_call = [c for c in calls if c[0][0][:1] == ["merge"] and "--no-edit" in c[0][0]]
    assert len(merge_call) == 1
    assert "--no-ff" in merge_call[0][0][0]
    assert (
        merge_call[0].kwargs.get("cwd") == "/tmp/repo" or merge_call[0][1].get("cwd") == "/tmp/repo"
    )
    assert result["pushed"] is False
    push_calls = [c for c in calls if c[0][0][:1] == ["push"]]
    assert len(push_calls) == 0
    assert result["project_path"] == "/tmp/repo"


@pytest.mark.asyncio
async def test_merge_worktree_does_not_mark_merged_when_target_lacks_source(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False, env=None):
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return MagicMock(returncode=0, stdout="main", stderr="")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "target_branch": "main"},
    )

    assert result["success"] is True
    assert result["merged"] is False
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_custom_refs_do_not_mark_unmerged_worktree_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/worktree",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    custom_merge = _local_merge_git_side_effect(source="release/source", target="release/target")

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False, env=None):
        if args == [
            "merge-base",
            "--is-ancestor",
            "refs/heads/feature/worktree",
            "refs/heads/main",
        ]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return custom_merge(args, cwd=cwd, timeout=timeout, check=check)

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call(
        "merge_worktree",
        {
            "worktree_id": wt.id,
            "source_branch": "release/source",
            "target_branch": "release/target",
        },
    )

    assert result["success"] is True
    assert result["merged"] is True
    assert result["source_branch"] == "release/source"
    assert result["target_branch"] == "release/target"
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_rejects_push_true(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """merge_worktree rejects push=True before any git command can run."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.side_effect = _local_merge_git_side_effect()
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {
            "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            "target_branch": "main",
            "push": True,
        },
    )

    assert result["success"] is False
    assert "never pushes" in result["error"]
    mock_git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_rejects_push_true_without_push_attempt(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """push=True is rejected instead of attempting a remote push."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    mock_git_manager._run_git.side_effect = _local_merge_git_side_effect()
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {
            "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            "target_branch": "main",
            "push": True,
        },
    )

    assert result["success"] is False
    assert "never pushes" in result["error"]
    mock_git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Merge fails when worktree not found."""
    mock_worktree_storage.get.return_value = None

    result = await registry.call(
        "merge_worktree", {"worktree_id": "missing", "target_branch": "main"}
    )

    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_merge_worktree_default_target_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Merge defaults target_branch to worktree's base_branch."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="develop",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.side_effect = _local_merge_git_side_effect(target="develop")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree", {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01"}
    )

    assert result["success"] is True
    calls = mock_git_manager._run_git.call_args_list
    wt_merge = [c for c in calls if c[0][0][:1] == ["merge"] and "--no-edit" in c[0][0]]
    assert len(wt_merge) == 1
    assert wt_merge[0][0][0] == [
        "merge",
        "refs/heads/feature/test",
        "--no-ff",
        "--no-edit",
    ]


@pytest.mark.asyncio
async def test_merge_worktree_conflict(registry, mock_worktree_storage, mock_git_manager) -> None:
    """Merge detects conflicts in worktree and aborts cleanly."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False, env=None):
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return MagicMock(returncode=0, stdout="main", stderr="")
        if args[0] == "fetch":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "merge" and "--no-edit" in args:
            return MagicMock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/foo.py\n"
                "CONFLICT (content): Merge conflict in src/bar.py",
                stderr="Automatic merge failed",
            )
        if args[0] == "diff" and "--diff-filter=U" in args:
            return MagicMock(returncode=0, stdout="src/foo.py\nsrc/bar.py\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "target_branch": "main"},
    )

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert len(result["conflicted_files"]) == 2
    mock_worktree_storage.mark_merged.assert_not_called()
    abort_calls = [
        c for c in mock_git_manager._run_git.call_args_list if c[0][0] == ["merge", "--abort"]
    ]
    assert len(abort_calls) == 1


@pytest.mark.asyncio
async def test_merge_worktree_non_conflict_failure(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Merge fails with non-conflict error."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False, env=None):
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return MagicMock(returncode=0, stdout="main", stderr="")
        if args[0] == "fetch":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "merge" and "--no-edit" in args:
            return MagicMock(
                returncode=128,
                stdout="",
                stderr="fatal: Not a valid object name 'main'",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "target_branch": "main"},
    )

    assert result["success"] is False
    assert result["has_conflicts"] is False
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_explicit_source_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Agent can specify source_branch explicitly."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.side_effect = _local_merge_git_side_effect(source="my-branch")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {
            "worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
            "source_branch": "my-branch",
            "target_branch": "main",
        },
    )

    assert result["success"] is True
    assert result["source_branch"] == "my-branch"
    push_calls = [c for c in mock_git_manager._run_git.call_args_list if c[0][0][:1] == ["push"]]
    assert len(push_calls) == 0
    mock_worktree_storage.mark_merged.assert_called_once_with(wt.id)


@pytest.mark.asyncio
async def test_merge_worktree_uses_project_repo_for_local_target_merge(
    registry: Any,
    mock_worktree_storage: MagicMock,
    mock_git_manager: MagicMock,
) -> None:
    """Git merge commands run in the project repo because the target is local."""
    wt = Worktree(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.side_effect = _local_merge_git_side_effect()
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01", "target_branch": "main"},
    )

    assert result["success"] is True
    merge_calls = [
        call
        for call in mock_git_manager._run_git.call_args_list
        if call[0][0] == ["merge", "refs/heads/feature/test", "--no-ff", "--no-edit"]
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0].kwargs.get("cwd") == "/tmp/repo"


@pytest.mark.asyncio
async def test_get_worktree_by_task_downgrades_stale_merged_status(
    registry: Any, mock_worktree_storage: MagicMock, mock_git_manager: MagicMock
) -> None:
    wt = Worktree(
        id="wt-123",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.MERGED.value,
        created_at=_VALID_TIMESTAMP,
        updated_at=_VALID_TIMESTAMP,
        task_id="task-1",
        agent_session_id=None,
        merged_at=datetime.fromisoformat("2026-04-22T00:00:00+00:00"),
        cleanup_after=datetime.fromisoformat("2026-04-29T00:00:00+00:00"),
    )
    mock_worktree_storage.get_by_task.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=1, stdout="", stderr="")

    result = await registry.call("get_worktree_by_task", {"task_id": "task-1"})

    assert result["success"] is True
    assert result["worktree"]["status"] == WorktreeStatus.ACTIVE.value
    assert result["worktree"]["stored_status"] == WorktreeStatus.MERGED.value
    assert result["worktree"]["git_merge_state"]["git_merged"] is False
    mock_worktree_storage.get_by_task.assert_called_once_with("task-1")


def test_delete_worktree_schema_requires_exactly_one_identifier(registry: Any) -> None:
    schema = registry.get_schema("delete_worktree")

    assert schema is not None
    input_schema = schema["inputSchema"]
    assert input_schema["oneOf"] == [
        {"required": ["worktree_id"], "not": {"required": ["worktree_path"]}},
        {"required": ["worktree_path"], "not": {"required": ["worktree_id"]}},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"worktree_id": "worktree-1", "worktree_path": "/tmp/adopted"}],
)
async def test_delete_worktree_runtime_rejects_invalid_identifier_count(
    registry: Any,
    arguments: dict[str, str],
) -> None:
    result = await registry.call("delete_worktree", arguments)

    assert result == {
        "success": False,
        "error": "Provide exactly one of worktree_id or worktree_path",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("branch_name", ["feature/adopt", None])
async def test_delete_worktree_adopts_path_before_deletion(
    registry: Any,
    mock_worktree_storage: MagicMock,
    mock_git_manager: MagicMock,
    branch_name: str | None,
) -> None:
    project_id = "11111111-1111-4111-8111-111111110001"
    worktree = Worktree(
        id="worktree-adopted",
        project_id=project_id,
        branch_name=branch_name,
        worktree_path="/tmp/adopted",
        base_branch="main",
        status="active",
        created_at=datetime.fromisoformat(_VALID_TIMESTAMP),
        updated_at=datetime.fromisoformat(_VALID_TIMESTAMP),
        task_id=None,
        agent_session_id=None,
    )
    mock_git_manager.inspect_worktree.return_value = WorktreeInfo(
        path="/tmp/adopted",
        branch=branch_name,
        commit="abc123",
        is_detached=branch_name is None,
    )
    mock_git_manager.get_default_branch.return_value = "main"
    mock_worktree_storage.register_adopted.return_value = (worktree, True)
    mock_worktree_storage.get.return_value = worktree
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True
    mock_worktree_storage.delete.return_value = True

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("gobby.mcp_proxy.tools.worktrees._lifecycle.emit_worktree_event") as emit_adoption,
    ):
        result = await registry.call("delete_worktree", {"worktree_path": "/tmp/../tmp/adopted"})

    assert result["success"] is True
    mock_git_manager.inspect_worktree.assert_called_once_with("/tmp/../tmp/adopted")
    mock_worktree_storage.register_adopted.assert_called_once_with(
        project_id=project_id,
        branch_name=branch_name,
        worktree_path="/tmp/adopted",
        base_branch="main",
    )
    emit_adoption.assert_called_once_with(
        "worktree_adopted",
        worktree_id="worktree-adopted",
        project_id=project_id,
        branch_name=branch_name,
        worktree_path="/tmp/adopted",
        base_branch="main",
    )


@pytest.mark.asyncio
async def test_delete_worktree_reuses_registered_path_without_adoption_event(
    registry: Any,
    mock_worktree_storage: MagicMock,
    mock_git_manager: MagicMock,
) -> None:
    worktree = Worktree(
        id="worktree-existing",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/adopt",
        worktree_path="/tmp/adopted",
        base_branch="main",
        status="active",
        created_at=datetime.fromisoformat(_VALID_TIMESTAMP),
        updated_at=datetime.fromisoformat(_VALID_TIMESTAMP),
        task_id=None,
        agent_session_id=None,
    )
    mock_git_manager.inspect_worktree.return_value = WorktreeInfo(
        path="/tmp/adopted", branch="feature/adopt", commit="abc123"
    )
    mock_git_manager.get_default_branch.return_value = "main"
    mock_worktree_storage.register_adopted.return_value = (worktree, False)
    mock_worktree_storage.get.return_value = worktree
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True
    mock_worktree_storage.delete.return_value = True

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("gobby.mcp_proxy.tools.worktrees._lifecycle.emit_worktree_event") as emit_adoption,
    ):
        result = await registry.call("delete_worktree", {"worktree_path": "/tmp/adopted"})

    assert result["success"] is True
    emit_adoption.assert_not_called()


@pytest.mark.asyncio
async def test_delete_worktree_invalid_path_is_not_idempotent(
    registry: Any,
    mock_git_manager: MagicMock,
) -> None:
    mock_git_manager.inspect_worktree.side_effect = ValueError("Path is not a linked worktree")

    result = await registry.call("delete_worktree", {"worktree_path": "/tmp/unlinked"})

    assert result == {"success": False, "error": "Path is not a linked worktree"}
    assert "already_deleted" not in result

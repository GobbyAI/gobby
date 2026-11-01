"""Close atomicity and optional Git observations under selected policy."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, TaskStaleStateError
from gobby.tasks.closure_snapshot import TaskClosurePolicyError
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _author(db: HubDatabase, project_id: str, when: str) -> str:
    RuleDefinitionManager(db).create(
        "integrity-observation",
        {
            "event": "task_close",
            "when": when,
            "effects": [{"type": "block", "reason": "The selected integrity observation failed."}],
        },
        project_id=project_id,
        tags=["close-integrity"],
    )
    return AgentSessions(db, project_id).add(
        "integrity-author",
        definition_fields={"workflows": {"rule_selectors": {"include": ["tag:close-integrity"]}}},
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    git(tmp_path, "init", "-b", "integration")
    git(tmp_path, "config", "user.name", "Close test")
    git(tmp_path, "config", "user.email", "close@example.invalid")
    (tmp_path / "owned.txt").write_text("base\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    landed = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-b", "task")
    (tmp_path / "owned.txt").write_text("task\n")
    git(tmp_path, "commit", "-am", "task")
    return tmp_path, landed, git(tmp_path, "rev-parse", "HEAD")


def test_close_is_atomic_and_idempotent(
    repository: tuple[Path, str, str],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    author = _author(temp_db, sample_project["id"], "not facts.get('children_closed')")
    repo, landed, _ = repository
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Atomic close", task_type="epic")
    child = manager.create_task(
        sample_project["id"],
        "Child",
        parent_task_id=task.id,
        validation_criteria="Observable completion.",
    )
    with pytest.raises(TaskClosurePolicyError):
        manager.close_task_with_commit(task.id, landed, cwd=repo, closed_in_session_id=author)
    rejected = manager.get_task(task.id)
    assert rejected.closed_at is None
    assert not rejected.commits
    manager.close_task(child.id, reason="moot")
    # The unclaimed structural parent may already have auto-closed. A fresh leaf
    # exercises the explicit commit/link transition and its repeated call.
    leaf = manager.create_task(
        sample_project["id"], "Leaf", validation_criteria="Observable completion."
    )
    closed = manager.close_task_with_commit(leaf.id, landed, cwd=repo)
    repeated = manager.close_task_with_commit(
        leaf.id, landed, cwd=repo, expected_updated_at=leaf.updated_at
    )
    assert closed.closed_at is not None
    assert repeated.closed_at == closed.closed_at
    assert repeated.updated_at == closed.updated_at
    assert repeated.commits == closed.commits
    assert len(closed.commits or []) == 1


def test_closing_note_is_written_only_with_its_close(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    kept = manager.create_task(
        sample_project["id"], "Kept", validation_criteria="Observable completion."
    )
    noted = manager.create_task(
        sample_project["id"], "Noted", validation_criteria="Observable completion."
    )
    manager.close_task(kept.id, reason="moot")
    manager.close_task(
        noted.id,
        reason="superseded",
        closing_note="closed as superseded; superseded by #24970",
    )
    rows = temp_db.fetchall(
        "SELECT task_id, reason FROM task_lifecycle_events WHERE to_state = 'closed' ORDER BY id"
    )
    assert [(str(row["task_id"]), row["reason"]) for row in rows] == [
        (noted.id, "closed as superseded; superseded by #24970")
    ]


def test_stale_close_does_not_link_commit(
    repository: tuple[Path, str, str],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    repo, landed, _ = repository
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Changed task", validation_criteria="Observable completion."
    )
    manager.add_label(task.id, "changed")
    with pytest.raises(TaskStaleStateError):
        manager.close_task_with_commit(
            task.id, landed, cwd=repo, expected_updated_at=task.updated_at
        )
    unchanged = manager.get_task(task.id)
    assert unchanged.closed_at is None
    assert not unchanged.commits


@pytest.mark.asyncio
async def test_close_needs_no_evidence_relay(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
    from gobby.utils.machine_id import require_machine_id

    repo = Path(sample_git_project["repo_path"])
    (repo / "work.txt").write_text("complete\n")
    git(repo, "add", "work.txt")
    git(repo, "commit", "-m", "completed work")
    sha = git(repo, "rev-parse", "HEAD")
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.register(
        external_id="four-invariant-close",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    task = manager.create_task(
        sample_git_project["id"], "No relay", validation_criteria="Live: ordinary reviewer text."
    )
    task = manager.claim_task(task.id, session.id)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    evidence = Mock(side_effect=AssertionError("close must not read transcript evidence"))
    monkeypatch.setattr("gobby.tasks.transcript_evidence.derive_transcript_evidence", evidence)
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    preview = await registry.call(
        "close_task",
        {
            "task_id": task.id,
            "commit_sha": sha,
            "preview": True,
        },
    )
    assert preview["can_close"] is True, (preview.get("error"), preview.get("gates"))
    pending = manager.get_task(task.id)
    assert pending.closed_at is None and not pending.commits
    closed = await registry.call("close_task", {"task_id": task.id, "commit_sha": sha})
    assert closed["closed"] is True, closed
    assert closed["policy_revision"] == preview["policy_revision"]
    assert manager.get_task(task.id).closed_commit_sha == sha
    assert manager.get_task(task.id).review_status == task.review_status
    evidence.assert_not_called()


@pytest.mark.asyncio
async def test_close_uses_authoritative_integration_checkout(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task

    main = Path(sample_git_project["repo_path"])
    clone = tmp_path / "clone"
    git(main, "clone", str(main), str(clone))
    git(clone, "config", "user.name", "Close test")
    git(clone, "config", "user.email", "close@example.invalid")
    branch = git(main, "symbolic-ref", "HEAD")
    base = git(main, "rev-parse", "HEAD")
    (clone / "work.txt").write_text("clone-only work\n")
    git(clone, "add", "work.txt")
    git(clone, "commit", "-m", "clone work")
    sha = git(clone, "rev-parse", "HEAD")
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.get(
        _author(temp_db, sample_git_project["id"], "facts.get('integration_ancestry') != True")
    )
    assert session is not None
    task = manager.create_task(
        sample_git_project["id"], "Clone close", validation_criteria="Landed work."
    )
    manager.claim_task(task.id, session.id)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    # Exercise an authorized clone at the repository-resolution boundary.
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.resolve_task_repo_path",
        lambda **kwargs: str(clone),
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    arguments = {"task_id": task.id, "commit_sha": sha, "project_path": str(clone)}
    for preview in (True, False):
        result = await registry.call("close_task", {**arguments, "preview": preview})
        assert result["can_close"] is False, result
        assert result["error"] == "closure_policy"
    assert manager.get_task(task.id).closed_at is None
    assert not manager.get_task(task.id).commits

    git(main, "fetch", str(clone), branch)
    git(main, "merge", "--ff-only", "FETCH_HEAD")
    git(clone, "checkout", "--detach", sha)
    git(clone, "update-ref", branch, base)
    preview = await registry.call("close_task", {**arguments, "preview": True})
    assert preview["can_close"] is True, preview
    closed = await registry.call("close_task", arguments)
    assert closed["closed"] is True, closed


@pytest.mark.parametrize("explicit_main", [False, True])
@pytest.mark.asyncio
async def test_no_work_close_checks_dirty_task_checkout(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_main: bool,
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task

    main = Path(sample_git_project["repo_path"])
    worktree = tmp_path / "dirty-task"
    git(main, "worktree", "add", "-b", "dirty-task", str(worktree))
    (worktree / "owned.txt").write_text("uncommitted task work\n")
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.get(
        _author(temp_db, sample_git_project["id"], "facts.get('task_files_clean') != True")
    )
    assert session is not None
    task = manager.create_task(
        sample_git_project["id"], "Dirty no-work", validation_criteria="Task-owned work."
    )
    manager.claim_task(task.id, session.id)
    ctx.worktree_manager.create(
        project_id=task.project_id,
        branch_name="dirty-task",
        worktree_path=str(worktree),
        task_id=task.id,
    )
    ctx.session_var_manager.merge_variables(
        session.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "task_claimed": True,
            "task_edited_files": {task.id: ["owned.txt"]},
            "task_edited_file_checkouts": {task.id: {str(worktree): ["owned.txt"]}},
            "session_dirty_files": ["owned.txt"],
            "session_dirty_file_checkouts": {str(worktree): ["owned.txt"]},
        },
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    args: dict[str, Any] = {"task_id": task.id, "reason": "moot"}
    if explicit_main:
        args["project_path"] = str(main)
    for preview in (True, False):
        result = await registry.call("close_task", {**args, "preview": preview})
        assert result["can_close"] is False, result
        assert result["error"] == "closure_policy"
    assert manager.get_task(task.id).closed_at is None

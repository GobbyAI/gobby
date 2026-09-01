"""Tests for session-scoped Changes panel resolution and git change detection."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.servers import session_changes
from gobby.servers.session_changes import (
    SessionWorkspace,
    compute_session_changes,
    compute_session_file_diff,
    is_safe_relative_path,
    resolve_session_workspace,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo),
            "PATH": os.environ.get("PATH", ""),
        },
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    (repo / "kept.txt").write_text("base\n", encoding="utf-8")
    (repo / "edited.txt").write_text("line1\nline2\n", encoding="utf-8")
    (repo / "removed.txt").write_text("bye\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _raise(exc: Exception) -> None:
    raise exc


@pytest.mark.asyncio
async def test_compute_session_changes_detects_new_edited_deleted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Edit, delete, and add files in the working tree.
    (repo / "edited.txt").write_text("line1\nCHANGED\n", encoding="utf-8")
    (repo / "removed.txt").unlink()
    (repo / "fresh.txt").write_text("new file\n", encoding="utf-8")
    # Internal noise that must be filtered out.
    (repo / ".gobby").mkdir()
    (repo / ".gobby" / "state.json").write_text("{}", encoding="utf-8")

    workspace = SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="none")
    changes = await compute_session_changes(workspace)

    by_path = {c.path: c.status for c in changes}
    assert by_path == {"fresh.txt": "W", "edited.txt": "E", "removed.txt": "D"}
    # New files sort before edits before deletes.
    assert [c.path for c in changes] == ["fresh.txt", "edited.txt", "removed.txt"]


@pytest.mark.asyncio
async def test_compute_changes_against_base_commit_includes_committed_work(tmp_path: Path) -> None:
    """A resumed session that already committed shows changes vs the base commit."""
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    # Commit new work after the base, as a resumed session would.
    (repo / "edited.txt").write_text("line1\ncommitted change\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "session work")

    # Diffing against HEAD would show nothing; against the base commit it appears.
    head_ws = SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="worktree")
    assert await compute_session_changes(head_ws) == []

    base_ws = SessionWorkspace(working_dir=str(repo), base_ref=base_sha, isolation="worktree")
    base_changes = await compute_session_changes(base_ws)
    assert {c.path for c in base_changes} == {"edited.txt"}


@pytest.mark.asyncio
async def test_compute_session_file_diff_for_edited_and_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "edited.txt").write_text("line1\nCHANGED\n", encoding="utf-8")
    (repo / "fresh.txt").write_text("brand new\n", encoding="utf-8")
    workspace = SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="none")

    edited_diff = await compute_session_file_diff(workspace, "edited.txt")
    assert "edited.txt" in edited_diff
    assert "+line1\nCHANGED" in edited_diff or "+CHANGED" in edited_diff

    fresh_diff = await compute_session_file_diff(workspace, "fresh.txt")
    assert "brand new" in fresh_diff


def test_new_file_diff_rejects_oversized_file_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"large")
    monkeypatch.setattr(session_changes, "_MAX_NEW_FILE_DIFF_BYTES", 4)

    def fail_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("oversized file content must not be opened")

    monkeypatch.setattr(Path, "open", fail_open)

    diff = session_changes._new_file_diff(path, "large.txt")

    assert "diff --git a/large.txt b/large.txt" in diff
    assert "File too large to display (5 bytes)" in diff


@pytest.mark.asyncio
async def test_compute_session_file_diff_rejects_path_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    workspace = SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="none")

    with pytest.raises(ValueError, match="unsafe or not a safe relative path"):
        await compute_session_file_diff(workspace, "../outside.txt")


def test_is_safe_relative_path(tmp_path: Path) -> None:
    base = str(tmp_path)
    assert is_safe_relative_path(base, "src/file.ts") is True
    assert is_safe_relative_path(base, ".") is True
    assert is_safe_relative_path(base, "") is False
    assert is_safe_relative_path(base, "/etc/passwd") is False
    assert is_safe_relative_path(base, "../../etc/passwd") is False


def _patch_checkout_root(monkeypatch: pytest.MonkeyPatch, root: str) -> None:
    monkeypatch.setattr(
        "gobby.storage.project_checkouts.require_root",
        lambda _db, _project_id, _machine_id: root,
    )
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_local_machine_id",
        lambda provided, **_kwargs: provided or "machine-1",
    )


def test_resolve_session_workspace_prefers_isolated_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    _patch_checkout_root(monkeypatch, str(repo))

    artifacts = SimpleNamespace(
        worktree_path=str(worktree),
        clone_path=None,
        base_commit_sha="abc123",
    )
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: [SimpleNamespace(id="task-1")],
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: artifacts),
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
    )
    assert ws == SessionWorkspace(
        working_dir=str(worktree), base_ref="abc123", isolation="worktree"
    )


def test_resolve_session_workspace_prefers_isolated_worktree_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.storage.project_checkouts import CheckoutNotFoundError

    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    monkeypatch.setattr(
        "gobby.storage.project_checkouts.require_root",
        lambda _db, _project_id, _machine_id: _raise(CheckoutNotFoundError("missing")),
    )
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_local_machine_id",
        lambda provided, **_kwargs: provided or "machine-1",
    )
    artifacts = SimpleNamespace(
        worktree_path=str(worktree), clone_path=None, base_commit_sha="abc123"
    )
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: [SimpleNamespace(id="task-1")],
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: artifacts),
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
    )

    assert ws == SessionWorkspace(
        working_dir=str(worktree), base_ref="abc123", isolation="worktree"
    )


def test_resolve_session_workspace_propagates_foreign_machine_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-foreign"),
        db=SimpleNamespace(),
    )
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id", lambda: "machine-local"
    )

    with pytest.raises(MachineOwnershipMismatchError):
        resolve_session_workspace(
            session_manager=session_manager, task_manager=None, session_id="sess-1"
        )


def test_resolve_session_workspace_falls_back_to_project_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    _patch_checkout_root(monkeypatch, str(repo))
    # No isolated task for this session.
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: [],
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: None),
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
    )
    assert ws == SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="none")


def test_resolve_session_workspace_recovers_from_project_shape_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )

    monkeypatch.setattr(
        "gobby.storage.project_checkouts.require_root",
        lambda _db, _project_id, _machine_id: _raise(KeyError("repo")),
    )
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_local_machine_id",
        lambda provided, **_kwargs: provided or "machine-1",
    )
    artifacts = SimpleNamespace(
        worktree_path=str(worktree),
        clone_path=None,
        base_commit_sha="abc123",
    )
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: [SimpleNamespace(id="task-1")],
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: artifacts),
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
    )
    assert ws == SessionWorkspace(
        working_dir=str(worktree), base_ref="abc123", isolation="worktree"
    )


def test_resolve_session_workspace_uses_project_repo_after_task_list_shape_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    _patch_checkout_root(monkeypatch, str(repo))
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: _raise(ValueError("tasks")),
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: None),
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
    )
    assert ws == SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="none")


def test_resolve_session_workspace_uses_project_repo_after_artifact_shape_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    _patch_checkout_root(monkeypatch, str(repo))
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: [SimpleNamespace(id="task-1")],
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: _raise(ValueError("artifacts"))),
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
    )
    assert ws == SessionWorkspace(working_dir=str(repo), base_ref="HEAD", isolation="none")


def test_resolve_session_workspace_propagates_unexpected_project_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    monkeypatch.setattr(
        "gobby.storage.project_checkouts.require_root",
        lambda _db, _project_id, _machine_id: _raise(RuntimeError("project boom")),
    )
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_local_machine_id",
        lambda provided, **_kwargs: provided or "machine-1",
    )

    with pytest.raises(RuntimeError, match="project boom"):
        resolve_session_workspace(
            session_manager=session_manager,
            task_manager=SimpleNamespace(list_tasks=lambda claimed_by_session_id: []),
            session_id="sess-1",
        )


def test_resolve_session_workspace_propagates_unexpected_artifact_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id="proj-1", machine_id="machine-1"),
        db=SimpleNamespace(),
    )
    _patch_checkout_root(monkeypatch, str(repo))
    task_manager = SimpleNamespace(
        list_tasks=lambda claimed_by_session_id: [SimpleNamespace(id="task-1")],
        artifacts=SimpleNamespace(get_artifacts=lambda _tid: _raise(RuntimeError("artifact boom"))),
    )

    with pytest.raises(RuntimeError, match="artifact boom"):
        resolve_session_workspace(
            session_manager=session_manager, task_manager=task_manager, session_id="sess-1"
        )


def test_resolve_session_workspace_unknown_session_returns_none() -> None:
    session_manager = SimpleNamespace(get=lambda _sid: None, db=SimpleNamespace())
    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=None, session_id="missing"
    )
    assert ws is None


def test_resolve_session_workspace_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(
            project_id=isolated.project.id, machine_id=isolated.machine_id
        ),
        db=temp_db,
    )

    ws = resolve_session_workspace(
        session_manager=session_manager, task_manager=None, session_id="sess-1"
    )

    assert ws == SessionWorkspace(working_dir=isolated.root_path, base_ref="HEAD", isolation="none")


def test_resolve_session_workspace_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from gobby.storage.projects import LocalProjectManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="session-changes-no-checkout")
    session_manager = SimpleNamespace(
        get=lambda _sid: SimpleNamespace(project_id=project.id, machine_id=machine_id),
        db=temp_db,
    )

    with pytest.raises(CheckoutNotFoundError):
        resolve_session_workspace(
            session_manager=session_manager, task_manager=None, session_id="sess-1"
        )

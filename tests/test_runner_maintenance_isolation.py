"""Tests for runner isolation maintenance cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.runner_maintenance import _cleanup_missing_isolation_records
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


def test_cleanup_missing_isolation_records_removes_dead_paths(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    """Worktree and clone records with missing directories are removed."""
    project = LocalProjectManager(temp_db).create(
        name="proj-1",
        repo_path=str(tmp_path / "repo"),
    )
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)

    existing_worktree_path = tmp_path / "existing-worktree"
    existing_worktree_path.mkdir()
    existing_clone_path = tmp_path / "existing-clone"
    existing_clone_path.mkdir()

    missing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/missing-worktree",
        worktree_path=str(tmp_path / "missing-worktree"),
    )
    existing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/existing-worktree",
        worktree_path=str(existing_worktree_path),
    )
    missing_clone = clones.create(
        project_id=project.id,
        branch_name="task/missing-clone",
        clone_path=str(tmp_path / "missing-clone"),
    )
    existing_clone = clones.create(
        project_id=project.id,
        branch_name="task/existing-clone",
        clone_path=str(existing_clone_path),
    )

    counts = _cleanup_missing_isolation_records(worktrees, clones)

    assert counts == {"worktrees": 1, "clones": 1}
    assert worktrees.get(missing_worktree.id) is None
    assert clones.get(missing_clone.id) is None
    assert worktrees.get(existing_worktree.id) is not None
    assert clones.get(existing_clone.id) is not None

"""Cross-machine isolation-maintenance regression tests."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import gobby.runner_maintenance.isolation as isolation_module
import gobby.storage.clones as clones_module
import gobby.storage.worktrees as worktrees_module
from gobby.runner_maintenance import _cleanup_missing_isolation_records
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


def test_missing_path_sweep_ignores_remote_rows(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon's missing-path sweep leaves another machine's records untouched."""
    local_machine_id = str(uuid.uuid4())
    remote_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, remote_machine_id):
        temp_db.execute(
            "INSERT INTO machines (id, hostname) VALUES (%s, %s)",
            (machine_id, f"host-{machine_id}"),
        )

    project = LocalProjectManager(temp_db).create(
        name=f"scope-{uuid.uuid4()}",
        repo_path=str(tmp_path / "repo"),
    )
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)
    worktree_owners = iter((local_machine_id, remote_machine_id))
    clone_owners = iter((local_machine_id, remote_machine_id))
    monkeypatch.setattr(
        worktrees_module,
        "require_machine_id",
        lambda: next(worktree_owners),
    )
    monkeypatch.setattr(clones_module, "require_machine_id", lambda: next(clone_owners))
    monkeypatch.setattr(isolation_module, "require_machine_id", lambda: local_machine_id)

    local_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/local-worktree",
        worktree_path=str(tmp_path / "missing-local-worktree"),
    )
    remote_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/remote-worktree",
        worktree_path=str(tmp_path / "missing-remote-worktree"),
    )
    local_clone = clones.create(
        project_id=project.id,
        branch_name="task/local-clone",
        clone_path=str(tmp_path / "missing-local-clone"),
    )
    remote_clone = clones.create(
        project_id=project.id,
        branch_name="task/remote-clone",
        clone_path=str(tmp_path / "missing-remote-clone"),
    )
    monkeypatch.setattr(worktrees_module, "require_machine_id", lambda: local_machine_id)
    monkeypatch.setattr(clones_module, "require_machine_id", lambda: local_machine_id)
    remote_sentinel = tmp_path / "remote-filesystem"
    remote_sentinel.mkdir()
    (remote_sentinel / "owned-by-remote").write_text("keep", encoding="utf-8")

    counts = _cleanup_missing_isolation_records(worktrees, clones)

    assert counts == {"worktrees": 1, "clones": 1}
    assert worktrees.get(local_worktree.id) is None
    assert clones.get(local_clone.id) is None
    assert temp_db.fetchone("SELECT id FROM worktrees WHERE id = %s", (remote_worktree.id,))
    assert temp_db.fetchone("SELECT id FROM clones WHERE id = %s", (remote_clone.id,))
    assert (remote_sentinel / "owned-by-remote").read_text(encoding="utf-8") == "keep"

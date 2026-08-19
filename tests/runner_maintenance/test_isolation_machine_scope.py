"""Cross-machine isolation-maintenance regression tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import gobby.storage.clones as clones_module
import gobby.storage.worktrees as worktrees_module
from gobby.runner_maintenance import (
    _cleanup_missing_isolation_records,
    _cleanup_missing_isolation_records_async,
)
from gobby.storage.clones import Clone, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@dataclass
class _CrossMachineFixture:
    worktrees: LocalWorktreeManager
    clones: LocalCloneManager
    local_worktree: Worktree
    remote_worktree: Worktree
    remote_missing_worktree: Worktree
    local_clone: Clone
    remote_clone: Clone
    remote_missing_clone: Clone
    remote_worktree_path: Path
    remote_clone_path: Path


def _seed_cross_machine_records(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _CrossMachineFixture:
    """Seed local-missing plus remote-existing and remote-missing records."""
    local_machine_id = str(uuid.uuid4())
    remote_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, remote_machine_id):
        temp_db.execute(
            "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s)",
            (machine_id, f"host-{machine_id}", TEST_USER_ID),
        )

    project = LocalProjectManager(temp_db).create(
        name=f"scope-{uuid.uuid4()}",
        repo_path=str(tmp_path / "repo"),
    )
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)
    worktree_owners = iter((local_machine_id, remote_machine_id, remote_machine_id))
    clone_owners = iter((local_machine_id, remote_machine_id, remote_machine_id))
    monkeypatch.setattr(
        worktrees_module,
        "require_machine_id",
        lambda: next(worktree_owners),
    )
    monkeypatch.setattr(clones_module, "require_machine_id", lambda: next(clone_owners))

    local_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/local-worktree",
        worktree_path=str(tmp_path / "missing-local-worktree"),
    )
    remote_worktree_path = tmp_path / "remote-worktree"
    remote_worktree_path.mkdir()
    remote_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/remote-worktree",
        worktree_path=str(remote_worktree_path),
    )
    remote_missing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/remote-missing-worktree",
        worktree_path=str(tmp_path / "missing-remote-worktree"),
    )
    local_clone = clones.create(
        project_id=project.id,
        branch_name="task/local-clone",
        clone_path=str(tmp_path / "missing-local-clone"),
    )
    remote_clone_path = tmp_path / "remote-clone"
    remote_clone_path.mkdir()
    remote_clone = clones.create(
        project_id=project.id,
        branch_name="task/remote-clone",
        clone_path=str(remote_clone_path),
    )
    remote_missing_clone = clones.create(
        project_id=project.id,
        branch_name="task/remote-missing-clone",
        clone_path=str(tmp_path / "missing-remote-clone"),
    )
    monkeypatch.setattr(worktrees_module, "require_machine_id", lambda: local_machine_id)
    monkeypatch.setattr(clones_module, "require_machine_id", lambda: local_machine_id)
    return _CrossMachineFixture(
        worktrees=worktrees,
        clones=clones,
        local_worktree=local_worktree,
        remote_worktree=remote_worktree,
        remote_missing_worktree=remote_missing_worktree,
        local_clone=local_clone,
        remote_clone=remote_clone,
        remote_missing_clone=remote_missing_clone,
        remote_worktree_path=remote_worktree_path,
        remote_clone_path=remote_clone_path,
    )


def _assert_only_local_records_swept(
    temp_db: HubDatabase,
    fixture: _CrossMachineFixture,
    counts: dict[str, int],
) -> None:
    assert counts == {"worktrees": 1, "clones": 1}
    assert fixture.worktrees.get(fixture.local_worktree.id) is None
    assert fixture.clones.get(fixture.local_clone.id) is None
    assert temp_db.fetchone("SELECT id FROM worktrees WHERE id = %s", (fixture.remote_worktree.id,))
    assert temp_db.fetchone(
        "SELECT id FROM worktrees WHERE id = %s", (fixture.remote_missing_worktree.id,)
    )
    assert temp_db.fetchone("SELECT id FROM clones WHERE id = %s", (fixture.remote_clone.id,))
    assert temp_db.fetchone(
        "SELECT id FROM clones WHERE id = %s", (fixture.remote_missing_clone.id,)
    )
    assert fixture.remote_worktree_path.is_dir()
    assert fixture.remote_clone_path.is_dir()


def test_missing_path_sweep_ignores_remote_rows(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon's missing-path sweep leaves another machine's records untouched."""
    fixture = _seed_cross_machine_records(temp_db, tmp_path, monkeypatch)

    counts = _cleanup_missing_isolation_records(fixture.worktrees, fixture.clones)

    _assert_only_local_records_swept(temp_db, fixture, counts)


@pytest.mark.asyncio
async def test_async_missing_path_sweep_ignores_remote_rows(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon-loop async sweep runs against real storage signatures (#19798)."""
    fixture = _seed_cross_machine_records(temp_db, tmp_path, monkeypatch)

    counts = await _cleanup_missing_isolation_records_async(
        fixture.worktrees,
        fixture.clones,
        run_db=None,
    )

    _assert_only_local_records_swept(temp_db, fixture, counts)

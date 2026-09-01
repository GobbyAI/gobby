"""LocalWorktreeManager resolves worktree references to full UUIDs (#21521).

Session and task refs already accept unique id prefixes; a worktree prefix was
passed straight into an ``id = %s`` compare and surfaced psycopg's
``invalid input syntax for type uuid``. ``resolve_reference`` is the one
place that turns a full UUID or a unique hex prefix into the stored id and
names the failure when nothing (or more than one row) matches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000002"
_PREFIX_A = "0a0a0a0a"
_ID_A = f"{_PREFIX_A}-1111-4111-8111-111111111111"
_ID_A_SIBLING = f"{_PREFIX_A}-2222-4222-8222-222222222222"


@pytest.fixture
def project_id(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One isolated project whose machine is pinned as local for the whole test."""
    return install_isolated_checkout_project(
        temp_db, tmp_path / "isolated-checkout", machine_id=MACHINE_ID, monkeypatch=monkeypatch
    ).project.id


def _create_with_id(
    manager: LocalWorktreeManager,
    temp_db: HubDatabase,
    project_id: str,
    *,
    worktree_id: str,
    branch_name: str,
) -> Worktree:
    created = manager.create(
        project_id=project_id,
        branch_name=branch_name,
        worktree_path=f"/tmp/worktrees/{branch_name}",
    )
    temp_db.execute("UPDATE worktrees SET id = %s WHERE id = %s", (worktree_id, created.id))
    resolved = manager.get(worktree_id)
    assert resolved is not None
    return resolved


def test_full_uuid_resolves_to_itself(temp_db: HubDatabase, project_id: str) -> None:
    manager = LocalWorktreeManager(temp_db)
    worktree = manager.create(
        project_id=project_id,
        branch_name="feature/full-uuid",
        worktree_path="/tmp/worktrees/full-uuid",
    )

    assert manager.resolve_reference(worktree.id) == worktree.id
    assert manager.resolve_reference(worktree.id.upper()) == worktree.id


def test_unique_prefix_resolves_to_full_id(temp_db: HubDatabase, project_id: str) -> None:
    manager = LocalWorktreeManager(temp_db)
    worktree = _create_with_id(
        manager,
        temp_db,
        project_id,
        worktree_id=_ID_A,
        branch_name="feature/prefix",
    )

    assert manager.resolve_reference(_PREFIX_A) == worktree.id
    assert manager.resolve_reference(_ID_A[:13]) == worktree.id


def test_ambiguous_prefix_names_the_matches(temp_db: HubDatabase, project_id: str) -> None:
    manager = LocalWorktreeManager(temp_db)
    for worktree_id, branch_name in ((_ID_A, "feature/one"), (_ID_A_SIBLING, "feature/two")):
        _create_with_id(
            manager,
            temp_db,
            project_id,
            worktree_id=worktree_id,
            branch_name=branch_name,
        )

    with pytest.raises(ValueError, match=f"[Aa]mbiguous.*'{_PREFIX_A}'") as excinfo:
        manager.resolve_reference(_PREFIX_A)

    assert _ID_A in str(excinfo.value)
    assert _ID_A_SIBLING in str(excinfo.value)


@pytest.mark.parametrize(
    "ref",
    ["deadbeef", "not-a-uuid!", "", "%", "_", "0a0a0a0a zz"],
    ids=["unknown-prefix", "garbage", "empty", "like-percent", "like-underscore", "non-hex-tail"],
)
def test_unmatched_reference_is_not_found(temp_db: HubDatabase, project_id: str, ref: str) -> None:
    manager = LocalWorktreeManager(temp_db)
    manager.create(
        project_id=project_id,
        branch_name="feature/present",
        worktree_path="/tmp/worktrees/present",
    )

    with pytest.raises(ValueError, match="not found") as excinfo:
        manager.resolve_reference(ref)

    assert "invalid input syntax" not in str(excinfo.value)

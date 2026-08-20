from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktrees import LocalWorktreeManager, Worktree

pytestmark = pytest.mark.unit

_TIMESTAMP = datetime.fromisoformat("2026-01-01T00:00:00+00:00")


def _worktree(*, project_id: str = "project-1") -> Worktree:
    return Worktree(
        id="worktree-1",
        project_id=project_id,
        branch_name="feature/adopt",
        worktree_path="/tmp/adopted",
        base_branch="main",
        status="active",
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        task_id=None,
        agent_session_id=None,
    )


@pytest.fixture
def manager() -> LocalWorktreeManager:
    return LocalWorktreeManager(MagicMock(spec=HubDatabase))


def test_register_adopted_creates_new_record(manager: LocalWorktreeManager) -> None:
    created = _worktree()
    with (
        patch.object(manager, "has_path_on_other_machine", return_value=False),
        patch.object(manager, "get_by_path", return_value=None),
        patch.object(manager, "create", return_value=created) as create,
    ):
        worktree, adopted = manager.register_adopted("project-1", None, "/tmp/adopted", "main")

    assert (worktree, adopted) == (created, True)
    create.assert_called_once_with(
        project_id="project-1",
        branch_name=None,
        worktree_path="/tmp/adopted",
        base_branch="main",
    )


def test_register_adopted_reuses_existing_same_project_record(
    manager: LocalWorktreeManager,
) -> None:
    existing = _worktree()
    with (
        patch.object(manager, "has_path_on_other_machine", return_value=False),
        patch.object(manager, "get_by_path", return_value=existing),
        patch.object(manager, "create") as create,
    ):
        result = manager.register_adopted("project-1", "feature/adopt", "/tmp/adopted", "main")

    assert result == (existing, False)
    create.assert_not_called()


@pytest.mark.parametrize("other_machine", [False, True])
def test_register_adopted_rejects_foreign_owner(
    manager: LocalWorktreeManager,
    other_machine: bool,
) -> None:
    existing = None if other_machine else _worktree(project_id="project-2")
    with (
        patch.object(manager, "has_path_on_other_machine", return_value=other_machine),
        patch.object(manager, "get_by_path", return_value=existing),
    ):
        with pytest.raises(ValueError, match="another (machine|project)"):
            manager.register_adopted("project-1", "feature/adopt", "/tmp/adopted", "main")


def test_register_adopted_collapses_same_path_insert_race(
    manager: LocalWorktreeManager,
) -> None:
    winner = _worktree()
    with (
        patch.object(manager, "has_path_on_other_machine", return_value=False),
        patch.object(manager, "get_by_path", side_effect=[None, winner]),
        patch.object(manager, "create", side_effect=psycopg.IntegrityError("duplicate")),
    ):
        result = manager.register_adopted("project-1", "feature/adopt", "/tmp/adopted", "main")

    assert result == (winner, False)


def test_register_adopted_propagates_unrelated_uniqueness_conflict(
    manager: LocalWorktreeManager,
) -> None:
    conflict = psycopg.IntegrityError("branch conflict")
    with (
        patch.object(manager, "has_path_on_other_machine", return_value=False),
        patch.object(manager, "get_by_path", side_effect=[None, None]),
        patch.object(manager, "create", side_effect=conflict),
    ):
        with pytest.raises(psycopg.IntegrityError, match="branch conflict"):
            manager.register_adopted("project-1", "feature/adopt", "/tmp/adopted", "main")

"""Per-worktree reconcile notes: owner, next action and blocker for the status report."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktree_notes import WorktreeNote, WorktreeNoteStore
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit

WRITTEN_AT = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)


def _worktree(db: HubDatabase, project_id: str, branch: str) -> str:
    return LocalWorktreeManager(db).create(project_id, branch, f"/repo/worktrees/{branch}").id


def test_note_round_trips_for_its_worktree(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    tree = _worktree(temp_db, str(sample_project["id"]), "task-1")
    store = WorktreeNoteStore(temp_db)

    store.upsert(
        tree,
        owner="dev-1",
        next_action="rerun the focused tests",
        blocker="",
        updated_at=WRITTEN_AT,
    )

    assert store.list_for([tree]) == {
        tree: WorktreeNote(
            owner="dev-1",
            next_action="rerun the focused tests",
            blocker="",
            updated_at=WRITTEN_AT,
        )
    }


def test_note_eta_round_trips_and_can_be_cleared(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    tree = _worktree(temp_db, str(sample_project["id"]), "eta-fixture")
    store = WorktreeNoteStore(temp_db)
    store.upsert(
        tree,
        owner="fixture-holder",
        next_action="validate",
        blocker="",
        updated_at=WRITTEN_AT,
        eta="14:45 CT",
    )
    assert store.list_for([tree])[tree].eta == "14:45 CT"
    store.upsert(
        tree,
        owner="fixture-holder",
        next_action="land",
        blocker="",
        updated_at=WRITTEN_AT,
    )
    assert store.list_for([tree])[tree].eta is None


def test_second_write_replaces_every_field(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    tree = _worktree(temp_db, str(sample_project["id"]), "task-1")
    store = WorktreeNoteStore(temp_db)
    store.upsert(tree, owner="dev-1", next_action="first", blocker="waiting", updated_at=WRITTEN_AT)

    store.upsert(tree, owner="dev-2", next_action="second", blocker="", updated_at=WRITTEN_AT)

    assert store.list_for([tree]) == {
        tree: WorktreeNote(owner="dev-2", next_action="second", blocker="", updated_at=WRITTEN_AT)
    }


def test_lookup_returns_only_the_requested_worktrees(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = str(sample_project["id"])
    noted = _worktree(temp_db, project_id, "task-1")
    bare = _worktree(temp_db, project_id, "task-2")
    WorktreeNoteStore(temp_db).upsert(
        noted, owner="dev-1", next_action="next", blocker="", updated_at=WRITTEN_AT
    )

    assert WorktreeNoteStore(temp_db).list_for([bare]) == {}


def test_deleting_a_worktree_removes_its_note(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    tree = _worktree(temp_db, str(sample_project["id"]), "task-1")
    store = WorktreeNoteStore(temp_db)
    store.upsert(tree, owner="dev-1", next_action="next", blocker="", updated_at=WRITTEN_AT)

    temp_db.execute("DELETE FROM worktrees WHERE id = %s", (tree,))

    assert store.list_for([tree]) == {}

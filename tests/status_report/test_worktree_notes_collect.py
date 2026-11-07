"""Collector: a worktree note is paired with the tree's last commit or dirty change, not guessed."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.status_report.collect import collect_status
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktree_notes import WorktreeNoteStore
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)
LAST_COMMIT = NOW - timedelta(minutes=12)
BRANCH = "0.5.0"


def _git(cwd: Path, *args: str, when: datetime | None = None) -> None:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="status",
        GIT_AUTHOR_EMAIL="status@example.invalid",
        GIT_COMMITTER_NAME="status",
        GIT_COMMITTER_EMAIL="status@example.invalid",
    )
    if when is not None:
        env.update(GIT_AUTHOR_DATE=when.isoformat(), GIT_COMMITTER_DATE=when.isoformat())
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", BRANCH)
    (path / "keep.txt").write_text("keep\n")
    _git(path, "add", "keep.txt")
    _git(path, "commit", "-q", "-m", "keep", when=LAST_COMMIT)
    return path


def _stamp(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def _collect_row(db: HubDatabase, project_id: str, project_path: Path) -> Any:
    worktrees = LocalWorktreeManager(db).list_worktrees(project_id=project_id)
    data = collect_status(
        db,
        project_id=project_id,
        project_path=project_path,
        integration_branch=BRANCH,
        now=NOW,
        window=timedelta(minutes=30),
        load_threshold=60.0,
        runs=(),
        worktrees=tuple(worktrees),
        loadavg=lambda: (0.0, 0.0, 0.0),
    )
    (row,) = data.worktrees
    return row


@pytest.mark.parametrize(
    ("dirty_at", "note_at", "current"),
    [
        (None, NOW - timedelta(minutes=15), False),
        (None, NOW - timedelta(minutes=6), True),
        (NOW - timedelta(minutes=3), NOW - timedelta(minutes=5), False),
        (NOW - timedelta(minutes=3), NOW - timedelta(minutes=2), True),
    ],
    ids=["older-than-commit", "newer-than-commit", "older-than-dirty", "newer-than-dirty"],
)
def test_note_is_current_only_while_newer_than_the_last_commit_and_dirty_change(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    dirty_at: datetime | None,
    note_at: datetime,
    current: bool,
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    expected_change = LAST_COMMIT
    if dirty_at is not None:
        scratch = repo / "scratch.txt"
        scratch.write_text("scratch\n")
        _stamp(scratch, dirty_at)
        expected_change = dirty_at
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    WorktreeNoteStore(temp_db).upsert(
        tree.id, owner="dev-1", next_action="merge", blocker="", updated_at=note_at
    )

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.note is not None
    assert row.note.updated_at == note_at
    assert row.change_at == expected_change
    assert (row.current_note is not None) is current


def test_deleted_tracked_file_counts_as_a_change_where_it_left(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    (repo / "keep.txt").unlink()
    removed_at = NOW - timedelta(minutes=3)
    _stamp(repo, removed_at)
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    WorktreeNoteStore(temp_db).upsert(
        tree.id,
        owner="dev-1",
        next_action="restore keep.txt",
        blocker="",
        updated_at=NOW - timedelta(minutes=5),
    )

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.change_at == removed_at
    assert row.current_note is None


def test_renamed_file_counts_once_and_its_old_path_is_scanned(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    _git(repo, "mv", "keep.txt", "moved.txt")
    moved_at = NOW - timedelta(minutes=2)
    _stamp(repo / "moved.txt", moved_at)
    _stamp(repo, NOW - timedelta(minutes=3))
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    WorktreeNoteStore(temp_db).upsert(
        tree.id,
        owner="dev-1",
        next_action="review the rename",
        blocker="",
        updated_at=NOW - timedelta(minutes=5),
    )

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.dirty == 1
    assert row.change_at == moved_at
    assert row.current_note is None

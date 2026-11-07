"""Collector: a worktree note is current only while newer than the tree's last change.

A tree's last change is its last commit or the latest metadata change of any dirty path, so a
mode change, a retargeted link, a copy that keeps an old mtime, or a staged removal all count.
Metadata times are real clock readings, so each scenario anchors its note to the moment the
change happened instead of a fixed fake date.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.status_report.collect import collect_status
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktree_notes import WorktreeNoteStore
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.integration

LAST_COMMIT = datetime(2026, 11, 5, 20, 18, tzinfo=UTC)
OLD = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
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
    keep = path / "keep.txt"
    keep.write_text("keep\n")
    os.utime(keep, (OLD.timestamp(), OLD.timestamp()))
    os.symlink("gone.txt", path / "link")  # committed dangling link
    _git(path, "add", "keep.txt", "link")
    _git(path, "commit", "-q", "-m", "keep", when=LAST_COMMIT)
    return path


def _collect_row(db: HubDatabase, project_id: str, project_path: Path) -> Any:
    worktrees = LocalWorktreeManager(db).list_worktrees(project_id=project_id)
    data = collect_status(
        db,
        project_id=project_id,
        project_path=project_path,
        integration_branch=BRANCH,
        now=datetime.now(UTC),
        window=timedelta(minutes=30),
        load_threshold=60.0,
        runs=(),
        worktrees=tuple(worktrees),
        loadavg=lambda: (0.0, 0.0, 0.0),
    )
    (row,) = data.worktrees
    return row


def _note(db: HubDatabase, tree_id: str, updated_at: datetime) -> None:
    WorktreeNoteStore(db).upsert(
        tree_id, owner="dev-1", next_action="merge", blocker="", updated_at=updated_at
    )


def _chmod(repo: Path) -> None:
    (repo / "keep.txt").chmod(0o755)


def _retarget_link(repo: Path) -> None:
    (repo / "link").unlink()
    os.symlink("keep.txt", repo / "link")


def _copy_keeping_old_mtime(repo: Path) -> None:
    shutil.copy2(repo / "keep.txt", repo / "copy.txt")


def _staged_removal(repo: Path) -> None:
    _git(repo, "rm", "--cached", "-q", "keep.txt")


def _delete_tracked(repo: Path) -> None:
    (repo / "keep.txt").unlink()


def _rename(repo: Path) -> None:
    _git(repo, "mv", "keep.txt", "moved.txt")


CHANGES: dict[str, Callable[[Path], None]] = {
    "mode-change": _chmod,
    "retargeted-link": _retarget_link,
    "copy-keeps-old-mtime": _copy_keeping_old_mtime,
    "staged-removal": _staged_removal,
    "deleted-tracked": _delete_tracked,
    "rename": _rename,
}


@pytest.mark.parametrize("change", CHANGES.values(), ids=CHANGES.keys())
def test_note_written_before_a_change_is_unreconciled(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    change: Callable[[Path], None],
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    note_at = datetime.now(UTC)
    change(repo)
    _note(temp_db, tree.id, note_at)

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.note is not None
    assert row.change_at is not None and row.change_at > note_at, row.change_at
    assert row.current_note is None


@pytest.mark.parametrize("change", CHANGES.values(), ids=CHANGES.keys())
def test_note_written_after_a_change_is_current(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    change: Callable[[Path], None],
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    change(repo)
    note_at = datetime.now(UTC)
    _note(temp_db, tree.id, note_at)

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.change_at is not None and row.change_at <= note_at, row.change_at
    assert row.current_note is not None


def test_collecting_status_does_not_rewrite_the_index_into_a_stale_note(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    # Stale stat data makes `git status` want to refresh and rewrite the index.
    os.utime(repo / "keep.txt", None)
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    _note(temp_db, tree.id, datetime.now(UTC))

    first = _collect_row(temp_db, project_id, tmp_path)
    second = _collect_row(temp_db, project_id, tmp_path)

    assert first.current_note is not None, first.change_at
    assert second.current_note is not None, second.change_at


@pytest.mark.parametrize(
    ("note_at", "current"),
    [(LAST_COMMIT - timedelta(minutes=1), False), (datetime.now(UTC), True)],
    ids=["older-than-commit", "newer-than-commit"],
)
def test_clean_tree_compares_the_note_with_the_last_commit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    note_at: datetime,
    current: bool,
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    _note(temp_db, tree.id, note_at)

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.change_at == LAST_COMMIT
    assert (row.current_note is not None) is current


def test_renamed_file_counts_once_and_the_rename_is_a_change(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _checkout(tmp_path / "wt")
    tree = LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    _note(temp_db, tree.id, datetime.now(UTC) - timedelta(days=1))
    _rename(repo)

    row = _collect_row(temp_db, project_id, tmp_path)

    assert row.dirty == 1
    assert row.current_note is None

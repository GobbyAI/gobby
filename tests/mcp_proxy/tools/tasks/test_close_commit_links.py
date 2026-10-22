"""Close-time commit links: one canonical SHA, and only the task's own commits (#23251)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, timeout=10, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(
        repo,
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


class _Leaf:
    def __init__(self, temp_db: HubDatabase, project: dict[str, Any]) -> None:
        self.manager = LocalTaskManager(temp_db)
        self.repo = Path(project["repo_path"])
        self.project_name = str(project["name"])
        self.project_id = str(project["id"])
        self.task = self.create("Linked leaf")

    def create(self, title: str) -> Task:
        return self.manager.create_task(
            project_id=self.project_id,
            title=title,
            validation_criteria="Close links only the task's own commits.",
        )

    def tag(self, task: Task) -> str:
        return f"[{self.project_name}-#{task.seq_num}]"

    def short(self, sha: str) -> str:
        return _git(self.repo, "rev-parse", "--short", sha)

    def fresh(self) -> Task:
        return self.manager.get_task(self.task.id)


@pytest.fixture
def leaf(temp_db: HubDatabase, sample_git_project: dict[str, Any]) -> _Leaf:
    return _Leaf(temp_db, sample_git_project)


def test_close_task_with_commit_links_the_canonical_short_sha(leaf: _Leaf) -> None:
    # The REST close route's shared path stored the caller's SHA verbatim.
    full = _commit(leaf.repo, f"{leaf.tag(leaf.task)} fix: work")

    closed = leaf.manager.close_task_with_commit(leaf.task.id, full, cwd=leaf.repo)

    assert closed.commits == [leaf.short(full)]


def test_close_task_with_commit_refuses_an_unresolved_sha_before_linking(leaf: _Leaf) -> None:
    with pytest.raises(ValueError, match="Invalid or unresolved commit SHA"):
        leaf.manager.close_task_with_commit(leaf.task.id, "f" * 40, cwd=leaf.repo)

    assert not leaf.fresh().commits

"""Close-time commit links: one canonical SHA, and only the task's own commits (#23251)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import (
    link_close_commit_shas,
    resolve_close_commit_shas,
)
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

    async def resolve(self, commit_sha: str) -> tuple[list[str], dict[str, Any] | None]:
        return await resolve_close_commit_shas(
            self.manager,
            task=self.fresh(),
            task_id=self.task.id,
            claim_started_at=None,
            commit_sha=commit_sha,
            cwd=str(self.repo),
            project_name=self.project_name,
        )


@pytest.fixture
def leaf(temp_db: HubDatabase, sample_git_project: dict[str, Any]) -> _Leaf:
    return _Leaf(temp_db, sample_git_project)


@pytest.mark.parametrize("prelinked", [False, True], ids=["unlinked", "short-prelinked"])
async def test_full_close_sha_is_linked_once_in_canonical_short_form(
    leaf: _Leaf, prelinked: bool
) -> None:
    tip = _commit(leaf.repo, f"{leaf.tag(leaf.task)} fix: the work")
    if prelinked:
        leaf.manager.link_commit(leaf.task.id, leaf.short(tip))

    shas, error = await leaf.resolve(tip)
    assert error is None
    linked, link_error = link_close_commit_shas(
        leaf.manager, task=leaf.fresh(), commit_shas=shas, cwd=str(leaf.repo)
    )

    assert link_error is None
    assert linked.commits == [leaf.short(tip)]


async def test_tagged_scan_does_not_duplicate_a_full_sha_link(
    leaf: _Leaf, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = _commit(leaf.repo, f"{leaf.tag(leaf.task)} fix: the work")
    leaf.manager.link_commit(leaf.task.id, tip)

    async def tagged(*_args: object, **_kwargs: object) -> list[str]:
        return [leaf.short(tip)]

    monkeypatch.setattr("gobby.tasks.commits.resolve_task_tagged_commits_async", tagged)
    shas, error = await resolve_close_commit_shas(
        leaf.manager,
        task=leaf.fresh(),
        task_id=leaf.task.id,
        claim_started_at="2026-01-01T00:00:00+00:00",
        commit_sha=None,
        cwd=str(leaf.repo),
        project_name=leaf.project_name,
    )

    assert error is None
    assert shas == [tip]


@pytest.mark.parametrize("foreign_tag", [True, False], ids=["package-landing", "untagged"])
async def test_close_refuses_a_commit_not_tagged_for_the_task_before_linking(
    leaf: _Leaf, foreign_tag: bool
) -> None:
    package = leaf.create("Package landing")
    subject = f"{leaf.tag(package)} chore: land package" if foreign_tag else "chore: untagged"
    landing = _commit(leaf.repo, subject)

    shas, error = await leaf.resolve(landing)

    assert error is not None
    assert error["error"] == "close_commit_not_task_tagged"
    assert landing in error["message"]
    assert (f"#{package.seq_num}" in error["message"]) is foreign_tag
    assert "link_commit" in error["message"]
    assert landing not in shas
    assert not leaf.fresh().commits


async def test_explicitly_linked_foreign_commit_may_be_the_close_candidate(leaf: _Leaf) -> None:
    package = leaf.create("Package landing")
    landing = _commit(leaf.repo, f"{leaf.tag(package)} chore: land package")
    leaf.manager.link_commit(leaf.task.id, leaf.short(landing))

    shas, error = await leaf.resolve(landing)

    assert error is None
    assert shas == [leaf.short(landing)]


async def test_a_sub_seven_character_link_does_not_admit_a_foreign_commit(leaf: _Leaf) -> None:
    package = leaf.create("Package landing")
    landing = _commit(leaf.repo, f"{leaf.tag(package)} chore: land package")
    leaf.manager.link_commit(leaf.task.id, landing[:4])

    _shas, error = await leaf.resolve(landing)

    assert error is not None
    assert error["error"] == "close_commit_not_task_tagged"

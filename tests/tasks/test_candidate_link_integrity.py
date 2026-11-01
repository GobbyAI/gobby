"""Git integrity at the generic task commit-link boundary."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.daemon_git import GitFailed, GitResult, GitTimeout, daemon_git
from gobby.utils.git import normalize_commit_sha
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


@pytest.fixture
def author(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    return AgentSessions(temp_db, sample_project["id"]).add("maker")


@pytest.fixture
def task(temp_db: HubDatabase, sample_project: dict[str, Any], author: str) -> Task:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Candidate commit integrity")
    manager.claim_task(task.id, author)
    return manager.get_task(task.id)


@pytest.fixture
def candidate_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Real Git ancestry with checkout discovery and daemon Git isolated.

    The candidates sit on their own branch, unlanded, while the registered main checkout
    stays on main at the base commit.
    """

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Candidate regression")
    git("config", "user.email", "test@example.invalid")
    git("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "base")
    git("checkout", "-b", "candidate")
    commits = []
    for index in range(3):
        git("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", f"step {index}")
        commits.append(git("rev-parse", "HEAD"))
    git("checkout", "main")
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.task_commits.resolve_task_repo_path",
        lambda **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.task_commits.normalize_commit_sha",
        AsyncMock(side_effect=normalize_commit_sha),
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._task_scope.collect_net_commit_paths_async",
        AsyncMock(return_value=SimpleNamespace(changed=frozenset(), deleted=frozenset())),
    )
    return commits


@pytest.mark.parametrize("stored_short", [False, True])
@pytest.mark.parametrize("requested_short", [False, True])
async def test_declared_candidate_relink_normalizes_and_preserves_order(
    temp_db: HubDatabase,
    task: Task,
    author: str,
    candidate_history: list[str],
    stored_short: bool,
    requested_short: bool,
) -> None:
    manager = LocalTaskManager(temp_db)
    old, middle, candidate = candidate_history
    stored = candidate[:10] if stored_short else candidate
    for sha in (stored, old, middle):
        manager.link_commit(task.id, sha)
    registry = create_commit_registry(manager)
    arguments = {
        "task_id": task.id,
        "commit_sha": candidate[:7] if requested_short else candidate,
    }
    with session_context_for_test(author):
        ordinary = await registry.call("link_commit", arguments)
        assert ordinary["commits"] == [stored, old, middle]
        assert ordinary["declared_candidate"] is None
        declared = await registry.call("link_commit", {**arguments, "candidate": True})
    assert "error" not in declared, declared
    assert declared["commits"] == [old, middle, stored]
    assert candidate.startswith(declared["declared_candidate"])
    assert manager.get_task(task.id).commits == [old, middle, stored]
    with session_context_for_test(author):
        repeated = await registry.call("link_commit", {**arguments, "candidate": True})
    assert repeated == declared


async def test_relinking_a_landed_commit_declares_no_candidate(
    temp_db: HubDatabase,
    task: Task,
    author: str,
    candidate_history: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Landed history cannot be declared as an unlanded candidate."""
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(return_value=(tmp_path, "main")),
    )
    subprocess.check_output(["git", "merge", "--ff-only", "candidate"], cwd=tmp_path)
    manager = LocalTaskManager(temp_db)
    landed = candidate_history[-1]
    manager.link_commit(task.id, landed)
    registry = create_commit_registry(manager)
    with session_context_for_test(author):
        relink = await registry.call(
            "link_commit", {"task_id": task.id, "commit_sha": landed, "candidate": True}
        )
    assert "error" not in relink, relink
    assert relink["declared_candidate"] is None
    assert manager.get_task(task.id).commits == [landed]


@pytest.mark.parametrize("unprovable", ["git_failed", "timeout", "detached_target"])
async def test_unprovable_landing_status_refuses_the_candidate(
    temp_db: HubDatabase,
    task: Task,
    author: str,
    candidate_history: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unprovable: str,
) -> None:
    """A candidate whose landing status is unproven is refused before anything persists."""
    manager = LocalTaskManager(temp_db)
    target = None if unprovable == "detached_target" else (tmp_path, "main")
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(return_value=target),
    )
    failure: GitResult = (
        GitTimeout("timeout", ("git", "merge-base"), 10.0)
        if unprovable == "timeout"
        else GitFailed("failed", ("git", "merge-base"), 128, "", "fatal: bad object")
    )
    real_run = daemon_git.run

    async def run(args: Sequence[str], **kwargs: Any) -> GitResult:
        return failure if args[0] == "merge-base" else await real_run(args, **kwargs)

    monkeypatch.setattr(daemon_git, "run", run)
    before = manager.get_task(task.id).commits
    registry = create_commit_registry(manager)
    candidate = candidate_history[-1]
    with session_context_for_test(author):
        relink = await registry.call(
            "link_commit", {"task_id": task.id, "commit_sha": candidate, "candidate": True}
        )
    assert "integration branch" in relink.get("error", ""), relink
    assert "declared_candidate" not in relink
    assert manager.get_task(task.id).commits == before


async def test_declared_candidate_cannot_demote_a_newer_link(
    temp_db: HubDatabase, task: Task, author: str, candidate_history: list[str]
) -> None:
    manager = LocalTaskManager(temp_db)
    old, middle, newest = candidate_history
    for sha in (old, newest):
        manager.link_commit(task.id, sha)
    registry = create_commit_registry(manager)
    with session_context_for_test(author):
        for candidate in (old, middle):
            refused = await registry.call(
                "link_commit", {"task_id": task.id, "commit_sha": candidate, "candidate": True}
            )
            assert "must descend from every other linked commit" in refused.get("error", "")
            assert "declared_candidate" not in refused
            assert manager.get_task(task.id).commits == [old, newest]

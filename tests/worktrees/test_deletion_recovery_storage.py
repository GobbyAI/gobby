"""A recovery refusal against real storage names the row, its tree and the reason (#25838).

The recovery pass runs each deleting record through the cleanup guard. An open task or a
claimed record makes the guard refuse, and that refusal must leave a log line naming the row.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.recovery import recover_deleting_worktrees
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.integration

MACHINE_ID = "21000000-0000-4000-8000-000000000002"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, MACHINE_ID)


@pytest.fixture
def project_id(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    isolated = install_isolated_checkout_project(
        temp_db,
        tmp_path / "isolated-checkout",
        machine_id=MACHINE_ID,
        monkeypatch=monkeypatch,
    )
    return str(isolated.project.to_dict()["id"])


async def test_recovery_refused_by_an_open_claimed_row_logs_its_id_path_and_reason(
    temp_db: HubDatabase,
    project_id: str,
    session_manager: SessionManager,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "task-branch", str(worktree), "main")

    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=project_id,
        title="Open task owning the tree",
        validation_criteria="The tree stays registered.",
    )
    session = session_manager.register(
        external_id=f"recovery-guard-{uuid.uuid4()}",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    manager = LocalWorktreeManager(temp_db)
    row = manager.create(
        project_id=project_id,
        branch_name="task-branch",
        worktree_path=str(worktree),
        task_id=task.id,
    )
    assert manager.claim_if_available(row.id, session.id) is not None
    manager.update(row.id, status="deleting")

    with caplog.at_level(logging.WARNING):
        results = await recover_deleting_worktrees(
            worktree_storage=manager,
            resolve_git_manager=lambda _row: WorktreeGitManager(repo),
            task_manager=tasks,
            executor=None,
            should_stop=lambda: False,
        )

    assert [result.error_code for result in results] == ["worktree_not_eligible"]
    assert worktree.exists()
    stored = manager.get(row.id)
    assert stored is not None
    assert stored.status == "deleting"
    assert stored.agent_session_id == session.id
    assert row.id in caplog.text
    assert str(worktree) in caplog.text
    assert "worktree_not_eligible" in caplog.text

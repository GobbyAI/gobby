"""unlink_commit refuses commits the integration branch already holds and audits every removal."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks import commit_unlink
from gobby.tasks.land_commit import LandingGitError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def integration_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "module.py").write_text("VALUE = 1\n")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "landed")
    landed = _git(repo, "rev-parse", "--short", "HEAD")
    _git(repo, "checkout", "-b", "candidate")
    (repo / "module.py").write_text("VALUE = 2\n")
    _git(repo, "commit", "-am", "candidate")
    unlanded = _git(repo, "rev-parse", "--short", "HEAD")
    _git(repo, "checkout", "main")
    return repo, landed, unlanded


@pytest.mark.asyncio
async def test_landed_commit_is_refused_and_unlanded_candidate_is_allowed(
    integration_repo: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, landed, unlanded = integration_repo
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(return_value=(repo, "main")),
    )
    task = cast(Task, SimpleNamespace(project_id="project-1"))

    refusal = await commit_unlink.integration_refusal(MagicMock(), task, landed)
    assert refusal is not None
    assert "landed commits cannot be unlinked" in refusal
    assert await commit_unlink.integration_refusal(MagicMock(), task, unlanded) is None


@pytest.mark.asyncio
async def test_missing_integration_checkout_leaves_nothing_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(side_effect=ValueError("no registered checkout")),
    )
    task = cast(Task, SimpleNamespace(project_id="project-1"))

    assert await commit_unlink.integration_refusal(MagicMock(), task, "abc1234") is None


@pytest.mark.asyncio
async def test_failed_integration_lookup_refuses_unlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(side_effect=LandingGitError("git timed out")),
    )
    task = cast(Task, SimpleNamespace(project_id="project-1"))

    refusal = await commit_unlink.integration_refusal(MagicMock(), task, "abc1234")
    assert refusal is not None
    assert "Cannot determine the integration branch" in refusal


@pytest.mark.asyncio
async def test_unlink_removes_an_unlanded_candidate_and_audits_the_reason(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    integration_repo: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, landed, unlanded = integration_repo
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(return_value=(repo, "main")),
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Superseded candidate", category="research", validation_criteria="x"
    )
    manager.link_commit(task.id, landed)
    manager.link_commit(task.id, unlanded)

    with pytest.raises(commit_unlink.CommitUnlinkRefused, match="landed commits cannot"):
        await commit_unlink.unlink_commit_audited(
            manager, manager.get_task(task.id), landed, reason="Superseded", actor="session-1"
        )
    updated = await commit_unlink.unlink_commit_audited(
        manager,
        manager.get_task(task.id),
        unlanded,
        reason="Superseded by #24970",
        actor="session-1",
    )

    assert updated.commits == [landed]
    events = TaskLifecycleEventManager(temp_db).list_lifecycle_events(task.id)
    assert [(event.to_state, event.by_actor, event.reason) for event in events] == [
        ("commit:unlinked", "session-1", f"Unlinked {unlanded}: Superseded by #24970")
    ]


@pytest.mark.asyncio
async def test_unlink_refuses_a_blank_reason_before_any_mutation(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target", AsyncMock(return_value=None)
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Superseded candidate", category="research", validation_criteria="x"
    )
    manager.link_commit(task.id, "abc1234")

    with pytest.raises(ValueError, match="A reason is required"):
        await commit_unlink.unlink_commit_audited(
            manager, manager.get_task(task.id), "abc1234", reason="  ", actor="session-1"
        )
    assert manager.get_task(task.id).commits == ["abc1234"]
    assert TaskLifecycleEventManager(temp_db).list_lifecycle_events(task.id) == []


@pytest.mark.asyncio
async def test_failed_audit_write_keeps_every_link_and_writes_no_event(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    integration_repo: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, landed, unlanded = integration_repo
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(return_value=(repo, "main")),
    )

    class _FailingAudit:
        def __init__(self, db: HubDatabase) -> None:
            self.db = db

        def record_lifecycle_event(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("audit insert failed")

    monkeypatch.setattr(commit_unlink, "TaskLifecycleEventManager", _FailingAudit)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Superseded candidate", category="research", validation_criteria="x"
    )
    manager.link_commit(task.id, landed)
    manager.link_commit(task.id, unlanded)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        await commit_unlink.unlink_commit_audited(
            manager,
            manager.get_task(task.id),
            unlanded,
            reason="Superseded by #24970",
            actor="session-1",
        )

    assert manager.get_task(task.id).commits == [landed, unlanded]
    assert TaskLifecycleEventManager(temp_db).list_lifecycle_events(task.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head_command",
    [("checkout", "--detach"), ("symbolic-ref", "HEAD", "refs/remotes/origin/main")],
    ids=["detached", "non-branch-symref"],
)
async def test_main_checkout_without_a_branch_refuses_the_unlink_of_a_landed_commit(
    head_command: tuple[str, ...],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    integration_repo: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, landed, unlanded = integration_repo
    _git(repo, *head_command)
    monkeypatch.setattr(
        "gobby.releases.window.require_root", lambda db, project_id, machine_id: str(repo)
    )
    monkeypatch.setattr("gobby.releases.window.require_machine_id", lambda: "machine-1")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Superseded candidate", category="research", validation_criteria="x"
    )
    manager.link_commit(task.id, landed)
    manager.link_commit(task.id, unlanded)

    with pytest.raises(commit_unlink.CommitUnlinkRefused, match="not on a branch"):
        await commit_unlink.unlink_commit_audited(
            manager, manager.get_task(task.id), landed, reason="Superseded", actor="session-1"
        )

    assert manager.get_task(task.id).commits == [landed, unlanded]
    assert TaskLifecycleEventManager(temp_db).list_lifecycle_events(task.id) == []

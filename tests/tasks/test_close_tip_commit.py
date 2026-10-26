"""A close that names a linked commit binds the tip-bound steps by history, not link order."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo: Path, name: str) -> str:
    (repo / f"{name}.txt").write_text(f"{name}\n")
    _git(repo, "add", f"{name}.txt")
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


class _Close:
    """One enforced-policy task in a real repository, closed through the close_task tool."""

    def __init__(
        self, db: HubDatabase, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.mcp_proxy.tools.internal import InternalToolRegistry
        from gobby.mcp_proxy.tools.tasks._context import RegistryContext
        from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
        from tests.tasks.test_close_invariants import _set_close_policy
        from tests.tasks.test_step_records import _Seats

        self.db = db
        self.repo = Path(project["repo_path"])
        self.manager = LocalTaskManager(db)
        _set_close_policy(db, project["id"], {"enforce_step_records": True})
        seats = _Seats(db, project["id"])
        self.developer = seats.add("tip-developer", ["implementation"])
        self.reviewer = seats.add("tip-reviewer", ["review"])
        self.closer = seats.add("tip-closer", ["closing"])
        task = self.manager.create_task(
            project["id"],
            "Tip by history",
            category="code",
            validation_criteria="The reviewed descendant closes the task.",
        )
        self.task = self.manager.claim_task(task.id, self.developer)
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id",
            lambda: self.developer,
        )
        self.registry = InternalToolRegistry("gobby-tasks")
        register_close_task(self.registry, RegistryContext(task_manager=self.manager))

    def short(self, sha: str) -> str:
        return _git(self.repo, "rev-parse", "--short", sha)

    def link(self, sha: str) -> None:
        self.manager.link_commit(self.task.id, self.short(sha))

    def approve_tip(self, sha: str) -> None:
        from gobby.tasks.step_records import StepSubmission, TaskStep, record_task_step

        steps: list[tuple[str, TaskStep]] = [
            (self.developer, "implementation"),
            (self.reviewer, "review"),
            (self.closer, "closing"),
        ]
        for session_id, step in steps:
            record_task_step(
                self.db,
                task_id=self.task.id,
                session_id=session_id,
                step=step,
                submission=StepSubmission(
                    status="approved",
                    epoch=1,
                    round_number=1,
                    feedback=f"The {step} step is approved.",
                    commit_sha=sha,
                ),
            )

    async def close(self, sha: str, *, preview: bool = False) -> dict[str, Any]:
        arguments: dict[str, Any] = {"task_id": self.task.id, "commit_sha": sha}
        if preview:
            arguments["preview"] = True
        result = await self.registry.call("close_task", arguments)
        assert isinstance(result, dict)
        return result

    def fresh(self) -> Task:
        return self.manager.get_task(self.task.id)


@pytest.fixture
def close(
    temp_db: HubDatabase, sample_git_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> _Close:
    return _Close(temp_db, sample_git_project, monkeypatch)


def _failed_gates(response: dict[str, Any]) -> list[str]:
    return [gate["name"] for gate in response.get("gates", []) if gate["status"] == "failed"]


@pytest.mark.asyncio
async def test_reviewed_descendant_closes_after_a_later_linked_ancestor(close: _Close) -> None:
    """A merge refresh linked after review does not displace the reviewed descendant (#25148)."""
    refresh = _commit(close.repo, "refresh")
    reviewed = _commit(close.repo, "reviewed")
    close.link(reviewed)
    close.approve_tip(reviewed)
    close.link(refresh)
    assert close.fresh().commits == [close.short(reviewed), close.short(refresh)]

    preview = await close.close(reviewed, preview=True)
    assert preview["can_close"] is True, (preview.get("error"), _failed_gates(preview))
    closed = await close.close(reviewed)

    assert closed["closed"] is True, (closed.get("error"), closed.get("message"))
    task = close.fresh()
    assert task.closed_commit_sha == reviewed
    assert task.commits == [close.short(refresh), close.short(reviewed)]


@pytest.mark.asyncio
async def test_older_commit_cannot_bypass_a_newer_linked_descendant(close: _Close) -> None:
    older = _commit(close.repo, "older")
    close.link(older)
    close.approve_tip(older)
    newer = _commit(close.repo, "newer")
    close.link(newer)

    refused = await close.close(older)

    assert refused.get("closed") is not True, refused
    assert "step_records" in _failed_gates(refused), refused
    task = close.fresh()
    assert task.closed_at is None
    assert task.commits == [close.short(older), close.short(newer)]


def test_persisted_close_keeps_link_order_for_an_older_commit(close: _Close) -> None:
    """The storage close moves a named commit to the tip only when it descends from the rest."""
    from tests.tasks.test_close_invariants import _set_close_policy

    _set_close_policy(close.db, close.task.project_id, {})
    older = _commit(close.repo, "older")
    close.link(older)
    newer = _commit(close.repo, "newer")
    close.link(newer)

    closed = close.manager.close_task_with_commit(close.task.id, older, cwd=close.repo)

    assert closed.closed_at is not None
    assert closed.commits == [close.short(older), close.short(newer)]

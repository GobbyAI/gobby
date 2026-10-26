"""unlink_commit refuses commits the integration branch already holds."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools import task_commits
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
    task = SimpleNamespace(project_id="project-1")

    refusal = await task_commits._integration_refusal(MagicMock(), task, landed)
    assert refusal is not None
    assert "landed commits cannot be unlinked" in refusal
    assert await task_commits._integration_refusal(MagicMock(), task, unlanded) is None


@pytest.mark.asyncio
async def test_missing_integration_checkout_leaves_nothing_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(side_effect=ValueError("no registered checkout")),
    )
    task = SimpleNamespace(project_id="project-1")

    assert await task_commits._integration_refusal(MagicMock(), task, "abc1234") is None


@pytest.mark.asyncio
async def test_failed_integration_lookup_refuses_unlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.land_commit.main_checkout_target",
        AsyncMock(side_effect=LandingGitError("git timed out")),
    )
    task = SimpleNamespace(project_id="project-1")

    refusal = await task_commits._integration_refusal(MagicMock(), task, "abc1234")
    assert refusal is not None
    assert "Cannot determine the integration branch" in refusal

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from gobby.build import delivery
from gobby.build.delivery import (
    github_repo_from_url,
    normalize_github_repo,
    resolve_project_source_repo_async,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.projects import LocalProjectManager
from gobby.utils.daemon_git import GitFailed, GitOk, GitTimeout, daemon_git
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("repo", ["owner/repo.git", "owner/.", "owner/..", "../repo"])
def test_normalize_github_repo_rejects_unsafe_owner_repo_values(repo: str) -> None:
    with pytest.raises(ValueError, match="expected 'owner/repo'"):
        normalize_github_repo(repo)


def test_github_repo_from_url_strips_remote_dot_git_suffix() -> None:
    assert github_repo_from_url("https://github.com/owner/repo.git") == "owner/repo"
    assert github_repo_from_url("git@github.com:owner/repo.git") == "owner/repo"


@pytest.mark.asyncio
async def test_resolve_project_source_repo_uses_checkout_origin(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "delivery-origin"
    isolated = install_isolated_checkout_project(
        temp_db,
        repo,
        name="delivery-origin",
        github_url=None,
        monkeypatch=monkeypatch,
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/from-origin.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert (
        await resolve_project_source_repo_async(temp_db, isolated.project.id) == "owner/from-origin"
    )


@pytest.mark.asyncio
async def test_resolve_project_source_repo_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create("delivery-missing-checkout", github_url=None)

    with pytest.raises(CheckoutNotFoundError):
        await resolve_project_source_repo_async(temp_db, project.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,match",
    [
        pytest.param(
            GitTimeout("timeout", ("git", "remote"), 10),
            "timed out",
            id="timeout",
        ),
        pytest.param(
            GitFailed("failed", ("git", "remote"), None, "", "daemon unavailable"),
            "unavailable",
            id="unavailable",
        ),
    ],
)
async def test_git_remote_url_fails_closed_on_daemon_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: GitTimeout | GitFailed,
    match: str,
) -> None:
    async def run_git(*_args: object, **_kwargs: object) -> GitTimeout | GitFailed:
        return result

    monkeypatch.setattr(daemon_git, "run", run_git)

    with pytest.raises(RuntimeError, match=match):
        await delivery._git_remote_url(tmp_path)


@pytest.mark.asyncio
async def test_git_remote_url_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run_git(*_args: object, **_kwargs: object) -> GitOk:
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon_git, "run", run_git)

    with pytest.raises(asyncio.CancelledError):
        await delivery._git_remote_url(tmp_path)

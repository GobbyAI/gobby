from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.build.delivery import (
    github_repo_from_url,
    normalize_github_repo,
    resolve_project_source_repo,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.projects import LocalProjectManager
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


def test_resolve_project_source_repo_uses_checkout_origin(
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

    assert resolve_project_source_repo(temp_db, isolated.project.id) == "owner/from-origin"


def test_resolve_project_source_repo_fails_closed_without_checkout(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create("delivery-missing-checkout", github_url=None)

    with pytest.raises(CheckoutNotFoundError):
        resolve_project_source_repo(temp_db, project.id)

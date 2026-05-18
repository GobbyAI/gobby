from __future__ import annotations

import pytest

from gobby.build.delivery import github_repo_from_url, normalize_github_repo

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("repo", ["owner/repo.git", "owner/.", "owner/..", "../repo"])
def test_normalize_github_repo_rejects_unsafe_owner_repo_values(repo: str) -> None:
    with pytest.raises(ValueError, match="expected 'owner/repo'"):
        normalize_github_repo(repo)


def test_github_repo_from_url_strips_remote_dot_git_suffix() -> None:
    assert github_repo_from_url("https://github.com/owner/repo.git") == "owner/repo"
    assert github_repo_from_url("git@github.com:owner/repo.git") == "owner/repo"

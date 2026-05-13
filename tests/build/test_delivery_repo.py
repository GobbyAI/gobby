from __future__ import annotations

import pytest

from gobby.build.delivery import github_repo_from_url, normalize_github_repo

pytestmark = pytest.mark.unit


def test_normalize_github_repo_preserves_literal_dot_git_suffix() -> None:
    assert normalize_github_repo("owner/repo.git") == "owner/repo.git"


def test_github_repo_from_url_strips_remote_dot_git_suffix() -> None:
    assert github_repo_from_url("https://github.com/owner/repo.git") == "owner/repo"
    assert github_repo_from_url("git@github.com:owner/repo.git") == "owner/repo"

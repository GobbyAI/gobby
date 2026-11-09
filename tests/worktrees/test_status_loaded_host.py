"""A worktree's status is still read on a loaded host, where git takes longer than the old deadline."""

import subprocess
from pathlib import Path

from gobby.worktrees.git import WorktreeGitManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


async def test_status_survives_a_loaded_host(tmp_path: Path, loaded_host: None) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Status Test")
    git(repo, "commit", "--allow-empty", "-m", "initial")

    status = await WorktreeGitManager(repo).get_worktree_status(repo)

    assert status is not None, "status was unavailable: a git query hit its deadline"
    assert status.branch == "main"
    assert status.commit == git(repo, "rev-parse", "--short", "HEAD")

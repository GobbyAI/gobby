"""Tests for Git-backed gobby-wiki branch setup and publishing."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from gobby.cli.installers.git_hooks import HOOK_TEMPLATES, install_git_hooks
from gobby.cli.installers.wiki_branch_setup import (
    GITIGNORE_START,
    GOBBY_WIKI_DIR,
    WIKI_BRANCH,
    setup_wiki_branch,
)

pytestmark = pytest.mark.unit

ZERO_SHA = "0000000000000000000000000000000000000000"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _git_raw(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git_raw("init", cwd=repo)
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial", "--no-verify")


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "repo.git"
    repo = tmp_path / "repo"
    _git_raw("init", "--bare", str(bare), cwd=tmp_path)
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")
    return repo, bare


def _make_fake_gobby(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gobby = fake_bin / "gobby"
    gobby.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    gobby.chmod(gobby.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GOBBY_AGENT_RUN_ID": "test-agent",
    }


def _run_prepush_hook(
    repo: Path,
    bare_remote: Path,
    ref_line: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / ".git" / "hooks" / "pre-push"), "origin", str(bare_remote)],
        cwd=repo,
        input=f"{ref_line}\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _remote_has_branch(bare_remote: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-parse", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


class TestWikiBranchSetup:
    def test_gitignore_block_is_created_and_idempotent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        first = setup_wiki_branch(repo)
        second = setup_wiki_branch(repo)

        content = (repo / ".gitignore").read_text(encoding="utf-8")
        assert first["success"] is True
        assert first["gitignore_updated"] is True
        assert first["gitignore_status"] == "updated"
        assert second["success"] is True
        assert second["gitignore_updated"] is False
        assert second["gitignore_status"] == "unchanged"
        assert content.count(GITIGNORE_START) == 1
        assert f"{GOBBY_WIKI_DIR}/" in content

    def test_tracked_wiki_files_warn_and_remain_tracked(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        wiki_dir = repo / GOBBY_WIKI_DIR
        wiki_dir.mkdir()
        (wiki_dir / "page.md").write_text("# Page\n", encoding="utf-8")
        _git(repo, "add", f"{GOBBY_WIKI_DIR}/page.md")
        _git(repo, "commit", "-m", "track wiki", "--no-verify")

        result = setup_wiki_branch(repo)

        tracked_after = _git(repo, "ls-files", "--", GOBBY_WIKI_DIR).stdout.splitlines()
        assert result["success"] is True
        assert result["tracked_files"] == [f"{GOBBY_WIKI_DIR}/page.md"]
        assert f"{GOBBY_WIKI_DIR}/page.md" in tracked_after
        assert any("git rm --cached -r gobby-wiki" in warning for warning in result["warnings"])

    def test_orphan_wiki_worktree_is_created_for_new_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = setup_wiki_branch(repo)

        worktree_path = Path(result["worktree_path"])
        branch = _git(worktree_path, "branch", "--show-current").stdout.strip()
        assert result["success"] is True
        assert worktree_path.exists()
        assert result["branch"] == WIKI_BRANCH
        assert branch == WIKI_BRANCH

    def test_existing_wiki_branch_is_reused(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        seed_worktree = tmp_path / "seed-wiki"
        _git(repo, "worktree", "add", "--orphan", "-b", WIKI_BRANCH, str(seed_worktree))
        _git(seed_worktree, "commit", "--allow-empty", "-m", "seed wiki", "--no-verify")
        _git(repo, "worktree", "remove", str(seed_worktree))

        result = setup_wiki_branch(repo)

        worktree_path = Path(result["worktree_path"])
        branch = _git(worktree_path, "branch", "--show-current").stdout.strip()
        assert result["success"] is True
        assert worktree_path == tmp_path / "repo-wiki"
        assert branch == WIKI_BRANCH

    def test_conflicting_sibling_path_skips_setup_nonfatally(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        conflict = tmp_path / "repo-wiki"
        conflict.mkdir()
        (conflict / "README.md").write_text("not a worktree\n", encoding="utf-8")

        result = install_git_hooks(repo)

        assert result["success"] is True
        assert result["wiki_setup"]["success"] is False
        assert result["wiki_setup"]["worktree_path"] == str(conflict)
        assert any("already exists" in warning for warning in result["wiki_setup"]["warnings"])


class TestPrePushWikiPublishing:
    def test_template_captures_refs_and_gates_wiki_publish(self) -> None:
        content = HOOK_TEMPLATES["pre-push"]
        assert "PUSH_REFS=$(cat)" in content
        assert "PUBLISH_WIKI=false" in content
        assert 'if [ "$DELETE_ONLY" = true ] || [ "$WIKI_ONLY" = true ]; then' in content

    def test_publishes_wiki_for_default_branch(self, tmp_path: Path) -> None:
        repo, bare = _init_repo_with_remote(tmp_path)
        wiki_dir = repo / GOBBY_WIKI_DIR
        wiki_dir.mkdir()
        (wiki_dir / "Home.md").write_text("# Home\n", encoding="utf-8")
        result = install_git_hooks(repo)
        assert result["success"] is True

        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        proc = _run_prepush_hook(
            repo,
            bare,
            f"refs/heads/main {sha} refs/heads/main {sha}",
            _make_fake_gobby(tmp_path),
        )

        assert proc.returncode == 0, proc.stderr
        assert _remote_has_branch(bare, WIKI_BRANCH)

    @pytest.mark.parametrize(
        ("local_ref", "local_sha", "remote_ref"),
        [
            pytest.param("refs/heads/feature", "HEAD", "refs/heads/feature", id="feature"),
            pytest.param("refs/heads/main", ZERO_SHA, "refs/heads/main", id="delete"),
            pytest.param("refs/heads/wiki", "wiki", "refs/heads/wiki", id="wiki"),
        ],
    )
    def test_skips_wiki_publish_for_non_default_refs(
        self,
        tmp_path: Path,
        local_ref: str,
        local_sha: str,
        remote_ref: str,
    ) -> None:
        repo, bare = _init_repo_with_remote(tmp_path)
        wiki_dir = repo / GOBBY_WIKI_DIR
        wiki_dir.mkdir()
        (wiki_dir / "Home.md").write_text("# Home\n", encoding="utf-8")
        result = install_git_hooks(repo)
        assert result["success"] is True

        if local_sha != ZERO_SHA:
            local_sha = _git(repo, "rev-parse", local_sha).stdout.strip()
        proc = _run_prepush_hook(
            repo,
            bare,
            f"{local_ref} {local_sha} {remote_ref} {ZERO_SHA}",
            _make_fake_gobby(tmp_path),
        )

        assert proc.returncode == 0, proc.stderr
        assert not _remote_has_branch(bare, WIKI_BRANCH)

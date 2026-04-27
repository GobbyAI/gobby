"""Tests for worktree-diff evidence resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.plans.evidence import EvidenceResolveStatus, resolve_evidence

pytestmark = pytest.mark.unit


@dataclass
class EvidenceContext:
    repo_root: Path
    artifacts: dict[str, Any] | None

    def get_task_diff(self, task_ref: str) -> str:
        raise AssertionError(f"unexpected task diff lookup: {task_ref}")

    def get_artifacts(self, task_ref: str) -> dict[str, Any] | None:
        assert task_ref == "#13252"
        return self.artifacts

    def get_commit_range_diff(self, range_: str) -> str:
        raise AssertionError(f"unexpected commit range lookup: {range_}")


def test_resolves_with_base_sha(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    base = _commit_file(repo, "base.txt", "base\n", "base")
    _commit_file(repo, "changed.txt", "changed\n", "changed")
    ctx = EvidenceContext(
        repo_root=tmp_path,
        artifacts={"worktree_path": str(repo), "base_commit_sha": base},
    )

    bundle = resolve_evidence("worktree-diff:#13252", ctx=ctx)

    assert bundle.rows[0].status is EvidenceResolveStatus.resolved
    assert bundle.rows[0].artifacts_touched == ("changed.txt",)
    assert "target_branch" not in bundle.rows[0].detail


def test_invalid_when_artifacts_missing(tmp_path: Path) -> None:
    bundle = resolve_evidence("worktree-diff:#13252", ctx=EvidenceContext(tmp_path, None))

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert bundle.rows[0].detail == "no artifacts row for #13252"


def test_invalid_when_no_isolation_path(tmp_path: Path) -> None:
    bundle = resolve_evidence("worktree-diff:#13252", ctx=EvidenceContext(tmp_path, {}))

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert bundle.rows[0].detail == "no isolation path on artifacts row for #13252"


def test_invalid_when_base_sha_null(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    bundle = resolve_evidence(
        "worktree-diff:#13252",
        ctx=EvidenceContext(tmp_path, {"worktree_path": str(repo), "base_commit_sha": None}),
    )

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert "missing base_commit_sha; rerun gobby build" in bundle.rows[0].detail


def test_invalid_when_base_sha_unresolvable(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    _commit_file(repo, "base.txt", "base\n", "base")

    bundle = resolve_evidence(
        "worktree-diff:#13252",
        ctx=EvidenceContext(
            tmp_path,
            {"worktree_path": str(repo), "base_commit_sha": "does-not-exist"},
        ),
    )

    assert bundle.rows[0].status is EvidenceResolveStatus.invalid
    assert f"base_commit_sha does-not-exist does not resolve in {repo}" == bundle.rows[0].detail


def test_picks_worktree_over_clone_when_both_present(tmp_path: Path) -> None:
    worktree = _git_repo(tmp_path / "worktree")
    clone = _git_repo(tmp_path / "clone")
    base = _commit_file(worktree, "base.txt", "base\n", "base")
    _commit_file(worktree, "worktree.txt", "worktree\n", "worktree")
    _commit_file(clone, "clone.txt", "clone\n", "clone")

    bundle = resolve_evidence(
        "worktree-diff:#13252",
        ctx=EvidenceContext(
            tmp_path,
            {
                "worktree_path": str(worktree),
                "clone_path": str(clone),
                "base_commit_sha": base,
            },
        ),
    )

    assert bundle.rows[0].status is EvidenceResolveStatus.resolved
    assert bundle.rows[0].artifacts_touched == ("worktree.txt",)


def _git_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "a6@example.test")
    _git(path, "config", "user.name", "A6")
    return path


def _commit_file(repo: Path, rel_path: str, content: str, message: str) -> str:
    path = repo / rel_path
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()

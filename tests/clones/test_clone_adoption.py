"""Regression tests for inspecting and containing unmanaged clones."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.clones import git as clone_git
from gobby.clones.git import CloneGitManager

pytestmark = pytest.mark.integration


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_source_repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("source\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_resolve_managed_clone_path_canonicalizes_and_contains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clones_root = tmp_path / "clones"
    monkeypatch.setattr(clone_git, "CLONES_ROOT", clones_root)
    manager = CloneGitManager(tmp_path)

    expected = clones_root / "project" / "clone"

    assert manager.resolve_managed_clone_path(expected / ".." / "clone") == expected
    assert manager.resolve_managed_clone_path(clones_root) is None
    assert manager.resolve_managed_clone_path(tmp_path / "outside") is None


@pytest.mark.parametrize(("detached", "expected_branch"), [(False, "main"), (True, None)])
def test_inspects_actual_branch_commit_and_origin_from_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detached: bool,
    expected_branch: str | None,
) -> None:
    source = tmp_path / "source"
    clones_root = tmp_path / "clones"
    clone_path = clones_root / "project" / "clone"
    _create_source_repository(source)
    clone_path.parent.mkdir(parents=True)
    _git(tmp_path, "clone", str(source), str(clone_path))
    if detached:
        _git(clone_path, "checkout", "--detach", "HEAD")

    monkeypatch.setattr(clone_git, "CLONES_ROOT", clones_root)
    manager = CloneGitManager(source)

    status = manager.get_clone_status(clone_path)

    assert status is not None
    assert status.branch == expected_branch
    assert status.commit == _git(clone_path, "rev-parse", "--short", "HEAD")
    assert manager.get_remote_url(cwd=clone_path) == str(source)

"""Regression tests for build workspace git subprocess handling."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from gobby.build.workspace_git import _git

pytestmark = pytest.mark.unit


def test_workspace_git_resolves_git_from_fallback_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fallback_bin = tmp_path / "fallback-bin"
    fallback_bin.mkdir()
    fake_git = fallback_bin / "git"
    fake_git.write_text("#!/bin/sh\nprintf 'workspace-fallback\\n'\n", encoding="utf-8")
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr("gobby.utils.git.GIT_FALLBACK_PATHS", (str(fallback_bin),))
    monkeypatch.setenv("PATH", "")

    result = _git(tmp_path, ["status", "--porcelain"], timeout=5)

    assert result.returncode == 0
    assert result.stdout == "workspace-fallback\n"

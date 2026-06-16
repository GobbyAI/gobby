"""Regression tests for lifecycle validation file-reference resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_validation import _resolve_referenced_files

pytestmark = pytest.mark.unit


def test_resolve_referenced_files_rejects_paths_outside_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    safe_file = repo / "safe.py"
    safe_file.write_text("print('safe')\n", encoding="utf-8")
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("print('secret')\n", encoding="utf-8")

    monkeypatch.setattr("gobby.utils.git.run_git_command", lambda *_args, **_kwargs: "safe.py\n")

    resolved = _resolve_referenced_files(
        mentioned_files=[str(outside_file), "../outside.py", "safe.py"],
        changed_files=[],
        repo_path=str(repo),
        max_files=10,
    )

    assert resolved == [safe_file.resolve()]

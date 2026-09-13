"""Guard behavior for session workspaces that can no longer answer git questions.

`_missing_workspace_git_context` is the single gate that decides whether session
summarization may shell out to git for a workspace. When it wrongly reports a
workspace as usable, `get_file_changes_async` runs `git diff HEAD` outside a
repository, git silently falls back to `--no-index`, and the resulting usage dump
becomes a fatal `GitStatusUnavailable` that kills the whole summary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.sessions.workspace_context import _missing_workspace_git_context
from gobby.utils.daemon_git import GitFailed, GitTimeout, daemon_git

pytestmark = pytest.mark.unit


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


async def test_missing_context_for_existing_non_repository_workspace(tmp_path: Path) -> None:
    """A directory that exists but sits outside any worktree reports unavailable.

    This is the case that produced the reported traceback: the workspace survived
    as a plain directory, so the `stat()` check passed and git ran anyway.
    """
    workspace = tmp_path / "detached-workspace"
    workspace.mkdir()

    message = await _missing_workspace_git_context(workspace)

    assert message is not None
    assert str(workspace) in message
    assert "git context unavailable" in message


async def test_no_missing_context_inside_a_git_worktree(tmp_path: Path) -> None:
    """A real worktree stays usable, so existing git enrichment is untouched."""
    workspace = tmp_path / "live-workspace"
    workspace.mkdir()
    _init_repo(workspace)

    assert await _missing_workspace_git_context(workspace) is None


async def test_no_missing_context_in_a_worktree_subdirectory(tmp_path: Path) -> None:
    """The probe accepts a subdirectory, not only the worktree root."""
    workspace = tmp_path / "live-workspace"
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    _init_repo(workspace)

    assert await _missing_workspace_git_context(nested) is None


@pytest.mark.parametrize(
    "result",
    [
        GitTimeout("timeout", ("git", "rev-parse", "--git-dir"), 5.0),
        GitFailed(
            "failed",
            ("git", "rev-parse", "--git-dir"),
            128,
            "",
            "fatal: detected dubious ownership in repository",
        ),
    ],
    ids=["timeout", "unrelated-failure"],
)
async def test_probe_failure_does_not_claim_missing_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: GitTimeout | GitFailed,
) -> None:
    """Only a positive 'not a git repository' may report a missing repository.

    A timeout, or a failure git did not attribute to a missing repository, leaves
    behavior as it was rather than mislabeling an unrelated git problem.
    """
    workspace = tmp_path / "unhappy-workspace"
    workspace.mkdir()

    async def _probe(*_args: object, **_kwargs: object) -> GitTimeout | GitFailed:
        return result

    # `workspace_context` holds this exact runner object, so patching it here
    # reaches the probe without reimporting the module under test.
    monkeypatch.setattr(daemon_git, "run", _probe)

    assert await _missing_workspace_git_context(workspace) is None


async def test_deleted_workspace_still_reports_missing_context(tmp_path: Path) -> None:
    """The pre-existing deleted-workspace message keeps working."""
    workspace = tmp_path / "never-created"

    message = await _missing_workspace_git_context(workspace)

    assert message is not None
    assert "no longer exists" in message
    assert str(workspace) in message

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.merge import create_merge_registry
from gobby.utils.daemon_git import GitOk, GitTimeout, daemon_git
from tests.mcp_proxy.tools.git_helpers import GitResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _origin_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daemon_git,
        "run",
        AsyncMock(
            return_value=GitOk(
                status="ok",
                argv=("git", "remote", "get-url", "origin"),
                stdout="https://example.invalid/acme/widgets.git\n",
                stderr="",
            )
        ),
    )


def _registry(git_manager: MagicMock):
    return create_merge_registry(
        merge_storage=MagicMock(),
        merge_resolver=MagicMock(),
        git_manager=git_manager,
        worktree_manager=None,
    )


@pytest.mark.asyncio
async def test_probe_branch_protection_dry_run_success_is_unprotected() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command = AsyncMock(return_value=GitResult(0, stdout="ok"))

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["success"] is True
    assert result["requires_pr"] is False
    assert result["source"] == "push_dry_run"
    assert result["protection_unknown"] is False
    git_manager.run_git_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_branch_protection_dry_run_protected_marker_requires_pr() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command = AsyncMock(
        return_value=GitResult(
            1,
            stderr="remote: error: GH006: Protected branch update failed",
        )
    )

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["requires_pr"] is True
    assert result["source"] == "push_dry_run"
    assert result["protection_unknown"] is False


@pytest.mark.asyncio
async def test_probe_branch_protection_dry_run_unknown_failure_is_unknown() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command = AsyncMock(
        return_value=GitResult(1, stderr="fatal: could not read Username")
    )

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["requires_pr"] is True
    assert result["protection_unknown"] is True


@pytest.mark.asyncio
async def test_probe_branch_protection_fails_closed_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daemon_git,
        "run",
        AsyncMock(
            return_value=GitTimeout(
                status="timeout",
                argv=("git", "remote", "get-url", "origin"),
                timeout=10.0,
            )
        ),
    )

    result = await _registry(MagicMock()).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result == {"success": False, "error": "No origin remote found"}

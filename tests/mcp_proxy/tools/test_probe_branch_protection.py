from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import gobby.mcp_proxy.tools.merge as merge_tools
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
                stdout="https://github.com/acme/widgets.git\n",
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


def _mock_github(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: response)

    def client_factory(**kwargs):
        return original_client(
            transport=transport,
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(merge_tools.httpx, "AsyncClient", client_factory)


@pytest.mark.asyncio
async def test_probe_branch_protection_reads_github_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    git_manager = MagicMock()
    _mock_github(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["test"],
                    "checks": [{"context": "lint"}],
                },
                "required_pull_request_reviews": {
                    "required_approving_review_count": 2,
                },
            },
        ),
    )

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["requires_pr"] is True
    assert result["requires_status_checks"] == ["lint", "test"]
    assert result["requires_up_to_date"] is True
    assert result["requires_review_count"] == 2
    assert result["protection_unknown"] is False


@pytest.mark.asyncio
async def test_probe_branch_protection_404_means_unprotected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_manager = MagicMock()
    _mock_github(monkeypatch, httpx.Response(404, json={"message": "Not Found"}))

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["requires_pr"] is False
    assert result["source"] == "github_api"
    assert result["owner"] == "acme"
    assert result["repo"] == "widgets"


@pytest.mark.asyncio
async def test_probe_branch_protection_403_falls_back_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_manager = MagicMock()
    git_manager.run_git_command = AsyncMock(
        return_value=GitResult(
            1,
            stderr="remote: error: GH006: Protected branch update failed",
        )
    )
    _mock_github(monkeypatch, httpx.Response(403, text="Forbidden"))

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["requires_pr"] is True
    assert result["source"] == "push_dry_run_after_403"
    assert result["protection_unknown"] is False


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

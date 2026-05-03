from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import httpx
import pytest

import gobby.mcp_proxy.tools.merge as merge_tools
from gobby.mcp_proxy.tools.merge import create_merge_registry

pytestmark = pytest.mark.unit


@dataclass
class GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


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
    git_manager.run_git_command.return_value = GitResult(
        0,
        "https://github.com/acme/widgets.git\n",
    )
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
    git_manager.run_git_command.return_value = GitResult(
        0,
        "git@github.com:acme/widgets.git\n",
    )
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
    git_manager.run_git_command.side_effect = [
        GitResult(0, "https://github.com/acme/widgets.git\n"),
        GitResult(1, stderr="remote: error: GH006: Protected branch update failed"),
    ]
    _mock_github(monkeypatch, httpx.Response(403, text="Forbidden"))

    result = await _registry(git_manager).call(
        "probe_branch_protection",
        {"repo_path": "/repo", "branch": "main"},
    )

    assert result["requires_pr"] is True
    assert result["source"] == "push_dry_run_after_403"
    assert result["protection_unknown"] is False

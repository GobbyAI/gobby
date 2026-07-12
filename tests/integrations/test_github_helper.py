from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.integrations.github_helper import (
    GitHubMCPHelper,
    GitHubMCPToolError,
    _github_page_limit,
)


def _mcp_result(text: str | None, *, is_error: bool) -> CallToolResult:
    content = [] if text is None else [TextContent(type="text", text=text)]
    return CallToolResult(content=content, isError=is_error)


def _helper(result: object) -> GitHubMCPHelper:
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    manager = SimpleNamespace(
        get_client_session=AsyncMock(return_value=session),
        has_server=lambda _name: True,
        health={"github": {"state": "connected"}},
    )
    return GitHubMCPHelper(manager, "/tmp/repo", "owner/repo")


def test_github_page_limit_accepts_api_range() -> None:
    assert _github_page_limit(1) == 1
    assert _github_page_limit(100) == 100


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_github_page_limit_rejects_silent_caps(limit: int) -> None:
    with pytest.raises(ValueError):
        _github_page_limit(limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"message":"branch denied"}', "branch denied"),
        ("permission denied", "permission denied"),
        (None, "unknown error"),
    ],
)
async def test_call_github_mcp_raises_typed_error_before_parsing(
    payload: str | None,
    message: str,
) -> None:
    helper = _helper(_mcp_result(payload, is_error=True))

    with pytest.raises(GitHubMCPToolError, match=message):
        await helper._call_github_mcp("create_branch", {})


@pytest.mark.asyncio
async def test_create_branch_falls_back_to_git_on_mcp_tool_error() -> None:
    helper = _helper(_mcp_result("branch creation rejected", is_error=True))
    run_git = AsyncMock(return_value=SimpleNamespace(returncode=0))
    helper._run_git_async = run_git

    created = await helper.create_branch("feature", from_branch="main")

    assert created is True
    run_git.assert_awaited_once_with(
        ["push", "origin", "main:refs/heads/feature"],
        timeout=60,
    )


@pytest.mark.asyncio
async def test_push_files_raises_on_mcp_tool_error() -> None:
    helper = _helper(_mcp_result('{"message":"push rejected"}', is_error=True))

    with pytest.raises(GitHubMCPToolError, match="push rejected"):
        await helper.push_files(
            "feature",
            [{"path": "README.md", "content": "updated"}],
            "Update README",
        )


@pytest.mark.asyncio
async def test_get_file_contents_falls_back_on_mcp_tool_error() -> None:
    helper = _helper(_mcp_result("remote read rejected", is_error=True))
    run_git = AsyncMock(return_value=SimpleNamespace(returncode=0, stdout="local contents"))
    helper._run_git_async = run_git

    contents = await helper.get_file_contents("README.md", branch="main")

    assert contents == "local contents"
    run_git.assert_awaited_once_with(["show", "main:README.md"], timeout=10)


@pytest.mark.asyncio
async def test_get_file_contents_raises_when_mcp_and_git_fail() -> None:
    helper = _helper(_mcp_result("remote read rejected", is_error=True))
    helper._run_git_async = AsyncMock(return_value=SimpleNamespace(returncode=1, stdout=""))

    with pytest.raises(FileNotFoundError, match="README.md at main"):
        await helper.get_file_contents("README.md", branch="main")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"sha":"abc123"}', {"sha": "abc123"}),
        ("plain file contents", "plain file contents"),
    ],
)
async def test_call_github_mcp_preserves_valid_text_results(
    payload: str,
    expected: object,
) -> None:
    helper = _helper(_mcp_result(payload, is_error=False))

    assert await helper._call_github_mcp("valid_tool", {}) == expected

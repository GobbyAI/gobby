from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.integrations.github_helper import (
    GitHubMCPHelper,
    GitHubMCPToolError,
    _github_page_limit,
)


def _mcp_result(text: str | None, *, is_error: bool) -> CallToolResult:
    content = [] if text is None else [TextContent(type="text", text=text)]
    return CallToolResult(content=content, is_error=is_error)


def _helper(result: object) -> GitHubMCPHelper:
    from gobby.mcp_proxy.models import MCPServerConfig
    from gobby.storage.projects import GLOBAL_PROJECT_ID

    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    config = MCPServerConfig(
        name="github",
        project_id=GLOBAL_PROJECT_ID,
        url="https://github.example.test",
        id="github",
    )
    manager = SimpleNamespace(
        get_client_session=AsyncMock(return_value=session),
        has_server=lambda _name: True,
        health={"github": {"state": "connected"}},
        server_configs=[config],
        get_server_config=lambda sid: config if sid == "github" else None,
        project_id=None,
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


def _github_commit(index: int) -> dict[str, object]:
    sha = f"{index:040x}"
    return {
        "sha": sha,
        "commit": {
            "message": f"Commit {index}",
            "author": {"name": "Author", "date": "2026-07-11T00:00:00Z"},
        },
        "html_url": f"https://github.com/owner/repo/commit/{sha}",
    }


@pytest.mark.asyncio
async def test_list_issues_paginates_past_one_hundred() -> None:
    helper = _helper([])
    call_tool = helper.mcp_manager.get_client_session.return_value.call_tool
    first_page = [{"id": index} for index in range(100)]
    second_page = [{"id": index} for index in range(100, 125)]
    call_tool.side_effect = [first_page, second_page]

    issues = await helper.list_issues(limit=125)

    assert [issue["id"] for issue in issues] == list(range(125))
    page_args = [call.args[1] for call in call_tool.await_args_list]
    assert [(args["page"], args["per_page"]) for args in page_args] == [
        (1, 100),
        (2, 25),
    ]


@pytest.mark.asyncio
async def test_list_commits_paginates_and_preserves_requested_limit() -> None:
    helper = _helper([])
    call_tool = helper.mcp_manager.get_client_session.return_value.call_tool
    call_tool.side_effect = [
        [_github_commit(index) for index in range(100)],
        [_github_commit(index) for index in range(100, 130)],
    ]

    commits = await helper.list_commits("main", limit=125)

    assert len(commits) == 125
    assert commits[-1]["message"] == "Commit 124"
    page_args = [call.args[1] for call in call_tool.await_args_list]
    assert [(args["page"], args["per_page"]) for args in page_args] == [
        (1, 100),
        (2, 25),
    ]


@pytest.mark.asyncio
async def test_list_commits_uses_git_fallback_for_unexpected_mcp_shape() -> None:
    helper = _helper({"unexpected": "shape"})
    helper._run_git = MagicMock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=("abcdef1234567890\tabcdef1\tFrom git\tGit Author\t2026-07-11T00:00:00Z\n"),
            stderr="",
        )
    )

    commits = await helper.list_commits("main")

    assert commits == [
        {
            "sha": "abcdef1234567890",
            "short_sha": "abcdef1",
            "message": "From git",
            "author": "Git Author",
            "date": "2026-07-11T00:00:00Z",
        }
    ]
    helper._run_git.assert_called_once_with(
        [
            "log",
            "main",
            "--max-count=20",
            "--format=%H\t%h\t%s\t%an\t%aI",
        ],
        timeout=15,
    )


@pytest.mark.asyncio
async def test_list_commits_chains_git_fallback_failure_to_mcp_shape_error() -> None:
    helper = _helper({"unexpected": "shape"})
    helper._run_git = MagicMock(side_effect=OSError("git log failed"))

    with pytest.raises(OSError, match="git log failed") as exc_info:
        await helper.list_commits("main")

    assert exc_info.value.__cause__ is not None
    assert "unexpected response" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("list_commits", ("--all",)),
        ("get_file_contents", ("README.md", "--help")),
        ("create_branch", ("feature", "--upload-pack=evil")),
        ("create_branch", ("--help", None)),
    ],
)
async def test_ref_arguments_reject_option_injection_before_external_calls(
    operation: str,
    arguments: tuple[str | None, ...],
) -> None:
    helper = _helper([])
    call_tool = helper.mcp_manager.get_client_session.return_value.call_tool
    helper._run_git_async = AsyncMock()
    helper._run_git = MagicMock()

    with pytest.raises(ValueError, match="valid Git ref"):
        await getattr(helper, operation)(*arguments)

    call_tool.assert_not_awaited()
    helper._run_git_async.assert_not_awaited()
    helper._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_push_files_rejects_option_like_branch_before_mcp_call() -> None:
    helper = _helper([])
    call_tool = helper.mcp_manager.get_client_session.return_value.call_tool

    with pytest.raises(ValueError, match="valid Git ref"):
        await helper.push_files("--help", [], "No-op")

    call_tool.assert_not_awaited()

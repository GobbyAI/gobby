from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.features import MergeResolutionConfig
from gobby.utils.daemon_git import GitFailed, GitOk
from gobby.worktrees.merge.resolver import MergeResolver

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_llm_service():
    """Mock for LLMService."""
    return MagicMock()


@pytest.fixture
def resolver(mock_llm_service):
    """MergeResolver instance with mocked LLM service."""
    res = MergeResolver()
    res.llm_service = mock_llm_service
    res.config = MergeResolutionConfig(candidates=["claude/sonnet"])
    return res


@pytest.mark.asyncio
async def test_git_merge_success(resolver):
    """Test git merge with no conflicts."""
    result_ok = GitOk(
        status="ok",
        argv=("git", "merge", "--no-commit", "--no-ff", "test-target"),
        stdout="",
        stderr="",
    )
    with patch(
        "gobby.worktrees.merge.resolver.daemon_git.run",
        new=AsyncMock(return_value=result_ok),
    ) as mock_run:
        result = await resolver._git_merge("/tmp/test-repo", "feature", "test-target")

        assert result["success"] is True
        assert result["conflicts"] == []

        mock_run.assert_awaited_once_with(
            ["merge", "--no-commit", "--no-ff", "test-target"],
            cwd="/tmp/test-repo",
            timeout=120.0,
        )


@pytest.mark.asyncio
async def test_git_merge_conflict(resolver):
    """Test git merge with conflicts."""
    merge_failed = GitFailed(
        status="failed",
        argv=("git", "merge"),
        returncode=1,
        stdout="",
        stderr="merge conflict",
    )
    diff_ok = GitOk(
        status="ok",
        argv=("git", "diff"),
        stdout="file.txt\n",
        stderr="",
    )
    with (
        patch(
            "gobby.worktrees.merge.resolver.daemon_git.run",
            new=AsyncMock(side_effect=[merge_failed, diff_ok]),
        ),
        patch.object(
            Path,
            "read_text",
            return_value="<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> feature\n",
        ),
    ):
        result = await resolver._git_merge("/tmp/test-repo", "feature", "test-target")

    assert result["success"] is False
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["file"] == "file.txt"
    assert len(result["conflicts"][0]["hunks"]) == 1


async def test_git_merge_preserves_all_unparseable_conflicted_paths(resolver):
    """Every unmerged path is reported even when parsing or reading fails."""
    merge_failed = GitFailed(
        status="failed",
        argv=("git", "merge"),
        returncode=1,
        stdout="",
        stderr="merge failed",
    )
    diff_ok = GitOk(
        status="ok",
        argv=("git", "diff"),
        stdout="malformed.py\nbinary.dat\n",
        stderr="",
    )

    def read_conflict(path: Path, *, encoding: str) -> str:
        assert encoding == "utf-8"
        if path.name == "binary.dat":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return "<<<<<<< HEAD\nours\n======= body\n=======\ntheirs\n>>>>>>> main\n"

    with (
        patch(
            "gobby.worktrees.merge.resolver.daemon_git.run",
            new=AsyncMock(side_effect=[merge_failed, diff_ok]),
        ),
        patch.object(Path, "read_text", autospec=True, side_effect=read_conflict),
    ):
        result = await resolver._git_merge("/tmp/test-repo", "feature", "main")

    assert result["success"] is False
    assert [conflict["file"] for conflict in result["conflicts"]] == [
        "malformed.py",
        "binary.dat",
    ]
    assert [conflict["hunks"] for conflict in result["conflicts"]] == [[], []]


@pytest.mark.asyncio
async def test_resolve_conflicts_only_success(resolver, mock_llm_service, tmp_path):
    """Tier 2 splices the LLM hunk response into the file on disk."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("before\n<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> feature\nafter\n")
    conflicts = [
        {
            "file": str(file_path),
            "hunks": [{"ours": "A", "theirs": "B", "start_line": 1, "end_line": 3}],
        }
    ]

    mock_llm_service.call_feature = AsyncMock(return_value="RESOLVED")

    result = await resolver._resolve_conflicts_only(conflicts)

    assert result["success"] is True
    # Spliced content has the conflict block replaced; surrounding lines preserved.
    content = result["resolutions"][0]["content"]
    assert content == "before\nRESOLVED\nafter\n"
    assert "<<<<<<<" not in content
    assert ">>>>>>>" not in content


@pytest.mark.asyncio
async def test_resolve_conflicts_only_failure(resolver, mock_llm_service):
    """Test conflict-only resolution failure."""
    conflicts = [{"file": "file.txt", "hunks": [{"ours": "A", "theirs": "B"}]}]

    # Mock LLM failure or empty response
    mock_llm_service.call_feature = AsyncMock(return_value=None)

    result = await resolver._resolve_conflicts_only(conflicts)
    assert result["success"] is False


@pytest.mark.asyncio
async def test_resolve_full_file(resolver, mock_llm_service):
    """Test full-file resolution."""
    conflicts = [{"file": "file.txt", "hunks": []}]

    # Mock reading full file
    with patch.object(Path, "read_text", return_value="FULL FILE CONTENT"):
        mock_llm_service.call_feature = AsyncMock(return_value="FIXED CONTENT")

        result = await resolver._resolve_full_file(conflicts)

        assert result["success"] is True
        assert len(result["resolutions"]) == 1
        assert result["resolutions"][0]["content"] == "FIXED CONTENT"

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.features import MergeResolutionConfig
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
    # Mock subprocess execution
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_process

        result = await resolver._git_merge("/tmp/test-repo", "feature", "test-target")

        assert result["success"] is True
        assert result["conflicts"] == []

        # Verify git merge called with expected args
        mock_exec.assert_called_with(
            "git",
            "merge",
            "--no-commit",
            "--no-ff",
            "test-target",
            cwd="/tmp/test-repo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


@pytest.mark.asyncio
async def test_git_merge_conflict(resolver):
    """Test git merge with conflicts."""
    # Mock git merge failing (conflict)
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        # 1. git merge fails
        mock_process_merge = AsyncMock()
        mock_process_merge.returncode = 1
        mock_process_merge.communicate.return_value = (
            b"CONFLICT (content): Merge conflict in file.txt",
            b"",
        )

        # 2. git diff finds conflicted files
        mock_process_diff = AsyncMock()
        mock_process_diff.returncode = 0
        mock_process_diff.communicate.return_value = (b"file.txt\n", b"")

        mock_exec.side_effect = [mock_process_merge, mock_process_diff]

        # Mock reading file content with conflicts
        with patch.object(
            Path,
            "read_text",
            return_value="<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> feature\n",
        ):
            result = await resolver._git_merge("/tmp/test-repo", "feature", "test-target")

            assert result["success"] is False
            assert len(result["conflicts"]) == 1
            assert result["conflicts"][0]["file"] == "file.txt"
            assert len(result["conflicts"][0]["hunks"]) == 1


async def test_git_merge_preserves_all_unparseable_conflicted_paths(resolver):
    """Every unmerged path is reported even when parsing or reading fails."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_process_merge = AsyncMock()
        mock_process_merge.returncode = 1
        mock_process_merge.communicate.return_value = (b"", b"merge failed")

        mock_process_diff = AsyncMock()
        mock_process_diff.returncode = 0
        mock_process_diff.communicate.return_value = (b"malformed.py\nbinary.dat\n", b"")
        mock_exec.side_effect = [mock_process_merge, mock_process_diff]

        def read_conflict(path: Path, *, encoding: str) -> str:
            assert encoding == "utf-8"
            if path.name == "binary.dat":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return "<<<<<<< HEAD\nours\n======= body\n=======\ntheirs\n>>>>>>> main\n"

        with patch.object(Path, "read_text", autospec=True, side_effect=read_conflict):
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

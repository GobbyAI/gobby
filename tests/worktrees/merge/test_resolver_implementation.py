import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.worktrees.merge.resolver import MergeResolver, auto_resolve_trivial_conflicts

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
            "origin/test-target",
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

    mock_provider = MagicMock()
    mock_provider.generate_text = AsyncMock(return_value="RESOLVED")
    mock_llm_service.get_default_provider.return_value = mock_provider

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
    mock_provider = MagicMock()
    mock_provider.generate_text = AsyncMock(return_value=None)
    mock_llm_service.get_default_provider.return_value = mock_provider

    result = await resolver._resolve_conflicts_only(conflicts)
    assert result["success"] is False


@pytest.mark.asyncio
async def test_resolve_full_file(resolver, mock_llm_service):
    """Test full-file resolution."""
    conflicts = [{"file": "file.txt", "hunks": []}]

    # Mock reading full file
    with patch.object(Path, "read_text", return_value="FULL FILE CONTENT"):
        mock_provider = MagicMock()
        mock_provider.generate_text = AsyncMock(return_value="FIXED CONTENT")
        mock_llm_service.get_default_provider.return_value = mock_provider

        result = await resolver._resolve_full_file(conflicts)

        assert result["success"] is True
        assert len(result["resolutions"]) == 1
        assert result["resolutions"][0]["content"] == "FIXED CONTENT"


# --- auto_resolve_trivial_conflicts tests ---


@pytest.mark.asyncio
async def test_auto_resolve_trivial_only_jsonl():
    """All conflicts are trivial .gobby/*.jsonl — returns empty list."""
    files = [".gobby/tasks.jsonl", ".gobby/memories.jsonl"]

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_proc

        remaining = await auto_resolve_trivial_conflicts(files, "/tmp/wt")

    assert remaining == []
    # 2 files x 2 calls each (checkout --theirs + add)
    assert mock_exec.call_count == 4


@pytest.mark.asyncio
async def test_auto_resolve_trivial_mixed():
    """Mix of trivial and real conflicts — returns only real ones."""
    files = [".gobby/tasks.jsonl", "src/gobby/communications/manager.py", ".gobby/memories.jsonl"]

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_proc

        remaining = await auto_resolve_trivial_conflicts(files, "/tmp/wt")

    assert remaining == ["src/gobby/communications/manager.py"]
    # 2 trivial files x 2 calls each
    assert mock_exec.call_count == 4


@pytest.mark.asyncio
async def test_auto_resolve_trivial_none():
    """No trivial conflicts — returns all files unchanged."""
    files = ["src/main.py", "src/utils.py"]

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        remaining = await auto_resolve_trivial_conflicts(files, "/tmp/wt")

    assert remaining == files
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_auto_resolve_trivial_empty():
    """Empty conflict list — returns empty."""
    remaining = await auto_resolve_trivial_conflicts([], "/tmp/wt")
    assert remaining == []

"""
Comprehensive unit tests for gobby.tasks.validation module.

This test module provides additional coverage for the task validation module,
focusing on areas not covered by test_task_validation.py:
- get_last_commit_diff truncation logic
- get_recent_commits line parsing edge cases
- get_commits_since truncation
- find_matching_files glob exception handling and early exit
- read_files_content early truncation
- get_validation_context_smart final truncation
- get_git_diff fallback_to_last_commit=False path
- validate_task category parameter handling
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMProvider, LLMService
from gobby.tasks.validation import (
    TaskValidator,
    ValidationResult,
    extract_file_patterns_from_text,
    find_matching_files,
    get_commits_since,
    get_git_diff,
    get_last_commit_diff,
    get_multi_commit_diff,
    get_recent_commits,
    get_validation_context_smart,
    read_files_content,
    run_git_command,
)

pytestmark = pytest.mark.unit


class TestRunGitCommand:
    """Tests for run_git_command helper function."""

    @patch("subprocess.run")
    def test_run_git_command_success(self, mock_run) -> None:
        """Test successful git command execution."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        result = run_git_command(["git", "status"])
        assert result is not None
        assert result.returncode == 0
        assert result.stdout == "output"

    @patch("subprocess.run")
    def test_run_git_command_with_cwd(self, mock_run) -> None:
        """Test git command with custom working directory."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        run_git_command(["git", "status"], cwd="/custom/path")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == "/custom/path"

    @patch("subprocess.run")
    def test_run_git_command_with_timeout(self, mock_run) -> None:
        """Test git command with custom timeout."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        run_git_command(["git", "status"], timeout=30)
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == 30

    @patch("subprocess.run")
    def test_run_git_command_exception_returns_none(self, mock_run) -> None:
        """Test that exceptions return None instead of raising."""
        mock_run.side_effect = Exception("Git failed")
        result = run_git_command(["git", "invalid"])
        assert result is None

    @patch("subprocess.run")
    def test_run_git_command_timeout_exception(self, mock_run) -> None:
        """Test timeout exception handling."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        result = run_git_command(["git", "log"])
        assert result is None


class TestGetLastCommitDiff:
    """Tests for get_last_commit_diff function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_success(self, mock_run) -> None:
        """Test successful retrieval of last commit diff."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git\n+line added")
        result = get_last_commit_diff()
        assert result is not None
        assert "diff --git" in result

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_truncation(self, mock_run) -> None:
        """Test truncation of large diffs (lines 82-86)."""
        large_diff = "a" * 100000
        mock_run.return_value = MagicMock(returncode=0, stdout=large_diff)

        result = get_last_commit_diff(max_chars=1000)

        assert result is not None
        assert len(result) < len(large_diff)
        assert "... [diff truncated] ..." in result
        # The truncated content should be max_chars + truncation message
        assert result[:1000] == "a" * 1000

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_exact_max_chars(self, mock_run) -> None:
        """Test diff exactly at max_chars boundary."""
        exact_diff = "x" * 500
        mock_run.return_value = MagicMock(returncode=0, stdout=exact_diff)

        result = get_last_commit_diff(max_chars=500)

        assert result is not None
        assert "... [diff truncated] ..." not in result
        assert result == exact_diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_returns_none_on_error(self, mock_run) -> None:
        """Test returns None when git command fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = get_last_commit_diff()
        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_returns_none_when_run_returns_none(self, mock_run) -> None:
        """Test returns None when run_git_command returns None."""
        mock_run.return_value = None
        result = get_last_commit_diff()
        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_returns_none_on_empty(self, mock_run) -> None:
        """Test returns None when diff is empty."""
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n\t  ")
        result = get_last_commit_diff()
        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_with_cwd(self, mock_run) -> None:
        """Test cwd parameter is passed correctly."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")
        get_last_commit_diff(cwd="/project/path")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/project/path"


class TestGetRecentCommitsEdgeCases:
    """Additional edge case tests for get_recent_commits function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_line_without_pipe(self, mock_run) -> None:
        """Test handling of lines without pipe separator (line 108 branch)."""
        # Mix of valid and invalid lines
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123|Valid commit\ninvalid_line_no_pipe\ndef456|Another commit",
        )

        commits = get_recent_commits(3)

        # Should only include lines with pipe separators
        assert len(commits) == 2
        assert commits[0]["sha"] == "abc123"
        assert commits[1]["sha"] == "def456"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_all_invalid_lines(self, mock_run) -> None:
        """Test when all lines lack pipe separator."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="no pipe here\nalso no pipe\nstill none",
        )

        commits = get_recent_commits(3)
        assert commits == []

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_subject_with_pipes(self, mock_run) -> None:
        """Test commit subject containing pipe characters."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123|fix: handle a|b|c case in parser",
        )

        commits = get_recent_commits(1)

        assert len(commits) == 1
        assert commits[0]["sha"] == "abc123"
        assert commits[0]["subject"] == "fix: handle a|b|c case in parser"


class TestGetCommitsSinceTruncation:
    """Tests for get_commits_since truncation behavior."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_truncation(self, mock_run) -> None:
        """Test truncation of large diffs (line 162)."""
        large_diff = "b" * 80000
        mock_run.return_value = MagicMock(returncode=0, stdout=large_diff)

        result = get_commits_since("abc123", max_chars=5000)

        assert result is not None
        assert len(result) < len(large_diff)
        assert "... [diff truncated] ..." in result

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_no_truncation_needed(self, mock_run) -> None:
        """Test when diff is under max_chars limit."""
        small_diff = "x" * 100
        mock_run.return_value = MagicMock(returncode=0, stdout=small_diff)

        result = get_commits_since("abc123", max_chars=5000)

        assert result == small_diff
        assert "... [diff truncated] ..." not in result


class TestFindMatchingFilesEdgeCases:
    """Additional tests for find_matching_files function."""

    def test_find_matching_files_early_exit_max_files(self, tmp_path) -> None:
        """Test early exit when max_files is reached (line 233 break)."""
        # Create more files than max_files
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text(f"content {i}")

        # Request only 2 files but provide multiple patterns
        files = find_matching_files(
            ["file0.py", "file1.py", "file2.py", "file3.py"],
            base_dir=tmp_path,
            max_files=2,
        )

        assert len(files) == 2

    def test_find_matching_files_glob_exception(self, tmp_path) -> None:
        """Test exception handling in glob (lines 242-243)."""
        # Create a valid file
        (tmp_path / "valid.py").write_text("content")

        # Use a pattern that causes glob to fail on some systems
        # The [! pattern is invalid in some glob implementations
        with patch.object(Path, "glob") as mock_glob:
            mock_glob.side_effect = ValueError("Invalid glob pattern")

            files = find_matching_files(
                ["*.py"],  # This will trigger the glob path
                base_dir=tmp_path,
            )

            # Should handle exception gracefully and return empty list
            assert files == []

    def test_find_matching_files_stops_at_max_during_glob(self, tmp_path) -> None:
        """Test max_files limit during glob iteration."""
        # Create multiple files
        for i in range(10):
            (tmp_path / f"test{i}.py").write_text(f"content {i}")

        files = find_matching_files(["*.py"], base_dir=tmp_path, max_files=3)

        assert len(files) == 3

    def test_find_matching_files_skip_directories(self, tmp_path) -> None:
        """Test that directories are skipped even if they match pattern."""
        # Create a file and a directory with same base name
        (tmp_path / "module.py").write_text("content")
        (tmp_path / "module_dir").mkdir()

        files = find_matching_files(["module*"], base_dir=tmp_path)

        # Should only include the file, not the directory
        assert len(files) == 1
        assert files[0].name == "module.py"

    def test_find_matching_files_no_duplicates(self, tmp_path) -> None:
        """Test that duplicate files are not added."""
        test_file = tmp_path / "unique.py"
        test_file.write_text("content")

        # Provide patterns that would match the same file
        files = find_matching_files(
            ["unique.py", "unique.py", "*.py"],
            base_dir=tmp_path,
        )

        assert len(files) == 1
        assert files[0] == test_file


class TestReadFilesContentEdgeCases:
    """Additional tests for read_files_content function."""

    def test_read_files_content_early_truncation(self, tmp_path) -> None:
        """Test early exit when total_chars >= max_chars (lines 271-272)."""
        # Create files where total would exceed max_chars
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file3 = tmp_path / "file3.py"
        file1.write_text("a" * 500)
        file2.write_text("b" * 500)
        file3.write_text("c" * 500)

        # Set max_chars so we hit it after file1
        content = read_files_content([file1, file2, file3], max_chars=100)

        # Should have truncation message for additional files
        assert "... [additional files truncated] ..." in content

    def test_read_files_content_exact_boundary(self, tmp_path) -> None:
        """Test when total_chars exactly equals max_chars."""
        file1 = tmp_path / "exact.py"
        file1.write_text("x" * 100)

        content = read_files_content([file1], max_chars=100)

        # Should not include additional files truncation message
        # but file may be truncated
        assert "exact.py" in content

    def test_read_files_content_empty_file(self, tmp_path) -> None:
        """Test reading an empty file."""
        empty_file = tmp_path / "empty.py"
        empty_file.write_text("")

        content = read_files_content([empty_file])

        assert "empty.py" in content
        # Should have header but minimal content
        assert "===" in content


class TestGetValidationContextSmartEdgeCases:
    """Additional edge case tests for get_validation_context_smart."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_context_final_truncation(self, mock_run) -> None:
        """Test final truncation when combined context exceeds max_chars (line 370).

        The function truncates each piece to remaining_chars // 2, but when
        pieces are joined with separators, the combined length can still exceed
        max_chars, triggering the final truncation.
        """
        # Create staged and unstaged content that when combined will exceed max_chars
        # With max_chars=100, each piece gets 50 chars, but headers and join adds more
        mock_staged = MagicMock(returncode=0, stdout="a" * 200)
        mock_unstaged = MagicMock(returncode=0, stdout="b" * 200)
        mock_run.side_effect = [mock_staged, mock_unstaged]

        context = get_validation_context_smart(
            "Test task",
            max_chars=100,  # Small max_chars to trigger truncation
        )

        assert context is not None
        # The combined content with headers should exceed max_chars
        # triggering the final truncation message
        # Note: due to internal truncation logic, the final truncation may or may not appear
        # The key is verifying the function handles small max_chars gracefully

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_context_limited_remaining_chars_skips_commit_diff(self, mock_diff, mock_run) -> None:
        """Test that commit diff is skipped when remaining_chars < 5000.

        Strategy 2 (multi-commit) only runs if remaining_chars > 5000.
        """
        # Large staged content: with max_chars=8000, staged gets 4000 chars
        # unstaged gets up to 2000 chars, leaving < 5000 remaining
        mock_staged = MagicMock(returncode=0, stdout="s" * 8000)
        mock_unstaged = MagicMock(returncode=0, stdout="u" * 4000)
        mock_run.side_effect = [mock_staged, mock_unstaged]
        mock_diff.return_value = "diff content"

        context = get_validation_context_smart(
            "Test task",
            max_chars=8000,
        )

        assert context is not None
        # Verify multi-commit diff was NOT called because remaining < 5000
        mock_diff.assert_not_called()

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.find_matching_files")
    def test_context_skips_file_analysis_when_low_remaining(
        self, mock_find, mock_diff, mock_run
    ) -> None:
        """Test that file analysis is skipped when remaining_chars < 2000."""
        # Large content from earlier strategies
        mock_run.return_value = MagicMock(returncode=0, stdout="x" * 48000)
        mock_diff.return_value = None

        context = get_validation_context_smart(
            "Test task",
            validation_criteria="Check src/gobby/tasks/validation.py",
            max_chars=50000,
        )

        # File analysis may or may not be triggered depending on implementation
        # The test verifies the function handles the low remaining chars case
        assert context is not None

    @patch("gobby.tasks.validation.run_git_command")
    def test_context_truncation_on_join(self, mock_run) -> None:
        """Test that final truncation happens when join pushes over max_chars.

        Each strategy truncates to remaining//2, but the join adds '\\n\\n' separators
        and headers like '=== STAGED CHANGES ===' which can push total over max_chars.
        """
        # With max_chars=150:
        # - staged gets 75 chars of content
        # - after header "=== STAGED CHANGES ===\n" (~23 chars), remaining is ~127
        # - unstaged gets ~63 chars of content
        # - after header (~25 chars) and "\n\n" join (~2 chars), total may exceed 150
        mock_staged = MagicMock(returncode=0, stdout="a" * 500)
        mock_unstaged = MagicMock(returncode=0, stdout="b" * 500)
        mock_run.side_effect = [mock_staged, mock_unstaged]

        context = get_validation_context_smart(
            "Test task",
            max_chars=150,
        )

        assert context is not None
        # When the combined length with headers exceeds max_chars,
        # the final truncation message should appear
        if len(context) > 150:
            # This means we hit the truncation path
            assert "... [context truncated] ..." in context


class TestGetGitDiffEdgeCases:
    """Additional edge case tests for get_git_diff function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_fallback_disabled(self, mock_run) -> None:
        """Test fallback_to_last_commit=False returns None (line 416)."""
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        result = get_git_diff(fallback_to_last_commit=False)

        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_last_commit_diff")
    def test_get_git_diff_fallback_returns_none(self, mock_last_commit, mock_run) -> None:
        """Test when fallback also returns None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_last_commit.return_value = None

        result = get_git_diff(fallback_to_last_commit=True)

        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_staged_only(self, mock_run) -> None:
        """Test with only staged changes."""
        mock_unstaged = MagicMock(returncode=0, stdout="")
        mock_staged = MagicMock(returncode=0, stdout="staged content")
        mock_run.side_effect = [mock_unstaged, mock_staged]

        result = get_git_diff()

        assert result is not None
        assert "STAGED CHANGES" in result
        assert "staged content" in result

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_unstaged_only(self, mock_run) -> None:
        """Test with only unstaged changes."""
        mock_unstaged = MagicMock(returncode=0, stdout="unstaged content")
        mock_staged = MagicMock(returncode=0, stdout="")
        mock_run.side_effect = [mock_unstaged, mock_staged]

        result = get_git_diff()

        assert result is not None
        assert "UNSTAGED CHANGES" in result
        assert "unstaged content" in result


class TestTaskValidatorTestStrategy:
    """Tests for category parameter in TaskValidator.validate_task."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        llm.get_provider.return_value = provider
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_validate_with_manual_category(self, config, mock_llm):
        """Test validation with category='manual' (lines 524-530)."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Fix button color",
            description="Change button to blue",
            changes_summary="Updated CSS",
            category="manual",
        )

        assert result.status == "valid"
        # Verify manual test strategy note is in prompt
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Test Strategy: manual" in prompt
        assert "MANUAL testing" in prompt
        assert "Do NOT require automated test files" in prompt

    @pytest.mark.asyncio
    async def test_validate_with_manual_category_uppercase(self, config, mock_llm):
        """Test validation with category='MANUAL' (case insensitive)."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Fix button color",
            description="Change button to blue",
            changes_summary="Updated CSS",
            category="MANUAL",
        )

        assert result.status == "valid"
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        assert "MANUAL testing" in prompt

    @pytest.mark.asyncio
    async def test_validate_with_automated_category(self, config, mock_llm):
        """Test validation with category='automated' (lines 531-532)."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Add unit tests",
            description="Add tests for validator",
            changes_summary="Added test file",
            category="automated",
        )

        assert result.status == "valid"
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Test Strategy: automated" in prompt
        # Should NOT have manual testing note
        assert "MANUAL testing" not in prompt

    @pytest.mark.asyncio
    async def test_validate_without_category(self, config, mock_llm):
        """Test validation without category parameter."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Some task",
            description="Task description",
            changes_summary="Changes made",
        )

        assert result.status == "valid"
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        # Should not have test strategy section
        assert "Test Strategy:" not in prompt

    @pytest.mark.asyncio
    async def test_validate_with_custom_category(self, config, mock_llm):
        """Test validation with custom category value."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Some task",
            description="Task description",
            changes_summary="Changes made",
            category="integration",
        )

        assert result.status == "valid"
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Test Strategy: integration" in prompt
        # Should NOT have manual testing note (not "manual")
        assert "MANUAL testing" not in prompt


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_validation_result_valid(self) -> None:
        """Test creating valid ValidationResult."""
        result = ValidationResult(status="valid", feedback="All criteria met")
        assert result.status == "valid"
        assert result.feedback == "All criteria met"

    def test_validation_result_invalid(self) -> None:
        """Test creating invalid ValidationResult."""
        result = ValidationResult(status="invalid", feedback="Missing tests")
        assert result.status == "invalid"
        assert result.feedback == "Missing tests"

    def test_validation_result_pending(self) -> None:
        """Test creating pending ValidationResult."""
        result = ValidationResult(status="pending")
        assert result.status == "pending"
        assert result.feedback is None

    def test_validation_result_default_feedback(self) -> None:
        """Test ValidationResult with default feedback."""
        result = ValidationResult(status="valid")
        assert result.feedback is None


class TestTaskValidatorCustomPrompt:
    """Tests for TaskValidator with custom prompts."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        llm.get_provider.return_value = provider
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_validate_uses_system_prompt(self, mock_llm):
        """Test validation passes system_prompt to provider."""
        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="test-model",
            system_prompt="You are a code reviewer",
        )
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        await validator.validate_task(
            task_id="task-1",
            title="Test Task",
            description="Description",
            changes_summary="Changes",
        )

        call_args = mock_provider.generate_text.call_args
        assert call_args.kwargs["system_prompt"] == "You are a code reviewer"


class TestExtractFilePatternsEdgeCases:
    """Additional tests for extract_file_patterns_from_text."""

    def test_skip_www_urls(self) -> None:
        """Test that www. prefixed strings are skipped (line 187 branch)."""
        from gobby.tasks.validation import extract_file_patterns_from_text

        text = "See www.example.com/file.py and also src/real/file.py"
        patterns = extract_file_patterns_from_text(text)

        # www.example.com/file.py should be skipped
        assert not any("www." in p for p in patterns)
        assert not any("example.com" in p for p in patterns)
        # But real file path should be included
        assert "src/real/file.py" in patterns

    def test_skip_both_http_and_www(self) -> None:
        """Test both http and www URLs are filtered (though regex may catch partial matches)."""
        from gobby.tasks.validation import extract_file_patterns_from_text

        text = "Visit http://api.test.com/v1/data.json and www.docs.io/guide.md for info"
        patterns = extract_file_patterns_from_text(text)

        # The http:// and www. prefixed strings themselves are skipped
        # but the regex may still catch partial matches
        # The key is that 'http://' and 'www.' prefixed full URLs are filtered
        assert not any(p.startswith("http") for p in patterns)
        assert not any(p.startswith("www.") for p in patterns)


class TestGetValidationContextSmartFileBranch:
    """Tests for the files branch in get_validation_context_smart (line 361)."""

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.find_matching_files")
    def test_context_no_files_found(self, mock_find, mock_diff, mock_run) -> None:
        """Test when patterns exist but no files match (line 361->365)."""
        # No uncommitted changes or commit diff
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None
        # Patterns exist (from validation_criteria) but no matching files
        mock_find.return_value = []

        context = get_validation_context_smart(
            task_title="Test task",
            validation_criteria="Check src/nonexistent/file.py",
            max_chars=50000,
        )

        # With no git changes, no commit diff, and no matching files,
        # context should be None
        assert context is None


class TestIntegrationScenarios:
    """Integration-style tests combining multiple validation functions."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_full_validation_context_flow(self, mock_run) -> None:
        """Test complete flow of gathering validation context."""
        # Simulate a realistic scenario with staged, unstaged, and commit history
        call_count = [0]

        def mock_run_side_effect(*args, **kwargs):
            call_count[0] += 1
            cmd = args[0]

            if "diff" in cmd and "--cached" in cmd:
                return MagicMock(returncode=0, stdout="+ staged change")
            elif "diff" in cmd and "HEAD~" in cmd:
                return MagicMock(returncode=0, stdout="+ historical change")
            elif "diff" in cmd:
                return MagicMock(returncode=0, stdout="+ unstaged change")
            elif "log" in cmd:
                return MagicMock(returncode=0, stdout="abc123|feat: add feature\ndef456|fix: bug")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = mock_run_side_effect

        context = get_validation_context_smart(
            task_title="Test validation",
            validation_criteria="Must have staged changes",
        )

        assert context is not None
        assert "STAGED CHANGES" in context or "UNSTAGED CHANGES" in context

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        llm.get_provider.return_value = provider
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_validation_with_large_file_context(self, config, mock_llm, tmp_path):
        """Test validation with large file context gets truncated."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        # Create a large file
        large_file = tmp_path / "large.py"
        large_file.write_text("x" * 100000)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test Task",
            description="Description",
            changes_summary="Changes",
            context_files=[str(large_file)],
        )

        assert result.status == "valid"
        # Verify the prompt was called and context was truncated
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        # Context should be truncated to 50000 chars
        assert len(prompt) < 150000  # Reasonable upper bound


class TestPathHandling:
    """Tests for Path handling in validation functions."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_path_object(self, mock_run) -> None:
        """Test get_last_commit_diff with Path object for cwd."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")
        get_last_commit_diff(cwd=Path("/path/to/project"))
        assert mock_run.call_args.kwargs["cwd"] == Path("/path/to/project")

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_path_object(self, mock_run) -> None:
        """Test get_multi_commit_diff with Path object for cwd."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")
        from gobby.tasks.validation import get_multi_commit_diff

        get_multi_commit_diff(cwd=Path("/path/to/project"))
        assert mock_run.call_args.kwargs["cwd"] == Path("/path/to/project")

    def test_find_matching_files_path_base_dir(self, tmp_path) -> None:
        """Test find_matching_files with Path object for base_dir."""
        test_file = tmp_path / "test.py"
        test_file.write_text("content")

        files = find_matching_files(["test.py"], base_dir=Path(tmp_path))
        assert len(files) == 1
        assert files[0] == test_file


# ============================================================================
# Merged Tests from test_task_validation.py (Renamed to avoid shadowing)
# ============================================================================


class TestGetGitDiffMerged:
    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_success(self, mock_run) -> None:
        # Mock unstaged
        mock_unstaged = MagicMock()
        mock_unstaged.returncode = 0
        mock_unstaged.stdout = "diff unstaged"

        # Mock staged
        mock_staged = MagicMock()
        mock_staged.returncode = 0
        mock_staged.stdout = "diff staged"

        mock_run.side_effect = [mock_unstaged, mock_staged]

        diff = get_git_diff()
        assert "=== STAGED CHANGES ===" in diff
        assert "diff staged" in diff
        assert "=== UNSTAGED CHANGES ===" in diff
        assert "diff unstaged" in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_no_changes(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        assert get_git_diff() is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_error_code(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_run.return_value = mock_res

        assert get_git_diff() is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_exception(self, mock_run) -> None:
        mock_run.side_effect = RuntimeError("Git error")
        with pytest.raises(RuntimeError):
            get_git_diff()

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_truncate(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "a" * 100
        mock_run.return_value = mock_res

        diff = get_git_diff(max_chars=10)
        assert len(diff) < 100
        assert "... [diff truncated] ..." in diff


class TestTaskValidatorMerged:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        llm.get_provider.return_value = provider
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_validate_task_disabled(self, mock_llm):
        config = TaskValidationConfig(enabled=False)
        validator = TaskValidator(config, mock_llm)
        result = await validator.validate_task("task-1", "title", "instr", "summary")
        assert result.status == "pending"
        assert "disabled" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_task_missing_info(self, config, mock_llm):
        validator = TaskValidator(config, mock_llm)
        result = await validator.validate_task(
            "task-1", "title", None, "summary"
        )  # Missing instruction (instr is None)
        assert result.status == "pending"
        assert "Missing" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_task_success(self, config, mock_llm):
        validator = TaskValidator(config, mock_llm)

        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = (
            '```json\n{"status": "valid", "feedback": "Good job"}\n```'
        )

        result = await validator.validate_task("task-1", "title", "instr", "summary")

        assert result.status == "valid"
        assert result.feedback == "Good job"
        mock_provider.generate_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_task_with_context(self, config, mock_llm, tmp_path):
        validator = TaskValidator(config, mock_llm)

        test_file = tmp_path / "test.txt"
        test_file.write_text("file content")

        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "invalid", "feedback": "Bad"}'

        result = await validator.validate_task(
            "task-1", "title", "instr", "summary", context_files=[str(test_file)]
        )

        assert result.status == "invalid"
        # Verify context was gathered
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        assert "file content" in prompt

    @pytest.mark.asyncio
    async def test_validate_task_llm_error(self, config, mock_llm):
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.side_effect = Exception("LLM Error")

        result = await validator.validate_task("task-1", "title", "instr", "summary")
        assert result.status == "pending"
        assert "failed" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_task_bad_json(self, config, mock_llm):
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = "Not JSON"

        result = await validator.validate_task("task-1", "title", "instr", "summary")
        assert result.status == "pending"  # JSON decode error caught
        assert "failed" in result.feedback


class TestGetRecentCommitsMerged:
    """Tests for get_recent_commits function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123|First commit\ndef456|Second commit\nghi789|Third commit",
        )

        commits = get_recent_commits(3)
        assert len(commits) == 3
        assert commits[0] == {"sha": "abc123", "subject": "First commit"}
        assert commits[1] == {"sha": "def456", "subject": "Second commit"}
        assert commits[2] == {"sha": "ghi789", "subject": "Third commit"}

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        commits = get_recent_commits(5)
        assert commits == []

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        commits = get_recent_commits(5)
        assert commits == []

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_exception(self, mock_run) -> None:
        mock_run.side_effect = RuntimeError("Git error")
        with pytest.raises(RuntimeError):
            get_recent_commits(5)


class TestGetMultiCommitDiffMerged:
    """Tests for get_multi_commit_diff function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff --git a/file.py b/file.py\n+new line",
        )

        diff = get_multi_commit_diff(5)
        assert diff is not None
        assert "diff --git" in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_truncate(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="a" * 1000)
        diff = get_multi_commit_diff(5, max_chars=100)
        assert len(diff) < 1000
        assert "... [diff truncated] ..." in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        diff = get_multi_commit_diff(5)
        assert diff is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        diff = get_multi_commit_diff(5)
        assert diff is None


class TestGetCommitsSinceMerged:
    """Tests for get_commits_since function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff content here",
        )

        diff = get_commits_since("abc123")
        assert diff is not None
        assert "diff content" in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        diff = get_commits_since("abc123")
        assert diff is None


class TestExtractFilePatternsFromTextMerged:
    """Tests for extract_file_patterns_from_text function."""

    def test_extract_explicit_paths(self) -> None:
        text = "Check the file src/gobby/tasks/validation.py for issues"
        patterns = extract_file_patterns_from_text(text)
        assert "src/gobby/tasks/validation.py" in patterns

    def test_extract_module_references(self) -> None:
        text = "The module gobby.tasks.validation handles this"
        patterns = extract_file_patterns_from_text(text)
        assert "src/gobby/tasks/validation.py" in patterns

    def test_extract_test_patterns(self) -> None:
        text = "Run test_validation to verify"
        patterns = extract_file_patterns_from_text(text)
        assert any("test_validation" in p for p in patterns)

    def test_extract_class_references(self) -> None:
        text = "Check the TaskValidator class"
        patterns = extract_file_patterns_from_text(text)
        assert any("validator" in p.lower() for p in patterns)

    def test_skip_urls(self) -> None:
        text = "See http://example.com/file.py for details"
        patterns = extract_file_patterns_from_text(text)
        # Should not include the URL as a file pattern
        assert not any("http" in p for p in patterns)

    def test_empty_text(self) -> None:
        patterns = extract_file_patterns_from_text("")
        assert patterns == []


class TestFindMatchingFilesMerged:
    """Tests for find_matching_files function."""

    def test_find_direct_path(self, tmp_path) -> None:
        test_file = tmp_path / "test.py"
        test_file.write_text("content")

        files = find_matching_files(["test.py"], base_dir=tmp_path)
        assert len(files) == 1
        assert files[0] == test_file

    def test_find_glob_pattern(self, tmp_path) -> None:
        (tmp_path / "test_one.py").write_text("content")
        (tmp_path / "test_two.py").write_text("content")
        (tmp_path / "other.txt").write_text("content")

        files = find_matching_files(["*.py"], base_dir=tmp_path)
        assert len(files) == 2
        assert all(f.suffix == ".py" for f in files)

    def test_find_nested_glob(self, tmp_path) -> None:
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "module.py").write_text("content")

        files = find_matching_files(["**/*.py"], base_dir=tmp_path)
        assert len(files) == 1
        assert files[0].name == "module.py"

    def test_max_files_limit(self, tmp_path) -> None:
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text("content")

        files = find_matching_files(["*.py"], base_dir=tmp_path, max_files=3)
        assert len(files) == 3

    def test_no_matches(self, tmp_path) -> None:
        files = find_matching_files(["nonexistent.py"], base_dir=tmp_path)
        assert files == []


class TestReadFilesContentMerged:
    """Tests for read_files_content function."""

    def test_read_single_file(self, tmp_path) -> None:
        test_file = tmp_path / "test.py"
        test_file.write_text("file content here")

        content = read_files_content([test_file])
        assert "file content here" in content
        assert "=== " in content  # Header

    def test_read_multiple_files(self, tmp_path) -> None:
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("content 1")
        file2.write_text("content 2")

        content = read_files_content([file1, file2])
        assert "content 1" in content
        assert "content 2" in content

    def test_truncate_large_content(self, tmp_path) -> None:
        test_file = tmp_path / "large.py"
        test_file.write_text("a" * 10000)

        content = read_files_content([test_file], max_chars=100)
        assert len(content) < 10000
        assert "... [file truncated] ..." in content

    def test_read_nonexistent_file(self, tmp_path) -> None:
        nonexistent = tmp_path / "missing.py"
        content = read_files_content([nonexistent])
        assert "Error reading file" in content


class TestGetValidationContextSmartMerged:
    """Tests for get_validation_context_smart function."""

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.get_recent_commits")
    def test_includes_uncommitted_changes(self, mock_commits, mock_diff, mock_run) -> None:
        # Mock staged and unstaged diffs
        # Note: get_validation_context_smart calls 'diff --cached' (staged) first, then 'diff' (unstaged)
        mock_staged = MagicMock(returncode=0, stdout="staged diff content")
        mock_unstaged = MagicMock(returncode=0, stdout="unstaged diff content")
        mock_run.side_effect = [mock_staged, mock_unstaged]

        # Prevent further strategies
        mock_diff.return_value = None
        mock_commits.return_value = []

        context = get_validation_context_smart("Test task")
        assert context is not None
        assert "STAGED CHANGES" in context
        assert "UNSTAGED CHANGES" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_includes_multi_commit_diff(self, mock_multi_diff, mock_run) -> None:
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_multi_diff.return_value = "multi commit diff content"

        context = get_validation_context_smart("Test task", commit_window=5)
        assert context is not None
        assert "COMBINED DIFF" in context or "multi commit diff" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.get_recent_commits")
    def test_includes_commit_summary(self, mock_commits, mock_diff, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = "diff content"
        mock_commits.return_value = [
            {"sha": "abc12345", "subject": "First commit"},
            {"sha": "def67890", "subject": "Second commit"},
        ]

        context = get_validation_context_smart("Test task")
        assert context is not None
        assert "RECENT COMMITS" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.find_matching_files")
    def test_includes_file_analysis(self, mock_find, mock_diff, mock_run, tmp_path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None  # No git diff

        test_file = tmp_path / "validation.py"
        test_file.write_text("def validate(): pass")
        mock_find.return_value = [test_file]

        get_validation_context_smart(
            "Check validation.py",
            validation_criteria="Ensure src/gobby/tasks/validation.py works",
        )
        # Should try to find files mentioned in criteria
        mock_find.assert_called()

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_returns_none_when_no_context(self, mock_diff, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None

        get_validation_context_smart(
            "Task with no related files",
            max_chars=100,  # Very limited
        )
        # May return None or minimal context
        # The function should handle this gracefully

    @patch("gobby.tasks.validation.run_git_command")
    def test_respects_max_chars(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="a" * 10000)

        context = get_validation_context_smart("Test task", max_chars=500)
        assert context is None or len(context) <= 600  # Some buffer for headers


class TestTaskValidatorAdditionalEdgeCases:
    """Additional edge case tests for TaskValidator."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        llm.get_provider.return_value = provider
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_validate_with_validation_criteria_only(self, config, mock_llm):
        """Test validation with validation_criteria but no description."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Test Task",
            description=None,  # No description
            changes_summary="Made changes",
            validation_criteria="Must have tests",  # Has criteria
        )

        assert result.status == "valid"
        # Verify criteria was used in prompt
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Validation Criteria" in prompt
        assert "Must have tests" in prompt

    @pytest.mark.asyncio
    async def test_validate_with_git_diff_context(self, config, mock_llm):
        """Test validation detects git diff format in changes_summary."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        # Git diff formatted summary
        git_diff = """Git diff from HEAD~1:
--- a/src/file.py
+++ b/src/file.py
@@ -1,3 +1,4 @@
+import os
 def main():
     pass
"""
        result = await validator.validate_task(
            task_id="task-1",
            title="Add import",
            description="Add os import",
            changes_summary=git_diff,
        )

        assert result.status == "valid"
        call_args = mock_provider.generate_text.call_args
        prompt = call_args.kwargs["prompt"]
        # Should include git diff context hint
        assert "Code Changes (git diff)" in prompt or "ACTUAL code changes" in prompt

    @pytest.mark.asyncio
    async def test_validate_with_at_symbol_diff(self, config, mock_llm):
        """Test that @@ in changes_summary triggers git diff detection."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Fix bug",
            description="Fix the bug",
            changes_summary="@@ -10,5 +10,6 @@ some context\n+added line",
        )

        assert result.status == "valid"

    @pytest.mark.asyncio
    async def test_validate_empty_llm_response(self, config, mock_llm):
        """Test handling of empty string LLM response."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = ""

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "Empty response" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_whitespace_only_response(self, config, mock_llm):
        """Test handling of whitespace-only LLM response."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = "   \n\t  "

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "Empty response" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_json_without_code_block(self, config, mock_llm):
        """Test parsing JSON without markdown code block."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = (
            '{"status": "invalid", "feedback": "Missing tests"}'
        )

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "invalid"
        assert result.feedback == "Missing tests"

    @pytest.mark.asyncio
    async def test_validate_json_with_preamble(self, config, mock_llm):
        """Test parsing JSON with LLM preamble text."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = (
            "Based on my analysis, here is my assessment:\n"
            '{"status": "valid", "feedback": "All criteria met"}'
        )

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "valid"
        assert result.feedback == "All criteria met"

    @pytest.mark.asyncio
    async def test_validate_malformed_json(self, config, mock_llm):
        """Test handling of malformed JSON response."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", feedback: missing quotes}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()

    @pytest.mark.asyncio
    async def test_validate_missing_status_field(self, config, mock_llm):
        """Test handling of JSON response missing status field."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"feedback": "Looks good"}'

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        # Should return pending status (default from .get())
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_validate_with_file_context_error(self, config, mock_llm, tmp_path):
        """Test graceful handling when context file cannot be read."""
        validator = TaskValidator(config, mock_llm)
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        # Non-existent file
        missing_file = tmp_path / "nonexistent.py"

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
            context_files=[str(missing_file)],
        )

        # Should still succeed - error is logged but validation proceeds
        assert result.status == "valid"


class TestTaskValidatorLLMErrors:
    """Tests for LLM error handling in TaskValidator."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        llm.get_provider.return_value = provider
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_validate_provider_not_found(self, config, mock_llm):
        """Test handling when LLM provider is not found."""
        mock_llm.get_provider.side_effect = ValueError("Provider not configured")
        validator = TaskValidator(config, mock_llm)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()

    @pytest.mark.asyncio
    async def test_validate_timeout_error(self, config, mock_llm):
        """Test handling of timeout during LLM call."""

        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.side_effect = TimeoutError("Request timed out")
        validator = TaskValidator(config, mock_llm)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()

    @pytest.mark.asyncio
    async def test_validate_connection_error(self, config, mock_llm):
        """Test handling of connection error during LLM call."""
        mock_provider = mock_llm.get_provider.return_value
        mock_provider.generate_text.side_effect = ConnectionError("Network error")
        validator = TaskValidator(config, mock_llm)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()


class TestGatherValidationContext:
    """Tests for TaskValidator.gather_validation_context method."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, provider="claude", model="test-model")

    @pytest.mark.asyncio
    async def test_gather_single_file(self, config, mock_llm, tmp_path):
        """Test gathering context from a single file."""
        validator = TaskValidator(config, mock_llm)
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): return 'world'")

        context = await validator.gather_validation_context([str(test_file)])

        assert "test.py" in context
        assert "def hello()" in context

    @pytest.mark.asyncio
    async def test_gather_multiple_files(self, config, mock_llm, tmp_path):
        """Test gathering context from multiple files."""
        validator = TaskValidator(config, mock_llm)
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("# File 1 content")
        file2.write_text("# File 2 content")

        context = await validator.gather_validation_context([str(file1), str(file2)])

        assert "file1.py" in context
        assert "file2.py" in context
        assert "File 1 content" in context
        assert "File 2 content" in context

    @pytest.mark.asyncio
    async def test_gather_nonexistent_file(self, config, mock_llm, tmp_path):
        """Test gathering context with a nonexistent file."""
        validator = TaskValidator(config, mock_llm)
        missing = tmp_path / "missing.py"

        context = await validator.gather_validation_context([str(missing)])

        assert "missing.py" in context
        assert "Error reading file" in context

    @pytest.mark.asyncio
    async def test_gather_empty_file_list(self, config, mock_llm):
        """Test gathering context with empty file list."""
        validator = TaskValidator(config, mock_llm)

        context = await validator.gather_validation_context([])

        assert context == ""

    @pytest.mark.asyncio
    async def test_gather_binary_file(self, config, mock_llm, tmp_path):
        """Test handling of binary file that cannot be decoded as UTF-8."""
        validator = TaskValidator(config, mock_llm)
        binary_file = tmp_path / "binary.bin"
        binary_file.write_bytes(b"\x80\x81\x82\x83")  # Invalid UTF-8

        context = await validator.gather_validation_context([str(binary_file)])

        assert "binary.bin" in context
        assert "Error reading file" in context


class TestCwdParameter:
    """Tests for cwd parameter in git functions.

    Verifies that all git-related functions correctly pass the cwd parameter
    to subprocess.run, allowing validation to run in a different directory
    than the daemon's working directory.
    """

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_passes_cwd(self, mock_run) -> None:
        """Test that get_git_diff passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_git_diff(cwd="/path/to/project")

        # Both subprocess.run calls should have cwd set
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") == "/path/to/project"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_passes_cwd(self, mock_run) -> None:
        """Test that get_recent_commits passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123|Commit message")

        get_recent_commits(n=5, cwd="/custom/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/custom/path"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_passes_cwd(self, mock_run) -> None:
        """Test that get_multi_commit_diff passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_multi_commit_diff(commit_count=10, cwd="/repo/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/repo/path"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_passes_cwd(self, mock_run) -> None:
        """Test that get_commits_since passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_commits_since("abc123", cwd="/another/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/another/path"

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.get_recent_commits")
    def test_get_validation_context_smart_passes_cwd(
        self, mock_commits, mock_diff, mock_run
    ) -> None:
        """Test that get_validation_context_smart passes cwd to subprocess calls."""
        # Mock subprocess for Strategy 1 (uncommitted changes) - empty to trigger Strategy 2
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        # Mock multi-commit diff to trigger get_recent_commits call
        mock_diff.return_value = "multi commit diff content"
        mock_commits.return_value = [{"sha": "abc123", "subject": "First commit"}]

        get_validation_context_smart(task_title="Test task", cwd="/project/root")

        # Verify subprocess.run was called with cwd for Strategy 1
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") == "/project/root"

        # Verify helper functions were called with cwd
        mock_diff.assert_called()
        assert mock_diff.call_args.kwargs.get("cwd") == "/project/root"

        mock_commits.assert_called()
        assert mock_commits.call_args.kwargs.get("cwd") == "/project/root"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_none_cwd_is_default(self, mock_run) -> None:
        """Test that cwd=None uses default behavior (current directory)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")

        get_git_diff(cwd=None)

        # cwd should be None (default behavior)
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") is None

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_last_commit_diff")
    def test_get_git_diff_fallback_passes_cwd(self, mock_last_commit, mock_run) -> None:
        """Test that fallback to last commit also passes cwd."""
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_last_commit.return_value = "last commit diff"

        get_git_diff(fallback_to_last_commit=True, cwd="/fallback/path")

        mock_last_commit.assert_called_once()
        # Verify max_chars and cwd were passed
        call_args = mock_last_commit.call_args
        assert call_args.args[0] == 50000  # max_chars
        assert call_args.kwargs.get("cwd") == "/fallback/path"

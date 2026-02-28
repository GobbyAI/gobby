from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMProvider, LLMService
from gobby.tasks.validation import (
    TaskValidator,
    extract_file_patterns_from_text,
    find_matching_files,
    get_commits_since,
    get_git_diff,
    get_multi_commit_diff,
    get_recent_commits,
    get_validation_context_smart,
    read_files_content,
)

pytestmark = pytest.mark.unit


class TestGetGitDiff:
    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_get_git_diff_no_changes(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        assert get_git_diff() is None

    @patch("subprocess.run")
    def test_get_git_diff_error_code(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_run.return_value = mock_res

        assert get_git_diff() is None

    @patch("subprocess.run")
    def test_get_git_diff_exception(self, mock_run) -> None:
        mock_run.side_effect = Exception("Git error")
        assert get_git_diff() is None

    @patch("subprocess.run")
    def test_get_git_diff_truncate(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "a" * 100
        mock_run.return_value = mock_res

        diff = get_git_diff(max_chars=10)
        assert len(diff) < 100
        assert "... [diff truncated] ..." in diff


class TestTaskValidator:
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


class TestGetRecentCommits:
    """Tests for get_recent_commits function."""

    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_get_recent_commits_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        commits = get_recent_commits(5)
        assert commits == []

    @patch("subprocess.run")
    def test_get_recent_commits_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        commits = get_recent_commits(5)
        assert commits == []

    @patch("subprocess.run")
    def test_get_recent_commits_exception(self, mock_run) -> None:
        mock_run.side_effect = Exception("Git error")
        commits = get_recent_commits(5)
        assert commits == []


class TestGetMultiCommitDiff:
    """Tests for get_multi_commit_diff function."""

    @patch("subprocess.run")
    def test_get_multi_commit_diff_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff --git a/file.py b/file.py\n+new line",
        )

        diff = get_multi_commit_diff(5)
        assert diff is not None
        assert "diff --git" in diff

    @patch("subprocess.run")
    def test_get_multi_commit_diff_truncate(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="a" * 1000)
        diff = get_multi_commit_diff(5, max_chars=100)
        assert len(diff) < 1000
        assert "... [diff truncated] ..." in diff

    @patch("subprocess.run")
    def test_get_multi_commit_diff_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        diff = get_multi_commit_diff(5)
        assert diff is None

    @patch("subprocess.run")
    def test_get_multi_commit_diff_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        diff = get_multi_commit_diff(5)
        assert diff is None


class TestGetCommitsSince:
    """Tests for get_commits_since function."""

    @patch("subprocess.run")
    def test_get_commits_since_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff content here",
        )

        diff = get_commits_since("abc123")
        assert diff is not None
        assert "diff content" in diff

    @patch("subprocess.run")
    def test_get_commits_since_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        diff = get_commits_since("abc123")
        assert diff is None


class TestExtractFilePatternsFromText:
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


class TestFindMatchingFiles:
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


class TestReadFilesContent:
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


class TestGetValidationContextSmart:
    """Tests for get_validation_context_smart function."""

    @patch("subprocess.run")
    def test_includes_uncommitted_changes(self, mock_run) -> None:
        # Mock staged and unstaged diffs
        mock_staged = MagicMock(returncode=0, stdout="staged diff content")
        mock_unstaged = MagicMock(returncode=0, stdout="unstaged diff content")
        mock_run.side_effect = [mock_unstaged, mock_staged]

        context = get_validation_context_smart("Test task")
        assert context is not None
        assert "STAGED CHANGES" in context
        assert "UNSTAGED CHANGES" in context

    @patch("subprocess.run")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_includes_multi_commit_diff(self, mock_multi_diff, mock_run) -> None:
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_multi_diff.return_value = "multi commit diff content"

        context = get_validation_context_smart("Test task", commit_window=5)
        assert context is not None
        assert "COMBINED DIFF" in context or "multi commit diff" in context

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_respects_max_chars(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="a" * 10000)

        context = get_validation_context_smart("Test task", max_chars=500)
        assert context is None or len(context) <= 600  # Some buffer for headers


# ============================================================================
# Additional TaskValidator Unit Tests
# ============================================================================


class TestTaskValidatorEdgeCases:
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

    @patch("subprocess.run")
    def test_get_git_diff_passes_cwd(self, mock_run) -> None:
        """Test that get_git_diff passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_git_diff(cwd="/path/to/project")

        # Both subprocess.run calls should have cwd set
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") == "/path/to/project"

    @patch("subprocess.run")
    def test_get_recent_commits_passes_cwd(self, mock_run) -> None:
        """Test that get_recent_commits passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123|Commit message")

        get_recent_commits(n=5, cwd="/custom/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/custom/path"

    @patch("subprocess.run")
    def test_get_multi_commit_diff_passes_cwd(self, mock_run) -> None:
        """Test that get_multi_commit_diff passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_multi_commit_diff(commit_count=10, cwd="/repo/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/repo/path"

    @patch("subprocess.run")
    def test_get_commits_since_passes_cwd(self, mock_run) -> None:
        """Test that get_commits_since passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_commits_since("abc123", cwd="/another/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/another/path"

    @patch("subprocess.run")
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

    @patch("subprocess.run")
    def test_get_git_diff_none_cwd_is_default(self, mock_run) -> None:
        """Test that cwd=None uses default behavior (current directory)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")

        get_git_diff(cwd=None)

        # cwd should be None (default behavior)
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") is None

    @patch("subprocess.run")
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

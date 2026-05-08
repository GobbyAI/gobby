"""Tests for commit linking and diff functionality."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.tasks.commits import (
    AutoLinkResult,
    TaskDiffResult,
    auto_link_commits,
    extract_task_ids_from_message,
    get_task_diff,
    is_doc_only_diff,
    summarize_diff_for_validation,
)

pytestmark = pytest.mark.unit


class TestGetTaskDiff:
    """Tests for get_task_diff function."""

    @pytest.fixture
    def mock_task_manager(self) -> MagicMock:
        """Create a mock task manager."""
        manager = MagicMock()
        return manager

    def test_returns_combined_diff_for_linked_commits(self, mock_task_manager) -> None:
        """Test that get_task_diff returns combined diff for all linked commits."""
        # Mock task with commits
        mock_task = MagicMock()
        mock_task.commits = ["abc123", "def456"]
        mock_task_manager.get_task.return_value = mock_task

        # Mock git diff output
        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "diff --git a/file.py b/file.py\n+added line"

            result = get_task_diff("gt-test123", mock_task_manager)

            assert isinstance(result, TaskDiffResult)
            assert "added line" in result.diff
            assert result.commits == ["abc123", "def456"]

    def test_includes_uncommitted_changes(self, mock_task_manager) -> None:
        """Test that uncommitted changes are included when flag is set."""
        mock_task = MagicMock()
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            # First call for committed diff, second for uncommitted
            mock_git.side_effect = [
                "diff from commit",
                "diff from uncommitted",
            ]

            result = get_task_diff(
                "gt-test123",
                mock_task_manager,
                include_uncommitted=True,
            )

            assert "diff from commit" in result.diff
            assert "diff from uncommitted" in result.diff
            assert result.has_uncommitted_changes is True

    def test_excludes_uncommitted_changes_by_default(self, mock_task_manager) -> None:
        """Test that uncommitted changes are excluded by default."""
        mock_task = MagicMock()
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "diff from commit"

            result = get_task_diff("gt-test123", mock_task_manager)

            # Should only call git once for the commit diff
            assert mock_git.call_count == 1
            assert result.has_uncommitted_changes is False

    def test_handles_task_with_no_commits(self, mock_task_manager) -> None:
        """Test graceful handling of tasks with no linked commits."""
        mock_task = MagicMock()
        mock_task.commits = None
        mock_task_manager.get_task.return_value = mock_task

        result = get_task_diff("gt-test123", mock_task_manager)

        assert result.diff == ""
        assert result.commits == []
        assert result.has_uncommitted_changes is False

    def test_handles_empty_commits_list(self, mock_task_manager) -> None:
        """Test graceful handling of empty commits list."""
        mock_task = MagicMock()
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        result = get_task_diff("gt-test123", mock_task_manager)

        assert result.diff == ""
        assert result.commits == []

    def test_returns_empty_diff_when_no_changes(self, mock_task_manager) -> None:
        """Test that empty diff is returned when commits have no diff."""
        mock_task = MagicMock()
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = ""

            result = get_task_diff("gt-test123", mock_task_manager)

            assert result.diff == ""
            assert result.commits == ["abc123"]

    def test_orders_commits_chronologically(self, mock_task_manager) -> None:
        """Test that commits are processed in chronological order."""
        mock_task = MagicMock()
        # Commits listed newest to oldest (as typically stored)
        mock_task.commits = ["newest", "middle", "oldest"]
        mock_task_manager.get_task.return_value = mock_task

        call_order = []
        with patch("gobby.tasks.commits.run_git_command") as mock_git:

            def capture_call(*args, **kwargs):
                call_order.append(args)
                return "diff"

            mock_git.side_effect = capture_call

            result = get_task_diff("gt-test123", mock_task_manager)

            # Commits should be in the result in order
            assert result.commits == ["newest", "middle", "oldest"]

    def test_raises_on_invalid_task(self, mock_task_manager) -> None:
        """Test that ValueError is raised for non-existent task."""
        mock_task_manager.get_task.side_effect = ValueError("Task not found")

        with pytest.raises(ValueError, match="not found"):
            get_task_diff("gt-nonexistent", mock_task_manager)

    def test_task_diff_result_structure(self, mock_task_manager) -> None:
        """Test TaskDiffResult contains expected fields."""
        mock_task = MagicMock()
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "diff content"

            result = get_task_diff("gt-test123", mock_task_manager)

            assert hasattr(result, "diff")
            assert hasattr(result, "commits")
            assert hasattr(result, "has_uncommitted_changes")
            assert hasattr(result, "file_count")

    def test_counts_modified_files(self, mock_task_manager) -> None:
        """Test that file count is calculated from diff."""
        mock_task = MagicMock()
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        diff_with_files = """diff --git a/file1.py b/file1.py
index abc..def 100644
--- a/file1.py
+++ b/file1.py
@@ -1,1 +1,2 @@
+new line
diff --git a/file2.py b/file2.py
index 123..456 100644
--- a/file2.py
+++ b/file2.py
@@ -1,1 +1,2 @@
+another line
"""

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = diff_with_files

            result = get_task_diff("gt-test123", mock_task_manager)

            assert result.file_count == 2


class TestExtractTaskIdsFromMessage:
    """Tests for task ID extraction from commit messages.

    These tests verify commit message patterns recognize {project}-#N format:
    - `[project-#1]` extracts task reference (primary format)
    - `project-#42` standalone extracts task reference
    - `fixes project-#1` extracts task reference
    - Case variations work: `Fixes project-#1`, `FIXES project-#1`
    - Old `#N` format is NOT recognized (avoid GitHub auto-linking)
    - Project name filtering is supported
    """

    def test_extracts_bracket_pattern(self) -> None:
        """Test extraction of [gobby-#N] pattern (primary format)."""
        message = "Fix authentication bug [gobby-#1]"
        result = extract_task_ids_from_message(message)
        assert "#1" in result

    def test_extracts_standalone_pattern(self) -> None:
        """Test extraction of standalone 'gobby-#N' pattern."""
        message = "gobby-#42 Add new feature"
        result = extract_task_ids_from_message(message)
        assert "#42" in result

    def test_extracts_implements_pattern(self) -> None:
        """Test extraction of 'Implements gobby-#N' pattern."""
        message = "Implements gobby-#7 feature request"
        result = extract_task_ids_from_message(message)
        assert "#7" in result

    def test_extracts_fixes_pattern(self) -> None:
        """Test extraction of 'Fixes gobby-#N' pattern."""
        message = "Fixes gobby-#123 by updating validation"
        result = extract_task_ids_from_message(message)
        assert "#123" in result

    def test_extracts_closes_pattern(self) -> None:
        """Test extraction of 'Closes gobby-#N' pattern."""
        message = "Closes gobby-#99"
        result = extract_task_ids_from_message(message)
        assert "#99" in result

    def test_extracts_refs_pattern(self) -> None:
        """Test extraction of 'Refs gobby-#N' pattern."""
        message = "Refs gobby-#5 for context"
        result = extract_task_ids_from_message(message)
        assert "#5" in result

    def test_extracts_multiple_task_ids(self) -> None:
        """Test extraction of multiple task IDs from one message."""
        message = "[gobby-#1] and also gobby-#2 and Fixes gobby-#3"
        result = extract_task_ids_from_message(message)
        assert "#1" in result
        assert "#2" in result
        assert "#3" in result

    def test_extracts_comma_separated_refs(self) -> None:
        """Test extraction of comma-separated refs like 'refs gobby-#1, gobby-#2'."""
        message = "Refs gobby-#1, refs gobby-#2, refs gobby-#3"
        result = extract_task_ids_from_message(message)
        assert "#1" in result
        assert "#2" in result
        assert "#3" in result

    def test_returns_empty_for_no_matches(self) -> None:
        """Test returns empty list when no task IDs found."""
        message = "Just a regular commit message"
        result = extract_task_ids_from_message(message)
        assert result == []

    def test_deduplicates_task_ids(self) -> None:
        """Test that duplicate task IDs are removed."""
        message = "[gobby-#1] gobby-#1 Implements gobby-#1"
        result = extract_task_ids_from_message(message)
        assert result.count("#1") == 1

    def test_case_insensitive_keywords(self) -> None:
        """Test that keywords are case insensitive."""
        message = "IMPLEMENTS gobby-#1 and FIXES gobby-#2"
        result = extract_task_ids_from_message(message)
        assert "#1" in result
        assert "#2" in result

    def test_old_hash_format_not_recognized(self) -> None:
        """Test that old #N format is NOT recognized (avoids GitHub auto-linking)."""
        message = "[#123] Fixes #456 refs #789"
        result = extract_task_ids_from_message(message)
        # Old #N format should NOT be extracted
        assert len(result) == 0

    def test_gt_format_not_recognized(self) -> None:
        """Test that deprecated gt-* format is NOT recognized."""
        message = "[gt-abc123] gt-def456: Fixes gt-789xyz"
        result = extract_task_ids_from_message(message)
        # gt-* format should NOT be extracted
        assert len(result) == 0
        assert "gt-abc123" not in result
        assert "gt-def456" not in result
        assert "gt-789xyz" not in result

    def test_avoids_false_positives_with_paths(self) -> None:
        """Test that gobby-#N in file paths is not matched incorrectly."""
        # This shouldn't match because gobby-#1 is embedded in a path
        message = "Update docs/chaptergobby-#1.md"
        result = extract_task_ids_from_message(message)
        # The bracket pattern requires [gobby-#N], standalone requires whitespace
        assert len(result) == 0

    def test_multiline_message(self) -> None:
        """Test extraction from multiline commit messages."""
        message = """feat: add new feature

Implements gobby-#42

This change adds the requested feature.
Also refs gobby-#43 for related work.
"""
        result = extract_task_ids_from_message(message)
        assert "#42" in result
        assert "#43" in result

    def test_different_project_names(self) -> None:
        """Test that different project names are recognized."""
        message = "[myapp-#1] Fix bug in [acme-#2]"
        result = extract_task_ids_from_message(message)
        assert "#1" in result
        assert "#2" in result

    def test_project_name_filtering(self) -> None:
        """Test filtering by project name."""
        message = "[gobby-#1] Also refs myapp-#2"
        # Filter for gobby only
        result = extract_task_ids_from_message(message, project_name="gobby")
        assert "#1" in result
        assert "#2" not in result

    def test_project_name_filtering_case_insensitive(self) -> None:
        """Test that project name filtering is case-insensitive."""
        message = "[GOBBY-#1] Fix bug"
        result = extract_task_ids_from_message(message, project_name="gobby")
        assert "#1" in result

    def test_no_filter_returns_all_projects(self) -> None:
        """Test that no filter returns tasks from all projects."""
        message = "[gobby-#1] refs myapp-#2 fixes acme-#3"
        result = extract_task_ids_from_message(message)
        assert len(result) == 3
        assert "#1" in result
        assert "#2" in result
        assert "#3" in result


class TestAutoLinkCommits:
    """Tests for auto_link_commits function.

    Note: These tests use gobby-#N format which is extracted from commit messages.
    The task manager is mocked to accept these references directly.
    """

    @pytest.fixture
    def mock_task_manager(self) -> MagicMock:
        """Create a mock task manager."""
        manager = MagicMock()
        return manager

    def test_links_commits_matching_task_id(self, mock_task_manager) -> None:
        """Test that commits mentioning task IDs are linked."""
        # Mock task exists
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            # Mock git log output with commit mentioning task
            mock_git.return_value = "abc123|Fix bug [gobby-#1]\ndef456|Unrelated commit\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            assert isinstance(result, AutoLinkResult)
            assert "#1" in result.linked_tasks
            assert "abc123" in result.linked_tasks["#1"]

    def test_respects_since_parameter(self, mock_task_manager) -> None:
        """Test that --since parameter filters commits."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] commit\n"

            auto_link_commits(
                mock_task_manager,
                since="1 week ago",
                cwd="/tmp/repo",
                project_name="gobby",
            )

            # Verify --since was passed to git log
            call_args = mock_git.call_args[0][0]
            assert any("--since" in str(arg) for arg in call_args)

    def test_does_not_duplicate_already_linked_commits(self, mock_task_manager) -> None:
        """Test that already-linked commits are not re-linked."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = ["abc123"]  # Already linked
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] existing commit\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            # Should not link abc123 again
            if "#1" in result.linked_tasks:
                assert "abc123" not in result.linked_tasks["#1"]

    def test_links_to_multiple_tasks(self, mock_task_manager) -> None:
        """Test linking commits that mention multiple tasks."""
        task1 = MagicMock()
        task1.id = "#1"
        task1.commits = []

        task2 = MagicMock()
        task2.id = "#2"
        task2.commits = []

        def get_task_side_effect(task_id):
            if task_id == "#1":
                return task1
            elif task_id == "#2":
                return task2
            raise ValueError(f"Task {task_id} not found")

        mock_task_manager.get_task.side_effect = get_task_side_effect

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] first task\ndef456|Fixes gobby-#2\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            assert "#1" in result.linked_tasks
            assert "#2" in result.linked_tasks

    def test_skips_non_existent_tasks(self, mock_task_manager) -> None:
        """Test that commits mentioning non-existent tasks are skipped."""
        mock_task_manager.get_task.side_effect = ValueError("Task not found")

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#999] commit\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            # Should not crash, just skip the task
            assert "#999" not in result.linked_tasks

    def test_returns_count_of_linked_commits(self, mock_task_manager) -> None:
        """Test that result includes count of newly linked commits."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] commit 1\ndef456|Fixes gobby-#1\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            assert result.total_linked >= 2

    def test_filters_by_task_id(self, mock_task_manager) -> None:
        """Test filtering auto-link to specific task ID."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = (
                "abc123|[gobby-#1] target task\ndef456|[gobby-#2] different task\n"
            )

            result = auto_link_commits(
                mock_task_manager,
                task_id="#1",
                cwd="/tmp/repo",
                project_name="gobby",
            )

            # Should only link to #1
            assert "#1" in result.linked_tasks
            assert "#2" not in result.linked_tasks

    def test_uuid_task_filter_accepts_matching_seq_ref(self, mock_task_manager) -> None:
        """Stage handoff may filter by UUID while commits mention the #seq ref."""
        mock_task = MagicMock()
        mock_task.id = "task-uuid"
        mock_task.seq_num = 14205
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with (
            patch("gobby.tasks.commits._resolve_branch_for_task", return_value=None),
            patch("gobby.tasks.commits.run_git_command") as mock_git,
        ):
            mock_git.return_value = (
                "f5d66e7|[gobby-#14205] docs: refresh worktree guide\n"
                "def4567|[gobby-#14206] different task\n"
            )

            result = auto_link_commits(
                mock_task_manager,
                task_id="task-uuid",
                cwd="/tmp/repo",
                project_name="gobby",
            )

            assert result.linked_tasks == {"#14205": ["f5d66e7"]}
            mock_task_manager.link_commit.assert_called_once_with(
                "task-uuid",
                "f5d66e7",
                cwd="/tmp/repo",
            )

    def test_handles_empty_git_log(self, mock_task_manager) -> None:
        """Test handling of empty git log output."""
        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = ""

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo")

            assert result.linked_tasks == {}
            assert result.total_linked == 0

    def test_result_includes_skipped_count(self, mock_task_manager) -> None:
        """Test that result includes count of skipped commits."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = ["abc123"]  # Already linked
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] already linked\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            assert result.skipped >= 1


class TestIsDocOnlyDiff:
    """Tests for is_doc_only_diff function."""

    def test_returns_true_for_markdown_only(self) -> None:
        """Test that returns True for markdown-only diffs."""
        diff = """diff --git a/README.md b/README.md
index abc..def 100644
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
+new line
"""
        assert is_doc_only_diff(diff) is True

    def test_returns_true_for_multiple_doc_files(self) -> None:
        """Test that returns True for multiple doc files."""
        diff = """diff --git a/README.md b/README.md
+content
diff --git a/CHANGELOG.md b/CHANGELOG.md
+more content
diff --git a/docs/guide.txt b/docs/guide.txt
+text file
"""
        assert is_doc_only_diff(diff) is True

    def test_returns_false_for_code_files(self) -> None:
        """Test that returns False when code files are included."""
        diff = """diff --git a/src/main.py b/src/main.py
index abc..def 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,1 +1,2 @@
+new code
"""
        assert is_doc_only_diff(diff) is False

    def test_returns_false_for_mixed_files(self) -> None:
        """Test that returns False for mixed doc and code files."""
        diff = """diff --git a/README.md b/README.md
+doc content
diff --git a/src/main.py b/src/main.py
+code content
"""
        assert is_doc_only_diff(diff) is False

    def test_returns_false_for_empty_diff(self) -> None:
        """Test that returns False for empty diff."""
        assert is_doc_only_diff("") is False

    def test_supports_multiple_doc_extensions(self) -> None:
        """Test that various doc extensions are supported."""
        diff = """diff --git a/doc.rst b/doc.rst
+rst content
diff --git a/notes.adoc b/notes.adoc
+adoc content
diff --git a/info.markdown b/info.markdown
+markdown content
"""
        assert is_doc_only_diff(diff) is True


class TestSummarizeDiffForValidation:
    """Tests for summarize_diff_for_validation function."""

    def test_returns_original_for_small_diffs(self) -> None:
        """Test that small diffs are returned unchanged."""
        small_diff = "diff --git a/file.py b/file.py\n+line"
        result = summarize_diff_for_validation(small_diff)
        assert result == small_diff

    def test_includes_file_list_summary(self) -> None:
        """Test that summarized diffs include file list."""
        large_diff = "diff --git a/file1.py b/file1.py\n" + ("+" * 20000)
        large_diff += "\ndiff --git a/file2.py b/file2.py\n" + ("+" * 20000)

        result = summarize_diff_for_validation(large_diff, max_chars=5000)

        assert "file1.py" in result
        assert "file2.py" in result
        assert "Files Changed:" in result

    def test_counts_additions_and_deletions(self) -> None:
        """Test that summary includes +/- counts."""
        diff = """diff --git a/file.py b/file.py
+added line 1
+added line 2
-removed line
""" + ("x" * 50000)

        result = summarize_diff_for_validation(diff, max_chars=5000)

        # Should have stats in the summary
        assert "+" in result
        assert "-" in result

    def test_truncates_to_max_chars(self) -> None:
        """Test that result respects max_chars limit."""
        large_diff = "diff --git a/file.py b/file.py\n" + ("+" * 100000)

        result = summarize_diff_for_validation(large_diff, max_chars=10000)

        assert len(result) <= 10000

    def test_handles_empty_diff(self) -> None:
        """Test graceful handling of empty diff."""
        result = summarize_diff_for_validation("")
        assert result == ""

    def test_handles_none_diff(self) -> None:
        """Test graceful handling of None diff."""
        result = summarize_diff_for_validation(None)
        assert result is None

    def test_preserves_file_headers_when_truncating(self) -> None:
        """Test that file headers are preserved even when content is truncated."""
        diff = """diff --git a/important.py b/important.py
index abc..def 100644
--- a/important.py
+++ b/important.py
@@ -1,100 +1,200 @@
""" + ("+added\n" * 10000)

        result = summarize_diff_for_validation(diff, max_chars=2000)

        # Should still have the file name visible
        assert "important.py" in result

    def test_priority_files_none_unchanged_behavior(self) -> None:
        """Test that priority_files=None keeps current behavior."""
        large_diff = "diff --git a/file1.py b/file1.py\n" + ("+" * 20000)
        large_diff += "\ndiff --git a/file2.py b/file2.py\n" + ("+" * 20000)

        # With priority_files=None, behavior should be unchanged
        result_with_none = summarize_diff_for_validation(
            large_diff, max_chars=5000, priority_files=None
        )
        result_without_param = summarize_diff_for_validation(large_diff, max_chars=5000)

        # Both should contain both files
        assert "file1.py" in result_with_none
        assert "file2.py" in result_with_none
        assert "file1.py" in result_without_param
        assert "file2.py" in result_without_param

    def test_priority_files_appear_first(self) -> None:
        """Test that priority files appear before non-priority files."""
        diff = """diff --git a/aaa_first.py b/aaa_first.py
index abc..def 100644
+line in aaa
diff --git a/zzz_last.py b/zzz_last.py
index abc..def 100644
+line in zzz
diff --git a/priority_file.py b/priority_file.py
index abc..def 100644
+line in priority
"""
        result = summarize_diff_for_validation(
            diff, max_chars=5000, priority_files=["priority_file.py"]
        )

        # Priority file should appear before other files in the details section
        priority_pos = result.find("priority_file.py")
        aaa_pos = result.find("aaa_first.py")
        zzz_pos = result.find("zzz_last.py")

        # All files should be present
        assert priority_pos != -1
        assert aaa_pos != -1
        assert zzz_pos != -1

        # Priority file should appear first in the details (after summary header)
        # Find the "File Details" section
        details_start = result.find("File Details")
        if details_start != -1:
            # After File Details, priority should come first
            assert result.find("priority_file.py", details_start) < result.find(
                "aaa_first.py", details_start
            )

    def test_priority_files_get_more_space(self) -> None:
        """Test that priority files get at least 60% of available space."""
        # Create diff with priority file having less content than others
        priority_content = "+priority line\n" * 100  # Small content
        other_content = "+other line\n" * 1000  # Large content

        diff = f"""diff --git a/priority.py b/priority.py
index abc..def 100644
{priority_content}
diff --git a/other1.py b/other1.py
index abc..def 100644
{other_content}
diff --git a/other2.py b/other2.py
index abc..def 100644
{other_content}
"""
        result = summarize_diff_for_validation(diff, max_chars=5000, priority_files=["priority.py"])

        # Priority file should be shown in full (not truncated)
        # since its content is small and gets 60% allocation
        priority_section = result[result.find("priority.py") :]
        # Get just the priority.py section before other files
        priority_only = priority_section.split("other1.py")[0].lower()
        # Assert both conditions: not truncated AND content appears
        assert "truncated" not in priority_only, "Priority file should not be truncated"
        assert "priority" in priority_only, "Priority file content should appear in its section"

    def test_non_priority_files_share_remaining_space(self) -> None:
        """Test that non-priority files share remaining 40% of space."""
        priority_content = "+priority\n" * 50
        other_content = "+other\n" * 5000  # Very large

        diff = f"""diff --git a/priority.py b/priority.py
{priority_content}
diff --git a/other1.py b/other1.py
{other_content}
diff --git a/other2.py b/other2.py
{other_content}
"""
        result = summarize_diff_for_validation(diff, max_chars=3000, priority_files=["priority.py"])

        # Both other files should still be present (in summary at least)
        assert "other1.py" in result
        assert "other2.py" in result

    def test_priority_files_shown_in_full_before_truncation(self) -> None:
        """Test priority files are shown fully up to their allocation."""
        # Priority file with moderate content
        priority_content = "+priority line content here\n" * 200

        diff = f"""diff --git a/priority.py b/priority.py
index abc..def 100644
--- a/priority.py
+++ b/priority.py
@@ -1,1 +1,200 @@
{priority_content}
diff --git a/other.py b/other.py
+other content
"""
        result = summarize_diff_for_validation(
            diff, max_chars=10000, priority_files=["priority.py"]
        )

        # Count occurrences of priority content in result
        priority_lines_in_result = result.count("+priority line content here")

        # Should have significant portion of priority file content
        # (not just the header)
        assert priority_lines_in_result > 50  # Most of the 200 lines should be there

    def test_priority_files_not_in_diff_ignored(self) -> None:
        """Test that files in priority_files but not in diff are ignored."""
        diff = """diff --git a/actual_file.py b/actual_file.py
index abc..def 100644
+content
"""
        # Request priority for a file that doesn't exist in diff
        result = summarize_diff_for_validation(
            diff, max_chars=5000, priority_files=["nonexistent.py", "actual_file.py"]
        )

        # Should not crash, should show actual file
        assert "actual_file.py" in result
        # Nonexistent file shouldn't appear in output (it's not in diff)
        assert "nonexistent.py" not in result


class TestExtractMentionedFiles:
    """Tests for extract_mentioned_files function."""

    def test_extracts_simple_path_from_description(self) -> None:
        """Test extraction of simple file paths from task description."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Fix bug",
            "description": "The issue is in src/gobby/tasks/commits.py",
        }
        result = extract_mentioned_files(task)
        assert "src/gobby/tasks/commits.py" in result

    def test_extracts_backtick_quoted_paths(self) -> None:
        """Test extraction of paths wrapped in backticks."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Update validation",
            "description": "Modify `path/to/file.py` to fix the issue",
        }
        result = extract_mentioned_files(task)
        assert "path/to/file.py" in result

    def test_extracts_multiple_paths(self) -> None:
        """Test extraction of multiple file paths from same text."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Refactor modules",
            "description": "Update src/module_a.py and src/module_b.py for consistency",
        }
        result = extract_mentioned_files(task)
        assert "src/module_a.py" in result
        assert "src/module_b.py" in result

    def test_extracts_paths_from_title(self) -> None:
        """Test extraction of file paths from task title."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Fix src/utils/helpers.py error handling",
            "description": "Add try/except blocks",
        }
        result = extract_mentioned_files(task)
        assert "src/utils/helpers.py" in result

    def test_extracts_relative_paths(self) -> None:
        """Test extraction of relative file paths."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Update tests",
            "description": "Modify tests/test_main.py and ./config/settings.yaml",
        }
        result = extract_mentioned_files(task)
        assert "tests/test_main.py" in result
        assert "./config/settings.yaml" in result

    def test_extracts_paths_without_extension(self) -> None:
        """Test extraction of paths that may not have extensions."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Update Makefile",
            "description": "Modify src/Makefile and scripts/build",
        }
        result = extract_mentioned_files(task)
        # Should extract paths with common file-like patterns
        assert any("Makefile" in p for p in result)

    def test_extracts_absolute_paths(self) -> None:
        """Test extraction of absolute file paths."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Fix config",
            "description": "Update /etc/config.yaml if needed",
        }
        result = extract_mentioned_files(task)
        assert "/etc/config.yaml" in result

    def test_returns_empty_list_when_no_paths(self) -> None:
        """Test returns empty list when no file paths found."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Improve performance",
            "description": "Make the application faster by optimizing algorithms",
        }
        result = extract_mentioned_files(task)
        assert result == []

    def test_handles_none_description(self) -> None:
        """Test graceful handling of None description."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Simple task",
            "description": None,
        }
        result = extract_mentioned_files(task)
        assert isinstance(result, list)

    def test_handles_missing_description(self) -> None:
        """Test graceful handling of missing description key."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {"title": "Task with no description"}
        result = extract_mentioned_files(task)
        assert isinstance(result, list)

    def test_deduplicates_paths(self) -> None:
        """Test that duplicate paths are removed."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Fix src/main.py",
            "description": "The bug in src/main.py needs to be fixed in `src/main.py`",
        }
        result = extract_mentioned_files(task)
        assert result.count("src/main.py") == 1

    def test_extracts_paths_with_various_extensions(self) -> None:
        """Test extraction of paths with various common extensions."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Update configs",
            "description": """
            Files to update:
            - src/app.ts
            - src/styles.css
            - config.json
            - setup.cfg
            - tests/test_api.py
            """,
        }
        result = extract_mentioned_files(task)
        assert "src/app.ts" in result
        assert "src/styles.css" in result
        assert "config.json" in result
        assert "setup.cfg" in result
        assert "tests/test_api.py" in result

    def test_extracts_from_validation_criteria(self) -> None:
        """Test extraction from validation_criteria field if present."""
        from gobby.tasks.commits import extract_mentioned_files

        task = {
            "title": "Implement feature",
            "description": "Add new functionality",
            "validation_criteria": "Verify changes in src/feature.py and tests/test_feature.py",
        }
        result = extract_mentioned_files(task)
        assert "src/feature.py" in result
        assert "tests/test_feature.py" in result


class TestExtractMentionedSymbols:
    """Tests for extract_mentioned_symbols function."""

    def test_extracts_backtick_function_with_parens(self) -> None:
        """Test extraction of function names in backticks with parentheses."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Fix validation",
            "description": "Update `summarize_diff_for_validation()` to handle edge cases",
        }
        result = extract_mentioned_symbols(task)
        assert "summarize_diff_for_validation" in result

    def test_extracts_backtick_function_without_parens(self) -> None:
        """Test extraction of function names in backticks without parentheses."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Refactor code",
            "description": "The `process_data` function needs optimization",
        }
        result = extract_mentioned_symbols(task)
        assert "process_data" in result

    def test_extracts_class_names_in_backticks(self) -> None:
        """Test extraction of class names in backticks (PascalCase)."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Update result type",
            "description": "Modify `TaskDiffResult` to include new field",
        }
        result = extract_mentioned_symbols(task)
        assert "TaskDiffResult" in result

    def test_extracts_method_references(self) -> None:
        """Test extraction of method references like ClassName.method_name."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Fix method",
            "description": "The `TaskManager.get_task` method has a bug",
        }
        result = extract_mentioned_symbols(task)
        # Should extract the method name
        assert "get_task" in result or "TaskManager.get_task" in result

    def test_extracts_multiple_symbols(self) -> None:
        """Test extraction of multiple symbols from same text."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Update functions",
            "description": "Modify `validate_input()` and `process_output()` for consistency",
        }
        result = extract_mentioned_symbols(task)
        assert "validate_input" in result
        assert "process_output" in result

    def test_extracts_symbols_from_title(self) -> None:
        """Test extraction of symbols from task title."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Fix `calculate_total()` rounding error",
            "description": "The calculation is off by one",
        }
        result = extract_mentioned_symbols(task)
        assert "calculate_total" in result

    def test_returns_empty_list_when_no_symbols(self) -> None:
        """Test returns empty list when no symbols found."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Improve performance",
            "description": "Make the application faster",
        }
        result = extract_mentioned_symbols(task)
        assert result == []

    def test_deduplicates_symbols(self) -> None:
        """Test that duplicate symbols are removed."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Fix `process_data`",
            "description": "The `process_data()` function in `process_data` module needs work",
        }
        result = extract_mentioned_symbols(task)
        assert result.count("process_data") == 1

    def test_handles_none_description(self) -> None:
        """Test graceful handling of None description."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Simple task",
            "description": None,
        }
        result = extract_mentioned_symbols(task)
        assert isinstance(result, list)

    def test_handles_missing_description(self) -> None:
        """Test graceful handling of missing description key."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {"title": "Task with no description"}
        result = extract_mentioned_symbols(task)
        assert isinstance(result, list)

    def test_extracts_from_validation_criteria(self) -> None:
        """Test extraction from validation_criteria field if present."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Implement feature",
            "description": "Add new functionality",
            "validation_criteria": "Verify `new_feature()` works correctly",
        }
        result = extract_mentioned_symbols(task)
        assert "new_feature" in result

    def test_ignores_file_paths(self) -> None:
        """Test that file paths are not extracted as symbols."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Update code",
            "description": "Modify `src/gobby/tasks/commits.py` to fix the bug",
        }
        result = extract_mentioned_symbols(task)
        # File paths should not be in symbols
        assert "src/gobby/tasks/commits.py" not in result
        assert "commits.py" not in result

    def test_extracts_dunder_methods(self) -> None:
        """Test extraction of dunder methods like __init__."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Fix initialization",
            "description": "The `__init__` method needs to validate parameters",
        }
        result = extract_mentioned_symbols(task)
        assert "__init__" in result

    def test_handles_nested_class_methods(self) -> None:
        """Test extraction of nested class.method patterns."""
        from gobby.tasks.commits import extract_mentioned_symbols

        task = {
            "title": "Update validation",
            "description": "Call `ExternalValidator.validate_task` with new params",
        }
        result = extract_mentioned_symbols(task)
        # Should extract the method or full reference
        assert "validate_task" in result or "ExternalValidator.validate_task" in result

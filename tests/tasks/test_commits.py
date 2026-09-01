"""Tests for commit linking and diff functionality."""

from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest

from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.commits import (
    AutoLinkResult,
    auto_link_commits,
    extract_task_ids_from_message,
    resolve_task_tagged_commits,
)

pytestmark = pytest.mark.unit


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

    @pytest.mark.parametrize("project_name", ["gobby-pro", "gobby pro"])
    def test_project_names_with_separators_in_all_formats(self, project_name: str) -> None:
        """Test task references for project names containing hyphens or spaces."""
        message = f"[{project_name}-#7]\n{project_name}-#8\nFixes {project_name}-#9"
        result = extract_task_ids_from_message(message, project_name=project_name)
        assert set(result) == {"#7", "#8", "#9"}

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

    @pytest.mark.parametrize("project_name", ["gobby", "gobby-pro"])
    def test_links_commits_matching_task_id(
        self,
        mock_task_manager: MagicMock,
        project_name: str,
    ) -> None:
        """Test that commits mentioning task IDs are linked."""
        # Mock task exists
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            # Mock git log output with commit mentioning task
            mock_git.return_value = f"abc123|Fix bug [{project_name}-#1]\ndef456|Unrelated commit\n"

            result = auto_link_commits(
                mock_task_manager, cwd="/tmp/repo", project_name=project_name
            )

            assert isinstance(result, AutoLinkResult)
            assert "#1" in result.linked_tasks
            assert "abc123" in result.linked_tasks["#1"]

    def test_respects_since_parameter(self, mock_task_manager: MagicMock) -> None:
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

    def test_does_not_duplicate_already_linked_commits(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
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

    def test_links_to_multiple_tasks(self, mock_task_manager: MagicMock) -> None:
        """Test linking commits that mention multiple tasks."""
        task1 = MagicMock()
        task1.id = "#1"
        task1.commits = []

        task2 = MagicMock()
        task2.id = "#2"
        task2.commits = []

        def get_task_side_effect(task_id: str) -> MagicMock:
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

    def test_skips_non_existent_tasks(self, mock_task_manager: MagicMock) -> None:
        """Test that commits mentioning non-existent tasks are skipped."""
        mock_task_manager.get_task.side_effect = ValueError("Task not found")

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#999] commit\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            # Should not crash, just skip the task
            assert "#999" not in result.linked_tasks
            assert result.skipped_refs == {"#999": ["abc123"]}

    def test_mixed_history_links_valid_refs_and_reports_unknown_refs(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Unknown project-scoped refs do not abort linking later valid refs."""
        task1 = MagicMock(id="task-1", commits=[])
        task2 = MagicMock(id="task-2", commits=[])

        def resolve_task_reference(task_ref: str, project_id: str) -> str:
            assert project_id == "project-1"
            if task_ref == "#1":
                return cast(str, task1.id)
            if task_ref == "#2":
                return cast(str, task2.id)
            raise TaskNotFoundError(f"Task {task_ref} not found in project")

        mock_task_manager.resolve_task_reference.side_effect = resolve_task_reference
        mock_task_manager.get_task.side_effect = lambda task_id: {
            task1.id: task1,
            task2.id: task2,
        }[task_id]

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = (
                "abc123|[gobby-#1] first valid task\n"
                "def456|[gobby-#999] removed task\n"
                "fed987|Fixes gobby-#2"
            )

            result = auto_link_commits(
                mock_task_manager,
                cwd="/tmp/repo",
                project_name="gobby",
                project_id="project-1",
            )

        assert result.linked_tasks == {"#1": ["abc123"], "#2": ["fed987"]}
        assert result.total_linked == 2
        assert result.skipped == 1
        assert result.skipped_refs == {"#999": ["def456"]}
        assert mock_task_manager.link_commit.call_args_list == [
            call(task1.id, "abc123", cwd="/tmp/repo"),
            call(task2.id, "fed987", cwd="/tmp/repo"),
        ]

    def test_returns_count_of_linked_commits(self, mock_task_manager: MagicMock) -> None:
        """Test that result includes count of newly linked commits."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] commit 1\ndef456|Fixes gobby-#1\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            assert result.total_linked >= 2

    def test_filters_by_task_id(self, mock_task_manager: MagicMock) -> None:
        """Test filtering auto-link to specific task ID."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with (
            patch("gobby.tasks.commits._resolve_branch_for_task", return_value=None),
            patch("gobby.tasks.commits.run_git_command") as mock_git,
        ):
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

    def test_task_filter_reports_unknown_refs_before_linking_target(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        task = MagicMock(id="task-1", seq_num=1, commits=[])
        mock_task_manager.get_task.side_effect = lambda task_id: (
            task
            if task_id == "task-1"
            else (_ for _ in ()).throw(TaskNotFoundError(f"Task {task_id} not found"))
        )
        mock_task_manager.resolve_task_reference.side_effect = lambda task_ref, _project_id: (
            "task-1"
            if task_ref == "#1"
            else (_ for _ in ()).throw(TaskNotFoundError(f"Task {task_ref} not found"))
        )

        with (
            patch("gobby.tasks.commits._resolve_branch_for_task", return_value=None),
            patch("gobby.tasks.commits.run_git_command") as mock_git,
        ):
            mock_git.return_value = (
                "bad111|[gobby-#999] removed task\ngood22|[gobby-#1] target task\n"
            )
            result = auto_link_commits(
                mock_task_manager,
                task_id="#1",
                cwd="/tmp/repo",
                project_name="gobby",
                project_id="project-1",
            )

        assert result.skipped_refs == {"#999": ["bad111"]}
        assert result.linked_tasks == {"#1": ["good22"]}
        assert result.total_linked == 1
        mock_task_manager.link_commit.assert_called_once_with(
            "task-1",
            "good22",
            cwd="/tmp/repo",
        )

    def test_task_filter_reuses_resolved_task(self, mock_task_manager: MagicMock) -> None:
        task = MagicMock(id="task-1", seq_num=1, commits=[])
        mock_task_manager.get_task.return_value = task

        with (
            patch("gobby.tasks.commits._resolve_branch_for_task", return_value=None),
            patch("gobby.tasks.commits.run_git_command", return_value="abc123|[gobby-#1] fix"),
        ):
            result = auto_link_commits(
                mock_task_manager,
                task_id="task-1",
                cwd="/tmp/repo",
                project_name="gobby",
            )

        assert result.total_linked == 1
        mock_task_manager.get_task.assert_called_once_with("task-1")

    def test_task_filter_uses_resolved_uuid_for_branch_lookup(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = MagicMock(id="task-uuid", seq_num=1, commits=[])
        mock_task_manager.resolve_task_reference.return_value = task.id
        mock_task_manager.get_task.return_value = task

        with (
            patch(
                "gobby.tasks.commits._resolve_branch_for_task", return_value=None
            ) as resolve_branch,
            patch(
                "gobby.tasks.commits.run_git_command",
                return_value="abc123|[gobby-#1] fix",
            ),
        ):
            result = auto_link_commits(
                mock_task_manager,
                task_id="#1",
                cwd="/tmp/repo",
                project_name="gobby",
                project_id="project-uuid",
            )

        assert result.linked_tasks == {"#1": ["abc123"]}
        assert result.total_linked == 1
        resolve_branch.assert_called_once_with(mock_task_manager, task.id)

    def test_read_only_resolver_returns_ordered_task_commits_without_linking(
        self, mock_task_manager: MagicMock
    ) -> None:
        task = MagicMock(id="task-uuid", seq_num=42, commits=["existing"])
        mock_task_manager.get_task.return_value = task

        with (
            patch("gobby.tasks.commits._resolve_branch_for_task", return_value="feature"),
            patch("gobby.tasks.commits.run_git_command") as mock_git,
        ):
            mock_git.return_value = (
                "old111|[gobby-#42] first\nskip22|[gobby-#43] other task\nnew333|Fixes gobby-#42\n"
            )

            result = resolve_task_tagged_commits(
                mock_task_manager,
                task_id="task-uuid",
                since="2026-07-01T00:00:00+00:00",
                cwd="/tmp/repo",
                project_name="gobby",
            )

        assert result == ["old111", "new333"]
        assert mock_git.call_args.args[0] == [
            "git",
            "log",
            "--reverse",
            "--pretty=format:%h|%s",
            "feature",
            "--since=2026-07-01T00:00:00+00:00",
        ]
        mock_task_manager.link_commit.assert_not_called()
        mock_task_manager.update_task.assert_not_called()

    def test_uuid_task_filter_accepts_matching_seq_ref(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
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

    def test_handles_empty_git_log(self, mock_task_manager: MagicMock) -> None:
        """Test handling of empty git log output."""
        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = ""

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo")

            assert result.linked_tasks == {}
            assert result.total_linked == 0

    def test_result_includes_skipped_count(self, mock_task_manager: MagicMock) -> None:
        """Test that result includes count of skipped commits."""
        mock_task = MagicMock()
        mock_task.id = "#1"
        mock_task.commits = ["abc123"]  # Already linked
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.tasks.commits.run_git_command") as mock_git:
            mock_git.return_value = "abc123|[gobby-#1] already linked\n"

            result = auto_link_commits(mock_task_manager, cwd="/tmp/repo", project_name="gobby")

            assert result.skipped >= 1


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
            "description": "Update `collect_commit_diff_text()` to handle edge cases",
        }
        result = extract_mentioned_symbols(task)
        assert "collect_commit_diff_text" in result

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

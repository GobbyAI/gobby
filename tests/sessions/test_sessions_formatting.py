"""Tests for session formatting helpers (format_handoff_as_markdown).

Relocated from tests/workflows/test_context_actions.py as part of dead-code cleanup.
"""

import pytest

from gobby.sessions.analyzer import HandoffContext
from gobby.sessions.formatting import format_handoff_as_markdown

pytestmark = pytest.mark.unit


class TestFormatHandoffAsMarkdown:
    """Tests for the format_handoff_as_markdown function."""

    def test_empty_context_returns_empty_string(self) -> None:
        """Should return empty string when all context fields are empty."""
        ctx = HandoffContext()
        result = format_handoff_as_markdown(ctx)
        assert result == ""

    def test_formats_active_task(self) -> None:
        """Should format active task section."""
        ctx = HandoffContext(
            active_gobby_task={
                "id": "gt-123",
                "title": "Fix auth bug",
                "state": {"current_stage": {"state": "in_progress"}},
            }
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Active Task" in result
        assert "**Fix auth bug** (gt-123)" in result
        assert "State: in_progress" in result

    def test_formats_active_task_with_missing_fields(self) -> None:
        """Should handle missing fields in active task with defaults."""
        ctx = HandoffContext(active_gobby_task={"some_field": "value"})
        result = format_handoff_as_markdown(ctx)

        assert "### Active Task" in result
        assert "**Untitled** (unknown)" in result
        assert "State: unknown" in result

    def test_formats_worktree_context(self) -> None:
        """Should format worktree context section."""
        ctx = HandoffContext(
            active_worktree={
                "branch_name": "feature/auth",
                "worktree_path": "/path/to/worktree",
                "base_branch": "main",
                "task_id": "gt-123",
            }
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Worktree Context" in result
        assert "**Branch**: `feature/auth`" in result
        assert "**Path**: `/path/to/worktree`" in result
        assert "**Base**: `main`" in result
        assert "**Task**: gt-123" in result

    def test_formats_worktree_without_task_id(self) -> None:
        """Should format worktree without task_id."""
        ctx = HandoffContext(
            active_worktree={
                "branch_name": "feature/auth",
                "worktree_path": "/path",
                "base_branch": "main",
            }
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Worktree Context" in result
        assert "**Task**" not in result

    def test_formats_git_commits(self) -> None:
        """Should format git commits section."""
        ctx = HandoffContext(
            git_commits=[
                {"hash": "abc123def456", "message": "feat: add feature"},
                {"hash": "789xyz", "message": "fix: bug fix"},
            ]
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Commits This Session" in result
        assert "- `abc123d` feat: add feature" in result
        assert "- `789xyz` fix: bug fix" in result

    def test_formats_git_status(self) -> None:
        """Should format git status section."""
        ctx = HandoffContext(git_status="M src/file.py\nA new_file.py")
        result = format_handoff_as_markdown(ctx)

        assert "### Uncommitted Changes" in result
        assert "```\nM src/file.py\nA new_file.py\n```" in result

    def test_formats_files_modified(self) -> None:
        """Should format files modified section only for uncommitted files."""
        ctx = HandoffContext(
            files_modified=["src/auth.py", "tests/test_auth.py"],
            git_status="M src/auth.py\nM tests/test_auth.py",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Files Being Modified" in result
        assert "- src/auth.py" in result
        assert "- tests/test_auth.py" in result

    def test_files_modified_filters_committed_files(self) -> None:
        """Should not show files that are no longer in git status (committed)."""
        ctx = HandoffContext(
            files_modified=["src/auth.py", "tests/test_auth.py"],
            git_status="M tests/test_auth.py",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Files Being Modified" in result
        assert "- src/auth.py" not in result
        assert "- tests/test_auth.py" in result

    def test_files_modified_not_shown_without_git_status(self) -> None:
        """Should not show files modified section if git_status is empty."""
        ctx = HandoffContext(
            files_modified=["src/auth.py", "tests/test_auth.py"],
            git_status="",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Files Being Modified" not in result

    def test_formats_initial_goal(self) -> None:
        """Should format initial goal section when no task or task is active."""
        ctx = HandoffContext(initial_goal="Implement user authentication")
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result
        assert "Implement user authentication" in result

    def test_initial_goal_shown_for_ready_task(self) -> None:
        """Should show initial goal when task state is ready."""
        ctx = HandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={
                "id": "gt-123",
                "title": "Fix bug",
                "state": {"current_stage": {"state": "ready"}},
            },
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result

    def test_initial_goal_shown_for_in_progress_task(self) -> None:
        """Should show initial goal when task state is in_progress."""
        ctx = HandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={
                "id": "gt-123",
                "title": "Fix bug",
                "state": {"current_stage": {"state": "in_progress"}},
            },
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result

    def test_initial_goal_hidden_for_closed_task(self) -> None:
        """Should not show initial goal when task is closed."""
        ctx = HandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={"id": "gt-123", "title": "Fix bug", "state": {"is_closed": True}},
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" not in result

    def test_initial_goal_hidden_for_review_task(self) -> None:
        """Should not show initial goal when task is in review."""
        ctx = HandoffContext(
            initial_goal="Fix the bug",
            active_gobby_task={
                "id": "gt-123",
                "title": "Fix bug",
                "state": {"current_stage": {"state": "needs_review"}},
            },
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" not in result

    def test_formats_recent_activity(self) -> None:
        """Should format recent activity section with max 5 items."""
        ctx = HandoffContext(
            recent_activity=[
                "Activity 1",
                "Activity 2",
                "Activity 3",
                "Activity 4",
                "Activity 5",
                "Activity 6",
                "Activity 7",
            ]
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Recent Activity" in result
        assert "- Activity 1" in result
        assert "- Activity 2" in result
        assert "- Activity 3" in result
        assert "- Activity 4" in result
        assert "- Activity 5" in result
        assert "- Activity 6" not in result
        assert "- Activity 7" not in result

    def test_formats_multiple_sections(self) -> None:
        """Should format multiple sections separated by double newlines."""
        ctx = HandoffContext(
            initial_goal="Fix the bug",
            git_status="M file.py",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" in result
        assert "### Uncommitted Changes" in result
        assert "\n\n" in result

    def test_prompt_template_parameter_is_ignored(self) -> None:
        """Should ignore prompt_template parameter (reserved for future)."""
        ctx = HandoffContext(initial_goal="Goal")
        result = format_handoff_as_markdown(ctx, prompt_template="custom template")

        assert "### Original Goal" in result
        assert "Goal" in result

    def test_handles_empty_strings_in_context(self) -> None:
        """Should not include sections with empty strings."""
        ctx = HandoffContext(
            initial_goal="",
            git_status="",
        )
        result = format_handoff_as_markdown(ctx)

        assert "### Original Goal" not in result
        assert "### Uncommitted Changes" not in result

    def test_handles_commit_with_empty_hash(self) -> None:
        """Should handle commits with empty hash gracefully."""
        ctx = HandoffContext(git_commits=[{"hash": "", "message": "test commit"}])
        result = format_handoff_as_markdown(ctx)

        assert "### Commits This Session" in result
        assert "- test commit" in result

    def test_active_skills_section_removed(self) -> None:
        """Active skills section was removed - redundant with _build_skill_injection_context()."""
        ctx = HandoffContext(git_commits=[{"hash": "abc1234", "message": "test"}])
        result = format_handoff_as_markdown(ctx)

        assert "### Active Skills" not in result
        assert "Skills available:" not in result

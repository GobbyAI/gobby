"""
Tests for task_sync.py MCP tools module (commit linking tools).

Sync tools (sync_tasks, get_sync_status, sync_import, sync_export) have been
removed from MCP — they are CLI-only operations.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestLinkCommit:
    """Tests for link_commit MCP tool."""

    def test_link_commit_success(self, mock_sync_registry) -> None:
        """Test successful commit linking."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.commits = ["abc123"]
        task_manager.link_commit.return_value = mock_task

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        link = registry.get_tool("link_commit")
        result = link(task_id="task-1", commit_sha="abc123")

        assert result["task_id"] == "task-1"
        assert "abc123" in result["commits"]
        task_manager.link_commit.assert_called_once_with("task-1", "abc123", cwd=None)

    def test_link_commit_error(self, mock_sync_registry) -> None:
        """Test link_commit returns error on failure."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        task_manager.link_commit.side_effect = ValueError("Task not found")

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        link = registry.get_tool("link_commit")
        result = link(task_id="task-1", commit_sha="abc123")

        assert "error" in result
        assert "Task not found" in result["error"]

    def test_link_commit_task_not_found_after_resolution(self, mock_sync_registry) -> None:
        """Resolved missing tasks return structured errors before Git work."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        task_manager.get_task.side_effect = [MagicMock(), None]

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        link = registry.get_tool("link_commit")
        result = link(task_id="task-uuid", commit_sha="abc123")

        assert result == {"error": "Task task-uuid not found"}
        task_manager.link_commit.assert_not_called()

    def test_link_commit_empty_commits_list(self, mock_sync_registry) -> None:
        """Test link_commit when task had no previous commits."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.commits = None  # No commits yet
        task_manager.link_commit.return_value = mock_task

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        link = registry.get_tool("link_commit")
        result = link(task_id="task-1", commit_sha="abc123")

        # Should handle None commits gracefully
        assert result["commits"] == []

    def test_link_commit_uses_registered_project_path_override(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Explicit commit repos must be registered for the task project."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "external" / "repo"
        repo_path.mkdir(parents=True)
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        mock_task.commits = ["abc123"]
        task_manager.get_task.return_value = mock_task
        task_manager.link_commit.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
        )

        link = registry.get_tool("link_commit")
        result = link(
            task_id="task-1",
            commit_sha="abc123",
            project_path=str(repo_path),
        )

        assert result["commits"] == ["abc123"]
        assert "error" not in result
        task_manager.get_task.assert_any_call("task-1")
        project_manager.get.assert_any_call("project-1")
        task_manager.link_commit.assert_called_once_with(
            "task-1",
            "abc123",
            cwd=str(repo_path),
        )

    def test_link_commit_rejects_unknown_project_path(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Unknown explicit repo paths are rejected before commit linking."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "repo"
        outside = tmp_path / "outside"
        repo_path.mkdir()
        outside.mkdir()
        task_manager = MagicMock()
        mock_task = MagicMock(id="task-1", project_id="project-1", commits=[])
        mock_task.parent_task_id = None
        task_manager.get_task.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
        )

        link = registry.get_tool("link_commit")
        result = link(task_id="task-1", commit_sha="abc123", project_path=str(outside))

        assert "error" in result
        assert "outside the task project repo" in result["error"]
        task_manager.link_commit.assert_not_called()


class TestUnlinkCommit:
    """Tests for unlink_commit MCP tool."""

    def test_unlink_commit_success(self, mock_sync_registry) -> None:
        """Test successful commit unlinking."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.commits = []  # After unlink
        task_manager.unlink_commit.return_value = mock_task

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        unlink = registry.get_tool("unlink_commit")
        result = unlink(task_id="task-1", commit_sha="abc123")

        assert result["task_id"] == "task-1"
        assert result["commits"] == []
        task_manager.unlink_commit.assert_called_once_with("task-1", "abc123", cwd=None)

    def test_unlink_commit_error(self, mock_sync_registry) -> None:
        """Test unlink_commit returns error on failure."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        task_manager.unlink_commit.side_effect = ValueError("Commit not linked")

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        unlink = registry.get_tool("unlink_commit")
        result = unlink(task_id="task-1", commit_sha="abc123")

        assert "error" in result
        assert "Commit not linked" in result["error"]

    def test_unlink_commit_task_not_found_after_resolution(self, mock_sync_registry) -> None:
        """Resolved missing tasks return structured errors before Git work."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        task_manager.get_task.side_effect = [MagicMock(), None]

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        unlink = registry.get_tool("unlink_commit")
        result = unlink(task_id="task-uuid", commit_sha="abc123")

        assert result == {"error": "Task task-uuid not found"}
        task_manager.unlink_commit.assert_not_called()

    def test_unlink_commit_uses_registered_project_path_override(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Explicit unlink repos must be registered for the task project."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "external" / "repo"
        repo_path.mkdir(parents=True)
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        mock_task.commits = []
        task_manager.get_task.return_value = mock_task
        task_manager.unlink_commit.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
        )

        unlink = registry.get_tool("unlink_commit")
        result = unlink(
            task_id="task-1",
            commit_sha="abc123",
            project_path=str(repo_path),
        )

        assert result["commits"] == []
        assert "error" not in result
        task_manager.get_task.assert_any_call("task-1")
        project_manager.get.assert_any_call("project-1")
        task_manager.unlink_commit.assert_called_once_with(
            "task-1",
            "abc123",
            cwd=str(repo_path),
        )

    def test_unlink_commit_rejects_unknown_project_path_before_git(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Unknown explicit repo paths are rejected before unlink Git work."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "repo"
        outside = tmp_path / "outside"
        repo_path.mkdir()
        outside.mkdir()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        mock_task.commits = ["abc123"]
        task_manager.get_task.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
        )

        unlink = registry.get_tool("unlink_commit")
        result = unlink(task_id="task-1", commit_sha="abc123", project_path=str(outside))

        assert "error" in result
        assert "outside the task project repo" in result["error"]
        task_manager.unlink_commit.assert_not_called()


class TestAutoLinkCommits:
    """Tests for auto_link_commits MCP tool."""

    def test_auto_link_commits_basic(self, mock_sync_registry) -> None:
        """Test auto_link_commits basic call."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        project_manager = MagicMock()
        mock_project = MagicMock()
        mock_project.repo_path = "/path/to/repo"
        project_manager.get.return_value = mock_project

        mock_result = MagicMock()
        mock_result.linked_tasks = ["task-1", "task-2"]
        mock_result.total_linked = 2
        mock_result.skipped = []

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            auto_link_commits_fn=MagicMock(return_value=mock_result),
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link()

        assert result["total_linked"] == 2
        assert "task-1" in result["linked_tasks"]
        assert "task-2" in result["linked_tasks"]

    def test_auto_link_commits_with_task_filter(self, mock_sync_registry) -> None:
        """Test auto_link_commits with task_id filter."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_fn = MagicMock()
        mock_result = MagicMock()
        mock_result.linked_tasks = ["task-1"]
        mock_result.total_linked = 1
        mock_result.skipped = []
        mock_fn.return_value = mock_result

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            auto_link_commits_fn=mock_fn,
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link(task_id="task-1")

        # Verify task_id was passed
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["task_id"] == "task-1"
        assert result["linked_tasks"] == ["task-1"]

    def test_auto_link_commits_task_filter_uses_registered_project_path_override(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Task-filtered auto-link supports registered explicit repo paths."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "external" / "repo"
        repo_path.mkdir(parents=True)
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        task_manager.get_task.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))

        mock_fn = MagicMock()
        mock_result = MagicMock()
        mock_result.linked_tasks = ["task-1"]
        mock_result.total_linked = 1
        mock_result.skipped = []
        mock_fn.return_value = mock_result

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            auto_link_commits_fn=mock_fn,
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link(task_id="task-1", project_path=str(repo_path))

        assert result["linked_tasks"] == ["task-1"]
        assert mock_fn.call_args.kwargs["cwd"] == str(repo_path)

    def test_auto_link_commits_task_filter_rejects_unknown_project_path_before_git(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Task-filtered auto-link rejects unknown explicit repo paths before Git scan."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "repo"
        outside = tmp_path / "outside"
        repo_path.mkdir()
        outside.mkdir()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        task_manager.get_task.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))
        mock_fn = MagicMock()

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            auto_link_commits_fn=mock_fn,
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link(task_id="task-1", project_path=str(outside))

        assert "error" in result
        assert "outside the task project repo" in result["error"]
        mock_fn.assert_not_called()

    def test_auto_link_commits_task_filter_not_found(self, mock_sync_registry) -> None:
        """Filtered auto-link returns structured missing-task errors."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        task_manager.get_task.side_effect = [MagicMock(), None]
        mock_fn = MagicMock()

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            auto_link_commits_fn=mock_fn,
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link(task_id="task-uuid")

        assert result == {"error": "Task task-uuid not found"}
        assert [call.args for call in task_manager.get_task.call_args_list] == [
            ("task-uuid",),
            ("task-uuid",),
        ]
        task_manager.resolve_task_reference.assert_not_called()
        mock_fn.assert_not_called()

    def test_auto_link_commits_with_since(self, mock_sync_registry) -> None:
        """Test auto_link_commits with since parameter."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_fn = MagicMock()
        mock_result = MagicMock()
        mock_result.linked_tasks = []
        mock_result.total_linked = 0
        mock_result.skipped = []
        mock_fn.return_value = mock_result

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            auto_link_commits_fn=mock_fn,
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link(since="1 week ago")

        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["since"] == "1 week ago"
        assert result["total_linked"] == 0

    def test_auto_link_commits_no_project(self, mock_sync_registry) -> None:
        """Test auto_link_commits when no project context."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_fn = MagicMock()
        mock_result = MagicMock()
        mock_result.linked_tasks = []
        mock_result.total_linked = 0
        mock_result.skipped = []
        mock_fn.return_value = mock_result

        with patch(
            "gobby.mcp_proxy.tools.task_sync.get_project_context",
            return_value=None,
        ):
            registry = create_commit_registry(
                task_manager=task_manager,
                sync_manager=MagicMock(),
                project_manager=project_manager,
                auto_link_commits_fn=mock_fn,
            )

            auto_link = registry.get_tool("auto_link_commits")
            result = auto_link()

            # Should still work, just with cwd=None
            call_kwargs = mock_fn.call_args.kwargs
            assert call_kwargs["cwd"] is None
            assert result["linked_tasks"] == []


class TestGetTaskDiff:
    """Tests for get_task_diff MCP tool."""

    def test_get_task_diff_basic(self, mock_sync_registry) -> None:
        """Test get_task_diff basic call."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        task_manager.get_task.return_value = mock_task

        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_diff_result = MagicMock()
        mock_diff_result.diff = "diff content"
        mock_diff_result.commits = ["abc123"]
        mock_diff_result.has_uncommitted_changes = False
        mock_diff_result.file_count = 3

        mock_get_task_diff = MagicMock(return_value=mock_diff_result)

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            get_task_diff_fn=mock_get_task_diff,
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="task-1")

        assert result["diff"] == "diff content"
        assert result["commits"] == ["abc123"]
        assert result["has_uncommitted_changes"] is False
        assert result["file_count"] == 3

    def test_get_task_diff_task_not_found(self, mock_sync_registry) -> None:
        """Test get_task_diff when task not found."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        task_manager.get_task.return_value = None

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="nonexistent")

        assert "error" in result
        assert "not found" in result["error"]

    def test_get_task_diff_include_uncommitted(self, mock_sync_registry) -> None:
        """Test get_task_diff with include_uncommitted=True."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.project_id = "project-1"
        task_manager.get_task.return_value = mock_task

        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_diff_result = MagicMock()
        mock_diff_result.diff = "diff with uncommitted"
        mock_diff_result.commits = []
        mock_diff_result.has_uncommitted_changes = True
        mock_diff_result.file_count = 5

        mock_get_task_diff = MagicMock(return_value=mock_diff_result)

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            get_task_diff_fn=mock_get_task_diff,
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="task-1", include_uncommitted=True)

        assert result["has_uncommitted_changes"] is True
        call_kwargs = mock_get_task_diff.call_args.kwargs
        assert call_kwargs["include_uncommitted"] is True

    def test_get_task_diff_uses_registered_project_path_override(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Explicit diff repos must be registered for the task project."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "external" / "repo"
        repo_path.mkdir(parents=True)
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.project_id = "project-1"
        task_manager.get_task.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))

        mock_diff_result = MagicMock()
        mock_diff_result.diff = "external diff"
        mock_diff_result.commits = ["abc123"]
        mock_diff_result.has_uncommitted_changes = False
        mock_diff_result.file_count = 1

        mock_get_task_diff = MagicMock(return_value=mock_diff_result)

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            get_task_diff_fn=mock_get_task_diff,
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="task-1", project_path=str(repo_path))

        assert result["diff"] == "external diff"
        assert mock_get_task_diff.call_args.kwargs["cwd"] == str(repo_path)

    def test_get_task_diff_rejects_unknown_project_path_before_git(
        self, mock_sync_registry, tmp_path: Path
    ) -> None:
        """Unknown explicit repo paths are rejected before diff Git work."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        repo_path = tmp_path / "repo"
        outside = tmp_path / "outside"
        repo_path.mkdir()
        outside.mkdir()
        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.project_id = "project-1"
        mock_task.parent_task_id = None
        task_manager.get_task.return_value = mock_task
        project_manager = MagicMock()
        project_manager.get.return_value = MagicMock(repo_path=str(repo_path))
        mock_get_task_diff = MagicMock()

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            get_task_diff_fn=mock_get_task_diff,
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="task-1", project_path=str(outside))

        assert "error" in result
        assert "outside the task project repo" in result["error"]
        mock_get_task_diff.assert_not_called()


class TestGitIntegrationEdgeCases:
    """Tests for git integration edge cases."""

    def test_link_commit_full_sha(self, mock_sync_registry) -> None:
        """Test linking with full SHA."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.commits = ["abc123def456"]
        task_manager.link_commit.return_value = mock_task

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        link = registry.get_tool("link_commit")
        full_sha = "abc123def456789abcdef123456789abcdef1234"
        link(task_id="task-1", commit_sha=full_sha)

        task_manager.link_commit.assert_called_with("task-1", full_sha, cwd=None)
        assert task_manager.link_commit.call_count >= 1
        assert task_manager.link_commit.call_args is not None

    def test_link_commit_short_sha(self, mock_sync_registry) -> None:
        """Test linking with short SHA."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.commits = ["abc123"]
        task_manager.link_commit.return_value = mock_task

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        link = registry.get_tool("link_commit")
        link(task_id="task-1", commit_sha="abc123")

        task_manager.link_commit.assert_called_with("task-1", "abc123", cwd=None)
        assert task_manager.link_commit.call_count >= 1
        assert task_manager.link_commit.call_args is not None

    def test_auto_link_with_skipped_commits(self, mock_sync_registry) -> None:
        """Test auto_link_commits reports skipped commits."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_fn = MagicMock()
        mock_result = MagicMock()
        mock_result.linked_tasks = ["task-1"]
        mock_result.total_linked = 1
        mock_result.skipped = [
            {"sha": "abc123", "reason": "already linked"},
            {"sha": "def456", "reason": "task not found"},
        ]
        mock_fn.return_value = mock_result

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            auto_link_commits_fn=mock_fn,
        )

        auto_link = registry.get_tool("auto_link_commits")
        result = auto_link()

        assert len(result["skipped"]) == 2
        assert result["skipped"][0]["reason"] == "already linked"

    def test_get_task_diff_no_commits(self, mock_sync_registry) -> None:
        """Test get_task_diff when task has no linked commits."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.project_id = "project-1"
        task_manager.get_task.return_value = mock_task

        project_manager = MagicMock()
        project_manager.get.return_value = None

        mock_diff_result = MagicMock()
        mock_diff_result.diff = ""
        mock_diff_result.commits = []
        mock_diff_result.has_uncommitted_changes = False
        mock_diff_result.file_count = 0

        mock_get_task_diff = MagicMock(return_value=mock_diff_result)

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            project_manager=project_manager,
            get_task_diff_fn=mock_get_task_diff,
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="task-1")

        assert result["diff"] == ""
        assert result["commits"] == []
        assert result["file_count"] == 0


@pytest.fixture
def mock_sync_registry():
    """Fixture providing mock dependencies for registry creation."""
    with patch("gobby.mcp_proxy.tools.task_sync.get_project_context") as mock_proj:
        mock_proj.return_value = {"id": "test-project-id"}
        yield mock_proj


def test_task_sync_git_helper_calls_follow_repo_path_resolution() -> None:
    """Commit/diff helpers must reject bad repo paths before Git helper work."""
    from gobby.mcp_proxy.tools.task_repo_paths import RepoPathValidationError
    from gobby.mcp_proxy.tools.task_sync import create_commit_registry

    class RejectingTaskManager:
        def __init__(self) -> None:
            self.task = SimpleNamespace(
                id="task-1",
                project_id="project-1",
                parent_task_id=None,
            )
            self.get_task_calls: list[str] = []
            self.link_commit_called = False
            self.unlink_commit_called = False

        def get_task(self, task_id: str) -> SimpleNamespace:
            self.get_task_calls.append(task_id)
            return self.task

        def link_commit(self, task_id: str, commit_sha: str, cwd: str | None = None) -> object:
            self.link_commit_called = True
            raise AssertionError("link_commit should not run after repo path rejection")

        def unlink_commit(self, task_id: str, commit_sha: str, cwd: str | None = None) -> object:
            self.unlink_commit_called = True
            raise AssertionError("unlink_commit should not run after repo path rejection")

    task_manager = RejectingTaskManager()
    auto_link_called = False
    get_task_diff_called = False

    def auto_link_commits_fn(**kwargs: object) -> object:
        nonlocal auto_link_called
        auto_link_called = True
        raise AssertionError("auto_link_commits_fn should not run after repo path rejection")

    def get_task_diff_fn(**kwargs: object) -> object:
        nonlocal get_task_diff_called
        get_task_diff_called = True
        raise AssertionError("get_task_diff_fn should not run after repo path rejection")

    registry = create_commit_registry(
        task_manager=task_manager,
        sync_manager=object(),
        project_manager=object(),
        auto_link_commits_fn=auto_link_commits_fn,
        get_task_diff_fn=get_task_diff_fn,
    )

    with patch(
        "gobby.mcp_proxy.tools.task_sync.resolve_task_repo_path",
        side_effect=RepoPathValidationError("repo path blocked"),
    ):
        results = [
            registry.get_tool("link_commit")(task_id="task-1", commit_sha="abc123"),
            registry.get_tool("unlink_commit")(task_id="task-1", commit_sha="abc123"),
            registry.get_tool("auto_link_commits")(task_id="task-1"),
            registry.get_tool("get_task_diff")(task_id="task-1"),
        ]

    assert results == [{"error": "repo path blocked"}] * 4
    assert len(task_manager.get_task_calls) >= len(results)
    assert set(task_manager.get_task_calls) == {"task-1"}
    assert task_manager.link_commit_called is False
    assert task_manager.unlink_commit_called is False
    assert auto_link_called is False
    assert get_task_diff_called is False

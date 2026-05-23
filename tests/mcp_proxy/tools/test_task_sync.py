"""
Tests for task_sync.py MCP tools module (commit linking tools).

Sync tools (sync_tasks, get_sync_status, sync_import, sync_export) have been
removed from MCP — they are CLI-only operations.
"""

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

    def test_link_commit_uses_project_path_override(self, mock_sync_registry) -> None:
        """Cross-repo commits should be resolved in the repository that contains them."""
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
        result = link(
            task_id="task-1",
            commit_sha="abc123",
            project_path="/external/repo",
        )

        assert result["commits"] == ["abc123"]
        task_manager.link_commit.assert_called_once_with(
            "task-1",
            "abc123",
            cwd="/external/repo",
        )


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

    def test_unlink_commit_uses_project_path_override(self, mock_sync_registry) -> None:
        """Cross-repo commit unlinking should resolve in the supplied repository."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.commits = []
        task_manager.unlink_commit.return_value = mock_task

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
        )

        unlink = registry.get_tool("unlink_commit")
        result = unlink(
            task_id="task-1",
            commit_sha="abc123",
            project_path="/external/repo",
        )

        assert result["commits"] == []
        task_manager.unlink_commit.assert_called_once_with(
            "task-1",
            "abc123",
            cwd="/external/repo",
        )


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
        task_manager.get_task.return_value = mock_task

        project_manager = MagicMock()
        mock_project = MagicMock()
        mock_project.repo_path = "/path/to/repo"
        project_manager.get.return_value = mock_project

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

    def test_get_task_diff_uses_project_path_override(self, mock_sync_registry) -> None:
        """Cross-repo task diffs should read commits from the supplied repository."""
        from gobby.mcp_proxy.tools.task_sync import create_commit_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.project_id = "project-1"
        task_manager.get_task.return_value = mock_task

        mock_diff_result = MagicMock()
        mock_diff_result.diff = "external diff"
        mock_diff_result.commits = ["abc123"]
        mock_diff_result.has_uncommitted_changes = False
        mock_diff_result.file_count = 1

        mock_get_task_diff = MagicMock(return_value=mock_diff_result)

        registry = create_commit_registry(
            task_manager=task_manager,
            sync_manager=MagicMock(),
            get_task_diff_fn=mock_get_task_diff,
        )

        get_diff = registry.get_tool("get_task_diff")
        result = get_diff(task_id="task-1", project_path="/external/repo")

        assert result["diff"] == "external diff"
        assert mock_get_task_diff.call_args.kwargs["cwd"] == "/external/repo"


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
